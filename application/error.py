import re

from flask import jsonify
from werkzeug.http import HTTP_STATUS_CODES


def response_error(code_status, message=None):
    payload = {'error': HTTP_STATUS_CODES.get(code_status, "something went wrong")}
    if message:
        payload['message'] = message
    response = jsonify(payload)
    response.status_code = code_status
    return response


def bad_request(status_code=400, message=''):
    return response_error(code_status=status_code, message=message)


_GENERIC_FALLBACK = (
    "An error occurred while processing your request. Please try again later."
)

# Any match here disqualifies the original string from being relayed to
# anonymous users. Order is not significant; each is OR'd.
_LEAK_PATTERNS = (
    # OpenAI-style API keys (sk-..., sk-live-..., sk-proj-...).
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    # Anthropic-style API keys.
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}", re.IGNORECASE),
    # Bearer / token authorization shapes.
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    re.compile(r"\bAuthorization\s*[:=]", re.IGNORECASE),
    # JWT-shaped strings (eyJ...). Trigger on the eyJ-prefixed header
    # alone since a payload of any length means a structured token has
    # leaked. We don't try to validate the trailing parts.
    re.compile(r"\beyJ[A-Za-z0-9_-]{16,}"),
    # Any URL (signed or otherwise) — defeats signed-URL leakage and
    # internal hostnames in URL form.
    re.compile(r"https?://\S+", re.IGNORECASE),
    # Query strings detached from a URL (?foo=bar&...).
    re.compile(r"\?[A-Za-z0-9_]+=\S+"),
    # Filesystem paths (POSIX + Windows).
    re.compile(r"(?:^|[\s\"'(<])(?:/(?:home|root|app|etc|var|usr|opt|tmp|srv|mnt|data)/)\S+"),
    re.compile(r"\b[A-Za-z]:\\\\[^\s\"']+"),
    # Org / project identifiers (OpenAI, OpenRouter, Anthropic, Google).
    re.compile(r"\borg[-_]?id\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\bproject[-_]?id\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"\borg-[A-Za-z0-9]{8,}", re.IGNORECASE),
    re.compile(r"\bproj_[A-Za-z0-9]{8,}", re.IGNORECASE),
    # Header-shaped credentials.
    re.compile(r"\bX-[A-Za-z][A-Za-z0-9-]*-(?:Token|Key|Auth|Secret|Api-Key)\b", re.IGNORECASE),
    # IPv4 / internal hostnames.
    re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b"),
    re.compile(
        r"\b[A-Za-z0-9._-]+\.(?:internal|local|cluster\.local|svc\.cluster\.local)\b",
        re.IGNORECASE,
    ),
    # Python traceback or JSON payload bodies leak structured detail.
    re.compile(r"\btraceback\b", re.IGNORECASE),
    re.compile(r"[{}]"),
)


def sanitize_api_error(error) -> str:
    """Convert technical API errors to user-safe messages.

    The output is intended for anonymous-facing surfaces (the docs widget,
    /ask, Discord). Behavior:

    1. Match a few known HTTP/network categories and return canned text.
    2. Otherwise scrub the original against a deny-list of leak shapes
       (bearer tokens, sk-/eyJ-style keys, URLs, query strings, file
       paths, org/project IDs, IPs, internal hostnames, JSON bodies,
       tracebacks). Any hit -> generic fallback.
    3. If the original survives the deny-list AND is short AND printable
       ASCII, return it verbatim. Anything longer falls back to generic.
    """
    error_str = str(error)
    lowered = error_str.lower()

    if "503" in error_str or "unavailable" in lowered or "high demand" in lowered:
        return (
            "The AI service is temporarily unavailable due to high demand. "
            "Please try again in a moment."
        )
    if "429" in error_str or "rate limit" in lowered or "quota" in lowered:
        return "Rate limit exceeded. Please wait a moment before trying again."
    if "401" in error_str or "unauthorized" in lowered or "invalid api key" in lowered:
        return "Authentication error. Please check your API configuration."
    if "timeout" in lowered or "timed out" in lowered:
        return "The request timed out. Please try again."
    if "connection" in lowered or "network" in lowered:
        return "Network error. Please check your connection and try again."

    if len(error_str) > 160:
        return _GENERIC_FALLBACK
    for pat in _LEAK_PATTERNS:
        if pat.search(error_str):
            return _GENERIC_FALLBACK
    # Reject anything non-printable or non-ASCII so we don't surface raw
    # protocol bytes or unicode payloads to the widget.
    if any(ord(c) < 32 or ord(c) > 126 for c in error_str):
        return _GENERIC_FALLBACK
    return error_str
