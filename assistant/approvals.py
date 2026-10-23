"""The approval gate. The only path from a proposal to a change on Liara.

propose() stores a PendingAction and shows an approval card. decide() records
the user's tap; on confirm it creates the Job and enqueues it. claim() is what
the worker calls: it atomically moves approved -> running exactly once.
"""

import hashlib
import json
import threading
from datetime import timedelta

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from . import chat
from .models import Job, Message, PendingAction


def digest(args) -> str:
    return hashlib.sha256(json.dumps(args, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def card(action) -> dict:
    return {
        "type": "approval",
        "action_id": action.id,
        "title": "تأیید شما لازم است",
        "lines": action.summary,
        "cost": action.cost_text,
        "downtime": action.downtime_text,
        "state": action.status,
    }


def propose(conv, tool, args, summary, *, site=None, cost_text="", downtime_text="", text="", defer=False) -> PendingAction:
    """defer=True: don't post the card yet; the caller attaches card(action) to its own answer via attach()."""
    action = PendingAction.objects.create(
        conversation=conv,
        site=site,
        tool=tool,
        args=args,
        args_digest=digest(args),
        summary=summary,
        cost_text=cost_text,
        downtime_text=downtime_text,
        expires_at=timezone.now() + timedelta(seconds=settings.APPROVAL_TTL_SECONDS),
    )
    if not defer:
        attach(action, chat.bot(conv, text, card(action)))
    return action


def attach(action, message):
    if not any(b.get("type") == "approval" and b.get("action_id") == action.id for b in message.ui):
        message.ui = [*message.ui, card(action)]
        message.save(update_fields=["ui"])
    action.message = message
    action.save(update_fields=["message"])


def _refresh_card(action):
    if action.message_id:
        msg = action.message
        msg.ui = [card(action) if b.get("type") == "approval" else b for b in msg.ui]
        msg.save(update_fields=["ui"])
        return msg
    return None


def expire_stale(conv):
    for action in PendingAction.objects.filter(
        conversation=conv, status=PendingAction.Status.PROPOSED, expires_at__lte=timezone.now()
    ):
        action.status = PendingAction.Status.EXPIRED
        action.save(update_fields=["status"])
        _refresh_card(action)


def decide(user, action_id, decision) -> list[Message]:
    """Returns the messages to send back (refreshed card + any new bot message)."""
    conv = chat.conversation_for(user)
    out = []
    with transaction.atomic():
        action = (
            PendingAction.objects.select_for_update()
            .filter(id=action_id, conversation__user=user)
            .first()
        )
        if action is None:
            return [chat.bot(conv, "", chat.error("این درخواست پیدا نشد."))]
        if action.status != PendingAction.Status.PROPOSED:
            # Double tap or stale card: show the real state and do nothing.
            msg = _refresh_card(action)
            return [msg] if msg else []
        if action.is_expired:
            action.status = PendingAction.Status.EXPIRED
            action.save(update_fields=["status"])
            out.append(_refresh_card(action))
            out.append(chat.bot(conv, "این تأیید بیش از ۱۵ دقیقه پیش ساخته شده و منقضی شد. اگر هنوز لازم است، دوباره بخواه تا تازه بررسی کنم."))
            return [m for m in out if m]
        if decision != "confirm":
            action.status = PendingAction.Status.REJECTED
            action.save(update_fields=["status"])
            out.append(_refresh_card(action))
            if action.tool == "deploy":
                out.append(chat.bot(conv, "لغو شد. چیزی ساخته نشد و هزینه‌ای هم کسر نشد."))
            else:
                out.append(chat.bot(conv, "باشه، انجامش نمی‌دهم. چیزی تغییر نکرد."))
                from .memory import note_declined

                note_declined(action)
            chat.set_state(conv, flow=None, step=None)
            return out
        action.status = PendingAction.Status.APPROVED
        action.save(update_fields=["status"])
        out.append(_refresh_card(action))
        from .jobs import steps_for

        job = Job.objects.create(pending_action=action, steps=[{"label": s, "state": "waiting"} for s in steps_for(action)])
        progress = chat.bot(conv, "", {"type": "progress", "job_id": job.id, "steps": job.steps, "done": False})
        job.message = progress
        job.save(update_fields=["message"])
        out.append(progress)
        transaction.on_commit(lambda: enqueue(action.id))
    return out


def enqueue(action_id):
    from .jobs import run_action

    if settings.RQ_SYNC:
        threading.Thread(target=run_action, args=(action_id,), daemon=True).start()
    else:
        import django_rq

        django_rq.get_queue("default").enqueue(run_action, action_id, job_timeout=1800)


def claim(action_id) -> PendingAction | None:
    """Approved -> running, exactly once. Returns None if someone else already claimed it."""
    updated = PendingAction.objects.filter(id=action_id, status=PendingAction.Status.APPROVED).update(
        status=PendingAction.Status.RUNNING
    )
    if updated != 1:
        return None
    action = PendingAction.objects.select_related("conversation__user", "site").get(id=action_id)
    if digest(action.args) != action.args_digest:
        action.status = PendingAction.Status.FAILED
        action.save(update_fields=["status"])
        return None
    return action
