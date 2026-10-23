"""Chat page and the JSON/SSE endpoints the page talks to. See docs/CONTRACTS.md."""

import json
import time

from django.conf import settings
from django.contrib.auth import login
from django.contrib.auth.decorators import login_required
from django.contrib.auth.forms import UserCreationForm
from django.http import HttpResponseBadRequest, JsonResponse, StreamingHttpResponse
from django.shortcuts import redirect, render
from django.views.decorators.debug import sensitive_post_parameters, sensitive_variables
from django.views.decorators.http import require_GET, require_POST

from . import approvals, chat, flows
from .models import Job, Message


def _reply(conv, messages, **extra):
    return JsonResponse({"messages": [chat.render(m) for m in messages if m], "composer": chat.composer_for(conv), **extra})


def _body(request):
    try:
        return json.loads(request.body or b"{}")
    except ValueError:
        return None


@login_required
def chat_page(request):
    conv = chat.conversation_for(request.user)
    approvals.expire_stale(conv)
    if not conv.messages.exists():
        flows.greet(request.user, conv)
    return render(request, "assistant/chat.html", {
        "messages": conv.messages.all(),
        "composer": chat.composer_for(conv),
        "max_zip_mb": settings.MAX_ZIP_BYTES // (1024 * 1024),
    })


@login_required
@require_POST
def send(request):
    body = _body(request)
    if body is None:
        return HttpResponseBadRequest()
    conv = chat.conversation_for(request.user)
    messages = flows.handle_text(request.user, conv, str(body.get("text", "")), str(body.get("value", "") or ""))
    return _reply(conv, messages)


@sensitive_variables("body", "key")
@sensitive_post_parameters()
@login_required
@require_POST
def submit_key(request):
    body = _body(request) or {}
    key = str(body.get("key", ""))
    conv = chat.conversation_for(request.user)
    if not key.strip():
        return _reply(conv, [chat.bot(conv, "", chat.error("کلید را وارد کنید."))])
    return _reply(conv, flows.connect_key(request.user, conv, key))


@login_required
@require_POST
def upload(request):
    conv = chat.conversation_for(request.user)
    f = request.FILES.get("file")
    if f is None:
        return _reply(conv, [chat.bot(conv, "", chat.error("فایلی دریافت نشد."))])
    return _reply(conv, flows.handle_upload(request.user, conv, f))


@login_required
@require_POST
def approve(request):
    body = _body(request) or {}
    try:
        action_id = int(body.get("action_id"))
    except (TypeError, ValueError):
        return HttpResponseBadRequest()
    decision = "confirm" if body.get("decision") == "confirm" else "cancel"
    conv = chat.conversation_for(request.user)
    return _reply(conv, approvals.decide(request.user, action_id, decision))


@login_required
@require_POST
def reset(request):
    conv = chat.conversation_for(request.user)
    conv.messages.all().delete()
    conv.state = {}
    conv.save(update_fields=["state"])
    return _reply(conv, flows.greet(request.user, conv), reset=True)


@login_required
@require_GET
def stream(request):
    try:
        job = Job.objects.select_related("message").get(
            id=int(request.GET.get("job", "")), pending_action__conversation__user=request.user)
    except (ValueError, Job.DoesNotExist):
        return HttpResponseBadRequest()
    message_id = job.message_id

    def events():
        last = None
        deadline = time.monotonic() + 25 * 60
        while time.monotonic() < deadline:
            msg = Message.objects.filter(id=message_id).first()
            if msg is None:
                return
            done = any(b.get("type") == "progress" and b.get("done") for b in msg.ui)
            payload = json.dumps(chat.render(msg) | {"message_id": msg.id, "done": done}, ensure_ascii=False)
            if payload != last:
                last = payload
                yield f"event: progress\ndata: {payload}\n\n"
            if done:
                return
            yield ": keep-alive\n\n"
            time.sleep(1)

    resp = StreamingHttpResponse(events(), content_type="text/event-stream")
    resp["Cache-Control"] = "no-cache"
    resp["X-Accel-Buffering"] = "no"
    return resp


def signup(request):
    form = UserCreationForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        user = form.save()
        login(request, user)
        return redirect("chat")
    return render(request, "assistant/signup.html", {"form": form})
