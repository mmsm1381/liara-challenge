"""GitHub import: URL parsing, the guarded download, top-folder stripping and the chat flow. No real network."""

import io
import json
import zipfile
from pathlib import Path

import pytest
import requests
from django.contrib.auth.models import User
from django.test import Client

from assistant import github, liara_client, jobs
from assistant.models import Conversation

ROOT = Path(__file__).resolve().parent.parent
DEMO = ROOT / "examples" / "django-postgresql-demo"
TOP = "octo-demo-abc123/"


# ------------------------------------------------------------------ helpers

def make_zip(files, top=TOP):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(top + name, data)
    return buf.getvalue()


def demo_files():
    return {str(p.relative_to(DEMO)): p.read_bytes() for p in sorted(DEMO.rglob("*"))
            if p.is_file() and "__pycache__" not in p.parts}


class FakeResp:
    def __init__(self, status=200, headers=None, body=b"", chunk=1024):
        self.status_code = status
        self.headers = headers or {}
        self.body = body
        self.chunk = chunk
        self.chunks_served = 0

    def iter_content(self, size):
        for i in range(0, len(self.body), self.chunk):
            self.chunks_served += 1
            yield self.body[i:i + self.chunk]

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeGet:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, **kwargs):
        self.calls.append((url, kwargs))
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def redirect(location="https://codeload.github.com/o/r/legacy.zip/refs/heads/main", status=302):
    return FakeResp(status, {"Location": location})


@pytest.fixture
def fake_get(monkeypatch):
    def install(*responses):
        fg = FakeGet(*responses)
        monkeypatch.setattr("assistant.github.requests.get", fg)
        return fg
    return install


# ------------------------------------------------------------------ parse_url

@pytest.mark.parametrize("url,expected", [
    ("https://github.com/o/r", ("o", "r", None)),
    ("https://github.com/o/r.git", ("o", "r", None)),
    ("https://github.com/o/r/", ("o", "r", None)),
    ("https://www.github.com/o/r", ("o", "r", None)),
    ("  https://github.com/o/r  ", ("o", "r", None)),
    ("https://github.com/o/r/tree/main", ("o", "r", "main")),
    ("https://github.com/o/r/tree/feature/x", ("o", "r", "feature/x")),
    ("https://github.com/o/my.repo-1_x", ("o", "my.repo-1_x", None)),
])
def test_parse_url_accepts(url, expected):
    assert github.parse_url(url) == expected


@pytest.mark.parametrize("url", [
    "http://evil.com/o/r",
    "https://github.com.evil.com/o/r",
    "https://evil.com/github.com/o/r",
    "https://github.com/o",
    "https://github.com/",
    "https://github.com/../etc",
    "https://github.com/o/..",
    "https://github.com/o/r/tree/../x",
    "https://github.com/o/r/tree/a/../x",
    "git@github.com:o/r.git",
    "ssh://git@github.com/o/r.git",
    "https://github.com/o/r?tab=readme",
    "https://github.com/o/r/tree/main?x=1",
    "https://github.com/o/r#readme",
    "https://github.com/o/r/blob/main/file",
    "https://github.com/o/r/issues",
    "",
    None,
    "سلام",
])
def test_parse_url_rejects(url):
    assert github.parse_url(url) is None


# ------------------------------------------------------------------ download

def test_download_follows_codeload_redirect(fake_get, tmp_path):
    fg = fake_get(redirect(), FakeResp(200, body=b"ZIPDATA" * 100))
    dest = tmp_path / "out.zip"
    github.download("o", "r", "main", dest, 10_000)
    assert dest.read_bytes() == b"ZIPDATA" * 100
    assert fg.calls[0][0] == "https://api.github.com/repos/o/r/zipball/main"
    assert fg.calls[0][1]["allow_redirects"] is False
    assert fg.calls[1][0].startswith("https://codeload.github.com/")
    assert fg.calls[1][1]["stream"] is True and fg.calls[1][1]["allow_redirects"] is False


def test_download_without_ref_uses_default_branch_url(fake_get, tmp_path):
    fg = fake_get(redirect(), FakeResp(200, body=b"x"))
    github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert fg.calls[0][0] == "https://api.github.com/repos/o/r/zipball"


def test_download_redirect_to_other_host_refused(fake_get, tmp_path):
    fg = fake_get(redirect("https://evil.example/x"), FakeResp(200, body=b"x"))
    with pytest.raises(github.GitHubError):
        github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert len(fg.calls) == 1


@pytest.mark.parametrize("location", [
    "http://codeload.github.com/o/r/zip",
    "https://codeload.github.com.evil.example/x",
    "https://evil.example/codeload.github.com",
    "https://169.254.169.254/latest",
    "",
])
def test_download_bad_locations_refused(fake_get, tmp_path, location):
    fg = fake_get(redirect(location), FakeResp(200, body=b"x"))
    with pytest.raises(github.GitHubError):
        github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert len(fg.calls) == 1


def test_download_non_redirect_success_refused(fake_get, tmp_path):
    fg = fake_get(FakeResp(200, body=b"x"), FakeResp(200, body=b"x"))
    with pytest.raises(github.GitHubError):
        github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert len(fg.calls) == 1


def test_download_404_says_not_found_or_private(fake_get, tmp_path):
    fake_get(FakeResp(404))
    with pytest.raises(github.GitHubError) as e:
        github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert "پیدا نشد" in e.value.message_fa and "خصوصی" in e.value.message_fa


@pytest.mark.parametrize("status", [403, 429])
def test_download_rate_limit(fake_get, tmp_path, status):
    fake_get(FakeResp(status))
    with pytest.raises(github.GitHubError) as e:
        github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert "سقف درخواست" in e.value.message_fa


def test_download_size_cap_stops_mid_stream(fake_get, tmp_path):
    big = FakeResp(200, body=b"a" * 10_000, chunk=1000)
    fg = fake_get(redirect(), big)
    with pytest.raises(github.GitHubError) as e:
        github.download("o", "r", None, tmp_path / "o.zip", 2500)
    assert fg.calls[1][1]["stream"] is True
    assert big.chunks_served < 10  # stopped early, did not read it all
    assert "بزرگ‌تر" in e.value.message_fa


def test_download_codeload_error_status(fake_get, tmp_path):
    fake_get(redirect(), FakeResp(500))
    with pytest.raises(github.GitHubError):
        github.download("o", "r", None, tmp_path / "o.zip", 100)


@pytest.mark.parametrize("which", [0, 1])
def test_download_request_exceptions(fake_get, tmp_path, which):
    seq = [requests.ConnectionError("boom")] if which == 0 else [redirect(), requests.Timeout("slow")]
    fake_get(*seq)
    with pytest.raises(github.GitHubError):
        github.download("o", "r", None, tmp_path / "o.zip", 100)


def test_download_token_only_on_api_call(fake_get, tmp_path, settings):
    settings.GITHUB_TOKEN = "ghp_secret"
    fg = fake_get(redirect(), FakeResp(200, body=b"x"))
    github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert fg.calls[0][1]["headers"]["Authorization"] == "Bearer ghp_secret"
    assert "Authorization" not in fg.calls[1][1]["headers"]


def test_download_no_token_no_authorization(fake_get, tmp_path, settings):
    settings.GITHUB_TOKEN = ""
    fg = fake_get(redirect(), FakeResp(200, body=b"x"))
    github.download("o", "r", None, tmp_path / "o.zip", 100)
    assert all("Authorization" not in kw["headers"] for _, kw in fg.calls)


# ------------------------------------------------------------------ strip_top_folder

def test_strip_top_folder_single(tmp_path):
    src, dest = tmp_path / "s.zip", tmp_path / "d.zip"
    src.write_bytes(make_zip({"manage.py": "x", "demo/settings.py": "y"}))
    assert github.strip_top_folder(src, dest) is True
    with zipfile.ZipFile(dest) as z:
        assert sorted(z.namelist()) == ["demo/settings.py", "manage.py"]


def test_strip_top_folder_multiple_tops(tmp_path):
    src, dest = tmp_path / "s.zip", tmp_path / "d.zip"
    with zipfile.ZipFile(src, "w") as z:
        z.writestr("a/x.txt", "1")
        z.writestr("b/y.txt", "2")
    assert github.strip_top_folder(src, dest) is False
    assert not dest.exists()


# ------------------------------------------------------------------ flows


@pytest.fixture
def env(settings, tmp_path, monkeypatch, detection_model):
    settings.LIARA_FAKE = True
    settings.TOKEN_ENCRYPTION_KEY = "aGVsbG8taGVsbG8taGVsbG8taGVsbG8taGVsbG8tMTI="
    settings.RQ_SYNC = True
    settings.GITHUB_TOKEN = ""
    settings.UPLOAD_DIR = tmp_path / "uploads"
    monkeypatch.setattr(liara_client, "_fake_path", lambda: tmp_path / "fake_liara.json", raising=False)
    monkeypatch.setattr(jobs, "BUILD_POLL_SECONDS", 0)
    monkeypatch.setattr("assistant.approvals.enqueue", lambda action_id: None)
    liara_client.fake_reset()
    return settings.UPLOAD_DIR


@pytest.fixture
def client(env):
    user = User.objects.create_user("mahdi", password="pw-12345678")
    c = Client()
    c.force_login(user)
    c.user = user
    c.get("/")
    c.post("/chat/send", data=json.dumps({"text": "کلید API دارم", "value": "have_key"}), content_type="application/json")
    c.post("/chat/key", data=json.dumps({"key": "good-token-123"}), content_type="application/json")
    return c


def post(c, **body):
    resp = c.post("/chat/send", data=json.dumps(body), content_type="application/json")
    assert resp.status_code == 200, resp.content
    return resp.json()


def html(out):
    return "".join(m["html"] for m in out["messages"])


def leftovers(upload_dir):
    return sorted(p.name for p in upload_dir.glob("*")) if upload_dir.exists() else []


def serve(fake_get, data):
    return fake_get(redirect(), FakeResp(200, body=data, chunk=4096))


@pytest.mark.django_db
def test_flow_button_then_link(client, env, fake_get, detection_model):
    fg = serve(fake_get, make_zip(demo_files()))
    out = post(client, text="لینک گیت‌هاب می‌دهم", value="deploy_new")
    out = post(client, text="لینک گیت‌هاب می‌دهم", value="github")
    assert "لینک مخزن" in html(out)
    out = post(client, text="https://github.com/octo/demo")
    assert "این تشخیص درست است؟" in html(out)
    assert fg.calls[0][0] == "https://api.github.com/repos/octo/demo/zipball"
    assert len(detection_model.detection_payloads) == 1
    prompt = json.dumps(detection_model.detection_payloads[0], ensure_ascii=False)
    tree = prompt.split("FILE TREE:")[1].split("\\n\\n")[0].split("\\n")
    tree = [t.strip() for t in tree if t.strip()]
    assert "manage.py" in tree
    assert any(t.startswith("demo/settings.py") for t in tree)
    assert not any(t.startswith("octo-demo-abc123") for t in tree)
    state = Conversation.objects.get(user=client.user).state
    assert state["step"] == "confirm_detect" and state["source_name"] == "demo"
    assert (env / f"{state['upload_id']}.zip").exists()
    assert leftovers(env) == [f"{state['upload_id']}.zip"]  # raw archive removed
    out = post(client, text="درسته", value="detect_ok")
    assert "چند نفر" in out["messages"][-1]["html"]


@pytest.mark.django_db
def test_flow_pasted_link_without_button(client, env, fake_get):
    serve(fake_get, make_zip(demo_files()))
    out = post(client, text="https://github.com/octo/demo/tree/main")
    assert "این تشخیص درست است؟" in html(out)
    assert Conversation.objects.get(user=client.user).state["source_name"] == "demo"


@pytest.mark.django_db
def test_flow_unsafe_archive(client, env, fake_get, detection_model):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("top/ok.txt", "1")
        z.writestr("top/../evil.txt", "x")
    serve(fake_get, buf.getvalue())
    out = post(client, text="https://github.com/octo/demo")
    assert "نمی‌توانم بپذیرم" in html(out)
    assert detection_model.detection_payloads == []
    assert leftovers(env) == []


@pytest.mark.django_db
def test_flow_bad_link_in_github_step(client, env, fake_get):
    fg = fake_get()
    post(client, text="x", value="deploy_new")
    post(client, text="x", value="github")
    out = post(client, text="http://evil.com/o/r")
    assert "این لینک مخزن گیت‌هاب نیست" in html(out)
    assert fg.calls == []
    assert Conversation.objects.get(user=client.user).state["step"] == "github_url"


@pytest.mark.django_db
def test_flow_404(client, env, fake_get):
    fake_get(FakeResp(404))
    out = post(client, text="https://github.com/octo/private")
    assert "پیدا نشد" in html(out)
    assert leftovers(env) == []


@pytest.mark.django_db
def test_flow_oversized_cleans_up(client, env, fake_get, settings):
    settings.MAX_ZIP_BYTES = 1000
    fake_get(redirect(), FakeResp(200, body=b"a" * 5000, chunk=500))
    out = post(client, text="https://github.com/octo/big")
    assert "بزرگ‌تر" in html(out)
    assert leftovers(env) == []
