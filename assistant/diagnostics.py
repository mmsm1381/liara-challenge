"""Deterministic signal extraction from fresh Liara evidence.

Same idea as the previous agent's rule-based triage, extended with the rules
from Phase 3 of the plan. Each signal names a likely cause, the evidence lines
behind it, and the fix tool to propose. The LLM may rank or explain these, but
it never invents a fix outside this list and the tool registry.
"""

import re
from dataclasses import asdict, dataclass, field

MISSING_MODULE = re.compile(
    r"(?:ModuleNotFoundError: No module named ['\"]?([\w.\-]+)|Cannot find module ['\"]([^'\"]+)['\"])"
)
MISSING_ENV = re.compile(
    r"(?:ImproperlyConfigured:.*?\b([A-Z][A-Z0-9_]{2,})\b|KeyError: ['\"]([A-Z][A-Z0-9_]{2,})['\"]"
    r"|\b([A-Z][A-Z0-9_]{2,}) is not defined|environment variable ([A-Z][A-Z0-9_]{2,}))"
)
DB_ERROR = re.compile(
    r"(?i)(could not connect to (?:the )?(?:database|server)|connection refused|ECONNREFUSED|"
    r"OperationalError|connection timed out|password authentication failed)"
)
OOM = re.compile(r"(?i)(OOMKilled|out of memory|JavaScript heap out of memory|MemoryError|Killed\s*$)")
PORT = re.compile(r"(?i)(?:listening|running|started).{0,40}?(?:port|:)\s*(\d{2,5})")
CRASH = re.compile(r"(?i)(Traceback \(most recent call last\)|exited with code [1-9]|CrashLoopBackOff|Error: )")


@dataclass
class Signal:
    code: str
    cause_fa: str
    fix_fa: str
    fix_tool: str = ""  # one of the propose_* tools, or "" when no automatic fix exists
    fix_args: dict = field(default_factory=dict)
    evidence: list = field(default_factory=list)
    confidence: str = "medium"  # high | medium | low

    def as_dict(self):
        return asdict(self)


def _hits(lines, pattern, limit=3):
    out = []
    for line in lines:
        if pattern.search(line):
            out.append(line[:240])
            if len(out) >= limit:
                break
    return out


def analyze(evidence: dict) -> list[dict]:
    """evidence keys (all optional): logs [str], ram_percent, cpu_percent, plan_id, oom_restarts,
    releases [{id, state, created_at}], current_release, release_count_since_deploy,
    last_release_after_crash (bool), listen_port, release_port, http {ok, status_code, ms},
    error_code (Liara error code from a failed read)."""
    logs = [str(line) for line in evidence.get("logs") or []]
    signals: list[Signal] = []

    if evidence.get("error_code") == "frozen":
        signals.append(Signal(
            "frozen", "موجودی حساب لیارا کافی نیست و برنامه متوقف شده.",
            "حساب را در کنسول لیارا شارژ کن؛ بعد از شارژ برنامه دوباره روشن می‌شود.",
            evidence=["لیارا خطای frozen_project برگرداند"], confidence="high"))

    module_lines = _hits(logs, MISSING_MODULE)
    if module_lines:
        match = MISSING_MODULE.search(module_lines[0])
        package = (match.group(1) or match.group(2) or "").split(".")[0]
        rollback = _previous_release(evidence)
        signals.append(Signal(
            "missing_dependency",
            f"بسته‌ی «{package}» در فایل وابستگی‌ها (requirements.txt یا package.json) نیست.",
            f"به نسخه‌ی قبلی برمی‌گردم تا سایت بالا بیاید؛ بعد «{package}» را به فایل وابستگی‌ها اضافه کن و نسخه‌ی جدید بفرست.",
            "propose_rollback" if rollback else "",
            {"release_id": rollback} if rollback else {},
            module_lines, "high"))

    env_lines = _hits(logs, MISSING_ENV)
    if env_lines:
        m = MISSING_ENV.search(env_lines[0])
        name = next(g for g in m.groups() if g)
        signals.append(Signal(
            "missing_env", f"متغیر محیطی «{name}» تنظیم نشده.",
            f"مقدار «{name}» را روی برنامه تنظیم می‌کنم (برنامه یک بار ری‌استارت می‌شود).",
            "propose_env_change", {"name": name}, env_lines, "high"))

    db_lines = _hits(logs, DB_ERROR)
    if db_lines:
        signals.append(Signal(
            "database", "برنامه به دیتابیس وصل نمی‌شود؛ دیتابیس خاموش است یا اطلاعات اتصال اشتباه است.",
            "وضعیت دیتابیس را بررسی می‌کنم و در صورت نیاز برنامه را ری‌استارت می‌کنم.",
            "propose_restart", {}, db_lines, "medium"))

    ram = evidence.get("ram_percent")
    oom_lines = _hits(logs, OOM)
    if oom_lines or (ram is not None and ram >= 90) or (evidence.get("oom_restarts") or 0) > 0:
        if not evidence.get("release_count_since_deploy"):
            ev = list(oom_lines)
            if ram is not None:
                ev.append(f"مصرف رم: {ram:.0f}٪ ظرفیت پلن")
            signals.append(Signal(
                "memory", "پلن برای ترافیک فعلی کوچک است و حافظه تمام می‌شود.",
                "پلن برنامه را یک پله بزرگ‌تر می‌کنم.",
                "propose_plan_change", {"direction": "up"}, ev, "high" if oom_lines else "medium"))

    listen, release_port = evidence.get("listen_port"), evidence.get("release_port")
    if listen and release_port and int(listen) != int(release_port):
        signals.append(Signal(
            "port_mismatch", f"برنامه روی پورت {listen} گوش می‌دهد ولی لیارا انتظار پورت {release_port} را دارد.",
            f"با پورت {listen} دوباره منتشر می‌کنم.", "propose_deploy", {"port": int(listen)},
            [f"listen {listen} / release {release_port}"], "high"))

    crash_lines = _hits(logs, CRASH)
    if crash_lines and evidence.get("last_release_after_crash") and not module_lines and not env_lines:
        rollback = _previous_release(evidence)
        signals.append(Signal(
            "bad_release", "خرابی دقیقاً بعد از آخرین نسخه شروع شده؛ احتمالاً آن نسخه مشکل دارد.",
            "به نسخه‌ی قبلی برمی‌گردم و بعد با هم مشکل کد را پیدا می‌کنیم.",
            "propose_rollback" if rollback else "", {"release_id": rollback} if rollback else {},
            crash_lines, "medium"))

    order = {"high": 0, "medium": 1, "low": 2}
    signals.sort(key=lambda s: order[s.confidence])
    return [s.as_dict() for s in signals]


def _previous_release(evidence):
    releases = [r for r in evidence.get("releases") or [] if r.get("id")]
    current = evidence.get("current_release") or (releases[0]["id"] if releases else None)
    for r in releases:
        if r["id"] != current and (r.get("state") or "").upper() in ("READY", "ACTIVE", "SUCCEEDED", "SUCCESS", ""):
            return r["id"]
    return None


def missing_evidence(evidence: dict) -> list[str]:
    names = {"logs": "لاگ‌ها", "ram_percent": "مصرف رم", "http": "پاسخ سایت", "releases": "فهرست نسخه‌ها"}
    return [label for key, label in names.items() if not evidence.get(key)]
