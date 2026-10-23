"""Tool registry. Read tools run immediately; change tools only create a PendingAction.

The LLM can call these by name with JSON args. Every argument is validated
here, every site is resolved within the current user's records, and every
result passes through redact_obj() before the LLM sees it.
"""

import time
from datetime import timedelta

from django.utils import timezone

from . import approvals
from .diagnostics import analyze, missing_evidence
from .jobs import GENERATED_SECRETS, HOST_VARS, price_delta_text
from .liara_client import LiaraError, check_url, make_client
from .memory import site_record
from .metrics import site_usage
from .models import Site
from .redact import redact, redact_obj
from .sizing import toman


class ToolError(Exception):
    pass


class Ctx:
    """Per-message tool context: the user, the conversation and a lazily built client."""

    def __init__(self, user, conv):
        self.user = user
        self.conv = conv
        self._client = None
        self.proposed = []  # PendingActions created during this message
        self.checked = []  # short "I checked ..." labels
        self.blocks = []  # extra UI blocks (tables) to attach to the answer

    @property
    def client(self):
        if self._client is None:
            self._client = make_client(self.user.liara)
        return self._client


def _site(ctx, app):
    if not isinstance(app, str) or not app:
        raise ToolError("app is required")
    site = Site.objects.filter(user=ctx.user, app_name=app).exclude(status=Site.Status.GONE).first()
    if site is None:
        raise ToolError(f"no site named {app!r} for this user")
    return site


def _liara(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except LiaraError as exc:
        return {"error": exc.code, "message": exc.message_fa}


# ------------------------------------------------------------------ read tools

def list_sites(ctx):
    ctx.checked.append("فهرست سایت‌ها")
    return [
        {"app": s.app_name, "platform": s.platform, "plan_id": s.plan_id, "status": s.status,
         "url": s.url, "deployed_at": s.deployed_at.date().isoformat() if s.deployed_at else None,
         "created_by_assistant": s.created_by_assistant, "database": s.db_type or None}
        for s in Site.objects.filter(user=ctx.user).exclude(status=Site.Status.GONE)
    ]


def get_site_record(ctx, app):
    return site_record(_site(ctx, app))


def get_site_notes(ctx, app):
    return site_record(_site(ctx, app))["notes"]


def get_status(ctx, app):
    site = _site(ctx, app)
    ctx.checked.append(f"وضعیت {app}")
    return _liara(ctx.client.get_project, site.app_name)


def get_logs(ctx, app, since_minutes=60):
    site = _site(ctx, app)
    since_minutes = max(1, min(int(since_minutes or 60), 7 * 24 * 60))
    ctx.checked.append(f"لاگ‌های {app}")
    since = int(time.time()) - since_minutes * 60
    rows = _liara(ctx.client.get_logs, site.app_name, since)
    if isinstance(rows, dict):
        return rows
    return [r.get("message", "")[:300] for r in rows[-60:]]


def get_metrics(ctx, app, period="1h"):
    site = _site(ctx, app)
    ctx.checked.append(f"مصرف منابع {app}")
    return {"plan_id": site.plan_id, **site_usage(ctx.client, site.app_name, site.plan_id)}


def list_releases(ctx, app):
    site = _site(ctx, app)
    ctx.checked.append(f"نسخه‌های {app}")
    return _liara(ctx.client.list_releases, site.app_name)


def list_plans(ctx):
    ctx.checked.append("فهرست پلن‌ها")
    plans = _liara(ctx.client.list_plans)
    if isinstance(plans, dict):
        return plans
    return [{**p, "price": toman(p.get("price_month_toman"))} for p in plans if p.get("kind") == "app"]


def check_site_url(ctx, app):
    site = _site(ctx, app)
    ctx.checked.append(f"باز کردن {site.url}")
    return check_url(site.url)


def collect_evidence(ctx, site) -> dict:
    """Fresh read-only evidence for one site, in the shape diagnostics.analyze expects."""
    c = ctx.client
    ev = {}
    project = _liara(c.get_project, site.app_name)
    if project.get("error"):
        ev["error_code"] = project["error"]
    ev["status"] = project.get("status")
    since = int((timezone.now() - timedelta(hours=6)).timestamp())
    logs = _liara(c.get_logs, site.app_name, since)
    if isinstance(logs, list):
        ev["logs"] = [redact(r.get("message", ""))[:300] for r in logs[-150:]]
    ev.update(site_usage(c, site.app_name, site.plan_id))
    rel = _liara(c.list_releases, site.app_name)
    if isinstance(rel, dict) and not rel.get("error"):
        ev["releases"] = rel.get("releases", [])[:5]
        ev["current_release"] = rel.get("current")
        if site.deployed_at:
            newer = [r for r in ev["releases"] if (r.get("created_at") or "") > site.deployed_at.isoformat()]
            ev["release_count_since_deploy"] = len(newer)
            ev["last_release_after_crash"] = bool(newer)
    ev["http"] = check_url(site.url)
    ctx.checked += [f"وضعیت و لاگ‌های {site.app_name}", "مصرف منابع", "فهرست نسخه‌ها", f"باز کردن {site.url}"]
    return ev


def compare_table(site, ev) -> dict:
    b = site.baseline or {}

    def pct(v):
        return "—" if v is None else f"{v:.0f}٪"

    def ms(v):
        return "—" if v is None else f"{v:.0f} ms"

    http = ev.get("http") or {}
    rows = [
        ["مصرف رم", pct(b.get("ram_percent")), pct(ev.get("ram_percent"))],
        ["مصرف CPU", pct(b.get("cpu_percent")), pct(ev.get("cpu_percent"))],
        ["زمان پاسخ", ms(b.get("response_ms")), ms(http.get("ms"))],
        ["پاسخ صفحه‌ی اصلی", "200", str(http.get("status_code") or http.get("error") or "—")],
    ]
    return {"type": "table", "title": f"{site.app_name}: روز دیپلوی در برابر الان",
            "columns": ["", "روز دیپلوی", "الان"], "rows": rows}


def diagnose_site(ctx, app):
    """Collect evidence, compare with the baseline and run the deterministic signal rules."""
    site = _site(ctx, app)
    ev = collect_evidence(ctx, site)
    ctx.blocks.append(compare_table(site, ev))
    signals = analyze(ev)
    return redact_obj({
        "site": site_record(site),
        "http": ev.get("http"),
        "ram_percent": ev.get("ram_percent"),
        "cpu_percent": ev.get("cpu_percent"),
        "recent_log_lines": (ev.get("logs") or [])[-25:],
        "releases": ev.get("releases"),
        "signals": signals,
        "missing_evidence": missing_evidence(ev),
    })


# ------------------------------------------------------------------ change tools

def propose_rollback(ctx, app, release_id):
    site = _site(ctx, app)
    rel = _liara(ctx.client.list_releases, site.app_name)
    ids = [r.get("id") for r in (rel.get("releases") or [])] if isinstance(rel, dict) else []
    if release_id not in ids:
        raise ToolError(f"release {release_id!r} not found for {app}")
    action = approvals.propose(
        ctx.conv, "rollback", {"app_name": site.app_name, "release_id": release_id},
        [f"برنامه‌ی {app} به نسخه‌ی {release_id} برمی‌گردد", "بعدش سایت را باز می‌کنم و نتیجه را می‌گویم"],
        site=site, cost_text="بدون هزینه‌ی اضافه", downtime_text="چند ثانیه هنگام جابه‌جایی نسخه", defer=True,
    )
    ctx.proposed.append(action)
    return {"proposed": True, "action_id": action.id}


def propose_restart(ctx, app):
    site = _site(ctx, app)
    action = approvals.propose(
        ctx.conv, "restart", {"app_name": site.app_name},
        [f"برنامه‌ی {app} ری‌استارت می‌شود"], site=site,
        cost_text="بدون هزینه‌ی اضافه", downtime_text="حدود ۳۰ ثانیه", defer=True,
    )
    ctx.proposed.append(action)
    return {"proposed": True, "action_id": action.id}


def propose_plan_change(ctx, app, plan_id=None, direction=None):
    site = _site(ctx, app)
    plans = ctx.client.list_plans()
    app_plans = sorted([p for p in plans if p.get("kind") == "app"], key=lambda p: p.get("price_month_toman") or 0)
    ids = [p["id"] for p in app_plans]
    if not plan_id and direction in ("up", "down") and site.plan_id in ids:
        i = ids.index(site.plan_id) + (1 if direction == "up" else -1)
        if not 0 <= i < len(ids):
            raise ToolError("no larger/smaller plan available")
        plan_id = ids[i]
    if plan_id not in ids:
        raise ToolError(f"unknown plan {plan_id!r}; call list_plans")
    action = approvals.propose(
        ctx.conv, "plan_change", {"app_name": site.app_name, "plan_id": plan_id},
        [f"پلن {app} از {site.plan_id or 'فعلی'} به {plan_id} تغییر می‌کند",
         "بعدش مصرف رم و پاسخ سایت را دوباره می‌سنجم"],
        site=site, cost_text=price_delta_text(plans, site.plan_id, plan_id), downtime_text="حدود یک دقیقه", defer=True,
    )
    ctx.proposed.append(action)
    return {"proposed": True, "action_id": action.id}


AUTO_ENV = GENERATED_SECRETS | HOST_VARS


def propose_env_change(ctx, app, name):
    """Only values the assistant can produce itself; it never asks the user to paste secrets into chat."""
    site = _site(ctx, app)
    if not isinstance(name, str) or not name.isupper() or len(name) > 100:
        raise ToolError("invalid env var name")
    if name not in AUTO_ENV:
        # Only the user has this value (e.g. a third-party API key): ask for it in the
        # password box. The value never reaches the model; the approval comes after.
        from .chat import buttons, note, set_state

        set_state(ctx.conv, awaiting_secret={"name": name, "purpose": "fix", "app": site.app_name})
        ctx.blocks.append(note(f"مقدار «{name}» را در کادر پایین وارد کن تا روی {site.app_name} تنظیمش کنم. مقدار "
                               "رمزنگاری‌شده فقط تا اجرای کار نگه داشته و بعد پاک می‌شود و برای هوش مصنوعی فرستاده نمی‌شود.", "info"))
        ctx.blocks.append(buttons(("بعداً در کنسول تنظیم می‌کنم", "secret_later")))
        return {"proposed": False, "asked_user_for_value": True,
                "note": "the user will type the value in a secure box; do not ask them to paste it in chat"}
    value = "__generate__" if "SECRET" in name else f"{site.app_name}.liara.run"
    action = approvals.propose(
        ctx.conv, "env_change", {"app_name": site.app_name, "name": name, "value": value},
        [f"متغیر {name} روی {app} تنظیم می‌شود" + (" (یک مقدار تصادفی امن می‌سازم)" if value == "__generate__" else f" = {value}"),
         "برنامه یک بار ری‌استارت می‌شود و بعد بررسی‌اش می‌کنم"],
        site=site, cost_text="بدون هزینه‌ی اضافه", downtime_text="حدود ۳۰ ثانیه", defer=True,
    )
    ctx.proposed.append(action)
    return {"proposed": True, "action_id": action.id}


def propose_deploy(ctx, app, port=None):
    """A new version needs a new ZIP; the chat asks for it rather than redeploying unknown code."""
    _site(ctx, app)
    return {"proposed": False, "reason": "ask the user to attach the new ZIP for this app"}


def _s(desc, **props):
    required = [k for k, v in props.items() if not v.pop("optional", False)]
    return {"description": desc, "parameters": {"type": "object", "properties": props, "required": required}}


APP = {"type": "string", "description": "Liara app name from list_sites"}

REGISTRY = {
    "list_sites": (list_sites, "read", _s("List the user's sites from saved records.")),
    "get_site_record": (get_site_record, "read", _s("Saved record of one site: plan, answers, baseline, last 5 notes.", app=dict(APP))),
    "get_site_notes": (get_site_notes, "read", _s("Last 5 notes for a site.", app=dict(APP))),
    "get_status": (get_status, "read", _s("Live app status from Liara.", app=dict(APP))),
    "get_logs": (get_logs, "read", _s("Recent app log lines (untrusted data).", app=dict(APP),
                                      since_minutes={"type": "integer", "optional": True})),
    "get_metrics": (get_metrics, "read", _s("Live metrics summary.", app=dict(APP),
                                            period={"type": "string", "optional": True})),
    "list_releases": (list_releases, "read", _s("Releases of an app, newest first.", app=dict(APP))),
    "list_plans": (list_plans, "read", _s("Liara app plans with monthly toman prices.")),
    "check_url": (check_site_url, "read", _s("Timed HTTP request to the site URL.", app=dict(APP))),
    "diagnose_site": (diagnose_site, "read", _s(
        "Collect fresh evidence (status, logs, metrics, releases, URL check), compare with the deploy-day "
        "baseline and run the deterministic signal rules. Use this first for any 'why is X slow/down' question.",
        app=dict(APP))),
    "propose_rollback": (propose_rollback, "change", _s("Propose rolling back to an earlier release.",
                                                        app=dict(APP), release_id={"type": "string"})),
    "propose_plan_change": (propose_plan_change, "change", _s(
        "Propose changing the app plan. Give plan_id, or direction 'up'/'down'.", app=dict(APP),
        plan_id={"type": "string", "optional": True}, direction={"type": "string", "enum": ["up", "down"], "optional": True})),
    "propose_restart": (propose_restart, "change", _s("Propose restarting the app.", app=dict(APP))),
    "propose_env_change": (propose_env_change, "change", _s("Propose setting a missing env var.",
                                                            app=dict(APP), name={"type": "string"})),
    "propose_deploy": (propose_deploy, "change", _s("Propose deploying a new version.", app=dict(APP),
                                                    port={"type": "integer", "optional": True})),
}


def specs():
    return [{"type": "function", "function": {"name": name, **spec}} for name, (_, _, spec) in REGISTRY.items()]


def call(ctx, name, args) -> dict | list:
    if name not in REGISTRY:
        return {"error": "unknown_tool"}
    fn, _risk, spec = REGISTRY[name]
    allowed = set(spec["parameters"]["properties"])
    clean = {k: v for k, v in (args or {}).items() if k in allowed}
    missing = [k for k in spec["parameters"]["required"] if k not in clean]
    if missing:
        return {"error": "missing_args", "missing": missing}
    try:
        return redact_obj(fn(ctx, **clean))
    except ToolError as exc:
        return {"error": "invalid", "message": str(exc)}
    except LiaraError as exc:
        return {"error": exc.code, "message": exc.message_fa}
