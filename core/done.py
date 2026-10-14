"""Mark a task done from Nebula and close its calendar event by the `calendar` skill's contract.

(2026-10-12) "there's no way to confirm whether a task has been accomplished so it's never
marked correctly in the calendar."

The contract (owned by ~/.claude/skills/calendar/SKILL.md §"Closing one" — mirrored, not invented):
  📌 DUE / OVERDUE  → ✅ SUBMITTED (handed in / uploaded) or ✅ DONE (nothing uploaded) · sage 2
  📌 QUIZ / EXAM / CONFIRM / CANCEL / BUY → ✅ DONE · sage 2
  📌 EVENT → ✅ ATTENDED · 🔴 colour UNCHANGED (peacock 7 stays peacock)
  Every close adds a `human: done` line, so `cal_sync.human_done` HOLDS it: a paper or in-class
  hand-in that Blackboard can't see is never reverted to 📌 OVERDUE by the next `apply`.
  Dropping the 📌 is what stops it reaching the 6 AM iPhone Shortcut. Never deleted.
"""
import re
from datetime import timedelta

from django.utils import timezone

from .models import DoneMark, Item

# Any 📌 label, not a fixed list: hand-written ones exist ("📌 NO DUE DATE:", "📌 NOTICE:", "📌 BRING:")
# and a fixed list closed them as "✅ DONE: 📌 NO DUE DATE: …" (2026-10-13, SOCORN2 Mental Health).
_PIN = re.compile(r"^📌\s*([A-Z][A-Z ]*?)\s*:\s*")


def kind_of(summary):
    m = _PIN.match(summary or "")
    return m.group(1).upper() if m else ""


def choices(summary=""):
    """The words a ✓ tap may offer for this event. A gradebook item with no event offers both."""
    k = kind_of(summary)
    if k == "EVENT":
        return ["ATTENDED"]
    if k == "NOTICE":
        return ["READ"]                       # calendar skill: an announcement acknowledged
    if k in ("DUE", "OVERDUE", "NO DUE DATE", ""):
        return ["SUBMITTED", "DONE"]
    return ["DONE"]                           # QUIZ, EXAM, CONFIRM, CANCEL, BUY, BRING, …


def closed_summary(summary, word):
    rest = _PIN.sub("", summary or "", count=1)
    return f"✅ {word}: {rest}"


def closed_color(summary, color):
    return color if kind_of(summary) == "EVENT" else "2"


def closed_description(desc, word):
    stamp = timezone.localtime().strftime("%a %d %b %Y, %I:%M %p").replace(" 0", " ")
    lines = (desc or "").rstrip()
    if not re.search(r"^human:\s*done\s*$", lines, re.M):
        lines += "\n\nhuman: done"
    return lines + f"\nClosed in Nebula by the student, {stamp} (✅ {word})."


def _service():
    from .calwrite import _service as svc
    return svc()


def find_item_event(svc, item):
    """cal_sync marks its events `bb:<courseId>:<columnId>` in the description. Item.uid is
    `<courseId>:<columnId>`. Look within a week either side of the due date."""
    if not item.due:
        return None
    marker = f"bb:{item.uid}"
    r = svc.events().list(calendarId="primary", singleEvents=True, maxResults=250,
                          timeMin=(item.due - timedelta(days=7)).isoformat(),
                          timeMax=(item.due + timedelta(days=21)).isoformat()).execute()
    for e in r.get("items", []):
        if marker in (e.get("description") or ""):
            return e
    return None


def mark(key, word, svc=None):
    """Close the task KEY (an Item uid or an event id) as WORD. Returns (DoneMark, message).
    Raises on a calendar failure, BEFORE anything is recorded, so a failure never looks done."""
    if word not in ("SUBMITTED", "DONE", "ATTENDED", "READ"):
        raise ValueError(f"unknown word {word!r}")
    if DoneMark.objects.filter(key=key).exists():
        return DoneMark.objects.get(key=key), "Already marked done"
    item = Item.objects.filter(uid=key).first()
    svc = svc or _service()
    if item:
        ev = find_item_event(svc, item)
        course, title = item.course, item.title
    else:
        ev = svc.events().get(calendarId="primary", eventId=key).execute()
        course, title = "", _PIN.sub("", ev.get("summary") or "")
        from .judge import COURSES
        course = next((c for c in COURSES if c in (ev.get("summary") or "")), "")
        if course and title.startswith(course):                 # "MATHMW1 — Coursework…" → "Coursework…"
            title = title[len(course):].lstrip(" —-:")
    if ev is None:
        # No event: nothing to close (the calendar skill never creates one for finished work).
        dm = DoneMark.objects.create(key=key, course=course, title=title, word=word)
        return dm, "Marked done (it had no calendar event)"
    s0, c0, d0 = ev.get("summary") or "", ev.get("colorId") or "", ev.get("description") or ""
    if s0.startswith("✅"):
        dm = DoneMark.objects.create(key=key, course=course, title=title, word=word, event_id=ev["id"],
                                     before_summary=s0, before_color=c0, before_description=d0)
        return dm, "The calendar already had it closed"
    if word not in choices(s0):
        word = choices(s0)[0]
    patch = {"summary": closed_summary(s0, word), "colorId": closed_color(s0, c0) or c0,
             "description": closed_description(d0, word)}
    if not patch["colorId"]:
        patch.pop("colorId")
    svc.events().patch(calendarId="primary", eventId=ev["id"], body=patch).execute()
    back = svc.events().get(calendarId="primary", eventId=ev["id"]).execute()
    bad = [k for k in patch if (back.get(k) or "") != patch[k]]
    if bad:
        raise RuntimeError(f"calendar read-back mismatch on {', '.join(bad)}")
    dm = DoneMark.objects.create(key=key, course=course, title=title, word=word, event_id=ev["id"],
                                 before_summary=s0, before_color=c0, before_description=d0)
    from .ingest import upsert_event
    upsert_event(back)
    return dm, f"✅ {word} — calendar updated"


def undo(dm, svc=None):
    """Put the event back exactly as it was, then forget the mark."""
    if dm.event_id and dm.before_summary and not dm.before_summary.startswith("✅"):
        svc = svc or _service()
        body = {"summary": dm.before_summary, "description": dm.before_description}
        if dm.before_color:
            body["colorId"] = dm.before_color
        from .ingest import upsert_event
        upsert_event(svc.events().patch(calendarId="primary", eventId=dm.event_id, body=body).execute())
    dm.delete()


def done_keys():
    return set(DoneMark.objects.values_list("key", flat=True))
