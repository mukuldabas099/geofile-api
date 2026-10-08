"""Upload validation and on-disk storage."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import BinaryIO

from app.enums import FileType
from app.exceptions import FileTooLargeError, InvalidUploadError, UnsupportedFileTypeError

_CHUNK = 1024 * 1024


def sanitize_filename(name: str | None) -> str:
    """Display-only name. Never used to build filesystem paths."""
    base = (name or "upload").replace("\\", "/").split("/")[-1]
    base = re.sub(r"[^\w.\- ()]", "_", base).strip(" .") or "upload"
    return base[:255]


def detect_file_type(filename: str) -> FileType:
    suffix = Path(filename).suffix.lower()
    if suffix == ".kml":
        return FileType.KML
    if suffix == ".zip":
        return FileType.SHAPEFILE
    raise UnsupportedFileTypeError(
        "Unsupported file type. Upload a .kml file or a .zip containing a Shapefile."
    )


def save_upload(stream: BinaryIO, destination: Path, max_bytes: int) -> int:
    """Stream to disk, aborting (and cleaning up) as soon as the size limit is exceeded."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    try:
        with destination.open("wb") as out:
            while chunk := stream.read(_CHUNK):
                written += len(chunk)
                if written > max_bytes:
                    raise FileTooLargeError(
                        f"File exceeds the maximum upload size of {max_bytes // (1024 * 1024)} MB."
                    )
                out.write(chunk)
    except Exception:
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise
    if written == 0:
        shutil.rmtree(destination.parent, ignore_errors=True)
        raise InvalidUploadError("Uploaded file is empty.")
    return written


def verify_signature(path: Path, file_type: FileType) -> None:
    """Cheap content sniffing so a renamed binary is rejected before GDAL sees it."""
    import zipfile

    if file_type is FileType.SHAPEFILE:
        if not zipfile.is_zipfile(path):
            raise InvalidUploadError("File has a .zip extension but is not a valid ZIP archive.")
        return
    with path.open("rb") as fh:
        head = fh.read(8192).lstrip(b"\xef\xbb\xbf \t\r\n")
    if not head.startswith(b"<") or b"kml" not in head.lower():
        raise InvalidUploadError("File has a .kml extension but does not look like KML (XML).")


def remove_stored(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)
