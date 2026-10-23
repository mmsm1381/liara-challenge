"""Turn the two deploy answers into one package priced from the live plan list.

Sizing rule from the plan: under 100 visitors -> smallest tier; 100-1000 or
"don't know" -> middle tier; over 1000 -> large tier. A real business gets
daily database backups. Prices always come from `client.list_plans()`; there
are no hard-coded prices here.
"""

TIER_FOR_VISITORS = {"lt100": 0, "100-1000": 1, "unknown": 1, "gt1000": 2}
TIER_NAMES = ["کوچک", "متوسط", "بزرگ"]


def toman(amount) -> str:
    if amount is None:
        return "نامشخص"
    digits = f"{int(amount):,}".translate(str.maketrans("0123456789,", "۰۱۲۳۴۵۶۷۸۹٬"))
    return f"{digits} تومان"


def _tiers(plans, kind):
    rows = [p for p in plans if p.get("kind") == kind and p.get("id")]
    rows.sort(key=lambda p: (p.get("price_month_toman") or 0, p.get("ram_mb") or 0))
    # Liara lists plans up to 64 GB; the three cheapest cover small, medium and large sites.
    return rows[:3]


def _pick(tiers, index):
    if not tiers:
        return None
    return tiers[min(index, len(tiers) - 1)]


def _describe(plan):
    parts = []
    if plan.get("ram_mb"):
        ram = plan["ram_mb"]
        parts.append(f"{ram // 1024} گیگ رم" if ram >= 1024 and ram % 1024 == 0 else f"{ram} مگ رم")
    if plan.get("cpu"):
        parts.append(f"{plan['cpu']} هسته")
    return f"{plan['id']} ({'، '.join(parts)})" if parts else plan["id"]


def recommend(plans, services, visitors, purpose, tier=None) -> dict:
    """Return the package dict that both the recommendation card and the deploy job use."""
    app_tiers = _tiers(plans, "app")
    db_tiers = _tiers(plans, "database")
    if tier is None:
        tier = TIER_FOR_VISITORS.get(visitors, 1)
    tier = max(0, min(tier, max(len(app_tiers) - 1, 0)))
    app_plan = _pick(app_tiers, tier)
    kinds = [s["kind"] for s in services]
    db_kind = next((k for k in kinds if k in ("postgresql", "mysql", "mongodb")), None)
    db_plan = _pick(db_tiers, tier) if db_kind else None
    backups = purpose == "business" and bool(db_kind)

    lines, total = [], 0
    if app_plan:
        lines.append({"label": "پلن برنامه", "value": f"{_describe(app_plan)} — {toman(app_plan.get('price_month_toman'))}"})
        total += app_plan.get("price_month_toman") or 0
    if db_kind:
        price = db_plan.get("price_month_toman") if db_plan else None
        lines.append({"label": f"دیتابیس {db_kind}", "value": f"{_describe(db_plan) if db_plan else '—'} — {toman(price)}"})
        total += price or 0
    if "redis" in kinds:
        lines.append({"label": "Redis", "value": "در این نسخه خودکار ساخته نمی‌شود؛ بعداً کمکت می‌کنم"})
    if "bucket" in kinds:
        lines.append({"label": "فضای ذخیره‌سازی فایل", "value": "طبق مصرف"})
    if db_kind:
        lines.append({"label": "پشتیبان‌گیری روزانه", "value": "روشن" if backups else "خاموش"})

    reason = {
        0: "چون بازدید روزانه کم است، کوچک‌ترین پلن کافی است و هر وقت لازم شد بزرگش می‌کنیم.",
        1: "برای چند صد بازدید روزانه پلن متوسط جای کافی دارد و هزینه را معقول نگه می‌دارد.",
        2: "برای بیش از هزار بازدید روزانه پلن بزرگ‌تر جلوی کندی و ری‌استارت‌های کمبود حافظه را می‌گیرد.",
    }[min(tier, 2)]
    if backups:
        reason += " چون کسب‌وکار واقعی است، پشتیبان‌گیری روزانه را روشن گذاشتم."

    return {
        "tier": tier,
        "app_plan": app_plan["id"] if app_plan else "",
        "db_kind": db_kind or "",
        "db_plan": db_plan["id"] if db_plan else "",
        "bucket": "bucket" in kinds,
        "backups": backups,
        "lines": lines,
        "total": total,
        "total_text": toman(total) + " در ماه" if total else "قیمت از لیارا دریافت نشد",
        "reason": reason,
        "can_go_cheaper": tier > 0,
    }


def recommendation_block(pkg) -> dict:
    return {
        "type": "recommendation",
        "title": f"پیشنهاد من: بسته‌ی {TIER_NAMES[min(pkg['tier'], 2)]}",
        "lines": pkg["lines"],
        "total": pkg["total_text"],
        "reason": pkg["reason"],
    }
