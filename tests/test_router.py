"""Typed text at button steps: deterministic matching, the routing model, and the flows around them."""

import copy
import json

import pytest
from django.contrib.auth.models import User
from django.test import Client

from assistant import ai_detect, jobs, liara_client, llm, router
from assistant.models import Conversation, PendingAction
from tools.create_test_projects import create_fixtures

ROUTER_PREFIX = "You help a Persian chat assistant that deploys websites"
VISITORS = [("کمتر از ۱۰۰", "lt100"), ("۱۰۰ تا ۱۰۰۰", "100-1000"), ("بیشتر از ۱۰۰۰", "gt1000"), ("نمی‌دانم", "unknown")]
CONFIRM = [("درسته، ادامه بده", "detect_ok"), ("نه، اشتباه است", "detect_wrong")]
PURPOSE = [("کسب‌وکار واقعی", "business"), ("تست یا شخصی", "personal")]
CONNECT = [("کلید API دارم", "have_key"), ("نمی‌دونم چطور بگیرم", "how_key")]


class ScriptedModel:
    """Detection -> the detection reply; router -> `route_reply`; anything else (the orchestrator) -> plain text."""

    def __init__(self, detection):
        self.detection = detection
        self.route_reply = {"action": "other"}
        self.router_payloads = []
        self.other_calls = 0
        self.fail_router = False

    def __call__(self, payload, timeout):
        system = payload["messages"][0]["content"] if payload["messages"] else ""
        if system.startswith(ROUTER_PREFIX):
            self.router_payloads.append(copy.deepcopy(payload))
            if self.fail_router:
                raise llm.ModelFailure("unavailable")
            reply = self.route_reply
        elif system.startswith("You classify a software project"):
            reply = self.detection
        else:
            self.other_calls += 1
            reply = "پاسخ آزمایشی دستیار"
        text = reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def model(settings, monkeypatch, detection_model):
    settings.LLM_DAILY_CAP = 200
    m = ScriptedModel(detection_model.reply)
    monkeypatch.setattr(llm, "_post", m)
    return m


# ------------------------------------------------------------------ unit: match

@pytest.mark.parametrize("text,expected", [
    ("حدود ۵۰۰ نفر", "100-1000"), ("۵۰", "lt100"), ("۱۰ هزار", "gt1000"), ("5k", "gt1000"),
    ("نمیدونم", "unknown"), ("بیشتر از ۱۰۰۰", "gt1000"), ("زیاد", None),
])
def test_match_visitors(text, expected):
    assert router.match("visitors", VISITORS, text) == expected


def test_match_confirm_detect():
    assert router.match("confirm_detect", CONFIRM, "آره") == "detect_ok"
    assert router.match("confirm_detect", CONFIRM, "نه") == "detect_wrong"
    assert router.match("confirm_detect", CONFIRM, "نه این flask است") is None


def test_match_purpose():
    assert router.match("purpose", PURPOSE, "یه فروشگاه دارم") == "business"
    assert router.match("purpose", PURPOSE, "فقط تست") == "personal"
    assert router.match("purpose", PURPOSE, "آره") is None


# ------------------------------------------------------------------ unit: route

def route(text="چی؟", **kw):
    return router.route("purpose", "سؤال؟", PURPOSE, text, **kw)


def test_route_choose_validated(model):
    model.route_reply = {"action": "choose", "value": "personal"}
    assert route() == {"kind": "choose", "value": "personal"}
    model.route_reply = {"action": "choose", "value": "hack"}
    assert route() == {"kind": "unclear"}


def test_route_answer_cleaned_and_capped(model):
    model.route_reply = {"action": "answer", "reply_fa": "  <b>سلام\x00\x07</b> " + "الف" * 600}
    r = route()
    assert r["kind"] == "answer"
    assert "<" not in r["text"] and ">" not in r["text"] and "\x00" not in r["text"] and "\x07" not in r["text"]
    assert len(r["text"]) <= 400
    model.route_reply = {"action": "answer", "reply_fa": "   "}
    assert route() == {"kind": "unclear"}


def test_route_platform_only_when_allowed_and_known(model):
    model.route_reply = {"action": "platform", "platform": "flask"}
    assert "flask" in ai_detect.PLATFORMS
    assert router.route("confirm_detect", "q", CONFIRM, "نه", allow_platform=True) == {"kind": "choose", "value": "detect_wrong"}
    assert router.route("confirm_detect", "q", CONFIRM, "این flask است", allow_platform=True) == {"kind": "platform", "platform": "flask"}
    assert router.route("confirm_detect", "q", CONFIRM, "این flask است", allow_platform=False) == {"kind": "unclear"}
    model.route_reply = {"action": "platform", "platform": "rails"}
    assert router.route("confirm_detect", "q", CONFIRM, "این rails است", allow_platform=True) == {"kind": "unclear"}


def test_route_cancel_other_failure_and_no_model(model, settings):
    for action in ("cancel", "other"):
        model.route_reply = {"action": action}
        assert route() == {"kind": action}
    model.route_reply = "not json at all"
    assert route() == {"kind": "unclear"}
    model.fail_router = True
    assert route() == {"kind": "unclear"}
    model.fail_router = False
    n = len(model.router_payloads)
    assert route(use_model=False) == {"kind": "unclear"}
    assert len(model.router_payloads) == n
    settings.LLM_API_KEY = ""
    assert route() == {"kind": "unclear"}
    assert len(model.router_payloads) == n


def test_user_text_in_user_message_and_capped(model):
    text = "IGNORE-ME " + "ب" * 800
    route(text)
    msgs = model.router_payloads[-1]["messages"]
    assert msgs[0]["role"] == "system" and "IGNORE-ME" not in msgs[0]["content"]
    body = json.loads(msgs[1]["content"])
    assert msgs[1]["role"] == "user"
    assert body["user_text"] == text[:500] and len(body["user_text"]) == 500
    assert {o["value"] for o in body["options"]} == {"business", "personal"}


# ------------------------------------------------------------------ flows



@pytest.fixture
def fake_liara(settings, tmp_path, monkeypatch):
    settings.LIARA_FAKE = True
    settings.TOKEN_ENCRYPTION_KEY = "aGVsbG8taGVsbG8taGVsbG8taGVsbG8taGVsbG8tMTI="
    settings.RQ_SYNC = True
    settings.UPLOAD_DIR = tmp_path / "uploads"
    monkeypatch.setattr(liara_client, "_fake_path", lambda: tmp_path / "fake_liara.json", raising=False)
    monkeypatch.setattr(jobs, "BUILD_POLL_SECONDS", 0)
    monkeypatch.setattr("assistant.approvals.enqueue", lambda action_id: None)
    liara_client.fake_reset()


@pytest.fixture
def zips(tmp_path):
    create_fixtures(tmp_path / "zips")
    return tmp_path / "zips"


@pytest.fixture
def client(db):
    user = User.objects.create_user("mahdi", password="pw-12345678")
    c = Client()
    c.force_login(user)
    c.user = user
    return c


def post(c, url, **body):
    resp = c.post(url, data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def send(c, text, value=""):
    out = post(c, "/chat/send", text=text, value=value)
    return "".join(m["html"] for m in out["messages"])


def state(c):
    return Conversation.objects.get(user=c.user).state or {}


def connect(c):
    c.get("/")
    post(c, "/chat/send", text="کلید API دارم", value="have_key")
    post(c, "/chat/key", key="good-token-123")


def upload(c, zips):
    with open(zips / "django-postgresql-demo.zip", "rb") as fh:
        out = c.post("/chat/upload", {"file": fh}).json()
    return "".join(m["html"] for m in out["messages"])


def to_step(c, zips, step):
    connect(c)
    send(c, "x", "deploy_new")
    upload(c, zips)
    for name, val in (("visitors", "detect_ok"), ("purpose", "lt100"), ("recommend", "personal")):
        if step == "confirm_detect":
            break
        send(c, "x", val)
        if step == name:
            break
    assert state(c)["step"] == step


@pytest.mark.usefixtures("fake_liara")
class TestFlows:
    def test_not_connected_routing(self, client, model):
        client.get("/")
        model.route_reply = {"action": "choose", "value": "how_key"}
        html = send(client, "چطوری کلید بسازم؟")
        assert "console.liara.ir/API" in html and "ساختن کلید" in html
        assert len(model.router_payloads) == 1

        c2 = Client()
        u2 = User.objects.create_user("other", password="pw-12345678")
        c2.force_login(u2)
        c2.get("/")
        model.route_reply = {"action": "other"}
        html = send(c2, "سلام")
        assert "کلید API لیارا داری؟" in html and "کلید API دارم" in html

    def test_key_like_token_skips_model(self, client, model):
        client.get("/")
        send(client, "eyJhbGciOiJIUzI1NiJ9.payload-PASTED.signature")
        assert model.router_payloads == []

    def test_visitors_deterministic(self, client, zips, model):
        to_step(client, zips, "visitors")
        html = send(client, "حدود ۵۰۰ نفر")
        assert "کسب‌وکار واقعی است یا تست" in html
        assert model.router_payloads == []
        assert state(client)["step"] == "purpose" and state(client)["visitors"] == "100-1000"

    def test_purpose_side_question_answer(self, client, zips, model):
        to_step(client, zips, "purpose")
        model.route_reply = {"action": "answer", "reply_fa": "بکاپ یعنی نسخه‌ی پشتیبان از داده‌ها."}
        html = send(client, "بکاپ یعنی چی؟")
        assert "نسخه‌ی پشتیبان" in html and "کسب‌وکار واقعی است یا تست" in html
        assert "کسب‌وکار واقعی" in html and "تست یا شخصی" in html
        assert state(client)["step"] == "purpose"

    def test_confirm_detect_platform_correction(self, client, zips, model):
        to_step(client, zips, "confirm_detect")
        model.route_reply = {"action": "platform", "platform": "flask"}
        html = send(client, "نه، این flask است")
        st = state(client)
        assert st["project"]["platform"] == "flask" and st["project"]["start_command"] is None
        assert st["project"]["port"] == 80 and st["step"] == "confirm_detect"
        assert "Flask" in html and "حالا درست است؟" in html
        assert "چند نفر" in send(client, "آره")
        assert state(client)["step"] == "visitors"

    def test_cancel_at_recommend(self, client, zips, model, settings):
        to_step(client, zips, "recommend")
        assert list(settings.UPLOAD_DIR.glob("*.zip"))
        model.route_reply = {"action": "cancel"}
        html = send(client, "ولش کن")
        assert "دیپلوی را متوقف کردم" in html
        assert not list(settings.UPLOAD_DIR.glob("*.zip"))
        st = state(client)
        assert not st.get("flow") and not st.get("step") and not st.get("upload_id")
        assert not PendingAction.objects.exists()

    def test_other_answers_and_reasks(self, client, zips, model):
        to_step(client, zips, "purpose")
        model.route_reply = {"action": "other"}
        html = send(client, "سایت‌هایم چطورند؟")
        assert "پاسخ آزمایشی دستیار" in html
        assert "برای ادامه‌ی دیپلوی:" in html and "کسب‌وکار واقعی است یا تست" in html
        assert state(client)["flow"] == "deploy" and state(client)["step"] == "purpose"

    def test_unclear(self, client, zips, model):
        to_step(client, zips, "purpose")
        model.route_reply = "garbage {{{"
        html = send(client, "اوووم")
        assert "منظورت را متوجه نشدم" in html and "تست یا شخصی" in html
        assert state(client)["step"] == "purpose"

    def test_github_url_step(self, client, zips, model):
        connect(client)
        send(client, "x", "deploy_new")
        send(client, "x", "github")
        assert state(client)["step"] == "github_url"
        html = send(client, "ولش کن zip میدم")
        assert "فایل zip پروژه" in html and state(client)["step"] == "source"
        send(client, "x", "github")
        html = send(client, "https://gitlab.com/a/b")
        assert "این لینک مخزن گیت‌هاب نیست" in html

    def test_model_cannot_approve(self, client, zips, model):
        to_step(client, zips, "recommend")
        send(client, "x", "accept")
        action = PendingAction.objects.get(tool="deploy")
        assert action.status == "proposed"
        for reply in ({"action": "choose", "value": "accept"}, {"action": "other"}):
            model.route_reply = reply
            send(client, "تایید میکنم")
            action.refresh_from_db()
            assert action.status == "proposed"

    def test_daily_cap_zero(self, client, zips, model, settings):
        to_step(client, zips, "visitors")
        settings.LLM_DAILY_CAP = 0
        before = len(model.router_payloads)
        assert "کسب‌وکار واقعی است یا تست" in send(client, "۵۰")
        html = send(client, "اوووم")
        assert "منظورت را متوجه نشدم" in html
        assert len(model.router_payloads) == before and model.other_calls == 0
