"""One-tap handoff of ONE task to the Gemini app (your Gemini app plan — no API quota).

(2026-10-11) "if there's a task for a subject … click on it and it transfers me to
Gemini with all the context it needs." The prompt carries the context INLINE (task, due,
points, the instructor's own words, the course's Drive files), so it works without a
study pack. School data only — the same rule as the judge.
"""
from django.utils import timezone

from .judge import COURSES, similar
from .models import Message
from .studypack import materials

BODY_CAP = 1500          # per quoted message — enough for instructions, short enough to paste


def _when(dt):
    return timezone.localtime(dt).strftime("%a %d %b, %I:%M %p").replace(" 0", " ") if dt else "no due date"


GENERIC = {"week", "group", "class", "quiz", "activity", "lesson", "module", "students", "student",
           "please", "today", "tomorrow", "submit", "submission", "good", "morning", "afternoon",
           "evening", "thank", "thanks", "your", "will", "with", "this", "that", "from", "have",
           "there", "about", "everyone", "reminder", "classroom", "notes", "review", "prepare",
           "inside", "posted", "new", "announcement", "assignment", "blackboard", "grade", "grades"}


def _words(s):
    import re
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 4} - GENERIC


def _related(course, title, extra=None, limit=3):
    """The instructor's own messages about THIS task: ranked by distinctive shared words.
    Untagged school email counts when it names the course (the Week 9 notice came that way)."""
    from django.db.models import Q
    want = _words(title)
    scored = []
    pool = Message.objects.filter(gemini_eligible=True).filter(
        Q(course=course) | Q(course="", body__icontains=course) | Q(course="", title__icontains=course)
        | Q(course="", source="school_mail")).order_by("-when")[:300]
    for m in pool:
        if extra is not None and m.pk == extra.pk:
            continue
        hay = _words(f"{m.title} {m.judge_tag} {m.body[:1500]}")
        if m.course != course and course.lower() not in f"{m.title} {m.body}".lower() \
                and COURSES.get(course, "~").lower() not in f"{m.title} {m.body}".lower():
            continue
        score = len(want & hay)
        if score >= 2:
            scored.append((score, m.when or timezone.now(), m))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    if scored:                                    # weak tag-alongs share only a word or two
        best = scored[0][0]
        scored = [t for t in scored if t[0] * 2 >= best]
    out = [extra] if extra is not None else []
    return out + [m for _, _, m in scored[:limit - len(out)]]


def prompt(course, title, due=None, pts=None, message=None, evidence=""):
    name = COURSES.get(course, course)
    lines = [f"Help me with a task for my {course} ({name}) class.", "",
             f"Task: {title}", f"Due: {_when(due) if due else 'not stated'}"]
    if pts:
        lines.append(f"Worth: {pts:g} points")
    if evidence:
        lines.append(f"The key line: “{evidence}”")
    msgs = _related(course, title, extra=message)
    if msgs:
        lines += ["", "What my instructor posted about it:"]
        for m in msgs:
            when = timezone.localtime(m.when).strftime("%a %d %b") if m.when else ""
            body = " ".join(m.body.split())[:BODY_CAP]
            lines.append(f"- ({m.source_long}{', ' + m.author if m.author else ''}, {when}) {body}")
    mats = [t for t, _ in materials(course) if "folder" not in t]
    if mats:
        lines += ["", f"My course files are on @Google Drive in school-out/{course}/materials: "
                      + ", ".join(mats[:15]) + ". Open the ones that match this task."]
    lines += ["", "First tell me exactly what is being asked and what I have to submit or bring. "
                  "Then help me work through it step by step, checking my answers against the "
                  "course files. Ask me before writing anything final for me."]
    return "\n".join(lines)


def for_item(i):
    return prompt(i.course, i.title, due=i.due, pts=i.pts)


def for_proposal(p):
    due = None
    if p.date:
        from datetime import datetime, time
        due = timezone.make_aware(datetime.combine(p.date, p.time or time(23, 59)))
    return prompt(p.course, p.title, due=due, message=p.message, evidence=p.evidence)


def for_message(m):
    return prompt(m.course or "", m.judge_tag or m.title or "this announcement", message=m)
