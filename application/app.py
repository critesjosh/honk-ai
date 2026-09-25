import fnmatch
import logging
import os
import platform
import uuid

import dotenv
from flask import Flask, jsonify, request
from jose import jwt

from application.auth import handle_auth

from application.core.logging_config import setup_logging

setup_logging()

from application.api import api  # noqa: E402
from application.api.answer import answer  # noqa: E402
from application.api.ingest import ingest_bp  # noqa: E402
from application.api.internal.routes import internal  # noqa: E402
from application.api.v1 import v1_bp  # noqa: E402
from application.celery_init import celery  # noqa: E402
from application.core.settings import settings  # noqa: E402
from application.storage.db.bootstrap import ensure_database_ready  # noqa: E402


if platform.system() == "Windows":
    import pathlib

    pathlib.PosixPath = pathlib.WindowsPath
dotenv.load_dotenv()

# Self-bootstrap the user-data Postgres DB. Runs before any blueprint or
# repository touches the engine, so the first request can't race the
# schema being created. Gated by AUTO_CREATE_DB / AUTO_MIGRATE settings
# (default ON for dev; disable in prod if schema is managed out-of-band).
ensure_database_ready(
    settings.POSTGRES_URI,
    create_db=settings.AUTO_CREATE_DB,
    migrate=settings.AUTO_MIGRATE,
    logger=logging.getLogger("application.app"),
)

app = Flask(__name__)
app.register_blueprint(answer)
app.register_blueprint(internal)
app.register_blueprint(ingest_bp)
app.register_blueprint(v1_bp)
app.config.update(
    UPLOAD_FOLDER="inputs",
    CELERY_BROKER_URL=settings.CELERY_BROKER_URL,
    CELERY_RESULT_BACKEND=settings.CELERY_RESULT_BACKEND,
    MONGO_URI=settings.MONGO_URI,
)
celery.config_from_object("application.celeryconfig")
api.init_app(app)

if settings.AUTH_TYPE in ("simple_jwt", "session_jwt") and not settings.JWT_SECRET_KEY:
    key_file = ".jwt_secret_key"
    try:
        with open(key_file, "r") as f:
            settings.JWT_SECRET_KEY = f.read().strip()
    except FileNotFoundError:
        new_key = os.urandom(32).hex()
        with open(key_file, "w") as f:
            f.write(new_key)
        settings.JWT_SECRET_KEY = new_key
    except Exception as e:
        raise RuntimeError(f"Failed to setup JWT_SECRET_KEY: {e}")
SIMPLE_JWT_TOKEN = None
if settings.AUTH_TYPE == "simple_jwt":
    payload = {"sub": "local"}
    SIMPLE_JWT_TOKEN = jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm="HS256")
    print(f"Generated Simple JWT Token: {SIMPLE_JWT_TOKEN}")


@app.route("/")
def home():
    return "DocsGPT-Aztec backend. See /ask for the public chat surface."


@app.route("/api/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/config")
def get_config():
    response = {
        "auth_type": settings.AUTH_TYPE,
        "requires_auth": settings.AUTH_TYPE in ["simple_jwt", "session_jwt"],
    }
    return jsonify(response)


@app.route("/api/generate_token")
def generate_token():
    if settings.AUTH_TYPE == "session_jwt":
        new_user_id = str(uuid.uuid4())
        token = jwt.encode(
            {"sub": new_user_id}, settings.JWT_SECRET_KEY, algorithm="HS256"
        )
        return jsonify({"token": token})
    return jsonify({"error": "Token generation not allowed in current auth mode"}), 400


@app.before_request
def authenticate_request():
    if request.method == "OPTIONS":
        return "", 200
    # OpenAI-compatible routes authenticate via opaque agent API keys in the
    # Authorization header, which the JWT decoder below would reject. Defer
    # auth to the route handlers (see application/api/v1/routes.py).
    if request.path.startswith("/v1/"):
        request.decoded_token = None
        return None
    decoded_token = handle_auth(request)
    if not decoded_token:
        request.decoded_token = None
    elif "error" in decoded_token:
        return jsonify(decoded_token), 401
    else:
        request.decoded_token = decoded_token


@app.after_request
def after_request(response):
    """Emit CORS headers based on CORS_ALLOWED_ORIGINS.

    - Empty/unset: same-origin only (no CORS headers emitted). Correct
      default for a reverse-proxy fronted deployment where browser and
      API share an origin.
    - "*": permissive; allows any origin. Insecure with credentials; only
      use for public read-only demos.
    - Comma-separated origin list: echoes the request's Origin back only
      if it matches, and adds Vary: Origin for correct caching. Entries
      may contain shell-style globs (``*``, ``?``) — for example
      ``https://*.netlify.app`` matches every Netlify preview URL.
    """
    allowed = (settings.CORS_ALLOWED_ORIGINS or "").strip()
    if not allowed:
        return response

    if allowed == "*":
        response.headers["Access-Control-Allow-Origin"] = "*"
    else:
        origin = request.headers.get("Origin", "")
        patterns = [o.strip() for o in allowed.split(",") if o.strip()]
        if any(fnmatch.fnmatchcase(origin, p) for p in patterns):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Vary"] = "Origin"

    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
    return response


if __name__ == "__main__":
    app.run(debug=settings.FLASK_DEBUG_MODE, port=7091)
