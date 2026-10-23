"""Scripted chat flows: connect the account (Phase 1), deploy from a ZIP (Phase 2)
and the returning-user greeting (Phase 3). Anything outside a flow step goes
to the orchestrator."""

import base64
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from . import ai_detect, approvals, chat, orchestrator, sizing
from .inspection import InspectionError, inspect_zip
from .liara_client import LiaraError, client_for_token, make_client
from .memory import age_fa, import_projects
from .models import LiaraConnection, Site

PLATFORM_FA = ai_detect.PLATFORM_FA
SERVICE_FA = {"postgresql": "دیتابیس PostgreSQL", "mysql": "دیتابیس MySQL", "mongodb": "دیتابیس MongoDB",
              "redis": "Redis", "bucket": "فضای ذخیره‌سازی فایل"}

KEY_STEPS = {
    "type": "steps",
    "items": [
        "وارد کنسول لیارا شو و بخش API را باز کن.",
        "یک تاریخ انقضا انتخاب کن (پیشنهاد من: یک ماه).",
        "روی «ساخت کلید جدید» بزن.",
        "کلید را همان لحظه کپی کن؛ فقط یک بار نشان داده می‌شود.",
    ],
}
KEY_LINK = {"type": "link", "href": "https://console.liara.ir/API", "label": "https://console.liara.ir/API"}


def _connected(user):
    conn = getattr(user, "liara", None)
    return conn if conn and conn.status == LiaraConnection.Status.ACTIVE else None


# ------------------------------------------------------------------ greeting / returning user

def greet(user, conv):
    if not _connected(user):
        return [chat.bot(conv, "سلام! من دستیار لیارا هستم و کمکت می‌کنم سایتت را روی لیارا بالا بیاوری و سالم نگهش داری.\n"
                               "برای شروع، به کلید API حساب لیارای تو نیاز دارم. کلید داری؟",
                         chat.buttons(("کلید API دارم", "have_key"), ("نمی‌دونم چطور بگیرم", "how_key")))]
    return returning(user, conv)


def returning(user, conv):
    out = []
    conn = _connected(user)
    try:
        import_projects(user, make_client(conn).list_projects())
    except LiaraError as exc:
        if exc.code == "unauthorized":
            conn.status = LiaraConnection.Status.INVALID
            conn.save(update_fields=["status"])
            chat.set_state(conv, awaiting_key=True)
            return [chat.bot(conv, "کلید API ذخیره‌شده دیگر معتبر نیست یا منقضی شده. یک کلید جدید بساز و اینجا بفرست:",
                             KEY_STEPS, KEY_LINK)]
        out.append(chat.bot(conv, "", chat.error(exc.message_fa, "فهرست زیر از حافظه‌ی من است و ممکن است به‌روز نباشد.")))
    sites = list(Site.objects.filter(user=user).exclude(status=Site.Status.GONE)[:6])
    last = max((s.deployed_at for s in sites if s.deployed_at), default=None)
    if last and (timezone.now() - last).days >= 1:
        out.append(chat.bot(conv, "", {"type": "divider", "text": f"{age_fa(last).replace(' پیش', '')} بعد از دیپلوی"}))
    if conn.expires_at and conn.expires_at - timezone.now() < timedelta(days=3):
        out.append(chat.bot(conv, "", chat.note("کلید API تو کمتر از ۳ روز دیگر منقضی می‌شود؛ یک کلید جدید بساز و بفرست.", "warning")))
    if not sites:
        out.append(chat.bot(conv, "سلام دوباره! هنوز سایتی روی حسابت نیست. بیا اولی را بالا بیاوریم.",
                            chat.buttons(("یک پروژه‌ی جدید بالا بیاور", "deploy_new"))))
        return out
    items = []
    for s in sites:
        parts = [PLATFORM_FA.get(s.platform, s.platform or "برنامه")]
        if s.db_type:
            parts.append(SERVICE_FA.get(s.db_type, s.db_type))
        if s.deployed_at:
            parts.append(f"دیپلوی {age_fa(s.deployed_at)}")
        elif not s.created_by_assistant:
            parts.append("ساخته‌شده خارج از دستیار")
        items.append({"title": s.url.replace("https://", "") or s.app_name, "subtitle": " · ".join(parts), "ltr_title": True})
    first = sites[0].app_name
    out.append(chat.bot(conv, "سلام دوباره! سایت‌هایی که روی حسابت هست:",
                        {"type": "card", "items": items, "footer": "چه کمکی از دستم برمی‌آید؟"},
                        chat.buttons(("سایت‌هایم چطورند؟", "sites_status"), (f"چرا {first} کند شده؟", f"diag:{first}"),
                                     ("نسخه‌ی جدید بفرستم", "new_version"))))
    return out


# ------------------------------------------------------------------ Phase 1: connect

def _jwt_exp(token):
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
        exp = payload.get("exp")
        return datetime.fromtimestamp(int(exp), tz=dt_timezone.utc) if exp else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def looks_like_key(text):
    return " " not in text and len(text) >= 20 and re.fullmatch(r"[A-Za-z0-9._\-]+", text) is not None


def connect_key(user, conv, key):
    """The key never enters Message; only the encrypted token is stored, and only on success."""
    key = (key or "").strip()
    if not key or any(c.isspace() for c in key) or len(key) > 4096:
        return [chat.bot(conv, "", chat.error("این کلید معتبر به نظر نمی‌رسد.", "کلید را کامل و بدون فاصله بچسبان."))]
    chat.user_says(conv, "🔑 کلید API فرستاده شد")
    try:
        projects = client_for_token(key).list_projects()
    except LiaraError as exc:
        if exc.code == "unauthorized":
            return [chat.bot(conv, "این کلید معتبر نیست یا منقضی شده. یک کلید تازه بساز:", KEY_STEPS, KEY_LINK,
                             chat.buttons(("کلید را گرفتم", "have_key")))]
        return [chat.bot(conv, "", chat.error("لیارا الان پاسخ نمی‌دهد؛ یک دقیقه دیگر دوباره امتحان کن.",
                                              "کلید ذخیره نشد؛ دوباره بفرستش."))]
    conn, _ = LiaraConnection.objects.get_or_create(user=user, defaults={"encrypted_token": ""})
    conn.set_token(key)
    conn.expires_at = _jwt_exp(key)
    conn.status = LiaraConnection.Status.ACTIVE
    conn.last_verified_at = timezone.now()
    conn.save()
    chat.set_state(conv, awaiting_key=None)
    sites = import_projects(user, projects)
    names = "، ".join(s.app_name for s in sites[:6]) + (" و ..." if len(sites) > 6 else "")
    found = f"{len(sites)} برنامه روی حسابت پیدا کردم: {names}" if sites else "هنوز برنامه‌ای روی حسابت نیست."
    return [chat.bot(conv, f"کلید معتبر است و وصل شدیم. {found}".translate(str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")),
                     chat.buttons(("یک پروژه‌ی جدید بالا بیاور", "deploy_new"), ("برنامه‌هایم را بررسی کن", "sites_status")))]


# ------------------------------------------------------------------ Phase 2: deploy

VISITOR_BUTTONS = chat.buttons(("کمتر از ۱۰۰", "lt100"), ("۱۰۰ تا ۱۰۰۰", "100-1000"),
                               ("بیشتر از ۱۰۰۰", "gt1000"), ("نمی‌دانم", "unknown"))
CONFIRM_DETECT_BUTTONS = chat.buttons(("درسته، ادامه بده", "detect_ok"), ("نه، اشتباه است", "detect_wrong"))
PURPOSE_BUTTONS = chat.buttons(("کسب‌وکار واقعی", "business"), ("تست یا شخصی", "personal"))


def start_deploy(conv):
    chat.set_state(conv, flow="deploy", step="source")
    return [chat.bot(conv, "حتماً! پروژه‌ات را از کجا بردارم؟",
                     chat.buttons(("فایل zip آپلود می‌کنم", "zip"), ("لینک گیت‌هاب می‌دهم", "github")))]


def handle_upload(user, conv, upload):
    if not _connected(user):
        return [chat.bot(conv, "", chat.error("اول باید حساب لیارا را وصل کنیم.", "کلید API را بفرست."))]
    chat.user_says(conv, f"📎 {Path(upload.name).name[:80]}")
    max_mb = settings.MAX_ZIP_BYTES // (1024 * 1024)
    if not upload.name.lower().endswith(".zip"):
        return [chat.bot(conv, "", chat.error("فقط فایل zip قبول می‌کنم.", f"پروژه را zip کن (حداکثر {max_mb} مگابایت)."))]
    if upload.size > settings.MAX_ZIP_BYTES:
        return [chat.bot(conv, "", chat.error(f"فایل بزرگ‌تر از {max_mb} مگابایت است.", "پوشه‌های node_modules و venv را از zip حذف کن."))]
    settings.UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    upload_id = uuid.uuid4().hex
    path = settings.UPLOAD_DIR / f"{upload_id}.zip"
    with path.open("wb") as fh:
        for chunk in upload.chunks():
            fh.write(chunk)
    try:
        from .inspection import InspectionLimits

        report = inspect_zip(path, InspectionLimits(archive_bytes=settings.MAX_ZIP_BYTES))
    except InspectionError as exc:
        path.unlink(missing_ok=True)
        return [chat.bot(conv, "", chat.error(f"این فایل را نمی‌توانم بپذیرم: {_inspection_fa(exc.code)}",
                                              "یک zip سالم از پوشه‌ی پروژه بساز و دوباره بفرست."))]
    project = report["project"]
    if project["platform"] is None:
        # The rules don't know this kind of project: ask the model, then let the user confirm.
        if not orchestrator._within_daily_cap(user):
            path.unlink(missing_ok=True)
            return [chat.bot(conv, "", chat.error("سقف استفاده‌ی امروز از هوش مصنوعی پر شده.", "فردا دوباره امتحان کن."))]
        try:
            project = ai_detect.detect(path)
        except ai_detect.DetectionFailed as exc:
            path.unlink(missing_ok=True)
            return [chat.bot(conv, "", chat.error(f"این پروژه را نشناختم: {exc.reason_fa}",
                                                  "یک liara.json با فیلد platform در ریشه‌ی پروژه بگذار و دوباره بفرست."))]
    elif not project["supported"]:
        # A known platform with a concrete problem (e.g. no start script): the rule's reason is precise.
        path.unlink(missing_ok=True)
        return [chat.bot(conv, "", chat.error(project["unsupported_reason_fa"] or "این نوع پروژه هنوز پشتیبانی نمی‌شود.",
                                              "مشکل را درست کن و دوباره فایل zip را بفرست."))]
    items = [
        {"title": f"پلتفرم: {PLATFORM_FA.get(project['platform'], project['platform'])}",
         "subtitle": f"دستور اجرا: {project['start_command']}" if project.get("start_command") else "دستور اجرا را لیارا خودش تعیین می‌کند"},
        {"title": f"پورت: {project['port']}", "subtitle": "برنامه روی این پورت اجرا می‌شود"},
    ]
    for svc in project["services"]:
        items.append({"title": SERVICE_FA.get(svc["kind"], svc["kind"]), "subtitle": svc["reason_fa"]})
    blocks = [{"type": "card", "title": "چیزهایی که در پروژه پیدا کردم", "items": items}]
    if project["services"]:
        blocks.append(chat.note("لازم نیست کاری بکنی: این‌ها را خودم روی لیارا می‌سازم و به برنامه‌ات وصل می‌کنم."))
    for warning in project.get("warnings_fa") or []:
        blocks.append(chat.note(warning, "warning"))
    ai = project.get("detected_by") == "ai"
    chat.set_state(conv, flow="deploy", step="confirm_detect" if ai else "visitors", upload_id=upload_id,
                   project=project, source_name=Path(upload.name).stem)
    if ai:
        summary = project.get("summary_fa") or ""
        out = [chat.bot(conv, "این نوع پروژه را با قوانین ثابتم نمی‌شناختم، پس با هوش مصنوعی بررسی‌اش کردم. " + summary, *blocks)]
        out.append(chat.bot(conv, "این تشخیص درست است؟", CONFIRM_DETECT_BUTTONS))
        return out
    out = [chat.bot(conv, "پروژه را بررسی کردم.", *blocks)]
    out.append(chat.bot(conv, "روزانه حدوداً چند نفر به سایتت سر می‌زنند؟", VISITOR_BUTTONS))
    return out


def _inspection_fa(code):
    return {
        "unsafe_path": "مسیر فایل‌ها ناامن است", "unsafe_entry": "لینک یا فایل ویژه دارد",
        "encrypted_archive": "zip رمزدار است", "archive_too_large": "حجمش زیاد است",
        "expanded_size_limit": "بعد از باز شدن بیش از حد بزرگ است", "compression_ratio_limit": "فشرده‌سازی مشکوک دارد",
        "too_many_entries": "تعداد فایل‌ها خیلی زیاد است", "invalid_archive": "فایل zip سالم نیست",
    }.get(code, "فایل قابل بررسی نیست")


def _app_name(source):
    base = re.sub(r"[^a-z0-9-]+", "-", (source or "app").lower()).strip("-")[:20] or "app"
    if not base[0].isalpha():
        base = "app-" + base
    return f"{base}-{secrets.token_hex(2)}"


def _recommend(user, conv, tier=None):
    state = conv.state
    try:
        plans = make_client(user.liara).list_plans()
    except LiaraError as exc:
        return [chat.bot(conv, "", chat.error(f"فهرست پلن‌ها را نتوانستم بگیرم: {exc.message_fa}", "یک دقیقه دیگر «دوباره» را بزن.")),
                chat.bot(conv, "", chat.buttons(("دوباره", "retry_recommend")))]
    pkg = sizing.recommend(plans, state["project"]["services"], state.get("visitors"), state.get("purpose"), tier)
    chat.set_state(conv, step="recommend", package=pkg)
    opts = [("قبول، همین را بساز", "accept")]
    if pkg["can_go_cheaper"]:
        opts.append(("ارزان‌ترش کن", "cheaper"))
    return [chat.bot(conv, "", sizing.recommendation_block(pkg), chat.buttons(*opts))]


def _approval(user, conv):
    state = conv.state
    pkg, project = state["package"], state["project"]
    app_name = _app_name(state.get("source_name"))
    args = {
        "app_name": app_name, "platform": project["platform"], "port": project["port"],
        "app_plan": pkg["app_plan"], "db_kind": pkg["db_kind"], "db_plan": pkg["db_plan"],
        "bucket": pkg["bucket"], "backups": pkg["backups"], "tier": pkg["tier"],
        "env_vars": project["env_vars"], "root": project["root"],
        "upload_path": str(settings.UPLOAD_DIR / f"{state['upload_id']}.zip"),
        "answers": {"visitors": state.get("visitors"), "purpose": state.get("purpose")},
        "total_text": pkg["total_text"],
    }
    lines = [f"برنامه‌ی {app_name} ({PLATFORM_FA.get(project['platform'])}) با پلن {pkg['app_plan']}"]
    if pkg["db_kind"]:
        lines.append(f"{SERVICE_FA[pkg['db_kind']]} با پلن {pkg['db_plan']}" + (" و پشتیبان‌گیری روزانه" if pkg["backups"] else ""))
    if pkg["bucket"]:
        lines.append("فضای ذخیره‌سازی فایل")
    lines.append("اتصال این سرویس‌ها به برنامه با متغیرهای محیطی")
    lines.append(f"آپلود کد، بیلد و انتشار روی https://{app_name}.liara.run")
    chat.set_state(conv, flow=None, step=None)
    approvals.propose(conv, "deploy", args, lines, cost_text=pkg["total_text"], downtime_text="ندارد (برنامه‌ی جدید)")
    return list(conv.messages.order_by("-id")[:1])


def deploy_step(user, conv, text, value):
    step = conv.state.get("step")
    if step == "source":
        if value == "github":
            return [chat.bot(conv, "اتصال گیت‌هاب در این نسخه هنوز آماده نیست. پوشه‌ی پروژه را zip کن و با دکمه‌ی سنجاق بفرست.")]
        return [chat.bot(conv, "فایل zip پروژه را با دکمه‌ی سنجاق کنار پیام بفرست.")]
    if step == "confirm_detect":
        if value == "detect_ok":
            chat.set_state(conv, step="visitors")
            return [chat.bot(conv, "روزانه حدوداً چند نفر به سایتت سر می‌زنند؟", VISITOR_BUTTONS)]
        if value == "detect_wrong":
            upload_id = conv.state.get("upload_id")
            if upload_id:
                (settings.UPLOAD_DIR / f"{upload_id}.zip").unlink(missing_ok=True)
            chat.set_state(conv, flow=None, step=None, upload_id=None, project=None)
            return [chat.bot(conv, "باشه، چیزی نمی‌سازم. اگر یک فایل liara.json با فیلد platform (مثلاً "
                                   "{\"platform\": \"flask\"}) در ریشه‌ی پروژه بگذاری، دقیق‌تر تشخیص می‌دهم.",
                             chat.buttons(("دوباره فایل می‌فرستم", "zip")))]
        return [chat.bot(conv, "این تشخیص درست است؟", CONFIRM_DETECT_BUTTONS)]
    if step == "visitors":
        if value not in sizing.TIER_FOR_VISITORS:
            return [chat.bot(conv, "یکی از گزینه‌ها را انتخاب کن:", VISITOR_BUTTONS)]
        chat.set_state(conv, visitors=value, step="purpose")
        return [chat.bot(conv, "این سایت برای یک کسب‌وکار واقعی است یا تست و شخصی؟", PURPOSE_BUTTONS)]
    if step == "purpose":
        if value not in ("business", "personal"):
            return [chat.bot(conv, "یکی از گزینه‌ها را انتخاب کن:", PURPOSE_BUTTONS)]
        chat.set_state(conv, purpose=value)
        return _recommend(user, conv)
    if step == "recommend":
        if value == "accept":
            return _approval(user, conv)
        if value == "cheaper":
            return _recommend(user, conv, tier=max(conv.state["package"]["tier"] - 1, 0))
        if value == "retry_recommend":
            return _recommend(user, conv)
    return None


# ------------------------------------------------------------------ dispatcher

def handle_text(user, conv, text, value=None):
    approvals.expire_stale(conv)
    text = (text or "").strip()[:2000]
    if not text:
        return []
    # A key pasted into the normal text box must never be stored as a Message.
    if not _connected(user) and not value and looks_like_key(text):
        return connect_key(user, conv, text)
    chat.user_says(conv, text)
    value = value or ""
    state = conv.state or {}

    if not _connected(user):
        if value == "how_key":
            chat.set_state(conv, awaiting_key=True)
            return [chat.bot(conv, "ساختن کلید فقط یک دقیقه طول می‌کشد:", KEY_STEPS, KEY_LINK,
                             chat.buttons(("کلید را گرفتم", "have_key")))]
        chat.set_state(conv, awaiting_key=True)
        return [chat.bot(conv, "کلید API لیارا را در کادر پایین بچسبان.",
                         chat.note("کلید رمزنگاری‌شده ذخیره می‌شود و هیچ‌وقت برای مدل هوش مصنوعی فرستاده نمی‌شود.", "info"))]

    if value == "retry_recommend" or (state.get("flow") == "deploy" and value):
        out = deploy_step(user, conv, text, value)
        if out is not None:
            return out
    if value in ("deploy_new", "new_version") or text in ("یک پروژه‌ی جدید بالا بیاور",):
        return start_deploy(conv)
    if value == "zip":
        return [chat.bot(conv, "فایل zip پروژه را با دکمه‌ی سنجاق کنار پیام بفرست.")]
    if value == "domain_help":
        return [chat.bot(conv, "برای وصل کردن دامنه: در کنسول لیارا به بخش دامنه‌های برنامه برو، دامنه را اضافه کن و "
                               "رکوردهای DNS را که نشان می‌دهد در سرویس دامنه‌ات ثبت کن. در این نسخه این کار را خودکار انجام نمی‌دهم.",
                         {"type": "link", "href": "https://console.liara.ir/domains", "label": "https://console.liara.ir/domains"})]
    if value == "sites_status":
        return orchestrator.sites_status(__import__("assistant.tools", fromlist=["Ctx"]).Ctx(user, conv))
    if value.startswith("diag:"):
        text = f"چرا {value[5:]} مشکل دارد؟"
    return orchestrator.answer(user, conv, text)
