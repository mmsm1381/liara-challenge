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


def kinds(result):
    return {s["kind"] for s in result["project"]["services"]}


def test_demo_zip(fixtures):
    r = inspect_zip(fixtures / "django-postgresql-demo.zip")
    p = r["project"]
    assert p["platform"] == "django" and p["supported"]
    assert "postgresql" in kinds(r)
    assert {"DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "SECRET_KEY"} <= set(p["env_vars"])
    # Liara fixes the django platform port at 80 (CLI src/utils/get-port.ts).
    assert p["port"] == 80 and p["start_command"].startswith("gunicorn ")
    assert len([s for s in p["services"] if s["kind"] == "postgresql"]) == 1
    assert r["source_supported"] is True


def test_node_express(fixtures):
    r = inspect_zip(fixtures / "node-express.zip")
    p = r["project"]
    assert p["platform"] == "node" and p["supported"]
    assert {"postgresql", "bucket"} <= kinds(r)
    assert p["port"] == 3000
    assert "DATABASE_URL" in p["env_vars"]
    assert p["root"] == "shop/"
    assert "npm start" in p["start_command"]


def test_node_no_start(fixtures):
    p = inspect_zip(fixtures / "node-no-start.zip")["project"]
    assert not p["supported"]
    assert p["unsupported_reason_fa"] == "package.json اسکریپت start ندارد."


def test_unsupported_project_is_safe(fixtures):
    r = inspect_zip(fixtures / "unsupported-project.zip")
    assert r["source_supported"] is False
    assert r["project"]["platform"] == "node"


def test_unsafe_path(fixtures):
    with pytest.raises(InspectionError) as exc:
        inspect_zip(fixtures / "unsafe-path.zip")
    assert exc.value.code == "unsafe_path"


def test_broken(fixtures):
    with pytest.raises(InspectionError) as exc:
        inspect_zip(fixtures / "broken.zip")
    assert exc.value.code == "invalid_archive"


def test_code_must_not_run(fixtures):
    r = inspect_zip(fixtures / "code-must-not-run.zip")
    assert r["project"]["platform"] == "django"


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
    assert {"TOKEN_X", "API_KEY"} <= set(r["project"]["env_vars"])
