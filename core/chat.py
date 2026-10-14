"""The Capture chat (2026-10-13) "the lighter model will keep talking to me … anything
that needs to be judged will wait for sweeps."

  * the student's line is saved as a Capture, so the NEXT SWEEP's judge reads it alongside the LMS
    messages (one request, one pass, no duplicate proposals from two notes about one quiz).
  * Nebula answers right away on Flash-Lite only (500/day, its own bucket); it never touches
    the Flash models the judge prefers.
  * Flash-Lite decides nothing. It may not say anything is on the calendar; it may flag
    `urgent` when a dated thing lands before the next sweep (or within 12 hours), and only then
    does the page offer "Check now" — a judge run that must leave 10 requests per Flash model
    for the sweeps (quota.next_model reserve).

Privacy — the same rule as the judge: only school data reaches the free API. The context is
gradebook items, judged SCHOOL messages (gemini_eligible) and school proposals. Never personal
mail, never classmate DMs, never calendar events.
"""
from datetime import datetime, time as dtime, timedelta

from django.utils import timezone

from . import gemini, quota
from .judge import COURSES
from .models import ChatTurn, Item, Message, Proposal

SWEEP_TIMES = ((6, 0), (7, 30), (10, 30), (13, 30), (16, 30), (19, 0), (21, 0), (23, 30))  # = nebula-sweep.timer
HISTORY = 12                           # chat lines sent back as context

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "reply": {"type": "STRING"},
        "urgent": {"type": "BOOLEAN"},
        "urgent_why": {"type": "STRING"},
    },
    "required": ["reply", "urgent"],
}


def next_sweep(now=None):
    now = timezone.localtime(now or timezone.now())
    for d in range(2):
        day = now.date() + timedelta(days=d)
        for h, m in SWEEP_TIMES:
            t = timezone.make_aware(datetime.combine(day, dtime(h, m)))
            if t > now:
                return t
    return now + timedelta(hours=6)


def _ampm(dt):
    return timezone.localtime(dt).strftime("%a %d %b %I:%M %p").replace(" 0", " ")


def context(now=None):
    """What Flash-Lite may know. School data only (see module docstring)."""
    now = now or timezone.now()
    L = ["Open graded work (Blackboard), soonest first:"]
    for i in Item.objects.filter(submitted=False, due__gte=now - timedelta(days=2),
                                 due__lte=now + timedelta(days=14)).order_by("due")[:20]:
        L.append(f"- {i.course} · {i.title} · due {_ampm(i.due)}" + (f" · {i.pts:g} pts" if i.pts else ""))
    L.append("Dated items Nebula already found (quizzes, exams, deadlines):")
    for p in Proposal.objects.filter(date__gte=now.date(), date__lte=(now + timedelta(days=14)).date()
                                     ).exclude(state="rejected").order_by("date")[:20]:
        when = p.date.strftime("%a %d %b") + (f" {p.time:%I:%M %p}" if p.time else "")
        L.append(f"- {p.course or '—'} · {p.get_kind_display()} · {p.title} · {when} · {p.get_state_display()}")
    L.append("Recent instructor messages (judged relevant):")
    for m in Message.objects.filter(gemini_eligible=True, judge_relevant=True,
                                    when__gte=now - timedelta(days=5)).order_by("-when")[:12]:
        L.append(f"- {m.course or '—'} · {timezone.localtime(m.when):%a %d %b} · {m.judge_tag or m.title}")
    return "\n".join(L)


def system_prompt(now=None):
    now = now or timezone.now()
    sweep = next_sweep(now)
    courses = ", ".join(f"{k} ({v})" for k, v in COURSES.items())
    return f"""You are Nebula, the school assistant inside the student's app. the student is a 1st-year BS Computer Science
student at the University of Baguio, section IAA1. Now: {_ampm(now)} (Asia/Manila).
His courses: {courses}.

the student types notes about what was said in class, corrections, or questions. Reply like a helpful
classmate on a phone: 1–3 short sentences, plain words, 12-hour times. Answer questions from the
school data you are given; if it is not there, say you don't see it — never invent a date, room or
requirement.

YOU DO NOT DECIDE OR WRITE ANYTHING. A separate checker reads the student's notes at the next sweep
({_ampm(sweep)}) and only then proposes calendar items for him to approve. So never say something is
"added", "saved to the calendar" or "scheduled". If his note contains a dated thing, say it will be
checked at {timezone.localtime(sweep).strftime("%I:%M %p").lstrip("0")}.

Set "urgent": true ONLY when his latest message names a quiz, exam, deadline or event that happens
before {_ampm(sweep)} or within 12 hours from now — something that cannot wait for the sweep. Put the
reason in "urgent_why" (e.g. "PROGIT1 quiz 8:00 AM tomorrow"). Otherwise "urgent": false."""


def reply(text, course=""):
    """Flash-Lite's answer to the student's latest line. Returns a saved ChatTurn (role nebula).
    Never raises: a failure becomes an honest line, and the note is still saved for the sweep."""
    now = timezone.now()
    hist = list(ChatTurn.objects.order_by("-created")[:HISTORY])[::-1]
    convo = "\n".join(f"{'the student' if t.role == 'me' else 'Nebula'}: {t.text}" for t in hist)
    user = (f"School data:\n{context(now)}\n\nConversation so far:\n{convo}\n\n"
            f"the student's latest message{f' (course: {course})' if course else ''}:\n{text}")
    model = quota.CHAIN[-1][0]                     # Flash-Lite, always — never the judge's models
    if not quota.available(model):
        return ChatTurn.objects.create(role="nebula", model="", text=(
            f"I can't reply right now (today's Flash-Lite quota is used up). Your note is saved and "
            f"will be checked at {timezone.localtime(next_sweep(now)).strftime('%I:%M %p').lstrip('0')}."))
    try:
        quota.record(model)
        out, _ = gemini.generate_json(model, system_prompt(now), user, SCHEMA, temperature=0.3)
    except gemini.GeminiError as e:
        if e.quota:
            quota.mark_exhausted(model)
        return ChatTurn.objects.create(role="nebula", model=model, text=(
            f"I couldn't reply ({'quota' if e.quota else 'Gemini error'}). Your note is saved and will "
            f"be checked at {timezone.localtime(next_sweep(now)).strftime('%I:%M %p').lstrip('0')}."))
    return ChatTurn.objects.create(role="nebula", model=model, text=(out.get("reply") or "").strip()[:2000]
                                   or "Noted.", urgent=bool(out.get("urgent")),
                                   urgent_why=(out.get("urgent_why") or "")[:200])
