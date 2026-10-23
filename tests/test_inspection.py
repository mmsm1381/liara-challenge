import json
import stat
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from assistant.inspection import InspectionError, inspect_zip  # noqa: E402
from tools.create_test_projects import create_fixtures  # noqa: E402


@pytest.fixture(scope="module")
def fixtures(tmp_path_factory):
    dest = tmp_path_factory.mktemp("zips")
    create_fixtures(dest)
    return dest


def test_safe_archive_report_shape(fixtures):
    r = inspect_zip(fixtures / "django-postgresql-demo.zip")
    assert r["schema_version"] == 2
    assert len(r["source_sha256"]) == 64
    assert r["archive"]["files"] > 0 and r["archive"]["expanded_bytes"] > 0
    assert "limits" in r and "warnings_fa" in r
    # Detecting the platform, services and env vars is the model's job, not inspection's.
    assert "project" not in r


def test_other_project_types_are_just_safe_archives(fixtures):
    for name in ("node-express.zip", "node-no-start.zip", "unsupported-project.zip"):
        r = inspect_zip(fixtures / name)
        assert r["schema_version"] == 2 and "project" not in r, name


def test_unsafe_path(fixtures):
    with pytest.raises(InspectionError) as exc:
        inspect_zip(fixtures / "unsafe-path.zip")
    assert exc.value.code == "unsafe_path"


def test_broken(fixtures):
    with pytest.raises(InspectionError) as exc:
        inspect_zip(fixtures / "broken.zip")
    assert exc.value.code == "invalid_archive"


def test_code_must_not_run(fixtures):
    # Inspecting must never execute project code (the fixture's code would fail loudly if imported/run).
    r = inspect_zip(fixtures / "code-must-not-run.zip")
    assert r["schema_version"] == 2 and r["archive"]["files"] > 0


def test_zip_bomb_rejected(tmp_path):
    path = tmp_path / "bomb.zip"
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("big.txt", "0" * (3 * 1024 * 1024))
    with pytest.raises(InspectionError) as exc:
        inspect_zip(path)
    assert exc.value.code == "compression_ratio_limit"


def test_symlink_rejected(tmp_path):
    path = tmp_path / "link.zip"
    with zipfile.ZipFile(path, "w") as z:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        z.writestr(info, "/etc/passwd")
    with pytest.raises(InspectionError) as exc:
        inspect_zip(path)
    assert exc.value.code == "unsafe_entry"


def test_env_values_never_in_output(tmp_path):
    path = tmp_path / "secret.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("package.json", '{"scripts":{"start":"node a.js"}}')
        z.writestr("a.js", "console.log(process.env.TOKEN_X)")
        z.writestr(".env", "TOKEN_X=super-secret-value-123\n")
        z.writestr(".env.example", "API_KEY=another-secret-456\n")
    r = inspect_zip(path)
    out = json.dumps(r, ensure_ascii=False)
    assert "super-secret-value-123" not in out and "another-secret-456" not in out
    assert ".env" in r["possible_secret_file_paths"]
    assert ".env.example" not in r["possible_secret_file_paths"]
    assert r["warnings_fa"]
