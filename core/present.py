"""How things are SHOWN — shared by every page, so the pages can't disagree.

Each helper exists because a page showed something wrong (review of 2026-10-12):
  * clean_title     — raw gradebook names ("…Implications-10122026") broke across lines;
  * absolute_tag    — a judge tag said "Quiz tomorrow" long after that day had passed;
  * settled         — Today hid the work you marked done, the course page called it overdue;
  * upcoming        — Courses said "Nothing dated ahead" with a quiz on the calendar that day.
"""
import re
from datetime import timedelta

from django.utils import timezone

# Trailing date stamps instructors glue onto column names: "-10122026", "- 9/24/26",
# "[9/24]", "[9/22,24]", "- 9/21&23/26", "-992026". Only at the END of the title, so real
# numbers are never touched.
_STAMP = re.compile(r"(?:\s*[-–]\s*|\s+)(?:\d{6,8}|[\d&,]*\d{1,2}/\d[\d/&,]*)\s*$"
                    r"|\s*\[\d{1,2}/[\d/,\s]+\]\s*$")
# "SocialImplications": two glued words, split only inside a long token (keeps "PowerPoint").
_GLUED = re.compile(r"\b(?=\w{14,}\b)(\w*?[a-z]{3})([A-Z][a-z]{3,}\w*)\b")


def clean_title(s):
    s = (s or "").strip()
    for _ in range(2):                       # "…-9/24/26 [9/24]" carries two stamps
        s = _STAMP.sub("", s).strip()
    s = re.sub(r"(?<=\w)- (?=\w)", " - ", s)              # "Week 9- Group" → "Week 9 - Group"
    # "Tools/Ethical&Social" → "Tools / Ethical & Social" — between LETTERS only, so dates
    # and real hyphens ("If-Else", "Self-Check") are left exactly as written.
    s = re.sub(r"(?<=[A-Za-z])\s*([/&])\s*(?=[A-Za-z])", r" \1 ", s)
    s = _GLUED.sub(r"\1 \2", s)
    return re.sub(r"\s{2,}", " ", s).strip(" -–")


def clean_event(summary):
    """A calendar title: clean the item name but keep a trailing "(50 pts)"."""
    m = re.match(r"(.*?)(\s*\(\d+(?:\.\d+)? pts\))?$", summary or "", re.S)
    return clean_title(m.group(1)) + (m.group(2) or "")


_REL = re.compile(r"\b(today|tonight|tomorrow|yesterday)\b", re.I)


def absolute_tag(tag, sent):
    """'Quiz tomorrow, do not be late' (sent Sun 11 Oct) → 'Quiz Mon 12 Oct, do not be late'.
    Words that can't be resolved ("next meeting") are left alone."""
    if not tag or not sent:
        return tag or ""
    base = timezone.localtime(sent).date()

    def day(m):
        w = m.group(1).lower()
        d = base + timedelta(days={"tomorrow": 1, "yesterday": -1}.get(w, 0))
        txt = d.strftime("%a %d %b").replace(" 0", " ")
        return f"{txt} night" if w == "tonight" else txt
    return _REL.sub(day, tag)


def settled(run):
    """(course, title) pairs the LMS load says are settled though Blackboard shows no attempt:
    marked done by you on the calendar (held_by_you) or an identical twin of submitted work."""
    art = (run.artifact or {}) if run else {}
    return {(h.get("course"), h.get("title")) for h in
            (art.get("held_by_you") or []) + (art.get("twins") or [])}


def upcoming(code, now, items, pins):
    """Dated things ahead for ONE course, soonest first, from the gradebook AND the calendar's
    📌 events (quizzes, labs and videos with no gradebook column live only there).
    items: open Item rows; pins: CalEvent rows. Returns [(due, title)]."""
    from .judge import similar
    out = [(i.due, clean_title(i.title)) for i in items if i.course == code and i.due and i.due >= now]
    for e in pins:
        if code not in e.summary or e.is_work or not e.summary.startswith("📌"):
            continue
        due = e.end if e.deadline_at_end else e.start
        if due < now or any(similar(code, i.title, e.summary) for i in items if i.course == code):
            continue
        out.append((due, clean_title(e.summary.split("—", 1)[-1].split(" · via")[0].strip())))
    return sorted(out, key=lambda t: t[0])
