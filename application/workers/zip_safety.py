"""Zip extraction with security limits.

Used by the ingest pipeline to safely unpack user-uploaded zip files.
Defends against zip slip (path traversal), zip bombs (excessive
compression ratio or uncompressed size), and recursion-bomb chains of
nested zips.
"""

from __future__ import annotations

import logging
import os
import zipfile


# Zip extraction security limits
MAX_UNCOMPRESSED_SIZE = 500 * 1024 * 1024  # 500 MB max uncompressed size
MAX_FILE_COUNT = 10000  # Maximum number of files to extract
MAX_COMPRESSION_RATIO = 100  # Maximum compression ratio (to detect zip bombs)


class ZipExtractionError(Exception):
    """Raised when zip extraction fails due to security constraints."""
    pass


def _is_path_safe(base_path: str, target_path: str) -> bool:
    """Check if target_path is safely within base_path (prevents zip slip).

    Args:
        base_path: The base directory where extraction should occur.
        target_path: The full path where a file would be extracted.

    Returns:
        True if the path is safe, False otherwise.
    """
    base_resolved = os.path.realpath(base_path)
    target_resolved = os.path.realpath(target_path)
    return (
        target_resolved.startswith(base_resolved + os.sep)
        or target_resolved == base_resolved
    )


def _validate_zip_safety(zip_path: str, extract_to: str) -> None:
    """Validate a zip file for security issues before extraction.

    Checks for:
    - Zip bombs (excessive compression ratio or uncompressed size)
    - Too many files
    - Path traversal attacks (zip slip)

    Raises:
        ZipExtractionError: If the zip file fails security validation.
    """
    try:
        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            compressed_size = os.path.getsize(zip_path)
            total_uncompressed = 0
            file_count = 0

            for info in zip_ref.infolist():
                file_count += 1

                if file_count > MAX_FILE_COUNT:
                    raise ZipExtractionError(
                        f"Zip file contains too many files (>{MAX_FILE_COUNT}). "
                        "This may be a zip bomb attack."
                    )

                total_uncompressed += info.file_size

                if total_uncompressed > MAX_UNCOMPRESSED_SIZE:
                    raise ZipExtractionError(
                        f"Zip file uncompressed size exceeds limit "
                        f"({total_uncompressed / (1024*1024):.1f} MB > "
                        f"{MAX_UNCOMPRESSED_SIZE / (1024*1024):.1f} MB). "
                        "This may be a zip bomb attack."
                    )

                target_path = os.path.join(extract_to, info.filename)
                if not _is_path_safe(extract_to, target_path):
                    raise ZipExtractionError(
                        f"Zip file contains path traversal attempt: {info.filename}"
                    )

            if compressed_size > 0 and total_uncompressed > 0:
                compression_ratio = total_uncompressed / compressed_size
                if compression_ratio > MAX_COMPRESSION_RATIO:
                    raise ZipExtractionError(
                        f"Zip file has suspicious compression ratio "
                        f"({compression_ratio:.1f}:1 > "
                        f"{MAX_COMPRESSION_RATIO}:1). "
                        "This may be a zip bomb attack."
                    )

    except zipfile.BadZipFile as e:
        raise ZipExtractionError(f"Invalid or corrupted zip file: {e}")


def extract_zip_recursive(zip_path, extract_to, current_depth=0, max_depth=5):
    """Recursively extract zip files with security protections.

    Security measures:
    - Limits recursion depth to prevent infinite loops
    - Validates uncompressed size to prevent zip bombs
    - Limits number of files to prevent resource exhaustion
    - Checks compression ratio to detect zip bombs
    - Validates paths to prevent zip slip attacks
    """
    if current_depth > max_depth:
        logging.warning(f"Reached maximum recursion depth of {max_depth}")
        return

    try:
        _validate_zip_safety(zip_path, extract_to)

        with zipfile.ZipFile(zip_path, "r") as zip_ref:
            zip_ref.extractall(extract_to)
        os.remove(zip_path)

    except ZipExtractionError as e:
        logging.error(f"Zip security validation failed for {zip_path}: {e}")
        try:
            os.remove(zip_path)
        except OSError:
            pass
        return
    except Exception as e:
        logging.error(f"Error extracting zip file {zip_path}: {e}", exc_info=True)
        return

    # Check for nested zip files and extract them
    for root, dirs, files in os.walk(extract_to):
        for file in files:
            if file.endswith(".zip"):
                file_path = os.path.join(root, file)
                extract_zip_recursive(
                    file_path, root, current_depth + 1, max_depth,
                )
