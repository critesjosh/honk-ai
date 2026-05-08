"""Shared file-extension constants for parsing and ingestion flows.

Audio extensions were dropped from this fork alongside the STT pipeline.
"""


SUPPORTED_SOURCE_DOCUMENT_EXTENSIONS = (
    ".rst",
    ".md",
    ".pdf",
    ".txt",
    ".docx",
    ".csv",
    ".epub",
    ".html",
    ".mdx",
    ".json",
    ".xlsx",
    ".pptx",
)

SUPPORTED_SOURCE_IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg")

SUPPORTED_SOURCE_EXTENSIONS = (
    *SUPPORTED_SOURCE_DOCUMENT_EXTENSIONS,
    *SUPPORTED_SOURCE_IMAGE_EXTENSIONS,
)
