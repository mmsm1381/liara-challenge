"""Short-lived encrypted storage for env var values the user types in chat.

The plan says app env var values are not stored. Values for a deploy must
survive until the worker runs the approved job, so they are kept encrypted
for at most the approval window plus the job, then deleted. Everything that
reads or writes them goes through this module.
"""

from datetime import timedelta

from django.conf import settings
from django.utils import timezone

from . import crypto
from .models import HeldSecret

# Long enough to finish the remaining questions and reach the approval card.
UNATTACHED_TTL = timedelta(minutes=30)


def hold(conv, name, value):
    HeldSecret.objects.filter(conversation=conv, action__isnull=True, name=name).delete()
    HeldSecret.objects.create(conversation=conv, name=name, encrypted_value=crypto.encrypt(value),
                              expires_at=timezone.now() + UNATTACHED_TTL)


def pending_names(conv):
    return list(HeldSecret.objects.filter(conversation=conv, action__isnull=True).values_list("name", flat=True))


def attach(conv, action, names=None):
    """Bind the conversation's unattached values to an approval; they expire with it."""
    qs = HeldSecret.objects.filter(conversation=conv, action__isnull=True)
    if names is not None:
        qs = qs.filter(name__in=names)
    qs.update(action=action, expires_at=action.expires_at + timedelta(seconds=settings.APPROVAL_TTL_SECONDS))


def values_for(action) -> dict:
    """Decrypted values for the job. The caller writes them to Liara and drops them."""
    return {s.name: crypto.decrypt(s.encrypted_value) for s in HeldSecret.objects.filter(action=action)}


def discard(action=None, conv=None):
    if action is not None:
        HeldSecret.objects.filter(action=action).delete()
    if conv is not None:
        HeldSecret.objects.filter(conversation=conv, action__isnull=True).delete()


def purge_expired():
    HeldSecret.objects.filter(expires_at__lte=timezone.now()).delete()
