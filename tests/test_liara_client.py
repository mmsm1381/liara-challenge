"""Liara client tests: no network (requests is mocked) and no database."""

import os
import tarfile

import django
import pytest
import requests

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.test import override_settings  # noqa: E402

from assistant import liara_client as lc  # noqa: E402
from assistant.liara_client import FakeLiaraClient, LiaraClient, LiaraError  # noqa: E402

TOKEN = "tok_SECRET_value_1234567890"


class Resp:
    def __init__(self, status=200, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else ("" if body is None else str(body))
        self.content = b"" if body is None and not text else b"x"

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


@pytest.fixture
def client(monkeypatch):
    c = LiaraClient(TOKEN, team_id="team1", base_url="https://api.test", storage_base_url="https://st.test", timeout=7)
    c.sent = []
    c.queue = []

    def fake_request(method, url, **kw):
        c.sent.append((method, url, kw))
        item = c.queue.pop(0) if c.queue else Resp(200, {})
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(c.session, "request", fake_request)
    return c


@pytest.fixture
def fake_env(tmp_path, settings):
    settings.BASE_DIR = tmp_path
    settings.LIARA_FAKE = True
    return tmp_path


# ---- transport / errors

@pytest.mark.parametrize(
    "resp,code",
    [
        (Resp(401), "unauthorized"),
        (Resp(403), "unauthorized"),
        (Resp(404), "not_found"),
        (Resp(400, {"message": "frozen_project"}), "frozen"),
        (Resp(429), "rate_limited"),
        (Resp(500), "unavailable"),
        (Resp(503), "unavailable"),
        (requests.ConnectionError("boom " + TOKEN), "unavailable"),
        (requests.Timeout("slow"), "timeout"),
        (Resp(422, {"message": "bad name"}), "bad_request"),
    ],
)
def test_error_mapping(client, resp, code):
    client.queue.append(resp)
    with pytest.raises(LiaraError) as e:
        client.list_projects()
    assert e.value.code == code
    assert e.value.message_fa
    assert TOKEN not in str(e.value) and TOKEN not in e.value.message_fa


def test_frozen_message_persian(client):
    client.queue.append(Resp(400, {"message": "frozen_project"}))
    with pytest.raises(LiaraError) as e:
        client.restart("a")
    assert e.value.message_fa == "موجودی حساب لیارا کافی نیست؛ حساب را در کنسول لیارا شارژ کنید."
    assert e.value.status == 400


def test_bad_request_redacts_token(client):
    client.queue.append(Resp(400, {"message": f"invalid token {TOKEN} password=hunter2"}))
    with pytest.raises(LiaraError) as e:
        client.restart("a")
    assert TOKEN not in e.value.message_fa and "hunter2" not in e.value.message_fa


def test_bearer_header_and_team_param(client):
    client.queue.append(Resp(200, {"projects": []}))
    client.list_projects()
    method, url, kw = client.sent[0]
    assert client.session.headers["Authorization"] == f"Bearer {TOKEN}"
    assert url == "https://api.test/v1/projects"
    assert kw["params"] == {"teamID": "team1"}
    assert kw["timeout"] == 7
    assert kw["allow_redirects"] is False


def test_no_team_param_when_empty():
    c = LiaraClient("t", base_url="https://x", storage_base_url="https://y")
    sent = {}
    c.session.request = lambda m, u, **kw: sent.update(kw) or Resp(200, {"projects": []})
    c.list_projects()
    assert sent["params"] is None


def test_no_retry_on_post(client):
    client.queue.extend([Resp(500), Resp(200, {})])
    with pytest.raises(LiaraError):
        client.restart("a")
    assert len(client.sent) == 1


def test_repr_hides_token(client):
    assert TOKEN not in repr(client)


def test_get_project_names_only_and_url(client):
    client.queue.append(Resp(200, {"project": {"project_id": "a", "type": "django", "planID": "p", "status": "RUNNING",
                                                "envs": [{"key": "A", "value": "secretvalue"}]}}))
    p = client.get_project("a")
    assert p["envs_keys"] == ["A"]
    assert "secretvalue" not in str(p)
    assert p["url"].startswith("https://a.")


def test_logs_capped_and_normalized(client):
    client.queue.append(Resp(200, [{"datetime": str(i), "message": f"m{i}"} for i in range(500)]))
    logs = client.get_logs("a", 100)
    assert len(logs) == 200 and logs[-1] == {"ts": "499", "message": "m499"}
    assert client.sent[0][2]["params"]["since"] == 100


def test_list_releases_and_rollback_without_source(client):
    body = {"currentRelease": "r2", "releases": [{"_id": "r1", "state": "READY", "createdAt": "t", "sourceAvailable": True}]}
    client.queue.extend([Resp(200, body)])
    with pytest.raises(LiaraError) as e:
        client.rollback("a", "r1")
    assert e.value.code == "bad_request"


def test_rollback_with_source(client):
    body = {"currentRelease": "r2", "releases": [{"_id": "r1", "state": "READY", "sourceID": "s1", "projectType": "node", "port": 3000}]}
    client.queue.extend([Resp(200, body), Resp(200, {"releaseID": "r3"})])
    assert client.rollback("a", "r1") == "r3"
    assert client.sent[1][2]["json"]["sourceID"] == "s1"


def test_update_envs_merges(client):
    client.queue.extend([Resp(200, {"project": {"envs": [{"key": "OLD", "value": "1"}]}}), Resp(200, {"message": "ok"})])
    client.update_envs("a", {"NEW": "2"})
    assert client.sent[1][2]["json"] == {"project": "a", "variables": [{"key": "OLD", "value": "1"}, {"key": "NEW", "value": "2"}]}


def test_build_logs_parsing(client):
    client.queue.append(Resp(200, {"release": {"state": "FAILED", "failReason": "oops"}, "buildOutput": ["l1", {"line": "l2"}]}))
    r = client.get_build_logs("rid", 0)
    assert r["state"] == "FAILED" and r["fail_reason"] == "oops" and r["lines"] == ["l1", "l2"]


def test_upload_and_release(client, tmp_path):
    f = tmp_path / "a.tar.gz"
    f.write_bytes(b"x")
    client.queue.extend([Resp(200, {"sourceID": "s9"}), Resp(200, {"releaseID": "r9"})])
    sid = client.upload_source("a", f)
    assert sid == "s9"
    assert "file" in client.sent[0][2]["files"]
    assert client.sent[0][2]["timeout"] >= 300
    assert client.create_release("a", sid, "node", 3000, message="m") == "r9"
    body = client.sent[1][2]["json"]
    assert body["sourceID"] == "s9" and body["type"] == "node" and body["port"] == 3000


def test_storage_uses_storage_base(client):
    client.queue.append(Resp(200, {"buckets": [{"id": "1", "name": "b", "plan": "20g", "permission": "private", "status": "x"}]}))
    assert client.list_buckets()[0]["name"] == "b"
    assert client.sent[0][1] == "https://st.test/api/v1/buckets"


def test_get_database_returns_password_to_caller(client):
    client.queue.append(Resp(200, {"database": {"id": "d", "type": "postgresql", "status": "RUNNING", "hostname": "h", "port": 5432,
                                                  "username": "root", "root_password": "pw"}}))
    d = client.get_database("d")
    assert d["password"] == "pw" and d["internal_host"] == "h"


def test_list_plans_reads_me_endpoint(client):
    """Liara has no /v1/plans; plans come from GET /v1/me (shape recorded from the live API)."""
    # Given: a /v1/me answer with current, legacy, unavailable and foreign-region plans
    client.queue.append(Resp(200, {"user": {}, "plans": {
        "projects": {
            "small-g2": {"available": True, "price": 659.72, "displayPrice": 475000, "volume": 5, "RAM": {"amount": 0.5}, "CPU": {"amount": 0.5}, "region": "iran"},
            "mini-g2": {"available": False, "price": 381.94, "volume": 2.5, "RAM": {"amount": 0.25}, "CPU": {"amount": 0.25}, "region": "iran"},
            "ir-small": {"available": True, "price": 1319.44, "volume": 5, "RAM": {"amount": 0.5}, "CPU": {"amount": 0.5}, "region": "iran"},
        },
        "databases": {
            "medium-g2": {"available": True, "price": 1145.83, "displayPrice": 825000, "volume": 10, "RAM": {"amount": 1}, "CPU": {"amount": 1}},
        },
    }}))

    # When: the plans are listed
    plans = client.list_plans()

    # Then: only available g2 plans come back, with the monthly price in toman
    assert client.sent[0][1] == "https://api.test/v1/me"
    assert plans == [
        {"id": "small-g2", "kind": "app", "ram_mb": 512, "cpu": 0.5, "disk_gb": 5, "price_month_toman": 475_000},
        {"id": "medium-g2", "kind": "database", "ram_mb": 1024, "cpu": 1, "disk_gb": 10, "price_month_toman": 825_000},
    ]
    assert lc._normalize_plans("garbage") == []


def test_create_project_sends_bundle_for_g2_plan(client):
    """The live API lists g2 plans with feature bundles; the Liara CLI always sends one."""
    # Given: a current-generation plan
    # When: the app is created
    client.create_project("shop", "django", "small-g2")

    # Then: the "standard" bundle, whose price is the plan's listed price, is sent
    assert client.sent[0][2]["json"] == {"name": "shop", "planID": "small-g2", "platform": "django", "bundlePlanID": "standard"}


def test_create_database_uses_liara_type_name(client):
    """The live API rejects "postgresql"; its type name is "postgres"."""
    # Given: Liara answers with the new database ID
    client.queue.append(Resp(200, {"databaseID": "d1"}))

    # When: a PostgreSQL database is created on a g2 plan
    client.create_database("postgresql", "shop-db", "small-g2")

    # Then: the Liara type name and the bundle are sent
    body = client.sent[0][2]["json"]
    assert body["type"] == "postgres" and body["bundlePlanID"] == "standard"


def test_payment_required_means_no_balance(client):
    """Live answer for an account without credit: 402 "Not enough balance."."""
    # Given: Liara answers 402
    client.queue.append(Resp(402, {"statusCode": 402, "message": "Not enough balance."}))

    # When / Then: the user is told to charge the account
    with pytest.raises(LiaraError) as e:
        client.create_project("shop", "django", "small-g2", network="n1")
    assert e.value.code == "frozen"


def test_networks(client):
    """Accounts with private networks need one; the CLI lists and creates them."""
    # Given: one network on the account
    client.queue.extend([Resp(200, {"networks": [{"_id": "n1", "name": "assistant", "createdAt": "x"}]}), Resp(200, {})])

    # When: networks are listed and one is created
    networks = client.list_networks()
    client.create_network("assistant")

    # Then: the CLI's paths and body are used
    assert networks == [{"id": "n1", "name": "assistant"}]
    assert client.sent[1][1] == "https://api.test/v1/networks"
    assert client.sent[1][2]["json"] == {"networkName": "assistant"}


def test_create_calls_send_network(client):
    """Without `network`, Liara answers g2_plan_unavailable for private-network accounts."""
    # Given: Liara accepts both calls
    client.queue.extend([Resp(200, {}), Resp(200, {"databaseID": "d1"})])

    # When: an app and a database are created in network n1
    client.create_project("shop", "django", "small-g2", network="n1")
    client.create_database("postgresql", "shop-db", "small-g2", network="n1")

    # Then: both bodies carry the network ID
    assert client.sent[0][2]["json"]["network"] == "n1"
    assert client.sent[1][2]["json"]["network"] == "n1"


def test_check_url_never_raises(monkeypatch, settings):
    settings.LIARA_FAKE = False

    class Boom:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, *a, **k):
            raise requests.ConnectionError("x")

    monkeypatch.setattr(lc.requests, "Session", lambda: Boom())
    r = lc.check_url("https://x.liara.run")
    assert r["ok"] is False and r["error"] and "ms" in r
    assert lc.check_url("ftp://x")["ok"] is False


# ---- archive

def test_build_archive_exclusions(tmp_path):
    src = tmp_path / "proj"
    for rel in ["app.py", "requirements.txt", ".env", ".env.production", ".env.example", ".git/config",
                "node_modules/x/index.js", "venv/bin/python", ".venv/a", "pkg/__pycache__/a.pyc", "pkg/mod.py", "skipme.log"]:
        p = src / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("data")
    (src / ".gitignore").write_text("*.log\n")
    out = tmp_path / "out" / "a.tar.gz"
    size = lc.build_archive(src, out)
    assert size == out.stat().st_size > 0
    with tarfile.open(out) as t:
        names = set(t.getnames())
    assert {"app.py", "requirements.txt", ".env.example", "pkg/mod.py", ".gitignore"} <= names
    assert not names & {".env", ".env.production", ".git/config", "node_modules/x/index.js", "venv/bin/python",
                        ".venv/a", "pkg/__pycache__/a.pyc", "skipme.log"}


def test_build_archive_too_big(tmp_path, monkeypatch):
    src = tmp_path / "p"
    src.mkdir()
    (src / "a.txt").write_text("hello")
    monkeypatch.setattr(lc, "MAX_ARCHIVE_BYTES", 10)
    with pytest.raises(LiaraError):
        lc.build_archive(src, tmp_path / "o.tar.gz")
    assert not (tmp_path / "o.tar.gz").exists()


# ---- fake client

def test_fake_invalid_tokens(fake_env):
    for t in ("fake-invalid", "bad-key"):
        with pytest.raises(LiaraError) as e:
            FakeLiaraClient(t).list_projects()
        assert e.value.code == "unauthorized"


def test_fake_seed_and_factories(fake_env):
    c = lc.client_for_token("anything")
    assert isinstance(c, FakeLiaraClient)
    assert {p["name"] for p in c.list_projects()} == {"my-shop", "blog"}
    plans = c.list_plans()
    assert len([p for p in plans if p["kind"] == "app"]) == 3


def test_fake_deploy_cycle(fake_env, tmp_path):
    c = FakeLiaraClient("ok")
    c.create_project("newapp", "node", "fake-app-small")
    src = tmp_path / "s"
    src.mkdir()
    (src / "package.json").write_text("{}")
    lc.build_archive(src, tmp_path / "a.tar.gz")
    sid = c.upload_source("newapp", tmp_path / "a.tar.gz")
    rid = c.create_release("newapp", sid, "node", 3000)
    first = c.get_build_logs(rid, 0)
    assert first["state"] == "DEPLOYING"
    second = c.get_build_logs(rid, first["next_since"])
    assert second["state"] == "READY"
    rel = c.list_releases("newapp")
    assert rel["current"] == rid and rel["releases"][0]["source_id"] == sid
    assert lc.check_url("https://newapp.liara.run")["ok"] is True
    # rollback re-uses the source
    assert c.rollback("newapp", rid)


def test_fake_build_failure(fake_env, tmp_path):
    c = FakeLiaraClient("ok")
    src = tmp_path / "s"
    src.mkdir()
    (src / "README").write_text("FORCE_BUILD_FAIL")
    lc.build_archive(src, tmp_path / "a.tar.gz")
    sid = c.upload_source("blog", tmp_path / "a.tar.gz")
    rid = c.create_release("blog", sid, "static", 80)
    c.get_build_logs(rid)
    r = c.get_build_logs(rid)
    assert r["state"] == "FAILED" and r["fail_reason"] == "package.json has no start script"
    assert c.list_releases("blog")["current"] != rid


def test_fake_scenarios(fake_env):
    c = FakeLiaraClient("ok")
    lc.fake_set_scenario("my-shop", "missing_package")
    assert any("No module named 'requests'" in l["message"] for l in c.get_logs("my-shop"))
    assert lc.check_url("https://my-shop.liara.run")["status_code"] == 502

    lc.fake_set_scenario("my-shop", "oom")
    mem = c.get_metrics_summary("my-shop")["memoryUsage"][0]["value"][0][1]
    assert float(mem) / (512 * 1024 * 1024) > 0.9
    assert any("OOMKilled" in l["message"] for l in c.get_logs("my-shop"))
    c.resize("my-shop", "fake-app-medium")
    assert lc.check_url("https://my-shop.liara.run")["ok"] is True

    lc.fake_set_scenario("my-shop", "missing_env")
    assert any("set the SECRET_KEY environment variable" in l["message"] for l in c.get_logs("my-shop"))
    c.update_envs("my-shop", {"SECRET_KEY": "x"})
    assert "SECRET_KEY" in c.get_project("my-shop")["envs_keys"]
    assert c.get_project("my-shop")["status"] == "RUNNING"

    lc.fake_set_scenario("my-shop", "healthy")
    assert lc.check_url("https://my-shop.liara.run")["ok"] is True
    with pytest.raises(ValueError):
        lc.fake_set_scenario("my-shop", "nope")


def test_fake_db_and_bucket(fake_env):
    c = FakeLiaraClient("ok")
    db = c.create_database("postgresql", "mydb", "fake-db-small")
    info = c.get_database(db["id"])
    assert info["password"] and info["port"] == 5432
    assert c.list_databases()[0]["id"] == db["id"]
    c.create_bucket("files", "20g")
    key = c.create_bucket_key("files")
    assert key["access_key"] and key["secret_key"]
    with pytest.raises(LiaraError):
        c.create_bucket_key("missing")


def test_fake_state_shared_across_instances(fake_env):
    FakeLiaraClient("a").restart("blog")
    FakeLiaraClient("a").create_project("zzz", "node", "fake-app-small")
    assert "zzz" in {p["name"] for p in FakeLiaraClient("b").list_projects()}
    assert (fake_env / "var" / "fake_liara.json").exists()


def test_make_client(settings):
    class Conn:
        team_id = "t"

        def get_token(self):
            return "abc"

    settings.LIARA_FAKE = True
    assert isinstance(lc.make_client(Conn()), FakeLiaraClient)
    settings.LIARA_FAKE = False
    c = lc.make_client(Conn())
    assert isinstance(c, LiaraClient) and c.team_id == "t"
