"""End-to-end chat flows against FakeLiaraClient (no network, no LLM)."""

import json
from datetime import timedelta
from pathlib import Path

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from assistant import jobs, liara_client
from assistant.models import AuditLog, LiaraConnection, Message, PendingAction, Site, SiteNote
from tools.create_test_projects import create_fixtures

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def fake_liara(settings, tmp_path, monkeypatch):
    settings.LIARA_FAKE = True
    settings.TOKEN_ENCRYPTION_KEY = "aGVsbG8taGVsbG8taGVsbG8taGVsbG8taGVsbG8tMTI="
    settings.RQ_SYNC = True
    settings.LLM_BASE_URL = ""  # deterministic path
    settings.UPLOAD_DIR = tmp_path / "uploads"
    monkeypatch.setattr(liara_client, "_fake_path", lambda: tmp_path / "fake_liara.json", raising=False)
    monkeypatch.setattr(jobs, "BUILD_POLL_SECONDS", 0)
    # Jobs run inline in the test instead of a thread/worker.
    monkeypatch.setattr("assistant.approvals.enqueue", lambda action_id: None)
    liara_client.fake_reset()


@pytest.fixture
def zips(tmp_path):
    create_fixtures(tmp_path / "zips")
    return tmp_path / "zips"


@pytest.fixture
def client():
    user = User.objects.create_user("mahdi", password="pw-12345678")
    c = Client()
    c.force_login(user)
    c.user = user
    return c


def post(c, url, **body):
    resp = c.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def all_text(user):
    return " ".join(m.text + json.dumps(m.ui, ensure_ascii=False) for m in Message.objects.filter(conversation__user=user))


def connect(c, key="good-token-123"):
    c.get("/")
    post(c, "/chat/send", text="کلید API دارم", value="have_key")
    return post(c, "/chat/key", key=key)


def run_last_action(user):
    action = PendingAction.objects.filter(conversation__user=user).order_by("-id").first()
    jobs.run_action(action.id)
    action.refresh_from_db()
    return action


def test_greeting_and_invalid_key(client):
    resp = client.get("/")
    assert "کلید API دارم" in resp.content.decode()
    out = connect(client, key="bad-token")
    html = "".join(m["html"] for m in out["messages"])
    assert "معتبر نیست" in html and "console.liara.ir/API" in html
    assert not LiaraConnection.objects.exists()


def test_valid_key_imports_apps_and_never_stores_plaintext(client):
    out = connect(client, key="good-token-SECRET")
    assert out["composer"]["mode"] == "text"
    conn = LiaraConnection.objects.get(user=client.user)
    assert "good-token-SECRET" not in conn.encrypted_token
    assert conn.get_token() == "good-token-SECRET"
    assert "good-token-SECRET" not in all_text(client.user)
    assert set(Site.objects.values_list("app_name", flat=True)) == {"my-shop", "blog"}


def deploy_demo(c, zips, purpose="business"):
    connect(c)
    post(c, "/chat/send", text="یک پروژه‌ی جدید بالا بیاور", value="deploy_new")
    with open(zips / "django-postgresql-demo.zip", "rb") as fh:
        out = c.post("/chat/upload", {"file": fh}).json()
    html = "".join(m["html"] for m in out["messages"])
    assert "PostgreSQL" in html and "Django" in html
    post(c, "/chat/send", text="۱۰۰ تا ۱۰۰۰", value="100-1000")
    out = post(c, "/chat/send", text="کسب‌وکار واقعی", value=purpose)
    assert "تومان" in out["messages"][-1]["html"]
    out = post(c, "/chat/send", text="قبول", value="accept")
    assert "تأیید شما لازم است" in out["messages"][-1]["html"]
    return PendingAction.objects.get(conversation__user=c.user, tool="deploy")


def test_full_deploy(client, zips):
    action = deploy_demo(client, zips)
    assert action.args["db_kind"] == "postgresql" and action.args["backups"] is True
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    site = Site.objects.get(app_name=action.args["app_name"])
    assert site.status == "live" and site.created_by_assistant and site.db_type == "postgresql"
    assert site.baseline.get("ram_percent") is not None and "taken_at" in site.baseline
    assert site.notes.filter(kind=SiteNote.Kind.DEPLOY).exists()
    assert all(step["state"] == "done" for step in action.job.steps)
    text = all_text(client.user)
    assert "سایتت بالا آمد" in text
    # DB password and generated SECRET_KEY are never stored or audited in clear.
    audit = json.dumps([[a.args, a.result] for a in AuditLog.objects.all()])
    assert "postgres://root:" not in audit
    assert all(len(w) < 40 for w in audit.replace('"', " ").split())  # no generated secrets


def test_double_confirm_runs_once(client, zips):
    action = deploy_demo(client, zips)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    jobs.run_action(action.id)
    jobs.run_action(action.id)
    assert AuditLog.objects.filter(tool="create_project").count() == 1


def test_cancel_and_expiry(client, zips):
    action = deploy_demo(client, zips)
    out = post(client, "/chat/approve", action_id=action.id, decision="cancel")
    assert "چیزی ساخته نشد" in out["messages"][-1]["html"]
    action.refresh_from_db()
    assert action.status == "rejected"

    post(client, "/chat/send", text="یک پروژه‌ی جدید بالا بیاور", value="deploy_new")
    with open(zips / "django-postgresql-demo.zip", "rb") as fh:
        client.post("/chat/upload", {"file": fh})
    post(client, "/chat/send", text="x", value="lt100")
    post(client, "/chat/send", text="x", value="personal")
    post(client, "/chat/send", text="x", value="accept")
    second = PendingAction.objects.filter(tool="deploy").order_by("-id").first()
    PendingAction.objects.filter(id=second.id).update(expires_at=timezone.now() - timedelta(minutes=1))
    out = post(client, "/chat/approve", action_id=second.id, decision="confirm")
    second.refresh_from_db()
    assert second.status == "expired"
    assert not hasattr(second, "job") or second.job is None


def test_rejects_unsafe_and_unsupported_zips(client, zips):
    connect(client)
    for name, needle in (("unsafe-path.zip", "نمی‌توانم بپذیرم"), ("broken.zip", "نمی‌توانم بپذیرم"),
                         ("node-no-start.zip", "start")):
        with open(zips / name, "rb") as fh:
            out = client.post("/chat/upload", {"file": fh}).json()
        assert needle in out["messages"][-1]["html"], name


def test_cheaper_hidden_at_smallest_tier(client, zips):
    connect(client)
    post(client, "/chat/send", text="x", value="deploy_new")
    with open(zips / "django-postgresql-demo.zip", "rb") as fh:
        client.post("/chat/upload", {"file": fh})
    post(client, "/chat/send", text="x", value="lt100")
    out = post(client, "/chat/send", text="x", value="personal")
    assert "ارزان‌ترش کن" not in out["messages"][-1]["html"]


def _deployed_site(client, zips):
    action = deploy_demo(client, zips)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    run_last_action(client.user)
    return Site.objects.get(app_name=action.args["app_name"])


def test_missing_package_rollback(client, zips):
    site = _deployed_site(client, zips)
    liara_client.fake_set_scenario(site.app_name, "missing_package")
    out = post(client, "/chat/send", text=f"چرا {site.app_name} بالا نمی‌آید؟")
    html = out["messages"][-1]["html"]
    assert "requests" in html and "تأیید شما لازم است" in html
    action = PendingAction.objects.filter(tool__in=["rollback"]).order_by("-id").first()
    assert action is not None, html
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    assert site.notes.filter(kind=SiteNote.Kind.FIX).exists()


def test_memory_pressure_plan_upgrade(client, zips):
    site = _deployed_site(client, zips)
    liara_client.fake_set_scenario(site.app_name, "oom")
    out = post(client, "/chat/send", text=f"چرا {site.app_name} کند شده؟")
    html = out["messages"][-1]["html"]
    assert "روز دیپلوی" in html  # deploy-day vs now table
    action = PendingAction.objects.filter(tool="plan_change").order_by("-id").first()
    assert action is not None, html
    assert "تومان" in action.cost_text
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    assert run_last_action(client.user).status == "succeeded"


def test_missing_env_set(client, zips):
    site = _deployed_site(client, zips)
    liara_client.fake_set_scenario(site.app_name, "missing_env")
    post(client, "/chat/send", text=f"چرا {site.app_name} خطا می‌دهد؟")
    action = PendingAction.objects.filter(tool="env_change").order_by("-id").first()
    assert action is not None and action.args["name"] == "SECRET_KEY"
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    assert run_last_action(client.user).status == "succeeded"


def test_returning_user_greeting(client, zips):
    _deployed_site(client, zips)
    out = post(client, "/chat/reset")
    html = "".join(m["html"] for m in out["messages"])
    assert "سلام دوباره" in html and "my-shop" in html and "سایت‌هایم چطورند؟" in html


def test_other_users_cannot_approve(client, zips):
    action = deploy_demo(client, zips)
    other = User.objects.create_user("other", password="pw-12345678")
    c2 = Client()
    c2.force_login(other)
    c2.get("/")
    post(c2, "/chat/approve", action_id=action.id, decision="confirm")
    action.refresh_from_db()
    assert action.status == "proposed"
