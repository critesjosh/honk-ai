import base64
import hashlib
import io
import logging
import os
import uuid
from typing import List

import tiktoken
from flask import jsonify, make_response
from werkzeug.utils import secure_filename

from application.core.model_utils import get_token_limit

from application.core.settings import settings

logger = logging.getLogger(__name__)


_encoding = None


def get_encoding():
    global _encoding
    if _encoding is None:
        _encoding = tiktoken.get_encoding("cl100k_base")
    return _encoding


def safe_filename(filename):
    """Create safe filename, preserving extension. Handles non-Latin characters."""
    if not filename:
        return str(uuid.uuid4())
    _, extension = os.path.splitext(filename)

    safe_name = secure_filename(filename)

    # If secure_filename returns just the extension or an empty string

    if not safe_name or safe_name == extension.lstrip("."):
        return f"{str(uuid.uuid4())}{extension}"
    return safe_name


def num_tokens_from_string(string: str) -> int:
    encoding = get_encoding()
    if isinstance(string, str):
        num_tokens = len(encoding.encode(string))
        return num_tokens
    else:
        return 0


def num_tokens_from_object_or_list(thing):
    if isinstance(thing, list):
        return sum([num_tokens_from_object_or_list(x) for x in thing])
    elif isinstance(thing, dict):
        return sum([num_tokens_from_object_or_list(x) for x in thing.values()])
    elif isinstance(thing, str):
        return num_tokens_from_string(thing)
    else:
        return 0


def count_tokens_docs(docs):
    docs_content = ""
    for doc in docs:
        docs_content += doc.page_content
    tokens = num_tokens_from_string(docs_content)
    return tokens


def calculate_doc_token_budget(
    model_id: str = "gpt-4o"
) -> int:
    """Token budget for retrieved documents injected into the LLM prompt.

    Upstream computes this as (model_context_window - reserved_tokens),
    which for a 200k-context model yields a ~195k budget and makes every
    RAG answer stuff in as many chunks as exist, slowing generation to a
    crawl. Cap at ``settings.RAG_MAX_DOC_TOKENS`` (default 15k) so the
    retriever stops accumulating once we have plenty of grounded context.
    """
    total_context = get_token_limit(model_id)
    reserved = sum(settings.RESERVED_TOKENS.values())
    doc_budget = total_context - reserved
    cap = getattr(settings, "RAG_MAX_DOC_TOKENS", 15000)
    if cap and cap > 0:
        doc_budget = min(doc_budget, cap)
    return max(doc_budget, 1000)


def get_missing_fields(data, required_fields):
    """Check for missing required fields. Returns list of missing field names."""
    return [field for field in required_fields if field not in data]


def check_required_fields(data, required_fields):
    """Validate required fields. Returns Flask 400 response if validation fails, None otherwise."""
    missing_fields = get_missing_fields(data, required_fields)
    if missing_fields:
        return make_response(
            jsonify(
                {
                    "success": False,
                    "message": f"Missing required fields: {', '.join(missing_fields)}",
                }
            ),
            400,
        )
    return None


def get_hash(data):
    return hashlib.md5(data.encode(), usedforsecurity=False).hexdigest()


def limit_chat_history(history, max_token_limit=None, model_id="docsgpt-local"):
    """Limit chat history to fit within token limit."""
    model_token_limit = get_token_limit(model_id)
    max_token_limit = (
        max_token_limit
        if max_token_limit and max_token_limit < model_token_limit
        else model_token_limit
    )

    if not history:
        return []
    trimmed_history = []
    tokens_current_history = 0

    for message in reversed(history):
        tokens_batch = 0
        if "prompt" in message and "response" in message:
            tokens_batch += num_tokens_from_string(message["prompt"])
            tokens_batch += num_tokens_from_string(message["response"])
        if "tool_calls" in message:
            for tool_call in message["tool_calls"]:
                tool_call_string = f"Tool: {tool_call.get('tool_name')} | Action: {tool_call.get('action_name')} | Args: {tool_call.get('arguments')} | Response: {tool_call.get('result')}"
                tokens_batch += num_tokens_from_string(tool_call_string)
        if tokens_current_history + tokens_batch < max_token_limit:
            tokens_current_history += tokens_batch
            trimmed_history.insert(0, message)
        else:
            break
    return trimmed_history


def convert_pdf_to_images(
    file_path: str,
    storage=None,
    max_pages: int = 20,
    dpi: int = 150,
    image_format: str = "PNG",
) -> List[dict]:
    """
    Convert PDF pages to images for LLMs that support images but not PDFs.

    This enables "synthetic PDF support" by converting each PDF page to an image
    that can be sent to vision-capable LLMs like Claude.

    Args:
        file_path: Path to the PDF file (can be storage path)
        storage: Optional storage instance for retrieving files
        max_pages: Maximum number of pages to convert (default 20 to avoid context overflow)
        dpi: Resolution for rendering (default 150 for balance of quality/size)
        image_format: Output format (PNG recommended for quality)

    Returns:
        List of dicts with keys:
        - 'data': base64-encoded image data
        - 'mime_type': MIME type (e.g., 'image/png')
        - 'page': Page number (1-indexed)

    Raises:
        ImportError: If pdf2image is not installed
        FileNotFoundError: If file doesn't exist
        Exception: If conversion fails
    """
    try:
        from pdf2image import convert_from_path, convert_from_bytes
    except ImportError:
        raise ImportError(
            "pdf2image is required for PDF-to-image conversion. "
            "Install it with: pip install pdf2image\n"
            "Also ensure poppler-utils is installed on your system."
        )

    images_data = []
    mime_type = f"image/{image_format.lower()}"

    try:
        # Get PDF content either from storage or direct file path
        if storage and hasattr(storage, "get_file"):
            with storage.get_file(file_path) as pdf_file:
                pdf_bytes = pdf_file.read()
                pil_images = convert_from_bytes(
                    pdf_bytes,
                    dpi=dpi,
                    fmt=image_format.lower(),
                    first_page=1,
                    last_page=max_pages,
                )
        else:
            pil_images = convert_from_path(
                file_path,
                dpi=dpi,
                fmt=image_format.lower(),
                first_page=1,
                last_page=max_pages,
            )

        for page_num, pil_image in enumerate(pil_images, start=1):
            # Convert PIL image to base64
            buffer = io.BytesIO()
            pil_image.save(buffer, format=image_format)
            buffer.seek(0)
            base64_data = base64.b64encode(buffer.read()).decode("utf-8")

            images_data.append({
                "data": base64_data,
                "mime_type": mime_type,
                "page": page_num,
            })

        return images_data

    except FileNotFoundError:
        logger.error(f"PDF file not found: {file_path}")
        raise
    except Exception as e:
        logger.error(f"Error converting PDF to images: {e}", exc_info=True)
        raise
