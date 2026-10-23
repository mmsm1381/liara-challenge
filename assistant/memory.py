"""Site memory: syncing Site records with Liara and writing SiteNotes."""

from django.utils import timezone

from .models import PendingAction, Site, SiteNote


def import_projects(user, projects) -> list[Site]:
    """Create minimal Site records for apps found in the account; mark vanished ones as gone."""
    seen = set()
    sites = []
    for p in projects:
        name = p.get("name")
        if not name:
            continue
        seen.add(name)
        site, created = Site.objects.get_or_create(
            user=user,
            app_name=name,
            defaults={"platform": p.get("platform") or "", "plan_id": p.get("plan_id") or "",
                      "url": f"https://{name}.liara.run", "status": Site.Status.UNKNOWN},
        )
        changed = []
        for field, value in (("platform", p.get("platform")), ("plan_id", p.get("plan_id"))):
            if value and getattr(site, field) != value:
                setattr(site, field, value)
                changed.append(field)
        if site.status == Site.Status.GONE:
            site.status = Site.Status.UNKNOWN
            changed.append("status")
        if changed:
            site.save(update_fields=changed + ["updated_at"])
        sites.append(site)
    Site.objects.filter(user=user).exclude(app_name__in=seen).exclude(status=Site.Status.DEPLOYING).update(
        status=Site.Status.GONE
    )
    return sites


def add_note(site, kind, text):
    if site:
        SiteNote.objects.create(site=site, kind=kind, text=text[:1000])


def note_declined(action: PendingAction):
    if action.site_id:
        lines = "؛ ".join(action.summary[:2])
        add_note(action.site, SiteNote.Kind.DECISION, f"کاربر این پیشنهاد را رد کرد: {lines}")


def site_record(site) -> dict:
    """What the LLM sees about a site: no secrets, no env values."""
    return {
        "app_name": site.app_name,
        "platform": site.platform,
        "plan_id": site.plan_id,
        "url": site.url,
        "status": site.status,
        "created_by_assistant": site.created_by_assistant,
        "database": site.db_type or None,
        "bucket": bool(site.bucket_name),
        "answers": site.answers,
        "baseline": site.baseline,
        "deployed_at": site.deployed_at.isoformat() if site.deployed_at else None,
        "notes": [
            {"kind": n.kind, "text": n.text, "at": n.created_at.date().isoformat()}
            for n in site.notes.all()[:5]
        ],
    }


def age_fa(when) -> str:
    if not when:
        return ""
    days = (timezone.now() - when).days
    fa = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
    if days < 1:
        return "امروز"
    if days < 14:
        return f"{days} روز پیش".translate(fa)
    return f"{days // 7} هفته پیش".translate(fa)
