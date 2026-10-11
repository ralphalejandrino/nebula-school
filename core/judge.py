"""Send NEW eligible school messages to free Gemini; store tags, proposals, questions.

Guards (each one is a failure that has really happened):
  * privacy  — build_payload() re-checks every row and FAILS CLOSED (PrivacyViolation);
  * evidence — a proposal's quote must appear verbatim in its source, else `verified=False`
               (quote-the-deadline-verbatim: "preferably this week" once became a deadline);
  * floor    — keyword hits stay visible whatever the model says (Message.shows_in_brief);
  * UNKNOWN  — no model, a network error or a bad reply is reported, never "nothing new".
"""
import logging
import re
import unicodedata
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from . import gemini, quota
from .models import Capture, Clarification, Item, Message, Proposal
from .privacy import PrivacyViolation, eligible
from .present import absolute_tag

log = logging.getLogger("nebula")

COURSES = {
    "PROGIT1": "Computer Programming", "HUMCOM1": "Human Computer Interaction",
    "INTRCS1": "Introduction to Computing Systems", "MATHMW1": "Mathematics in the Modern World",
    "LITERA1": "Living in the IT Era", "ARTAPP1": "Art Appreciation", "NSTPRO1": "NSTP 1",
    "PATHFT1": "Movement Competency Training (PE)", "CMPTRB1": "Basic Computer Troubleshooting",
    "SOCORN2": "Social Orientation",
}
BODY_CAP = 6000            # chars per message; long mail threads are mostly quoted history
BATCH_CHARS = 350_000      # ≈ 90K tokens, well inside 250K TPM
MAX_AGE_DAYS = 14

SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "messages": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "id": {"type": "STRING"}, "relevant": {"type": "BOOLEAN"},
            "tag": {"type": "STRING"}}, "required": ["id", "relevant", "tag"]}},
        "proposals": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "source_id": {"type": "STRING"},
            "kind": {"type": "STRING", "enum": ["deadline", "quiz", "exam", "event", "buy",
                                                "confirm", "notice"]},
            "course": {"type": "STRING"}, "title": {"type": "STRING"},
            "date": {"type": "STRING"}, "time": {"type": "STRING"},
            "end_time": {"type": "STRING"}, "room": {"type": "STRING"},
            "evidence": {"type": "STRING"},
            "label": {"type": "STRING", "enum": ["stated", "extrapolated", "unknown"]}},
            "required": ["source_id", "kind", "course", "title", "date", "time", "evidence",
                         "label"]}},
        "clarifications": {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {
            "source_id": {"type": "STRING"}, "question": {"type": "STRING"}},
            "required": ["question"]}},
    },
    "required": ["messages", "proposals", "clarifications"],
}


# ---------------------------------------------------------------- payload
def _system():
    return (Path(settings.BASE_DIR) / "prompts" / "judge.md").read_text()


def _known_block():
    now = timezone.now()
    lines = []
    for i in Item.objects.filter(submitted=False, due__gte=now - timedelta(days=2))[:40]:
        lines.append(f"- {i.course} | {i.title} | due {timezone.localtime(i.due):%a %Y-%m-%d %H:%M}")
    for p in Proposal.objects.filter(state__in=["pending", "approved", "written", "duplicate"],
                                     date__gte=now.date() - timedelta(days=2)):
        lines.append(f"- {p.course} | {p.get_kind_display()} {p.title} | {p.date} {p.time or ''}")
    return "\n".join(lines) or "(none)"


def build_payload(messages, captures, today=None, known=None):
    """(system, user, idmap). 🔴 Re-checks privacy on every row and fails CLOSED."""
    idmap, blocks = {}, []
    for m in messages:
        if not (m.gemini_eligible and eligible(m.source, m.instructor_thread)):
            raise PrivacyViolation(f"{m.source} row {m.pk} is not eligible for the free API")
        mid = f"M{m.pk}"
        idmap[mid] = m
        when = timezone.localtime(m.when).strftime("%a %Y-%m-%d %H:%M") if m.when else "?"
        blocks.append(f"### {mid} | {m.get_source_display()} | course {m.course or '-'} | "
                      f"from {m.author or '-'} | sent {when}\n"
                      f"{(m.title + chr(10)) if m.source in ('school_mail', 'bb_announce') else ''}"
                      f"{m.body[:BODY_CAP]}")
    for c in captures:
        cid = f"N{c.pk}"
        idmap[cid] = c
        who = "the student's answer to a question" if c.kind == "answer" else "own note"
        blocks.append(f"### {cid} | {who} | course {c.course or '-'} | written "
                      f"{timezone.localtime(c.created):%a %Y-%m-%d %H:%M}\n{c.text}")
    today = timezone.localtime(today) if today else timezone.localtime()
    user = (f"Today is {today:%A %Y-%m-%d %H:%M} (Asia/Manila).\n\n"
            "## Courses\n" + "\n".join(f"- {k}: {v}" for k, v in COURSES.items()) +
            "\n\n## KNOWN (already tracked — do not re-propose)\n" +
            (known if known is not None else _known_block()) +
            "\n\n## Messages\n\n" + "\n\n".join(blocks))
    return _system(), user, idmap


# ---------------------------------------------------------------- evidence
_DATEISH = re.compile(
    r"\b(mon|tue|wed|thu|fri|sat|sun)\w*\b|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)\w*\b|"
    r"\d{1,2}[/:-]\d{1,2}|\b\d{1,2}\s*(am|pm|nn|noon)\b|\b(today|tomorrow|tonight|next|later|"
    r"noon|midnight|deadline|due)\b", re.I)


def _norm(s):
    s = unicodedata.normalize("NFKC", s or "")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


def verify(evidence, source_text, has_date):
    """(ok, note). The quote must be IN the source, and a dated claim must quote a date."""
    ev = _norm(evidence).strip(' ."\'')
    if len(ev) < 6:
        return False, "evidence too short to check"
    if ev not in _norm(source_text):
        return False, "evidence NOT found verbatim in the source"
    if has_date and not _DATEISH.search(ev):
        return False, "evidence quotes no date/time words"
    return True, "evidence found verbatim"


def _parse_date(s):
    try:
        return datetime.strptime((s or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _parse_time(s):
    s = (s or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).time()
        except ValueError:
            pass
    return None


# ---------------------------------------------------------------- run
def pending_work(max_age_days=MAX_AGE_DAYS, include_messages=True):
    cutoff = timezone.now() - timedelta(days=max_age_days)
    msgs = [] if not include_messages else list(Message.objects.filter(gemini_eligible=True, judged_at__isnull=True)
                .filter(Q(when__gte=cutoff) | Q(when__isnull=True)).order_by("when"))
    caps = list(Capture.objects.filter(judged_at__isnull=True))
    return msgs, caps


def _batches(msgs, caps):
    batch, size = [], 0
    for m in msgs:
        n = min(len(m.body), BODY_CAP) + 200
        if batch and size + n > BATCH_CHARS:
            yield batch, []
            batch, size = [], 0
        batch.append(m)
        size += n
    yield batch, caps


class NoModel(RuntimeError):
    """No free model answered — the caller reports UNKNOWN, never 'nothing new'."""


def call(system, user, floor_only=False):
    """Walk the free chain: 429 → exhausted for today, 5xx/timeout → next model this call,
    other errors → stop. Returns (reply, model) or raises NoModel."""
    skip, last_err = set(), ""
    while True:
        model = quota.next_model(floor_only=floor_only, skip=skip)
        if model is None:
            raise NoModel(f"no model answered ({last_err[:160] or 'every free model is out of quota today'})")
        try:
            quota.record(model)
            reply, _usage = gemini.generate_json(model, system, user, SCHEMA)
            return reply, model
        except gemini.GeminiError as e:
            log.warning("gemini %s failed: %s", model, e)
            last_err = f"{model}: {str(e)[:120]}"
            if e.quota:
                quota.mark_exhausted(model)
            elif e.status in (500, 502, 503, 504) or e.status is None:
                skip.add(model)
            else:
                raise NoModel(last_err) from None


def run(max_age_days=MAX_AGE_DAYS, floor_only=False, include_messages=True):
    """Judge everything pending. Returns a status string for Run.gemini."""
    msgs, caps = pending_work(max_age_days, include_messages)
    if not msgs and not caps:
        return "ok · nothing new to judge"
    used, made, asked = [], 0, 0
    for bmsgs, bcaps in _batches(msgs, caps):
        if not bmsgs and not bcaps:
            continue
        system, user, idmap = build_payload(bmsgs, bcaps)
        try:
            reply, model = call(system, user, floor_only=floor_only)
        except NoModel as e:
            return f"UNKNOWN · {e} — {len(msgs)} message(s) NOT judged (keyword floor still shown)"
        used.append(model)
        made_b, asked_b = store(reply, idmap, model)
        made += made_b
        asked += asked_b
    return f"ok · {', '.join(sorted(set(used)))} · {len(msgs)} msg · {made} proposal(s) · {asked} question(s)"


def store(reply, idmap, model):
    now = timezone.now()
    for r in reply.get("messages", []):
        obj = idmap.get(r.get("id"))
        if isinstance(obj, Message):
            obj.judge_relevant = bool(r.get("relevant"))
            # "Quiz tomorrow" goes stale at midnight; pin relative days to the message's date.
            obj.judge_tag = absolute_tag((r.get("tag") or "")[:120], obj.when) if r.get("relevant") else ""
            obj.judge_model = model
    for obj in idmap.values():                     # judged even if the model skipped it
        obj.judged_at = now
        obj.save()

    made = 0
    tracked = {}                                    # capture pk -> titles already tracked
    for p in reply.get("proposals", []):
        src = idmap.get(p.get("source_id"))
        if src is None:
            continue
        text = src.body if isinstance(src, Message) else src.text
        d, t = _parse_date(p.get("date")), _parse_time(p.get("time"))
        ok, note = verify(p.get("evidence", ""), text, has_date=bool(d))
        course = (p.get("course") or "").upper().strip()
        course = course if course in COURSES else ""
        if _duplicate(course, p.get("kind"), d, p.get("title", "")):
            if isinstance(src, Capture):
                tracked.setdefault(src.pk, []).append((p.get("title") or "").strip())
            continue
        from .calwrite import apply_class_slot
        prop = Proposal(
            message=src if isinstance(src, Message) else None,
            capture=src if isinstance(src, Capture) else None,
            kind=p.get("kind") or "notice", course=course,
            title=(p.get("title") or "")[:200], date=d, time=t,
            end_time=_parse_time(p.get("end_time")), room=(p.get("room") or "")[:80],
            evidence=p.get("evidence") or "", label=p.get("label") or "unknown",
            verified=ok, verify_note=note, model=model)
        apply_class_slot(prop)
        prop.save()
        made += 1
    # A note must never just vanish: say what became of it (Capture tab, 2026-10-12).
    for obj in idmap.values():
        if isinstance(obj, Capture):
            n = obj.proposal_set.count()
            dup = tracked.get(obj.pk)
            obj.outcome = (f"{n} proposal{'s' if n != 1 else ''}" if n else
                           f"already tracked: {', '.join(dup)}"[:200] if dup else
                           "kept as context" if obj.kind == "answer" else "nothing dated in it")
            obj.save(update_fields=["outcome"])
    asked = 0
    for c in reply.get("clarifications", []):
        q = (c.get("question") or "").strip()
        if not q or Clarification.objects.filter(question__iexact=q).exists():
            continue
        src = idmap.get(c.get("source_id"))
        Clarification.objects.create(question=q, message=src if isinstance(src, Message) else None)
        asked += 1
    return made, asked


def _tokens(s):
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if len(w) >= 4}


# A shared "kind" word is strong evidence two titles are the same thing on the same day.
KIND_WORDS = {"quiz", "exam", "presentation", "report", "reporting", "activity", "video",
              "assembly", "paper", "lab", "midterm", "lecture"}


def similar(course, title, other):
    """Same course named in `other` AND (≥2 shared words, or 1 shared word that is a kind word)."""
    if course and course not in (other or "").upper():
        return False
    shared = _tokens(title) & _tokens(other)
    return len(shared) >= 2 or bool(shared & KIND_WORDS)


def on_calendar(course, d, title):
    """LOCAL check against the calendar snapshot. Never sent anywhere (privacy rule)."""
    from .models import CalEvent
    lo = timezone.make_aware(datetime.combine(d, dtime.min))
    for e in CalEvent.objects.filter(start__gte=lo, start__lt=lo + timedelta(days=1)):
        if e.is_class or e.is_work:
            continue
        if similar(course, title, e.summary):
            return e
    return None


def _duplicate(course, kind, d, title):
    """Same course + date + ≥2 shared words with a live proposal or an Item due that day."""
    if d is None:
        return Proposal.objects.filter(course=course, kind=kind, date__isnull=True,
                                       title__iexact=title).exists()
    if on_calendar(course, d, title):
        return True
    tt = _tokens(title)
    for p in Proposal.objects.filter(course=course, date=d).exclude(state="rejected"):
        if len(tt & _tokens(p.title)) >= 2 or p.title.lower() == title.lower():
            return True
    lo = timezone.make_aware(datetime.combine(d, dtime.min))
    for i in Item.objects.filter(course=course, due__gte=lo, due__lt=lo + timedelta(days=1)):
        if len(tt & _tokens(i.title)) >= 2:
            return True
    return False
