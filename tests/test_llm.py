"""assistant.llm: reply parsing and transport error mapping (network mocked)."""

import json

import pytest
import requests

from assistant import llm

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def llm_settings(settings):
    settings.LLM_BASE_URL = "https://llm.example.test/v1"
    settings.LLM_API_KEY = "sk-test-key"
    settings.LLM_MODEL = "test-model"
    settings.LLM_TOOL_MODE = "native"


def wrap(message):
    return {"choices": [{"message": message}]}


def test_is_configured(settings):
    assert llm.is_configured()
    settings.LLM_API_KEY = ""
    assert not llm.is_configured()


def test_parse_text_reply():
    r = llm.parse_reply(wrap({"content": "سلام"}))
    assert r.text == "سلام" and r.tool_calls == []


def test_parse_tool_calls_with_json_string_arguments():
    r = llm.parse_reply(wrap({"content": None, "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_logs", "arguments": '{"app": "blog", "since_minutes": 5}'}},
        {"id": "c2", "type": "function", "function": {"name": "list_sites", "arguments": ""}},
    ]}))
    assert [(c.id, c.name, c.args) for c in r.tool_calls] == [
        ("c1", "get_logs", {"app": "blog", "since_minutes": 5}), ("c2", "list_sites", {})]


def test_parse_invalid_arguments_json():
    bad = wrap({"tool_calls": [{"id": "c", "function": {"name": "get_logs", "arguments": "{not json"}}]})
    with pytest.raises(llm.ModelFailure) as e:
        llm.parse_reply(bad)
    assert e.value.code == "invalid_response"
    notdict = wrap({"tool_calls": [{"id": "c", "function": {"name": "get_logs", "arguments": "[1]"}}]})
    with pytest.raises(llm.ModelFailure) as e:
        llm.parse_reply(notdict)
    assert e.value.code == "invalid_response"


def test_parse_refusal():
    with pytest.raises(llm.ModelFailure) as e:
        llm.parse_reply(wrap({"content": None, "refusal": "no"}))
    assert e.value.code == "refused"


@pytest.mark.parametrize("data", [wrap({"content": ""}), wrap({"content": "  "}), wrap({}), {"choices": []}, {}, None])
def test_parse_empty_or_malformed(data):
    with pytest.raises(llm.ModelFailure) as e:
        llm.parse_reply(data)
    assert e.value.code == "invalid_response"


NAMES = {"list_sites", "get_logs"}


def test_prompt_json_tool():
    r = llm.parse_prompt_json('{"tool": "get_logs", "args": {"app": "blog"}}', NAMES)
    assert r.tool_calls[0].name == "get_logs" and r.tool_calls[0].args == {"app": "blog"}
    r = llm.parse_prompt_json('Sure:\n```json\n{"tool": "list_sites"}\n```', NAMES)
    assert r.tool_calls[0].name == "list_sites" and r.tool_calls[0].args == {}


def test_prompt_json_unknown_tool_and_bad_args():
    with pytest.raises(llm.ModelFailure):
        llm.parse_prompt_json('{"tool": "delete_everything", "args": {}}', NAMES)
    with pytest.raises(llm.ModelFailure):
        llm.parse_prompt_json('{"tool": "list_sites", "args": "x"}', NAMES)


def test_prompt_json_answer_and_prose():
    assert llm.parse_prompt_json('{"answer": "همه خوب است"}', NAMES).text == "همه خوب است"
    r = llm.parse_prompt_json("فقط یک متن ساده", NAMES)
    assert r.text == "فقط یک متن ساده" and not r.tool_calls


class FakeResp:
    def __init__(self, status=200, body=None, content=None):
        self.status_code = status
        self._body = body if body is not None else {"ok": True}
        self.content = content if content is not None else json.dumps(self._body).encode()

    def json(self):
        return self._body


@pytest.mark.parametrize("status,code", [(401, "authentication"), (403, "authentication"), (429, "rate_limited"),
                                         (400, "configuration"), (500, "unavailable"), (503, "unavailable")])
def test_post_status_mapping(monkeypatch, status, code):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(status))
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({"x": 1}, 5)
    assert e.value.code == code


def test_post_timeout_and_connection_errors(monkeypatch):
    def timeout(*a, **k):
        raise requests.Timeout()

    def conn(*a, **k):
        raise requests.ConnectionError()

    monkeypatch.setattr(requests, "post", timeout)
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({}, 5)
    assert e.value.code == "timeout"
    monkeypatch.setattr(requests, "post", conn)
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({}, 5)
    assert e.value.code == "unavailable"


def test_post_success_and_invalid_body(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {"a": 1}))
    assert llm._post({}, 5) == {"a": 1}

    class BadJson(FakeResp):
        def json(self):
            raise ValueError("x")

    monkeypatch.setattr(requests, "post", lambda *a, **k: BadJson())
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({}, 5)
    assert e.value.code == "invalid_response"
    monkeypatch.setattr(requests, "post", lambda *a, **k: FakeResp(200, {}, content=b"x" * (256 * 1024 + 1)))
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({}, 5)
    assert e.value.code == "invalid_response"


@pytest.mark.parametrize("key", ["sk-test-key", "Bearer sk-test-key", "bearer sk-test-key"])
def test_authorization_header_has_single_bearer_prefix(monkeypatch, settings, key):
    settings.LLM_API_KEY = key
    seen = {}

    def fake_post(url, json=None, headers=None, timeout=None, allow_redirects=None):
        seen.update(url=url, headers=headers, redirects=allow_redirects)
        return FakeResp(200, {"ok": 1})

    monkeypatch.setattr(requests, "post", fake_post)
    llm._post({}, 5)
    assert seen["headers"]["Authorization"].lower() == "bearer sk-test-key"
    assert seen["headers"]["Authorization"].lower().count("bearer") == 1
    assert seen["url"] == "https://llm.example.test/v1/chat/completions"
    assert seen["redirects"] is False


def test_key_with_newline_is_rejected(monkeypatch, settings):
    settings.LLM_API_KEY = "sk\nX-Evil: 1"
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("must not send"))
    with pytest.raises(llm.ModelFailure) as e:
        llm._post({}, 5)
    assert e.value.code == "configuration"


def test_chat_requires_configuration(settings):
    settings.LLM_BASE_URL = ""
    with pytest.raises(llm.ModelFailure) as e:
        llm.chat([{"role": "user", "content": "x"}])
    assert e.value.code == "configuration"


def test_chat_native_sends_tools_and_prompt_json_does_not(monkeypatch, settings):
    sent = []
    monkeypatch.setattr(llm, "_post", lambda payload, timeout: sent.append(payload) or wrap({"content": '{"answer": "ok"}'}))
    tools = [{"type": "function", "function": {"name": "list_sites", "description": "", "parameters": {}}}]
    llm.chat([{"role": "user", "content": "x"}], tools)
    assert sent[-1]["tools"] == tools and sent[-1]["tool_choice"] == "auto"
    settings.LLM_TOOL_MODE = "prompt_json"
    assert llm.chat([{"role": "user", "content": "x"}], tools).text == "ok"
    assert "tools" not in sent[-1]
