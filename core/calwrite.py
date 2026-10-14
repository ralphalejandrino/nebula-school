"""Write ONE approved proposal to Google Calendar, by the `calendar` skill's contract.

Conventions (owned by ~/.claude/skills/calendar/SKILL.md — mirrored, not invented):
  📌 DUE / QUIZ / EXAM / BUY / CONFIRM → flamingo 4 · 📌 EVENT → peacock 7 · never unset.
  Reminders: graded → 2 days + 8 hours · prepare/attend → night before (~14 h).
  Read before write: an existing event that day for the same course with ≥2 shared
  words is reported as a duplicate, not created again.
  The description must answer the event on its own: what, where it came from, the quote.
School writes are standing-authorised; Nebula still writes only on your tap.
Only ever INSERTS — never updates, recolours or deletes anything it did not create — with ONE
exception, also on his tap: a proposal that MOVES a date retitles the OLD open 📌 event
"↪ MOVED → <new date>: …", graphite 8, reminders off, original description kept below (#2).
Never deleted.
"""
import re
from datetime import datetime, timedelta

from django.utils import timezone

PREFIX = {"deadline": "📌 DUE", "quiz": "📌 QUIZ", "exam": "📌 EXAM", "event": "📌 EVENT",
          "buy": "📌 BUY", "confirm": "📌 CONFIRM"}
COLOR = {"event": "7"}                                       # everything else flamingo 4
GRADED = {"deadline", "quiz", "exam"}


SNAP = {"deadline", "quiz", "exam", "event", "confirm"}     # not BUY: buying happens before


def class_slot(course, d):
    """(start, end) of COURSE's class block on date D, from the local calendar snapshot."""
    from .models import CalEvent
    if not course or d is None:
        return None
    lo = timezone.make_aware(datetime.combine(d, datetime.min.time()))
    for e in CalEvent.objects.filter(start__gte=lo, start__lt=lo + timedelta(days=1), all_day=False):
        s = e.summary.strip()
        if s.upper().startswith(course) and not s.startswith(("📌", "✅", "✖", "💼")):
            return timezone.localtime(e.start), timezone.localtime(e.end)
    return None


def apply_class_slot(p):
    """An untimed quiz/exam/deadline on a day the course meets → put it on the class slot.
    (2026-10-11) "the tickets are not on the class slot". Returns True if changed."""
    if p.time or p.kind not in SNAP:
        return False
    slot = class_slot(p.course, p.date)
    if not slot:
        return False
    p.time, p.end_time = slot[0].time(), slot[1].time()
    p.verify_note = (p.verify_note + " · " if p.verify_note else "") + "time = class slot"
    return True


def title_for(p):
    pre = PREFIX[p.kind]
    via = f" · via {p.message.short_source}" if p.message and p.message.short_source else ""
    if p.kind in ("buy",):
        return f"{pre}: {p.title}" + (f" ({p.course})" if p.course else "") + via
    return (f"{pre}: {p.course} — {p.title}" if p.course else f"{pre}: {p.title}") + via


def color_for(p):
    return COLOR.get(p.kind, "4")


def reminders_for(p):
    mins = [2880, 480] if p.kind in GRADED else [840]
    return {"useDefault": False, "overrides": [{"method": "popup", "minutes": m} for m in mins]}


def body_for(p, approver="you"):
    m = p.message
    src = (f"{m.source_long}{' — ' + m.author if m.author else ''}, "
           f"{timezone.localtime(m.when):%a %d %b %Y %I:%M %p}" if m and m.when else
           "your own note in Nebula" if p.capture else "unknown")
    lines = [
        f"📣 Source: {src}",
        f"What: {p.title}" + (f" — room {p.room}" if p.room else ""),
        f"Quote: “{p.evidence.strip()}”",
        f"Confidence: {p.get_label_display()}" + ("" if p.verified else f" · ⚠ {p.verify_note}"),
    ]
    if m and m.url:
        lines.append(f"Link: {m.url}")
    if "class slot" in (p.verify_note or ""):
        lines.append(f"⏰ The message gave no time — placed on your {p.course} class slot.")
    elif p.date and not p.time:
        lines.append("⏰ No time was given and no class that day — all-day placeholder.")
    lines.append(f"Written by Nebula after {approver} approved proposal #{p.pk}. nebula:{p.pk}")
    return "\n".join(lines)


def _when(p):
    if p.time:
        start = timezone.make_aware(datetime.combine(p.date, p.time))
        end = (timezone.make_aware(datetime.combine(p.date, p.end_time))
               if p.end_time and p.end_time > p.time else start + timedelta(hours=1))
        return ({"dateTime": start.isoformat(), "timeZone": "Asia/Manila"},
                {"dateTime": end.isoformat(), "timeZone": "Asia/Manila"})
    return ({"date": p.date.isoformat()}, {"date": (p.date + timedelta(days=1)).isoformat()})


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 4}


def _service():
    import gcreds
    from googleapiclient.discovery import build
    return build("calendar", "v3", credentials=gcreds.credentials("calendar", quiet=True),
                 cache_discovery=False)


def find_existing(svc, p):
    lo = timezone.make_aware(datetime.combine(p.date, datetime.min.time()))
    r = svc.events().list(calendarId="primary", timeMin=lo.isoformat(),
                          timeMax=(lo + timedelta(days=1)).isoformat(), singleEvents=True,
                          maxResults=100).execute()
    from .judge import similar
    for e in r.get("items", []):
        s = e.get("summary") or ""
        if f"nebula:{p.pk}" in (e.get("description") or ""):
            return e
        if s.lstrip("✅ ").startswith("💼") or e.get("colorId") in ("1", "3", "8"):
            continue                                   # work tickets and class blocks
        if similar(p.course, p.title, s):
            return e
    return None


def moved_summary(summary, new_date):
    from .done import _PIN
    return f"↪ MOVED → {new_date:%a %m-%d}: {_PIN.sub('', summary or '', count=1)}"


def mark_moved(svc, p):
    """Retitle the event P replaces. Sets p.moved_done; a failure is reported, never raised —
    the new date is already on the calendar and that is the part that matters."""
    try:
        old = svc.events().get(calendarId="primary", eventId=p.replaces_event_id).execute()
        s0 = old.get("summary") or ""
        if not s0.startswith("📌"):
            return                                   # already closed or moved by someone else
        stamp = timezone.localtime().strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
        when = f"{p.date:%a %d %b %Y}" + (f", {p.time:%I:%M %p}".replace(" 0", " ") if p.time else "")
        desc = (f"↪ MOVED to {when}. Live event: {title_for(p)}.\n"
                f"Quote: “{p.evidence.strip()}”\n"
                f"Kept, not deleted. Marked by Nebula after the student approved proposal #{p.pk}, {stamp}."
                f"\n\n--- original ---\n{old.get('description') or ''}")
        patch = {"summary": moved_summary(s0, p.date), "colorId": "8", "description": desc,
                 "reminders": {"useDefault": False, "overrides": []}}
        back = svc.events().patch(calendarId="primary", eventId=p.replaces_event_id, body=patch).execute()
        if back.get("summary") != patch["summary"]:
            raise RuntimeError("read-back mismatch on summary")
        from .ingest import upsert_event
        upsert_event(back)
        p.moved_done = True
    except Exception as e:
        p.error = (p.error + " · " if p.error else "") + f"old event NOT marked moved: {e}"[:300]
    p.save()


def write(p, svc=None, approver="you"):
    """Insert and read back. Sets p.state to written | duplicate | failed. Returns p."""
    if p.kind not in PREFIX or p.date is None:
        p.state, p.error = "failed", "only dated calendar kinds can be written"
        p.save()
        return p
    if apply_class_slot(p):
        p.save()
    svc = svc or _service()
    dup = find_existing(svc, p)
    if dup:
        p.state, p.event_id, p.event_link = "duplicate", dup["id"], dup.get("htmlLink", "")
        p.decided = timezone.now()
        p.save()
        if p.replaces_event_id:                      # the new date was there, the old one wasn't closed
            mark_moved(svc, p)
        return p
    start, end = _when(p)
    ev = {"summary": title_for(p), "description": body_for(p, approver), "start": start, "end": end,
          "colorId": color_for(p), "reminders": reminders_for(p)}
    if p.room:
        ev["location"] = p.room
    made = svc.events().insert(calendarId="primary", body=ev).execute()
    back = svc.events().get(calendarId="primary", eventId=made["id"]).execute()
    # Read-back: the echoed summary, colour and start must be what we sent.
    bad = [k for k in ("summary", "colorId") if back.get(k) != ev[k]]
    if (back.get("start") or {}).get("date", (back.get("start") or {}).get("dateTime", ""))[:10] \
            != p.date.isoformat():
        bad.append("start")
    from .ingest import upsert_event
    upsert_event(back)                       # show it in Nebula now, not at the next sweep
    p.event_id, p.event_link = made["id"], made.get("htmlLink", "")
    p.decided = timezone.now()
    p.state = "failed" if bad else "written"
    p.error = f"read-back mismatch: {', '.join(bad)}" if bad else ""
    p.save()
    if p.replaces_event_id and p.state == "written":
        mark_moved(svc, p)
    return p
