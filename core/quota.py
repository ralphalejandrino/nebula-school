"""Which free Gemini model may take the next request.

Limits measured on a free-tier AI Studio project, 2026-10-11 12:39 PM:
every 3.x Flash 5 RPM / 20 RPD (EACH model has its own 20), 3.5 Flash-Lite 15 RPM /
500 RPD. Google publishes no table and changes them silently — re-read
aistudio.google.com/rate-limit ("All models") before trusting these.

RPD resets at midnight PACIFIC time, so days are counted in America/Los_Angeles.
"""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from django.db import transaction

from .models import QuotaUse

PACIFIC = ZoneInfo("America/Los_Angeles")

# Smartest first. Flash-Lite is the floor: weakest, but 500/day.
CHAIN = [
    ("gemini-3.8-flash", 5, 20),
    ("gemini-3.7-flash", 5, 20),
    ("gemini-3.6-flash", 5, 20),
    ("gemini-3.5-flash", 5, 20),
    ("gemini-3.5-flash-lite", 15, 500),
]
LIMITS = {m: (rpm, rpd) for m, rpm, rpd in CHAIN}


def pacific_day(now=None):
    return (now or datetime.now(PACIFIC)).astimezone(PACIFIC).date()


def _row(model, day):
    row, _ = QuotaUse.objects.get_or_create(model=model, day=day)
    return row


def available(model, now_s=None, day=None, reserve=0):
    """`reserve`: leave this many requests untouched for the sweeps (the chat's "Check now"
    passes 10, so a chatty day can never push the sweep's judge off the Flash models)."""
    rpm, rpd = LIMITS[model]
    row = _row(model, day or pacific_day())
    now_s = now_s or time.time()
    recent = [t for t in row.recent if now_s - t < 60]
    return (not row.exhausted) and row.count < rpd - reserve and len(recent) < rpm


def next_model(now_s=None, day=None, floor_only=False, skip=(), reserve=0):
    """First model in the chain with headroom, or None (→ the sweep shows UNKNOWN).
    `skip`: models that failed transiently (503 overload) in THIS run only."""
    chain = CHAIN[-1:] if floor_only else CHAIN
    for m, _, _ in chain:
        if m in skip:
            continue
        floor = m == CHAIN[-1][0]                   # Flash-Lite: 500/day, no reserve needed
        if available(m, now_s, day, 0 if floor else reserve):
            return m
    return None


@transaction.atomic
def record(model, now_s=None, day=None):
    row = _row(model, day or pacific_day())
    now_s = now_s or time.time()
    row.count += 1
    row.recent = [t for t in row.recent if now_s - t < 60] + [now_s]
    row.save()


@transaction.atomic
def mark_exhausted(model, day=None):
    """Google answered 429 / RESOURCE_EXHAUSTED: believe it over our own count."""
    row = _row(model, day or pacific_day())
    row.exhausted = True
    row.save()


def summary(day=None):
    d = day or pacific_day()
    out = []
    for m, rpm, rpd in CHAIN:
        row = QuotaUse.objects.filter(model=m, day=d).first()
        used = row.count if row else 0
        out.append({"model": m, "used": used, "limit": rpd,
                    "exhausted": bool(row and row.exhausted)})
    return out
