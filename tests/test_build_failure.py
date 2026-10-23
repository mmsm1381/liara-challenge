"""Build failures: full, clean log tail plus one Persian sentence."""

from unittest import mock

from assistant import jobs, llm
from assistant.redact import redact

TRACE = ("\x1b[91mTraceback (most recent call last):\n"
         '  File "/usr/local/lib/python3.11/site-packages/poetry/core/factory.py", line 59, in poetry_config\n'
         "RuntimeError: [tool.poetry] section not found in /app/pyproject.toml\x1b[0m")


def test_clean_log_strips_colour_codes_and_keeps_every_line():
    lines = jobs.clean_log([TRACE])
    assert lines[0] == "Traceback (most recent call last):"
    assert "/usr/local/lib/python3.11/site-packages/poetry/core/factory.py" in lines[1]
    assert lines[-1].startswith("RuntimeError: [tool.poetry]")
    assert not any("\x1b" in line or "[91m" in line or "[0m" in line for line in lines)


def test_redact_keeps_long_paths_but_not_keys():
    assert "/usr/local/lib/python3.11/site-packages/poetry/core/factory.py" in redact(
        'File "/usr/local/lib/python3.11/site-packages/poetry/core/factory.py"')
    assert "FAKETOKENabcdefghijklmnopqrstuvwxyz0123456789" not in redact(
        "key FAKETOKENabcdefghijklmnopqrstuvwxyz0123456789")


def test_build_failed_without_model_uses_rules_and_attaches_log(settings):
    settings.LLM_BASE_URL = ""
    exc = jobs.build_failed("", [TRACE], "python")
    assert "Poetry" in exc.text
    assert exc.blocks[0]["type"] == "code"
    assert "poetry_config" in exc.blocks[0]["text"]


def test_build_failed_uses_model_sentence(settings):
    settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL = "http://x/v1", "k", "m"
    reply = llm.ModelReply(text='{"reason_fa": "pyproject.toml بخش Poetry ندارد.", "fix_fa": "یک requirements.txt اضافه کن."}')
    with mock.patch.object(llm, "chat", return_value=reply) as chat:
        exc = jobs.build_failed("", [TRACE], "python")
    assert exc.text == "بیلد ناموفق بود: pyproject.toml بخش Poetry ندارد."
    assert exc.next_step == "یک requirements.txt اضافه کن."
    # The log reaches the model only inside the user message, after cleaning.
    messages = chat.call_args.args[0]
    assert "untrusted" in messages[0]["content"] and "\x1b" not in messages[1]["content"]


def test_build_failed_falls_back_when_model_fails(settings):
    settings.LLM_BASE_URL, settings.LLM_API_KEY, settings.LLM_MODEL = "http://x/v1", "k", "m"
    with mock.patch.object(llm, "chat", side_effect=llm.ModelFailure("unavailable")):
        exc = jobs.build_failed("", ["npm ERR! Missing script: start"], "node")
    assert "start" in exc.text and exc.blocks
