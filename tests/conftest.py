"""Shared fixtures: a scripted detection model patched in at `assistant.llm._post`."""

import copy
import json

import pytest

from assistant import llm
from assistant.models import Message  # noqa: F401  (ensures Django apps are loaded)

DETECTION_PREFIX = "You classify a software project"

DJANGO_DEMO_REPLY = {
    "platform": "django", "confidence": "high", "root": "", "start_command": "gunicorn demo.wsgi", "port": 8000,
    "services": [{"kind": "postgresql", "evidence": "psycopg", "reason_fa": "برنامه به PostgreSQL وصل می‌شود."}],
    "env_vars": ["DB_HOST", "DB_NAME", "DB_USER", "DB_PASSWORD", "SECRET_KEY"],
    "blocker_fa": None, "summary_fa": "یک برنامه‌ی Django با PostgreSQL.",
}


class DetectionModel:
    """Answers detection requests with `reply` (dict or raw string); everything else raises
    ModelFailure("unavailable") so the orchestrator falls back to its deterministic rules."""

    def __init__(self, reply):
        self.reply = reply
        self.detection_payloads = []

    def __call__(self, payload, timeout):
        system = payload["messages"][0]["content"] if payload["messages"] else ""
        if not system.startswith(DETECTION_PREFIX):
            raise llm.ModelFailure("unavailable")
        self.detection_payloads.append(copy.deepcopy(payload))
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply, ensure_ascii=False)
        return {"choices": [{"message": {"role": "assistant", "content": text}}]}


@pytest.fixture
def detection_model(settings, monkeypatch):
    settings.LLM_BASE_URL = "https://llm.example.test/v1"
    settings.LLM_API_KEY = "sk-test-key"
    settings.LLM_MODEL = "test-model"
    settings.LLM_DAILY_CAP = 200
    model = DetectionModel(copy.deepcopy(DJANGO_DEMO_REPLY))
    monkeypatch.setattr(llm, "_post", model)
    return model
