"""Small utilities shared across worker modules.

Kept private (leading underscore) because it's an internal seam, not a
public API. Anything genuinely worth exposing would graduate to a real
module elsewhere.
"""

from __future__ import annotations

import json
import logging
import os
import string
from urllib.parse import urljoin

import requests

from application.core.settings import settings


# Chunking constants used by every worker that runs the ingest pipeline.
MIN_TOKENS = 150
MAX_TOKENS = 1250
RECURSION_DEPTH = 2


def metadata_from_filename(title):
    """SimpleDirectoryReader's ``file_metadata`` callback."""
    return {"title": title}


def generate_random_string(length):
    return "".join([string.ascii_letters[i % 52] for i in range(length)])


def _normalize_file_name_map(file_name_map):
    if not file_name_map:
        return {}
    if isinstance(file_name_map, str):
        try:
            file_name_map = json.loads(file_name_map)
        except Exception:
            return {}
    return file_name_map if isinstance(file_name_map, dict) else {}


def _get_display_name(file_name_map, rel_path):
    if not file_name_map or not rel_path:
        return None
    if rel_path in file_name_map:
        return file_name_map[rel_path]
    base_name = os.path.basename(rel_path)
    return file_name_map.get(base_name)


def _apply_display_names_to_structure(structure, file_name_map, prefix=""):
    if not isinstance(structure, dict) or not file_name_map:
        return structure
    for name, node in structure.items():
        if isinstance(node, dict) and "type" in node and "size_bytes" in node:
            rel_path = f"{prefix}/{name}" if prefix else name
            display_name = _get_display_name(file_name_map, rel_path)
            if display_name:
                node["display_name"] = display_name
        elif isinstance(node, dict):
            next_prefix = f"{prefix}/{name}" if prefix else name
            _apply_display_names_to_structure(node, file_name_map, next_prefix)
    return structure


def download_file(url, params, dest_path):
    try:
        response = requests.get(url, params=params, timeout=100)
        response.raise_for_status()
        with open(dest_path, "wb") as f:
            f.write(response.content)
    except requests.RequestException as e:
        logging.error(f"Error downloading file: {e}")
        raise


def upload_index(full_path, file_data):
    """Upload a built index back to the backend's ``/api/upload_index``.

    Used by every ingest worker (local, remote, connector). FAISS sends
    two binary files; everything else just sends the metadata form.
    """
    files = None
    try:
        headers = {}
        if settings.INTERNAL_KEY:
            headers["X-Internal-Key"] = settings.INTERNAL_KEY

        if settings.VECTOR_STORE == "faiss":
            faiss_path = full_path + "/index.faiss"
            pkl_path = full_path + "/index.pkl"

            if not os.path.exists(faiss_path):
                logging.error(f"FAISS index file not found: {faiss_path}")
                raise FileNotFoundError(f"FAISS index file not found: {faiss_path}")

            if not os.path.exists(pkl_path):
                logging.error(f"FAISS pickle file not found: {pkl_path}")
                raise FileNotFoundError(f"FAISS pickle file not found: {pkl_path}")

            files = {
                "file_faiss": open(faiss_path, "rb"),
                "file_pkl": open(pkl_path, "rb"),
            }
            response = requests.post(
                urljoin(settings.API_URL, "/api/upload_index"),
                files=files,
                data=file_data,
                headers=headers,
                timeout=100,
            )
        else:
            response = requests.post(
                urljoin(settings.API_URL, "/api/upload_index"),
                data=file_data,
                headers=headers,
                timeout=100,
            )
        response.raise_for_status()
    except (requests.RequestException, FileNotFoundError) as e:
        logging.error(f"Error uploading index: {e}")
        raise
    finally:
        if settings.VECTOR_STORE == "faiss" and files is not None:
            for file in files.values():
                file.close()
