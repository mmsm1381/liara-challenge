"""Bounded static ZIP safety checks. Uploaded code is never extracted, imported or executed.

This module only decides whether an archive is safe to read; what kind of
project it contains is decided by ai_detect.py."""

from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import stat
import zipfile
import zlib


@dataclass(frozen=True)
class InspectionLimits:
    archive_bytes: int = 10 * 1024 * 1024
    expanded_bytes: int = 50 * 1024 * 1024
    file_bytes: int = 5 * 1024 * 1024
    entries: int = 1500
    text_bytes: int = 256 * 1024
    compression_ratio: int = 200


class InspectionError(ValueError):
    """A safe, displayable rejection, with no uploaded file contents."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def _validate_entries(archive: zipfile.ZipFile, limits: InspectionLimits):
    entries = archive.infolist()
    if len(entries) > limits.entries:
        raise InspectionError("too_many_entries", "The ZIP contains too many files or folders.")
    seen = set()
    expanded = 0
    for entry in entries:
        name = entry.orig_filename
        parts = name.removesuffix("/").split("/")
        if (
            not name
            or name.startswith("/")
            or "\\" in name
            or any(part in ("", ".", "..") for part in parts)
            or ":" in parts[0]
            or any(ord(char) < 32 or ord(char) == 127 for char in name)
        ):
            raise InspectionError("unsafe_path", "The ZIP contains an unsafe file path.")
        normalized = "/".join(parts)
        if normalized in seen:
            raise InspectionError("duplicate_path", "The ZIP contains duplicate file or folder paths.")
        seen.add(normalized)
        kind = stat.S_IFMT(entry.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise InspectionError("unsafe_entry", "Links and special files are not accepted.")
        if entry.flag_bits & 1:
            raise InspectionError("encrypted_archive", "Upload a ZIP without password protection.")
        if entry.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            raise InspectionError("unsupported_compression", "Use ZIP stored or deflate compression.")
        if entry.is_dir() and entry.file_size:
            raise InspectionError("invalid_archive", "A ZIP folder contains unexpected data.")
        if entry.file_size > limits.file_bytes:
            raise InspectionError("file_too_large", "A file exceeds the inspection size limit.")
        expanded += entry.file_size
        if expanded > limits.expanded_bytes:
            raise InspectionError("expanded_size_limit", "The expanded ZIP exceeds the size limit.")
        if entry.file_size > max(1, entry.compress_size) * limits.compression_ratio:
            raise InspectionError("compression_ratio_limit", "A file has an excessive compression ratio.")
    # Reject a path that is simultaneously used as a file and a parent folder.
    files = {entry.filename for entry in entries if not entry.is_dir()}
    for name in seen:
        if any(str(parent) in files for parent in PurePosixPath(name).parents if str(parent) != "."):
            raise InspectionError("path_conflict", "A ZIP path conflicts with a file path.")
    # Check actual decompression and CRC, including files we will not analyze.
    actual_total = 0
    for entry in entries:
        if entry.is_dir():
            continue
        actual_file = 0
        with archive.open(entry) as stream:
            while chunk := stream.read(64 * 1024):
                actual_file += len(chunk)
                actual_total += len(chunk)
                if actual_file > limits.file_bytes or actual_total > limits.expanded_bytes:
                    raise InspectionError("expanded_size_limit", "The expanded ZIP exceeds the size limit.")
        if actual_file != entry.file_size:
            raise InspectionError("invalid_archive", "A ZIP file has inconsistent size metadata.")
    return files, expanded


SECRET_NAMES = {".env", "id_rsa", "id_ed25519"}
ENV_EXAMPLES = {".env.example", ".env.sample", ".env.template"}


def _secret_files(files):
    out = []
    for name in files:
        base = PurePosixPath(name).name
        if base in SECRET_NAMES or name.endswith((".pem", ".key")) or (base.startswith(".env.") and base not in ENV_EXAMPLES):
            out.append(name)
    return sorted(out)


def _analyze(files, expanded, digest, archive_size, limits):
    secrets = _secret_files(files)
    warnings = []
    if secrets:
        warnings.append("در ZIP فایل حساس (مثل .env) بود؛ آن را روی لیارا آپلود نمی‌کنم و مقادیر لازم را به‌صورت متغیر محیطی تنظیم می‌کنم.")
    return {
        "schema_version": 2,
        "source_sha256": digest,
        "archive": {"bytes": archive_size, "expanded_bytes": expanded, "files": len(files)},
        "possible_secret_file_paths": secrets,
        "warnings_fa": warnings,
        "limits": asdict(limits),
    }


def inspect_zip(path: str | Path, limits: InspectionLimits | None = None) -> dict:
    """Validate a ZIP without extracting it and return display-safe archive facts.

    Raises InspectionError for invalid archives, unsafe paths, or exceeded limits.
    """
    limits = limits or InspectionLimits()
    path = Path(path)
    if path.suffix.lower() != ".zip":
        raise InspectionError("zip_required", "Upload a project as a .zip file.")
    try:
        with path.open("rb") as stream:
            payload = stream.read(limits.archive_bytes + 1)
    except OSError as exc:
        raise InspectionError("unreadable_archive", "The project ZIP could not be read.") from exc
    if len(payload) > limits.archive_bytes:
        raise InspectionError("archive_too_large", "The project ZIP exceeds the upload size limit.")
    try:
        with zipfile.ZipFile(BytesIO(payload)) as archive:
            files, expanded = _validate_entries(archive, limits)
            return _analyze(files, expanded, sha256(payload).hexdigest(), len(payload), limits)
    except InspectionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, ValueError, zlib.error) as exc:
        raise InspectionError("invalid_archive", "The file is not a readable, intact ZIP archive.") from exc

