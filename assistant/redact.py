"""Strip secrets from text before it reaches the LLM, the audit log or the browser."""

import re

_PATTERNS = [
    # Bearer tokens and JWTs
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+"), "Bearer [REDACTED]"),
    (re.compile(r"eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}"), "[REDACTED_JWT]"),
    # Credentials inside connection URLs: scheme://user:pass@host
    (re.compile(r"([a-zA-Z][a-zA-Z0-9+.\-]*://)([^:/@\s]+):([^@\s]+)@"), r"\1[REDACTED]:[REDACTED]@"),
    # KEY=value / "key": "value" for secret-looking names
    (
        re.compile(
            r"(?i)\b([A-Z0-9_]*(?:PASSWORD|PASSWD|SECRET|TOKEN|API_KEY|APIKEY|ACCESS_KEY|PRIVATE_KEY)[A-Z0-9_]*)"
            r"(\s*[=:]\s*|\"\s*:\s*\")([^\s\"',]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    # Long opaque hex/base64 strings that look like keys
    # (no "/" in the class: long runs with slashes are file paths in tracebacks, not keys)
    (re.compile(r"\b[A-Za-z0-9+_\-]{40,}={0,2}\b"), "[REDACTED_KEY]"),
]


def redact(text) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        text = str(text)
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


def redact_obj(obj):
    """Redact every string inside a JSON-like structure; drop values of secret-looking keys."""
    secret_key = re.compile(r"(?i)password|passwd|secret|token|api_?key|access_?key|private_?key")
    if isinstance(obj, dict):
        return {
            k: ("[REDACTED]" if isinstance(k, str) and secret_key.search(k) else redact_obj(v))
            for k, v in obj.items()
        }
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj
