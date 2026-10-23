"""Bounded static ZIP inspection. Uploaded code is never imported or executed."""

import ast
import json
from dataclasses import asdict, dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
import re
import stat
import tomllib
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


def _read_text(archive, name, limits):
    if archive.getinfo(name).file_size > limits.text_bytes:
        raise InspectionError("metadata_too_large", "A project metadata file is too large to inspect.")
    try:
        return archive.read(name).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise InspectionError("invalid_text", "Project metadata must use UTF-8 text.") from exc


def _parse_python(text):
    try:
        return ast.parse(text)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise InspectionError("invalid_python_metadata", "A project settings or entry file could not be parsed.") from exc


def _declares_django(archive, files, root, limits):
    manifests = sorted(
        name for name in files
        if name == root + "requirements.txt"
        or (name.startswith(root + "requirements/") and name.endswith(".txt"))
    )
    found = False
    for name in manifests:
        text = _read_text(archive, name, limits)
        found |= bool(re.search(r"(?im)^\s*django(?:\[[^\]]+\])?\s*(?:[<>=!~;@]|$)", text))
    pyproject = root + "pyproject.toml"
    if pyproject in files:
        manifests.append(pyproject)
        try:
            data = tomllib.loads(_read_text(archive, pyproject, limits))
        except (tomllib.TOMLDecodeError, RecursionError) as exc:
            raise InspectionError("invalid_project_metadata", "The pyproject.toml file is invalid.") from exc
        project = data.get("project", {})
        tool = data.get("tool", {})
        if not isinstance(project, dict) or not isinstance(tool, dict):
            raise InspectionError("invalid_project_metadata", "Project metadata has an unexpected structure.")
        dependencies = project.get("dependencies", [])
        if not isinstance(dependencies, list) or not all(isinstance(item, str) for item in dependencies):
            raise InspectionError("invalid_project_metadata", "Project dependencies must be a list of strings.")
        found |= any(re.match(r"(?i)^django(?:\[[^\]]+\])?\s*(?:[<>=!~;@]|$)", item.strip()) is not None for item in dependencies)
        poetry = tool.get("poetry", {})
        if isinstance(poetry, dict):
            poetry_dependencies = poetry.get("dependencies", {})
            if isinstance(poetry_dependencies, dict):
                found |= any(key.lower() == "django" for key in poetry_dependencies)
    return found, manifests


def _settings_module(tree):
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "setdefault"
            and isinstance(node.func.value, ast.Attribute)
            and isinstance(node.func.value.value, ast.Name)
            and node.func.value.value.id == "os"
            and node.func.value.attr == "environ"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "DJANGO_SETTINGS_MODULE"
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            module = node.args[1].value
            if all(part.isidentifier() for part in module.split(".")):
                return module
    return None


def _settings_facts(tree):
    variables = set()
    backends = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            is_getenv = isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id == "os" and func.attr == "getenv"
            is_environ = (
                isinstance(func, ast.Attribute) and func.attr in ("get", "setdefault")
                and isinstance(func.value, ast.Attribute) and func.value.attr == "environ"
                and isinstance(func.value.value, ast.Name) and func.value.value.id == "os"
            )
            if (is_getenv or is_environ) and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                variables.add(node.args[0].value)
        if (
            isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name) and node.value.value.id == "os"
            and node.value.attr == "environ" and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            variables.add(node.slice.value)
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if node.value in {"django.db.backends.postgresql", "django.db.backends.sqlite3", "django.db.backends.mysql", "django.db.backends.oracle"}:
                backends.add(node.value)
    # Arbitrary constants are never copied into a report, including secret values.
    variables = {key for key in variables if re.fullmatch(r"[A-Z][A-Z0-9_]{0,127}", key)}
    return sorted(variables), sorted(backends)


def _analyze_django(archive, files, expanded, digest, archive_size, limits):
    warnings = []
    candidates = sorted(name for name in files if PurePosixPath(name).name == "manage.py")
    root = ""
    manage = candidates[0] if len(candidates) == 1 else None
    if manage:
        root = manage.removesuffix("manage.py")
    declared, manifests = _declares_django(archive, files, root, limits)
    module = None
    settings_path = None
    variables, backends = [], []
    if manage:
        module = _settings_module(_parse_python(_read_text(archive, manage, limits)))
        expected = root + module.replace(".", "/") + ".py" if module else None
        if expected in files:
            settings_path = expected
            variables, backends = _settings_facts(_parse_python(_read_text(archive, expected, limits)))
        else:
            warnings.append("Settings could not be resolved statically; split or dynamic settings need manual review.")
    if len(candidates) > 1:
        warnings.append("Multiple manage.py files found. Upload a ZIP containing one project.")
    if not manage:
        warnings.append("A single Django manage.py entry point was not found.")
    if not declared:
        warnings.append("Django was not explicitly declared in the supported dependency manifests.")
    sensitive_files = sorted(name for name in files if PurePosixPath(name).name in {".env", "id_rsa", "id_ed25519"} or name.endswith((".pem", ".key")) or (PurePosixPath(name).name.startswith(".env.") and PurePosixPath(name).name not in {".env.example", ".env.sample", ".env.template"}))
    if sensitive_files:
        warnings.append("Possible secret files are present. Remove real credentials before any deployment; their contents were not analyzed.")
    supported = bool(manage and declared and settings_path)
    warnings.append("Static inspection cannot prove runtime compatibility, dependency installation, or successful deployment.")
    result = {
        "schema_version": 1,
        "status": "recognized" if supported else "unsupported",
        "framework": "django" if manage and declared else "unknown",
        "source_supported": supported,
        "deployment_ready": False,
        "source_sha256": digest,
        "archive": {"bytes": archive_size, "expanded_bytes": expanded, "files": len(files)},
        "facts": {
            "project_root": root or ".",
            "dependency_manifests": manifests,
            "django_declared": declared,
            "manage_path": manage,
            "settings_module": module,
            "settings_path": settings_path,
            "environment_variable_names": variables,
            "database_backends_observed": backends,
            "wsgi_paths": sorted(name for name in files if name.startswith(root) and name.endswith("/wsgi.py")),
            "possible_secret_file_paths": sensitive_files,
        },
        "warnings": warnings,
        "next_step": "Confirm project facts and answer requirements questions." if supported else "Review the warnings and provide a single supported Django project.",
        "limits": asdict(limits),
    }
    return result, settings_path, backends


UNSUPPORTED_FA = "فعلاً فقط پروژه‌های Django و Node.js با اسکریپت start پشتیبانی می‌شوند."
NO_START_FA = "package.json اسکریپت start ندارد."
PY_SERVICES = {
    "postgresql": ("postgresql", {"psycopg", "psycopg2", "psycopg2-binary", "psycopg-binary", "psycopg-pool"}),
    "mysql": ("mysql", {"mysqlclient", "pymysql"}),
    "mongodb": ("mongodb", {"pymongo"}),
    "redis": ("redis", {"redis", "django-redis"}),
}
NODE_SERVICES = {
    "pg": "postgresql", "mysql2": "mysql", "mongoose": "mongodb", "mongodb": "mongodb",
    "redis": "redis", "ioredis": "redis", "multer": "bucket",
}
SERVICE_NAMES_FA = {"postgresql": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB", "redis": "Redis", "bucket": "فضای ذخیره‌سازی فایل (Bucket)"}
JS_EXTENSIONS = (".js", ".ts", ".mjs", ".cjs")
ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}")


class _Services:
    def __init__(self):
        self.items = {}

    def add(self, kind, source, token, what):
        if kind in self.items:
            return
        label = SERVICE_NAMES_FA[kind]
        if what == "package":
            reason = f"در {source} بسته‌ی {token} پیدا شد، پس به {label} نیاز دارد."
        elif what == "folder":
            reason = f"پوشه‌ی {token} در پروژه هست، پس برای ذخیره‌ی فایل‌ها به {label} نیاز دارد."
        else:
            reason = f"در {source} نشانه‌ی {token} پیدا شد، پس به {label} نیاز دارد."
        self.items[kind] = {"kind": kind, "reason_fa": reason, "evidence": f"{source}:{token}"}

    def list(self):
        order = ["postgresql", "mysql", "mongodb", "redis", "bucket"]
        return [self.items[k] for k in order if k in self.items]


def _py_requirement_names(text):
    names = set()
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match and not line.startswith("-"):
            names.add(re.sub(r"[_.]+", "-", match.group(1)).lower())
    return names


def _pyproject_names(text):
    try:
        data = tomllib.loads(text)
    except (tomllib.TOMLDecodeError, RecursionError):
        return set()
    names = set()
    project = data.get("project", {})
    deps = project.get("dependencies", []) if isinstance(project, dict) else []
    if isinstance(deps, list):
        names |= _py_requirement_names("\n".join(d for d in deps if isinstance(d, str)))
    poetry = data.get("tool", {}).get("poetry", {}) if isinstance(data.get("tool"), dict) else {}
    pd = poetry.get("dependencies", {}) if isinstance(poetry, dict) else {}
    if isinstance(pd, dict):
        names |= {re.sub(r"[_.]+", "-", k).lower() for k in pd}
    return names


def _env_example_names(archive, files, root, limits):
    names = set()
    for name in sorted(files):
        if PurePosixPath(name).name in (".env.example", ".env.sample", "env.example") and name.startswith(root):
            for line in _read_text(archive, name, limits).splitlines():
                match = re.match(r"\s*(?:export\s+)?([A-Z][A-Z0-9_]*)\s*=", line)
                if match and ENV_NAME.fullmatch(match.group(1)):
                    names.add(match.group(1))
    return names


def _bucket_folder(files, root):
    for name in sorted(files):
        if not name.startswith(root):
            continue
        parts = name[len(root):].split("/")[:-1]
        for part in parts:
            if part in ("uploads", "media"):
                return part + "/"
    return None


def _python_project(archive, files, result, settings_path, backends, limits):
    facts = result["facts"]
    root = "" if facts["project_root"] == "." else facts["project_root"]
    services = _Services()
    for name in facts["dependency_manifests"]:
        base = PurePosixPath(name).name
        if base == "pyproject.toml":
            names = _pyproject_names(_read_text(archive, name, limits))
        else:
            names = _py_requirement_names(_read_text(archive, name, limits))
        for kind, (_, pkgs) in PY_SERVICES.items():
            for pkg in sorted(pkgs):
                if pkg in names:
                    services.add(kind, base, pkg, "package")
                    break
    if "django.db.backends.postgresql" in backends:
        services.add("postgresql", PurePosixPath(settings_path).name, "django.db.backends.postgresql", "token")
    if "django.db.backends.mysql" in backends:
        services.add("mysql", PurePosixPath(settings_path).name, "django.db.backends.mysql", "token")
    if settings_path and re.search(r"\bMEDIA_ROOT\b", _read_text(archive, settings_path, limits)):
        services.add("bucket", PurePosixPath(settings_path).name, "MEDIA_ROOT", "token")
    folder = _bucket_folder(files, root)
    if folder:
        services.add("bucket", folder, folder, "folder")
    wsgi = None
    paths = facts["wsgi_paths"]
    if paths:
        wsgi = paths[0][len(root):].removesuffix(".py").replace("/", ".")
    elif facts["settings_module"] and "." in facts["settings_module"]:
        wsgi = facts["settings_module"].rsplit(".", 1)[0] + ".wsgi"
    env = set(facts["environment_variable_names"]) | _env_example_names(archive, files, root, limits)
    return {
        "platform": "django", "supported": True, "unsupported_reason_fa": None, "root": root,
        "start_command": f"gunicorn {wsgi}" if wsgi else None, "port": 80, "wsgi_module": wsgi,
        "services": services.list(), "env_vars": sorted(env), "warnings_fa": [],
    }


def _node_project(archive, files, limits):
    pkgs = sorted(n for n in files if PurePosixPath(n).name == "package.json" and "node_modules" not in PurePosixPath(n).parts)
    loaded = {}
    for name in pkgs:
        try:
            data = json.loads(_read_text(archive, name, limits))
        except (json.JSONDecodeError, RecursionError) as exc:
            raise InspectionError("invalid_project_metadata", "The package.json file is invalid.") from exc
        loaded[name] = data if isinstance(data, dict) else {}

    def start_of(data):
        scripts = data.get("scripts")
        value = scripts.get("start") if isinstance(scripts, dict) else None
        return value if isinstance(value, str) and value.strip() else None

    with_start = [n for n in pkgs if start_of(loaded[n])]
    base = {"platform": "node", "supported": False, "root": "", "start_command": None, "port": 3000,
            "wsgi_module": None, "services": [], "env_vars": [], "warnings_fa": []}
    if len(with_start) != 1:
        if len(with_start) > 1:
            reason = "چند package.json با اسکریپت start پیدا شد؛ فقط یک پروژه را آپلود کنید."
        else:
            reason = NO_START_FA
        return {**base, "unsupported_reason_fa": reason}
    name = with_start[0]
    root = name.removesuffix("package.json")
    data = loaded[name]
    services = _Services()
    for section in ("dependencies", "devDependencies"):
        deps = data.get(section)
        if isinstance(deps, dict):
            for pkg, kind in NODE_SERVICES.items():
                if pkg in deps:
                    services.add(kind, "package.json", pkg, "package")
    schema = root + "prisma/schema.prisma"
    if schema in files:
        match = re.search(r'provider\s*=\s*"(postgresql|mysql|mongodb)"', _read_text(archive, schema, limits))
        if match:
            services.add(match.group(1), "schema.prisma", f'provider = "{match.group(1)}"', "token")
    folder = _bucket_folder(files, root)
    if folder:
        services.add("bucket", folder, folder, "folder")
    env = set()
    port = None
    listen_port = None
    scanned = 0
    for fname in sorted(files):
        if scanned >= 200:
            break
        if not fname.startswith(root) or not fname.endswith(JS_EXTENSIONS) or "node_modules" in PurePosixPath(fname).parts:
            continue
        if archive.getinfo(fname).file_size > limits.text_bytes:
            continue
        scanned += 1
        text = archive.read(fname).decode("utf-8", "replace")
        for var in re.findall(r"process\.env\.([A-Z][A-Z0-9_]*)", text):
            env.add(var)
        if port is None:
            match = re.search(r"process\.env\.PORT\s*(?:\|\||\?\?)\s*(\d{2,5})", text)
            if match:
                port = int(match.group(1))
        if listen_port is None:
            match = re.search(r"\.listen\(\s*(\d{2,5})\b", text)
            if match:
                listen_port = int(match.group(1))
    env |= _env_example_names(archive, files, root, limits)
    env.discard("PORT")
    port = port or listen_port or 3000
    if not 1 <= port <= 65535:
        port = 3000
    script = start_of(data).strip()[:200]
    return {**base, "supported": True, "root": root, "start_command": f"npm start ({script})", "port": port,
            "services": services.list(), "env_vars": sorted(env)}


def _analyze(archive, files, expanded, digest, archive_size, limits):
    result, settings_path, backends = _analyze_django(archive, files, expanded, digest, archive_size, limits)
    if result["source_supported"]:
        project = _python_project(archive, files, result, settings_path, backends, limits)
    elif any(PurePosixPath(n).name == "package.json" and "node_modules" not in PurePosixPath(n).parts for n in files):
        project = _node_project(archive, files, limits)
    else:
        project = {"platform": None, "supported": False, "unsupported_reason_fa": UNSUPPORTED_FA, "root": "",
                   "start_command": None, "port": 3000, "wsgi_module": None, "services": [], "env_vars": [], "warnings_fa": []}
    if not project["supported"] and not project["unsupported_reason_fa"]:
        project["unsupported_reason_fa"] = UNSUPPORTED_FA
    sensitive = result["facts"]["possible_secret_file_paths"]
    if sensitive:
        project["warnings_fa"].append("در ZIP فایل حساس (مثل .env) بود؛ آن را روی لیارا آپلود نمی‌کنم و مقادیر لازم را به‌صورت متغیر محیطی تنظیم می‌کنم.")
    result["project"] = project
    return result


def inspect_zip(path: str | Path, limits: InspectionLimits | None = None) -> dict:
    """Validate a ZIP and return display-safe facts without extracting any files.

    A recognized project is eligible for the next setup step, not for deployment.
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
            return _analyze(archive, files, expanded, sha256(payload).hexdigest(), len(payload), limits)
    except InspectionError:
        raise
    except (zipfile.BadZipFile, zipfile.LargeZipFile, OSError, EOFError, RuntimeError, NotImplementedError, ValueError, zlib.error) as exc:
        raise InspectionError("invalid_archive", "The file is not a readable, intact ZIP archive.") from exc

