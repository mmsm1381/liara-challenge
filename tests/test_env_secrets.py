"""Env var values typed by the user: asked for, held encrypted, written once, never leaked."""

import json
import zipfile
from datetime import timedelta

import pytest
from django.contrib.auth.models import User
from django.test import Client
from django.utils import timezone

from assistant import ai_detect, approvals, jobs, liara_client, llm, tools
from assistant.liara_client import FakeLiaraClient, LiaraError
from assistant.models import AuditLog, HeldSecret, Job, Message, PendingAction, Site
from tests.test_flows import all_text, connect, post, run_last_action  # noqa: F401
from tests.test_flows import client, fake_liara  # noqa: F401  (fixtures)

pytestmark = pytest.mark.django_db

VALUE = "sk_live_TOPSECRET"
MAILGUN_VALUE = "key-MAILGUN-SECRET-999"

APP_PY = (
    "import os\n"
    "STRIPE = os.environ[\"STRIPE_SECRET_KEY\"]\n"
    "MAIL = os.environ[\"MAILGUN_KEY\"]\n"
    "SENTRY = os.getenv(\"SENTRY_DSN\", \"\")\n"
    "DB = os.environ[\"DATABASE_URL\"]\n"
    "SK = os.environ[\"SECRET_KEY\"]\n"
)

REPLY = {
    "platform": "flask", "confidence": "high", "root": "", "start_command": "gunicorn app:app", "port": 8000,
    "services": [{"kind": "postgresql", "evidence": "DATABASE_URL", "reason_fa": "دیتابیس"}],
    "env_vars": ["DATABASE_URL", "SECRET_KEY", "STRIPE_SECRET_KEY", "SENTRY_DSN", "MAILGUN_KEY"],
    "blocker_fa": None, "summary_fa": "یک برنامه‌ی Flask.",
}


@pytest.fixture
def payloads(detection_model, monkeypatch):
    """Every request that reaches the (mocked) model."""
    seen = []

    def post_(payload, timeout):
        seen.append(json.dumps(payload, ensure_ascii=False))
        return detection_model(payload, timeout)

    monkeypatch.setattr(llm, "_post", post_)
    detection_model.reply = dict(REPLY)
    return seen


@pytest.fixture
def secret_zip(tmp_path):
    path = tmp_path / "stripe-app.zip"
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("requirements.txt", "flask\ngunicorn\npsycopg2-binary\n")
        z.writestr("app.py", APP_PY)
    return path


def reach_secrets(c, secret_zip):
    connect(c)
    post(c, "/chat/send", text="x", value="deploy_new")
    with open(secret_zip, "rb") as fh:
        c.post("/chat/upload", {"file": fh})
    post(c, "/chat/send", text="x", value="detect_ok")
    post(c, "/chat/send", text="x", value="100-1000")
    post(c, "/chat/send", text="x", value="business")
    return post(c, "/chat/send", text="قبول", value="accept")


def html_of(out):
    return "".join(m["html"] for m in out["messages"])


def conv_of(c):
    from assistant.models import Conversation
    return Conversation.objects.get(user=c.user)


def secret(c, value):
    return post(c, "/chat/secret", key=value)


def to_approval(c, secret_zip):
    """Questions come in sorted order: MAILGUN_KEY (set later) then STRIPE_SECRET_KEY (typed)."""
    reach_secrets(c, secret_zip)
    post(c, "/chat/send", text="x", value="secret_later")
    return secret(c, VALUE)


def deploy_action(c):
    return PendingAction.objects.get(conversation__user=c.user, tool="deploy")


def assert_no_leak(user, *values):
    blobs = [all_text(user)]
    blobs.append(json.dumps([[a.args, a.result] for a in AuditLog.objects.all()], ensure_ascii=False))
    blobs.append(json.dumps([c.state for c in __import__("assistant.models", fromlist=["Conversation"]).Conversation.objects.all()]))
    blobs.append(json.dumps([a.args for a in PendingAction.objects.all()], ensure_ascii=False))
    blobs.append(json.dumps([a.summary for a in PendingAction.objects.all()], ensure_ascii=False))
    blobs.append(json.dumps([[j.result, j.error, j.steps] for j in Job.objects.all()], ensure_ascii=False, default=str))
    for v in values:
        for b in blobs:
            assert v not in b


# ---------------------------------------------------------------- 1, 2: pure helpers

@pytest.mark.parametrize("code,expected", [
    ('os.getenv("X","d")', True),
    ('os.environ.get("X","d")', True),
    ("env('X','d')", True),
    ("process.env.X || 'd'", True),
    ('process.env["X"] ?? 1', True),
    ('os.environ["X"]', False),
    ('os.environ.get("X")', False),
    ("process.env.X", False),
    ('os.getenv("XY", "d")', False),  # a different name with the same prefix
    ('os.getenv("X")', False),
])
def test_has_default(code, expected):
    assert ai_detect.has_default("X", code) is expected


def test_auto_env_names():
    plain = jobs.auto_env_names("flask", None, None)
    assert {"SECRET_KEY", "ALLOWED_HOSTS"} <= plain
    assert not (plain & jobs.DB_VARS) and not (plain & jobs.BUCKET_VARS) and "APP_KEY" not in plain
    with_db = jobs.auto_env_names("flask", "postgresql", None)
    assert jobs.DB_VARS <= with_db and not (with_db & jobs.BUCKET_VARS)
    with_bucket = jobs.auto_env_names("flask", None, "x-files")
    assert jobs.BUCKET_VARS <= with_bucket and not (with_bucket & jobs.DB_VARS)
    assert "APP_KEY" in jobs.auto_env_names("laravel", None, None)
    assert "APP_KEY" not in jobs.auto_env_names("django", "mysql", "b")
    for p in (plain, with_db, with_bucket, jobs.auto_env_names("laravel", "mysql", "b")):
        assert {"SECRET_KEY", "ALLOWED_HOSTS"} <= p
    # A user-provided key is never "automatic".
    assert "STRIPE_SECRET_KEY" not in jobs.auto_env_names("laravel", "mysql", "b")


# ---------------------------------------------------------------- 3: asking

def test_asks_only_for_needed_vars(client, secret_zip, payloads):
    out = reach_secrets(client, secret_zip)
    html = html_of(out)
    assert "MAILGUN_KEY" in html and "(۱ از ۲)" in html
    assert "SENTRY_DSN" not in html.split("(۱ از ۲)")[1]
    assert out["composer"]["mode"] == "password"
    assert out["composer"]["endpoint"] == "/chat/secret"
    assert out["composer"]["label"] == "MAILGUN_KEY"
    state = conv_of(client).state
    assert state["secrets_queue"] == ["MAILGUN_KEY", "STRIPE_SECRET_KEY"]  # sorted; DATABASE_URL/SECRET_KEY/SENTRY_DSN absent
    assert state["awaiting_secret"] == {"name": "MAILGUN_KEY", "purpose": "deploy"}
    assert PendingAction.objects.count() == 0


# ---------------------------------------------------------------- 4, 5: the happy path

def test_full_secret_flow(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)  # asks MAILGUN_KEY first (names are sorted)
    out = post(client, "/chat/send", text="بعداً در کنسول تنظیم می‌کنم", value="secret_later")
    assert "STRIPE_SECRET_KEY" in html_of(out) and "(۲ از ۲)" in html_of(out)
    assert out["composer"]["label"] == "STRIPE_SECRET_KEY" and out["composer"]["endpoint"] == "/chat/secret"
    out = secret(client, VALUE)
    html = html_of(out)
    assert "🔒 مقدار STRIPE_SECRET_KEY وارد شد" in html
    assert Message.objects.filter(role="user", text="🔒 مقدار STRIPE_SECRET_KEY وارد شد").exists()
    assert "تأیید شما لازم است" in html
    action = deploy_action(client)
    joined = " ".join(action.summary)
    assert "STRIPE_SECRET_KEY" in joined and "MAILGUN_KEY" in joined
    assert "تأیید شما لازم است" in html
    later_line = next(line for line in action.summary if "بعداً خودت در کنسول" in line)
    assert "MAILGUN_KEY" in later_line and "STRIPE_SECRET_KEY" not in later_line
    assert VALUE not in html and VALUE not in joined
    assert action.args["user_env"] == ["STRIPE_SECRET_KEY"]
    assert VALUE not in json.dumps(action.args)
    assert out["composer"]["mode"] == "text"
    assert conv_of(client).state.get("awaiting_secret") is None

    row = HeldSecret.objects.get()
    assert row.action_id == action.id and row.name == "STRIPE_SECRET_KEY"
    assert VALUE not in row.encrypted_value and "TOPSECRET" not in row.encrypted_value
    assert row.expires_at > action.expires_at

    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    assert HeldSecret.objects.count() == 1  # still held until the job runs
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    envs = liara_envs(action.args["app_name"])
    assert envs["STRIPE_SECRET_KEY"] == VALUE
    assert "MAILGUN_KEY" not in envs and "SENTRY_DSN" not in envs  # skipped ones are not invented
    assert envs["SECRET_KEY"] and envs["DATABASE_URL"]
    assert HeldSecret.objects.count() == 0
    assert_no_leak(client.user, VALUE)
    assert all(VALUE not in p for p in payloads)
    assert payloads  # detection did reach the model, so the check above is meaningful


def liara_envs(app):
    with liara_client._fake_state() as st:
        return dict(st["apps"][app]["envs"])


def test_both_values_written_and_leak_free(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    out = secret(client, MAILGUN_VALUE)
    assert "🔒 مقدار MAILGUN_KEY وارد شد" in html_of(out) and MAILGUN_VALUE not in html_of(out)
    assert "STRIPE_SECRET_KEY" in html_of(out) and out["composer"]["label"] == "STRIPE_SECRET_KEY"
    secret(client, VALUE)
    action = deploy_action(client)
    assert action.args["user_env"] == ["MAILGUN_KEY", "STRIPE_SECRET_KEY"]
    assert not any("بعداً خودت" in line for line in action.summary)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    envs = liara_envs(action.args["app_name"])
    assert envs["STRIPE_SECRET_KEY"] == VALUE and envs["MAILGUN_KEY"] == MAILGUN_VALUE
    assert HeldSecret.objects.count() == 0
    assert_no_leak(client.user, VALUE, MAILGUN_VALUE)
    assert all(VALUE not in p and MAILGUN_VALUE not in p for p in payloads)


def test_tampered_args_never_run_and_still_clean(client, secret_zip, payloads):
    action_msg = to_approval(client, secret_zip)
    action = deploy_action(client)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    PendingAction.objects.filter(id=action.id).update(args={**action.args, "user_env": []})
    jobs.run_action(action.id)
    assert HeldSecret.objects.count() == 0  # claim() refuses, but the value is still dropped
    assert not Site.objects.filter(app_name=action.args["app_name"]).exists()
    assert action.args["app_name"] not in [a["name"] for a in FakeLiaraClient("t").list_projects()]


# ---------------------------------------------------------------- 6: every exit deletes

def test_cancel_in_secrets_step_discards(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    assert HeldSecret.objects.count() == 1
    out = post(client, "/chat/send", text="لغو", value="cancel_deploy")
    assert HeldSecret.objects.count() == 0
    assert conv_of(client).state.get("awaiting_secret") is None
    assert out["composer"]["mode"] == "text"
    assert not PendingAction.objects.exists()
    # The old password box can no longer be used.
    out = secret(client, "late-value")
    assert "الان منتظر مقداری نبودم" in html_of(out)
    assert HeldSecret.objects.count() == 0


def test_reject_discards(client, secret_zip, payloads):
    to_approval(client, secret_zip)
    action = deploy_action(client)
    assert HeldSecret.objects.count() == 1
    post(client, "/chat/approve", action_id=action.id, decision="cancel")
    assert HeldSecret.objects.count() == 0
    assert_no_leak(client.user, VALUE)


def test_expiry_discards_via_expire_stale(client, secret_zip, payloads):
    to_approval(client, secret_zip)
    action = deploy_action(client)
    PendingAction.objects.filter(id=action.id).update(expires_at=timezone.now() - timedelta(minutes=1))
    # The value itself outlives the card by APPROVAL_TTL, so expire_stale must discard it explicitly.
    assert HeldSecret.objects.filter(action=action, expires_at__gt=timezone.now()).exists()
    approvals.expire_stale(conv_of(client))
    action.refresh_from_db()
    assert action.status == "expired"
    assert HeldSecret.objects.count() == 0


def test_confirming_expired_card_discards(client, secret_zip, payloads):
    to_approval(client, secret_zip)
    action = deploy_action(client)
    PendingAction.objects.filter(id=action.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    # Bypass expire_stale (called by chat_page/send) by going straight to decide().
    approvals.decide(client.user, action.id, "confirm")
    action.refresh_from_db()
    assert action.status == "expired" and not Job.objects.exists()
    assert HeldSecret.objects.count() == 0


def test_job_failure_discards(client, secret_zip, payloads, monkeypatch):
    def boom(self, *a, **k):
        raise LiaraError("bad_request", 400, "خطا")

    monkeypatch.setattr(FakeLiaraClient, "create_project", boom)
    to_approval(client, secret_zip)
    action = deploy_action(client)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    assert HeldSecret.objects.count() == 1
    action = run_last_action(client.user)
    assert action.status == "failed"
    assert HeldSecret.objects.count() == 0
    assert_no_leak(client.user, VALUE)


def test_unexpected_exception_in_job_discards(client, secret_zip, payloads, monkeypatch):
    def boom(self, *a, **k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(FakeLiaraClient, "create_project", boom)
    to_approval(client, secret_zip)
    action = deploy_action(client)
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    try:
        jobs.run_action(action.id)
    except RuntimeError:
        pass
    assert HeldSecret.objects.count() == 0


def test_reset_discards(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    assert HeldSecret.objects.count() == 1
    post(client, "/chat/reset")
    assert HeldSecret.objects.count() == 0
    assert conv_of(client).state == {}
    assert_no_leak(client.user, MAILGUN_VALUE)


def test_reset_discards_attached_values_too(client, secret_zip, payloads):
    to_approval(client, secret_zip)
    assert HeldSecret.objects.filter(action__isnull=False).count() == 1
    post(client, "/chat/reset")
    assert HeldSecret.objects.count() == 0


def test_restarting_secrets_does_not_keep_old_values(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    # Asking again for the same name replaces rather than duplicates.
    from assistant import held
    held.hold(conv_of(client), "MAILGUN_KEY", "other")
    assert HeldSecret.objects.filter(name="MAILGUN_KEY").count() == 1


# ---------------------------------------------------------------- 7: purge

def test_purge_of_unattached_values(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    row = HeldSecret.objects.get()
    assert row.action is None
    HeldSecret.objects.filter(id=row.id).update(expires_at=timezone.now() - timedelta(seconds=1))
    approvals.expire_stale(conv_of(client))
    assert HeldSecret.objects.count() == 0


def test_purge_keeps_live_values(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    approvals.expire_stale(conv_of(client))
    assert HeldSecret.objects.count() == 1


def test_purge_also_runs_for_chat_page(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    HeldSecret.objects.update(expires_at=timezone.now() - timedelta(seconds=1))
    client.get("/")
    assert HeldSecret.objects.count() == 0


# ---------------------------------------------------------------- 8: input validation

def test_secret_when_nothing_awaited(client, payloads):
    connect(client)
    out = secret(client, VALUE)
    assert "الان منتظر مقداری نبودم" in html_of(out)
    assert HeldSecret.objects.count() == 0
    assert VALUE not in all_text(client.user)
    assert "🔒" not in all_text(client.user)


def test_empty_value(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    for blank in ("", "   ", "\t "):
        out = secret(client, blank)
        assert "مقدار را وارد کنید." in html_of(out)
    assert HeldSecret.objects.count() == 0
    assert conv_of(client).state["awaiting_secret"]["name"] == "MAILGUN_KEY"
    # missing field / non-JSON body are handled too
    assert client.post("/chat/secret", data="{}", content_type="application/json").status_code == 200
    assert HeldSecret.objects.count() == 0


@pytest.mark.parametrize("bad", ["line1\nline2", "a\r\nb", "x" * 4097], ids=["lf", "crlf", "too-long"])
def test_invalid_values_rejected(client, secret_zip, payloads, bad):
    reach_secrets(client, secret_zip)
    out = secret(client, bad)
    assert "این مقدار معتبر نیست" in html_of(out)
    assert HeldSecret.objects.count() == 0
    assert conv_of(client).state["awaiting_secret"]["name"] == "MAILGUN_KEY"
    assert "🔒" not in all_text(client.user)


def test_value_is_stripped_and_long_value_ok(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, "  " + "k" * 4096 + "  ")
    from assistant import crypto
    assert crypto.decrypt(HeldSecret.objects.get().encrypted_value) == "k" * 4096


def test_secret_requires_login_and_post(secret_zip):
    anon = Client()
    resp = anon.post("/chat/secret", data=json.dumps({"key": VALUE}), content_type="application/json")
    assert resp.status_code in (302, 401, 403)
    assert HeldSecret.objects.count() == 0
    c = Client()
    c.force_login(User.objects.create_user("u", password="pw-12345678"))
    assert c.get("/chat/secret").status_code == 405


def test_free_text_while_waiting_is_not_stored_as_secret(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    post(client, "/chat/send", text=VALUE, value="")
    assert HeldSecret.objects.count() == 0
    # Typing the value in the normal box is a plain message; it never becomes a held value.
    assert conv_of(client).state["awaiting_secret"]["name"] == "MAILGUN_KEY"


# ---------------------------------------------------------------- 9: isolation

def test_other_user_cannot_touch_my_secrets(client, secret_zip, payloads):
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    state_before = json.dumps(conv_of(client).state, sort_keys=True)
    rows_before = list(HeldSecret.objects.values_list("id", "name", "encrypted_value", "conversation_id"))

    other = User.objects.create_user("eve", password="pw-12345678")
    c2 = Client()
    c2.force_login(other)
    c2.get("/")
    out = c2.post("/chat/secret", data=json.dumps({"key": "evil"}), content_type="application/json").json()
    assert "الان منتظر مقداری نبودم" in html_of(out)
    post(c2, "/chat/reset")
    assert json.dumps(conv_of(client).state, sort_keys=True) == state_before
    assert list(HeldSecret.objects.values_list("id", "name", "encrypted_value", "conversation_id")) == rows_before
    assert not HeldSecret.objects.filter(conversation__user=other).exists()


def test_other_user_cannot_decide_on_my_card(client, secret_zip, payloads):
    to_approval(client, secret_zip)
    action = deploy_action(client)
    other = User.objects.create_user("eve", password="pw-12345678")
    c2 = Client()
    c2.force_login(other)
    c2.get("/")
    for decision in ("confirm", "cancel"):
        post(c2, "/chat/approve", action_id=action.id, decision=decision)
    action.refresh_from_db()
    assert action.status == "proposed" and HeldSecret.objects.filter(action=action).count() == 1


def test_users_own_secrets_do_not_cross_into_other_deploy(client, secret_zip, payloads):
    """Two users deploying at once: each job only receives its own value."""
    reach_secrets(client, secret_zip)
    secret(client, MAILGUN_VALUE)
    secret(client, VALUE)
    bob = User.objects.create_user("bob", password="pw-12345678")
    c2 = Client()
    c2.force_login(bob)
    c2.user = bob
    reach_secrets(c2, secret_zip)
    secret(c2, "bob-mail")
    secret(c2, "bob-stripe")
    a1 = PendingAction.objects.get(conversation__user=client.user, tool="deploy")
    a2 = PendingAction.objects.get(conversation__user=bob, tool="deploy")
    post(c2, "/chat/approve", action_id=a2.id, decision="confirm")
    jobs.run_action(a2.id)
    envs2 = liara_envs(PendingAction.objects.get(id=a2.id).args["app_name"])
    assert envs2["STRIPE_SECRET_KEY"] == "bob-stripe"
    assert HeldSecret.objects.filter(action=a1).count() == 2  # untouched
    assert HeldSecret.objects.filter(action=a2).count() == 0


# ---------------------------------------------------------------- 10, 11: fix path

def fix_ctx(client):
    connect(client)
    site = Site.objects.get(user=client.user, app_name="my-shop")
    ctx = tools.Ctx(client.user, conv_of(client))
    return site, ctx


def test_fix_path_holds_value_and_sets_env(client, payloads):
    site, ctx = fix_ctx(client)
    res = tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    assert res["proposed"] is False and res["asked_user_for_value"] is True
    conv = conv_of(client)
    assert conv.state["awaiting_secret"] == {"name": "MAILGUN_KEY", "purpose": "fix", "app": "my-shop"}
    from assistant import chat
    comp = chat.composer_for(conv)
    assert comp["endpoint"] == "/chat/secret" and comp["mode"] == "password" and comp["label"] == "MAILGUN_KEY"
    assert not PendingAction.objects.exists()

    out = secret(client, MAILGUN_VALUE)
    html = html_of(out)
    assert "🔒 مقدار MAILGUN_KEY وارد شد" in html and "تأیید شما لازم است" in html
    assert MAILGUN_VALUE not in html
    assert out["composer"]["mode"] == "text"
    action = PendingAction.objects.get(tool="env_change")
    assert action.args == {"app_name": "my-shop", "name": "MAILGUN_KEY", "value": "__held__"}
    assert MAILGUN_VALUE not in json.dumps(action.summary, ensure_ascii=False)
    assert conv_of(client).state.get("awaiting_secret") is None
    row = HeldSecret.objects.get()
    assert row.action_id == action.id and MAILGUN_VALUE not in row.encrypted_value

    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    assert liara_envs("my-shop")["MAILGUN_KEY"] == MAILGUN_VALUE
    assert HeldSecret.objects.count() == 0
    assert_no_leak(client.user, MAILGUN_VALUE)
    assert all(MAILGUN_VALUE not in p for p in payloads)


def test_fix_path_reject_discards(client, payloads):
    site, ctx = fix_ctx(client)
    tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    secret(client, MAILGUN_VALUE)
    action = PendingAction.objects.get(tool="env_change")
    post(client, "/chat/approve", action_id=action.id, decision="cancel")
    assert HeldSecret.objects.count() == 0
    assert "MAILGUN_KEY" not in liara_envs("my-shop")


def test_fix_path_job_failure_discards(client, payloads, monkeypatch):
    site, ctx = fix_ctx(client)
    tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    secret(client, MAILGUN_VALUE)
    action = PendingAction.objects.get(tool="env_change")
    post(client, "/chat/approve", action_id=action.id, decision="confirm")

    def boom(self, *a, **k):
        raise LiaraError("bad_request", 400, "خطا")

    monkeypatch.setattr(FakeLiaraClient, "update_envs", boom)
    action = run_last_action(client.user)
    assert action.status == "failed"
    assert HeldSecret.objects.count() == 0
    assert "MAILGUN_KEY" not in liara_envs("my-shop")
    assert_no_leak(client.user, MAILGUN_VALUE)


def test_fix_path_via_diagnosis(client, payloads, monkeypatch):
    """The logs-driven path: a crash loop that names a missing non-generatable variable."""
    connect(client)
    liara_client.fake_set_scenario("my-shop", "missing_env")
    orig = FakeLiaraClient.get_logs

    def logs(self, name, since=None):
        return [{**r, "message": r["message"].replace("SECRET_KEY", "MAILGUN_KEY")} for r in orig(self, name, since)]

    monkeypatch.setattr(FakeLiaraClient, "get_logs", logs)
    post(client, "/chat/send", text="چرا my-shop خطا می‌دهد؟")
    state = conv_of(client).state
    assert (state.get("awaiting_secret") or {}).get("name") == "MAILGUN_KEY"
    assert not PendingAction.objects.filter(tool="env_change").exists()
    out = secret(client, MAILGUN_VALUE)
    assert "تأیید شما لازم است" in html_of(out)
    assert all(MAILGUN_VALUE not in p for p in payloads)
    assert_no_leak(client.user, MAILGUN_VALUE)


def test_fix_secret_later_button(client, payloads):
    site, ctx = fix_ctx(client)
    tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    out = post(client, "/chat/send", text="بعداً در کنسول تنظیم می‌کنم", value="secret_later")
    html = html_of(out)
    assert "console.liara.ir/apps/my-shop/envs" in html and "MAILGUN_KEY" in html
    assert conv_of(client).state.get("awaiting_secret") is None
    assert out["composer"]["mode"] == "text"
    assert HeldSecret.objects.count() == 0 and not PendingAction.objects.exists()
    out = secret(client, MAILGUN_VALUE)
    assert "الان منتظر مقداری نبودم" in html_of(out)
    assert HeldSecret.objects.count() == 0


def test_fix_path_for_unknown_or_foreign_app(client, payloads):
    site, ctx = fix_ctx(client)
    res = tools.call(ctx, "propose_env_change", {"app": "nobody-app", "name": "MAILGUN_KEY"})
    assert res.get("error")
    assert not (conv_of(client).state or {}).get("awaiting_secret")
    # Lower-case / junk names are refused before anything is asked.
    for bad in ("mailgun_key", "A" * 101, "", 5):
        res = tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": bad})
        assert res.get("error")
    assert not (conv_of(client).state or {}).get("awaiting_secret")


def test_fix_secret_for_site_removed_meanwhile(client, payloads):
    site, ctx = fix_ctx(client)
    tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    site.delete()
    out = secret(client, MAILGUN_VALUE)
    assert "این سایت پیدا نشد" in html_of(out)
    assert HeldSecret.objects.count() == 0
    assert not PendingAction.objects.exists()


@pytest.mark.parametrize("name,generated", [("SECRET_KEY", True), ("JWT_SECRET", True), ("ALLOWED_HOSTS", False)])
def test_generated_vars_skip_secure_prompt(client, payloads, name, generated):
    site, ctx = fix_ctx(client)
    res = tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": name})
    assert res["proposed"] is True
    conv = conv_of(client)
    assert not (conv.state or {}).get("awaiting_secret")
    action = PendingAction.objects.get(tool="env_change")
    assert action.args["value"] == ("__generate__" if generated else "my-shop.liara.run")
    assert HeldSecret.objects.count() == 0
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    action = run_last_action(client.user)
    assert action.status == "succeeded", action.job.error
    value = liara_envs("my-shop")[name]
    assert value != "__generate__" and value != "__held__"
    assert_no_leak(client.user, value) if generated else None


def test_held_placeholder_without_value_fails_cleanly(client, payloads):
    """If the held value is gone by the time the job runs, nothing is written and the job fails."""
    site, ctx = fix_ctx(client)
    tools.call(ctx, "propose_env_change", {"app": site.app_name, "name": "MAILGUN_KEY"})
    secret(client, MAILGUN_VALUE)
    action = PendingAction.objects.get(tool="env_change")
    post(client, "/chat/approve", action_id=action.id, decision="confirm")
    HeldSecret.objects.all().delete()
    action = run_last_action(client.user)
    assert action.status == "failed"
    assert "MAILGUN_KEY" not in liara_envs("my-shop")
    assert "__held__" not in json.dumps(liara_envs("my-shop"))
