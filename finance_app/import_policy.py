from __future__ import annotations

from starlette.formparsers import MultiPartParser


# These are resource-safety ceilings, not assumptions about any bank format.
# Twenty thousand reviewed rows already exceeds a realistic personal workflow.
MAX_CSV_ROWS = 20_000
MAX_CSV_BYTES = 16 * 1024 * 1024
MAX_UPLOAD_REQUEST_BYTES = MAX_CSV_BYTES + 1024 * 1024


def configure_memory_only_uploads() -> None:
    """Keep every accepted CSV in memory instead of a readable OS temp file."""
    MultiPartParser.spool_max_size = MAX_UPLOAD_REQUEST_BYTES


def oversized_upload_request(method: str, path: str, content_length: str | None) -> bool:
    """Fail closed before multipart parsing can create a temporary file."""
    if method.upper() != "POST" or "/upload/" not in path:
        return False
    try:
        return content_length is None or int(content_length) > MAX_UPLOAD_REQUEST_BYTES
    except ValueError:
        return True
