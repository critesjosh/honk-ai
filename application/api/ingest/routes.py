"""POST /api/upload + GET /api/task_status.

Carved out of ``application/api/user/sources/upload.py`` when the
upstream admin-SPA blueprint was removed. Drops the upstream
audio-size guard (STT was deleted alongside the SPA) and the
connector / remote / manage_source_files siblings (SPA-only).
"""

import os
import tempfile
import zipfile

from flask import current_app, jsonify, make_response, request
from flask_restx import fields, Namespace, Resource

from application.api import api
from application.core.settings import settings
from application.parser.file.constants import SUPPORTED_SOURCE_EXTENSIONS
from application.storage.storage_creator import StorageCreator
from application.utils import check_required_fields, safe_filename
from application.workers.ingest import ingest as ingest_task


ingest_ns = Namespace(
    "ingest", description="Corpus ingest operations", path="/api"
)
api.add_namespace(ingest_ns)


@ingest_ns.route("/upload")
class UploadFile(Resource):
    @api.expect(
        api.model(
            "UploadModel",
            {
                "user": fields.String(required=True, description="User ID"),
                "name": fields.String(required=True, description="Job name"),
                "file": fields.Raw(required=True, description="File(s) to upload"),
            },
        )
    )
    @api.doc(description="Upload a corpus zip for vectorization and indexing")
    def post(self):
        decoded_token = request.decoded_token
        if not decoded_token:
            return make_response(jsonify({"success": False}), 401)
        data = request.form
        files = request.files.getlist("file")
        required_fields = ["user", "name"]
        missing_fields = check_required_fields(data, required_fields)
        if missing_fields or not files or all(file.filename == "" for file in files):
            return make_response(
                jsonify(
                    {
                        "status": "error",
                        "message": "Missing required fields or files",
                    }
                ),
                400,
            )
        user = decoded_token.get("sub")
        job_name = request.form["name"]

        safe_user = safe_filename(user)
        dir_name = safe_filename(job_name)
        base_path = f"{settings.UPLOAD_FOLDER}/{safe_user}/{dir_name}"
        file_name_map = {}

        try:
            storage = StorageCreator.get_storage()

            for file in files:
                original_filename = os.path.basename(file.filename)
                safe_file = safe_filename(original_filename)
                if original_filename:
                    file_name_map[safe_file] = original_filename

                with tempfile.TemporaryDirectory() as temp_dir:
                    temp_file_path = os.path.join(temp_dir, safe_file)
                    file.save(temp_file_path)

                    # Only extract real .zip files, not Office formats which
                    # are technically zip archives but should be processed as-is.
                    is_office_format = safe_file.lower().endswith(
                        (".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub")
                    )
                    if zipfile.is_zipfile(temp_file_path) and not is_office_format:
                        try:
                            with zipfile.ZipFile(temp_file_path, "r") as zip_ref:
                                zip_ref.extractall(path=temp_dir)
                                for root, _, extracted_files in os.walk(temp_dir):
                                    for extracted_file in extracted_files:
                                        if (
                                            os.path.join(root, extracted_file)
                                            == temp_file_path
                                        ):
                                            continue
                                        rel_path = os.path.relpath(
                                            os.path.join(root, extracted_file),
                                            temp_dir,
                                        )
                                        storage_path = f"{base_path}/{rel_path}"
                                        with open(
                                            os.path.join(root, extracted_file), "rb"
                                        ) as f:
                                            storage.save_file(f, storage_path)
                        except Exception as e:
                            current_app.logger.error(
                                f"Error extracting zip: {e}", exc_info=True
                            )
                            file_path = f"{base_path}/{safe_file}"
                            with open(temp_file_path, "rb") as f:
                                storage.save_file(f, file_path)
                    else:
                        file_path = f"{base_path}/{safe_file}"
                        with open(temp_file_path, "rb") as f:
                            storage.save_file(f, file_path)
            task = ingest_task.delay(
                settings.UPLOAD_FOLDER,
                list(SUPPORTED_SOURCE_EXTENSIONS),
                job_name,
                user,
                file_path=base_path,
                filename=dir_name,
                file_name_map=file_name_map,
            )
        except Exception as err:
            current_app.logger.error(f"Error uploading file: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(jsonify({"success": True, "task_id": task.id}), 200)


@ingest_ns.route("/task_status")
class TaskStatus(Resource):
    task_status_model = api.model(
        "TaskStatusModel",
        {"task_id": fields.String(required=True, description="Task ID")},
    )

    @api.expect(task_status_model)
    @api.doc(description="Get celery job status")
    def get(self):
        task_id = request.args.get("task_id")
        if not task_id:
            return make_response(
                jsonify({"success": False, "message": "Task ID is required"}), 400
            )
        try:
            from application.celery_init import celery

            task = celery.AsyncResult(task_id)
            task_meta = task.info

            if task.status == "PENDING":
                inspect = celery.control.inspect()
                active_workers = inspect.ping()
                if not active_workers:
                    raise ConnectionError("Service unavailable")

            if not isinstance(
                task_meta, (dict, list, str, int, float, bool, type(None))
            ):
                task_meta = str(task_meta)
        except ConnectionError as err:
            current_app.logger.error(f"Connection error getting task status: {err}")
            return make_response(
                jsonify({"success": False, "message": "Service unavailable"}), 503
            )
        except Exception as err:
            current_app.logger.error(f"Error getting task status: {err}", exc_info=True)
            return make_response(jsonify({"success": False}), 400)
        return make_response(
            jsonify(
                {
                    "success": True,
                    "status": task.status,
                    "result": task_meta,
                }
            ),
            200,
        )
