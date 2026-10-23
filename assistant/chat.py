"""Helpers for writing messages into the user's single conversation thread."""

from django.template.loader import render_to_string

from .models import Conversation, Message

TEXT_COMPOSER = {"mode": "text", "placeholder": "هر سؤالی داری بپرس...", "label": "پیام", "attach": True}
KEY_COMPOSER = {"mode": "password", "placeholder": "کلید را اینجا بچسبانید", "label": "کلید API لیارا", "attach": False}


def conversation_for(user) -> Conversation:
    conv, _ = Conversation.objects.get_or_create(user=user)
    return conv


def bot(conv, text="", *ui) -> Message:
    return Message.objects.create(conversation=conv, role=Message.Role.BOT, text=text, ui=[b for b in ui if b])


def user_says(conv, text) -> Message:
    return Message.objects.create(conversation=conv, role=Message.Role.USER, text=text)


def buttons(*options):
    """buttons(("label", "value"), ...) -> a buttons block."""
    return {"type": "buttons", "options": [{"label": label, "value": value} for label, value in options]}


def note(text, tone="success"):
    return {"type": "note", "tone": tone, "text": text}


def error(text, next_step=""):
    return {"type": "error", "text": text, "next": next_step}


def render(message) -> dict:
    return {"id": message.id, "html": render_to_string("assistant/_message.html", {"m": message})}


def set_state(conv, **changes):
    state = dict(conv.state or {})
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    conv.state = state
    conv.save(update_fields=["state"])


def composer_for(conv) -> dict:
    return KEY_COMPOSER if (conv.state or {}).get("awaiting_key") else TEXT_COMPOSER
