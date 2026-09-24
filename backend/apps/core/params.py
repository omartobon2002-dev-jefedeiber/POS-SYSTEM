"""Query-parameter parsing that fails as a 400, never as a 500.

A malformed `?from=` or `?location=` is a client mistake; letting Python's
ValueError escape turns it into a server error the client cannot act on.
"""
from __future__ import annotations

import uuid

from django.utils import timezone
from rest_framework.exceptions import ValidationError


def parse_datetime_param(value: str | None, name: str):
    if not value:
        return None
    try:
        parsed = timezone.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValidationError({name: "Use an ISO-8601 date or datetime."}) from None
    return parsed if timezone.is_aware(parsed) else timezone.make_aware(parsed)


def parse_int_param(value: str | None, name: str, *, default: int, maximum: int) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except ValueError:
        raise ValidationError({name: "Must be an integer."}) from None
    if number < 1:
        raise ValidationError({name: "Must be at least 1."})
    return min(number, maximum)


def parse_uuid_param(value: str | None, name: str):
    if not value:
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        raise ValidationError({name: "Must be a valid id."}) from None
