import json
import subprocess
import sys
from collections import OrderedDict
from datetime import datetime, time as dtime, timedelta

from django.conf import settings
from django.templatetags.static import static
from django.contrib import messages as flash
from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from . import ingest, quota
from .judge import COURSES
from .models import CalEvent, Capture, Clarification, Item, Message, Proposal, Run
from . import tutor
from .studypack import materials, tutor_prompt

# Kept for templates that still pass `icons`; the icon itself is drawn by the srcico filter.
SOURCE_ICON = {k: k for k in ("school_mail", "personal_mail", "bb_announce", "classroom",
                              "chat_space", "chat_dm", "fb")}
COV_SOURCES = ["gradebook", "announcements", "contents", "classroom", "chat", "facebook"]
MANAGE = [sys.executable, str(settings.BASE_DIR / "manage.py")]


def _bg(*args):
    """Fire a management command detached, so a phone tap never waits on a 2-minute sweep.
    Demo mode runs nothing: a demo must never reach a real account. Returns whether it ran."""
    if settings.DEMO:
        return False
    log = open(settings.DATA_DIR / "background.log", "a")
    subprocess.Popen(MANAGE + list(args), cwd=settings.BASE_DIR, stdout=log, stderr=log,
                     start_new_session=True)
    return True


def nav_context(request):
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return {"login_username": settings.LOGIN_USERNAME, "demo": settings.DEMO}
    return {"demo": settings.DEMO,
            "nav_pending": Proposal.objects.filter(state="pending").count(),
            "nav_questions": Clarification.objects.filter(answered__isnull=True).count(),
            "nav_running": ingest.is_running(),
            "nav_last": Run.objects.filter(finished__isnull=False).first()}


# ---------------------------------------------------------------- helpers
def _day_bounds(d):
    lo = timezone.make_aware(datetime.combine(d, dtime.min))
    return lo, lo + timedelta(days=1)


def sweep_board(run):
    """SWEEP rows: each source with a status (done / warn / overdue / na) and WHY."""
    rows = []
    if not run:
        return [{"name": "No sweep yet", "glyph": "overdue", "detail": "tap Sweep now"}]
    cov = run.coverage or {}
    courses = [c for c in cov if c != "EVAL"]
    for src in COV_SOURCES:
        cells = {c: cov[c].get(src) for c in courses}
        ok = [c for c, v in cells.items() if v == "OK"]
        unk = [c for c, v in cells.items() if v == "UNKNOWN"]
        na = [c for c, v in cells.items() if v == "n/a"]
        glyph = "overdue" if unk and not ok else "warn" if unk else "done" if ok else "na"
        detail = f"{len(ok)}/{len(ok) + len(unk)} read" + (f" · UNKNOWN: {', '.join(unk)}" if unk else "") \
            + (f" · {len(na)} n/a by design" if na else "")
        rows.append({"name": src.capitalize(), "glyph": glyph, "detail": detail})
    for name, val in (run.steps or {}).items():
        if name == "lms_load":
            continue
        rows.append({"name": name.replace("_", " ").capitalize(),
                     "glyph": "done" if str(val).startswith("ok") else "overdue",
                     "detail": str(val).split("·", 1)[-1].strip()})
    if "lms_load" in (run.steps or {}) and not str(run.steps["lms_load"]).startswith("ok"):
        rows.insert(0, {"name": "LMS load", "glyph": "overdue",
                        "detail": str(run.steps["lms_load"]).split("·", 1)[-1].strip()})
    g = run.gemini or "not run"
    rows.append({"name": "Gemini judge", "glyph": "done" if g.startswith("ok") else "overdue",
                 "detail": g.split("·", 1)[-1].strip() if "·" in g else g})
    for d in run.degraded or []:
        rows.append({"name": "Degraded", "glyph": "overdue", "detail": str(d)[:160]})
    return rows


def notice_groups(now, days=3):
    """NOTICES = what the judge said matters (school only), one row per notice even when it
    came on three channels. Keyword-only hits are kept, but SET ASIDE (collapsed) — the floor
    stays visible without burying the real notices (2026-10-11) "a lot of bloat")."""
    import re as _re
    recent = Message.objects.exclude(source="personal_mail").filter(
        when__gte=now - timedelta(days=days)).order_by("-when")
    groups, order = {}, []
    for m in recent.filter(judge_relevant=True):
        key = _re.sub(r"[^a-z0-9]+", " ", m.judge_tag.lower()).strip()
        if key not in groups:
            groups[key] = {"m": m, "icons": [], "n": 0, "course": m.course}
            order.append(key)
        g = groups[key]
        g["course"] = g["course"] or m.course
        g["n"] += 1
        if m.source not in g["icons"]:
            g["icons"].append(m.source)
    set_aside = [m for m in recent.filter(keyword_hit=True).exclude(judge_relevant=True)[:40]]
    return [groups[k] for k in order], set_aside


def collisions(events):
    """💼 WORK vs school overlaps — the single most useful thing a load surfaces."""
    out = []
    timed = [e for e in events if not e.all_day]
    for a in timed:
        if not a.is_work:
            continue
        for b in timed:
            if b.is_work or b.pk == a.pk:
                continue
            if a.start < b.end and b.start < a.end:
                out.append((a, b))
    return out


# ---------------------------------------------------------------- pages
@login_required
def today(request):
    now = timezone.localtime()
    lo, hi = _day_bounds(now.date())
    run = Run.objects.filter(finished__isnull=False).first()
    due_today = list(Item.objects.filter(submitted=False, due__gte=lo, due__lt=hi))
    # Overdue = the LMS load's own reconciliation: what you marked done on the calendar
    # (held_by_you) and twins are excluded, and only the last 7 days count — older misses
    # are history, still listed on the course page.
    art = (run.artifact or {}) if run else {}
    settled = {(h.get("course"), h.get("title")) for h in
               (art.get("held_by_you") or []) + (art.get("twins") or [])}
    overdue = [i for i in Item.objects.filter(submitted=False, due__lt=now,
                                              due__gte=now - timedelta(days=7)).order_by("-due")
               if (i.course, i.title) not in settled]
    tomorrow_items = list(Item.objects.filter(submitted=False, due__gte=hi, due__lt=hi + timedelta(days=1)))
    tasks = []
    from .judge import similar

    def _cal_pins(lo_, hi_, items):
        """The calendar's 📌 school items in [lo_, hi_) — quizzes, labs and videos with no
        gradebook column live only there. Skips ones already listed from the gradebook."""
        out_ = []
        for e in CalEvent.objects.filter(start__gte=lo_, start__lt=hi_).order_by("start"):
            if not e.summary.startswith("📌") or e.is_work:
                continue
            code = next((c for c in COURSES if c in e.summary), "")
            if any(i.course == code and similar(code, i.title, e.summary) for i in items):
                continue
            out_.append(e)
        return out_

    def _label(e):
        return e.summary.split(":", 1)[-1].split(" · via")[0].strip()

    def _due(e):
        # Only cal_sync's gradebook events end ON the deadline; a class-slot 📌 starts on time.
        return e.end if e.deadline_at_end else e.start
    for i in overdue:
        tasks.append({"pri": "overdue", "text": f"{i.course} {i.title}", "due": i.due, "tag": "overdue",
                      "prompt": tutor.for_item(i)})
    for i in due_today:
        tasks.append({"pri": "due", "text": f"{i.course} {i.title}", "due": i.due, "tag": "today",
                      "prompt": tutor.for_item(i)})
    for e in _cal_pins(lo, hi, overdue + due_today):
        code = next((c for c in COURSES if c in e.summary), "")
        title = e.summary.split("—", 1)[-1].split(" · via")[0].strip()
        tasks.append({"pri": "due", "text": _label(e), "due": _due(e), "tag": "today",
                      "prompt": tutor.prompt(code, title, due=e.start) if code else ""})
    # Tomorrow: every 📌 on the calendar, plus gradebook items not already among them.
    pins_tmr = _cal_pins(hi, hi + timedelta(days=1), [])
    tomorrow = [{"text": _label(e), "due": _due(e)} for e in pins_tmr] + [
        {"text": f"{i.course} — {i.title}", "due": i.due} for i in tomorrow_items
        if not any(similar(i.course, i.title, e.summary) for e in pins_tmr)]
    tomorrow.sort(key=lambda t: t["due"])
    tasks.sort(key=lambda t: (t["tag"] != "overdue", t["due"]))
    # E#: what the judge said matters, plus anything the mail filter calls actionable and
    # still unread. Receipts and digests stay in the Inbox, counted, not listed here.
    notices, set_aside = notice_groups(now)
    import re as _re
    shown = {_re.sub(r"[^a-z0-9]+", " ", g["m"].judge_tag.lower()).strip() for g in notices}
    week = list(Message.objects.filter(source="school_mail", when__gte=now - timedelta(days=3))
                .order_by("-when")[:120])
    mails = [m for m in week if (m.judge_relevant or (m.rule_actionable and m.unread))
             and _re.sub(r"[^a-z0-9]+", " ", m.judge_tag.lower()).strip() not in shown][:10]
    mail_others = len(week) - len(mails)
    personal_unread = Message.objects.filter(source="personal_mail", unread=True).count()
    agenda = CalEvent.objects.filter(end__gte=now, start__lt=now + timedelta(days=14))
    ctx = {
        "now": now, "run": run, "board": sweep_board(run),
        "tasks": tasks, "tomorrow": tomorrow,
        "proposals": Proposal.objects.filter(state="pending").select_related("message"),
        "questions": Clarification.objects.filter(answered__isnull=True)[:10],
        "notices": notices, "set_aside": set_aside, "mails": mails, "mail_others": mail_others,
        "personal_unread": personal_unread,
        "agenda": _group_by_day(agenda), "clashes": collisions(list(agenda)),
        "quota": quota.summary(), "icons": SOURCE_ICON,
    }
    return render(request, "core/today.html", ctx)


def _group_by_day(events):
    days = OrderedDict()
    for e in events:
        days.setdefault(timezone.localtime(e.start).date(), []).append(e)
    return days


@login_required
def inbox(request):
    src, course = request.GET.get("src", ""), request.GET.get("course", "")
    qs = Message.objects.all()
    if src:
        qs = qs.filter(source=src)
    if course:
        qs = qs.filter(course=course)
    only = request.GET.get("only")
    rows = list(qs.order_by("-when")[:200])
    if only == "matters":
        rows = [m for m in rows if m.shows_in_brief]
    sel = None
    if request.GET.get("m"):
        sel = Message.objects.filter(pk=request.GET["m"]).first()
    return render(request, "core/inbox.html", {
        "rows": rows, "src": src, "course": course, "only": only, "sel": sel,
        "gemini_prompt": tutor.for_message(sel) if sel and sel.gemini_eligible else "",
        "sources": Message.SOURCES, "courses": list(COURSES), "icons": SOURCE_ICON})


@login_required
def message(request, pk):
    m = get_object_or_404(Message, pk=pk)
    return render(request, "core/message.html", {
        "m": m, "icons": SOURCE_ICON, "props": m.proposal_set.all(),
        "gemini_prompt": tutor.for_message(m) if m.gemini_eligible else ""})


@login_required
def calendar(request):
    now = timezone.localtime()
    evs = list(CalEvent.objects.filter(end__gte=now - timedelta(hours=2),
                                       start__lt=now + timedelta(days=14)))
    return render(request, "core/calendar.html", {
        "days": _group_by_day(evs), "clashes": collisions(evs), "now": now})


@login_required
def courses(request):
    now = timezone.now()
    cards = []
    for code, name in COURSES.items():
        nxt = Item.objects.filter(course=code, submitted=False, due__gte=now).order_by("due").first()
        cards.append({"code": code, "name": name, "next": nxt,
                      "open": Item.objects.filter(course=code, submitted=False, due__gte=now).count(),
                      "recent": Message.objects.filter(course=code,
                                                       when__gte=now - timedelta(days=7)).count()})
    return render(request, "core/courses.html", {"cards": cards})


@login_required
def course(request, code):
    if code not in COURSES:
        raise Http404
    now = timezone.now()
    pack = settings.DATA_DIR / "packs" / f"{code}.status.json"
    return render(request, "core/course.html", {
        "code": code, "name": COURSES[code],
        # Open work first (soonest due), then overdue, then submitted — dimmed, at the bottom.
        "items": [(i, tutor.for_item(i) if not i.submitted else "") for i in sorted(
            Item.objects.filter(course=code).filter(due__gte=now - timedelta(days=14)),
            key=lambda i: (i.submitted, i.due < now, i.due if i.due >= now else now - (i.due - now)))],
        "msgs": Message.objects.filter(course=code).order_by("-when")[:25],
        "props": [(p, tutor.for_proposal(p)) for p in
                  Proposal.objects.filter(course=code).exclude(state="rejected").order_by("-created")[:10]],
        "materials": materials(code), "prompt": tutor_prompt(code),
        "pack": json.loads(pack.read_text()) if pack.exists() else None,
        "icons": SOURCE_ICON, "now": now})


@login_required
def capture(request):
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        if text:
            Capture.objects.create(text=text, course=request.POST.get("course", ""))
            if not ingest.is_running():
                _bg("judge", "--captures-only")
            flash.success(request, "Saved — Gemini will turn it into a proposal shortly.")
        return redirect("capture")
    return render(request, "core/capture.html", {
        "caps": Capture.objects.all()[:30], "courses": list(COURSES)})


# ---------------------------------------------------------------- actions
@login_required
@require_POST
def proposal_decide(request, pk, verdict):
    if verdict not in ("approve", "reject"):
        raise Http404
    p = get_object_or_404(Proposal, pk=pk, state="pending")
    if verdict == "reject":
        p.state, p.decided = "rejected", timezone.now()
        p.save()
        flash.info(request, f"Rejected: {p.title}")
    elif p.kind == "notice" or p.date is None:
        p.state, p.decided = "approved", timezone.now()
        p.save()
        flash.info(request, "Noted (undated — nothing to put on the calendar).")
    else:
        from . import calwrite
        try:
            calwrite.write(p)
            msg = {"written": "On your calendar", "duplicate": "Already on your calendar — not duplicated",
                   "failed": f"Write failed: {p.error}"}[p.state]
            (flash.success if p.state != "failed" else flash.error)(request, f"{msg}: {p.title}")
        except Exception as e:
            p.state, p.error = "failed", f"{type(e).__name__}: {e}"[:400]
            p.save()
            flash.error(request, f"Calendar write failed — {p.error}")
    return redirect(request.POST.get("next") or "today")


@login_required
@require_POST
def answer(request, pk):
    c = get_object_or_404(Clarification, pk=pk)
    c.answer = request.POST.get("answer", "").strip()
    c.answered = timezone.now() if c.answer else None
    c.save()
    if c.answer:
        Capture.objects.create(text=f"Answer to “{c.question}”: {c.answer}")
        flash.success(request, "Answer saved.")
    return redirect(request.POST.get("next") or "today")


@login_required
@require_POST
def sweep_now(request):
    if ingest.is_running():
        flash.info(request, "A sweep is already running.")
    elif not _bg("sweep", "--trigger", "button"):
        flash.info(request, "Demo mode — sweeps are switched off.")
    else:
        flash.success(request, "Sweep started — about 2 minutes. This page refreshes itself.")
    return redirect(request.POST.get("next") or "today")


@login_required
@require_POST
def build_pack(request, code):
    if code not in COURSES:
        raise Http404
    if not _bg("studypack", code):
        flash.info(request, "Demo mode — study packs are switched off.")
    else:
        flash.success(request, f"Building the {code} study pack — it lands on Drive in about a minute.")
    return redirect("course", code=code)


@login_required
def status(request):
    run = Run.objects.first()
    return JsonResponse({"running": ingest.is_running(),
                         "last": run.started.isoformat() if run else None,
                         "finished": bool(run and run.finished)})


# ---------------------------------------------------------------- PWA
def manifest(request):
    data = {"id": "/", "name": "Nebula", "short_name": "Nebula", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#0b1020", "theme_color": "#1d4ed8",
            "description": "School, in one place.",
            "icons": [{"src": static("core/icon-192.png"), "sizes": "192x192", "type": "image/png",
                       "purpose": "any maskable"},
                      {"src": static("core/icon-512.png"), "sizes": "512x512", "type": "image/png",
                       "purpose": "any"}]}
    return HttpResponse(json.dumps(data), content_type="application/manifest+json")


def touch_icon(request, size="180"):
    """/favicon.ico and /apple-touch-icon*.png — devices ask for these by fixed name; answering
    them (instead of 404) means nothing can fall back to an old, cached 'N' icon."""
    f = settings.BASE_DIR / "core" / "static" / "core" / f"icon-{size}.png"
    return HttpResponse(f.read_bytes(), content_type="image/png",
                        headers={"Cache-Control": "public, max-age=86400"})


def service_worker(request):
    js = (settings.BASE_DIR / "core" / "static" / "core" / "sw.js").read_text()
    return HttpResponse(js, content_type="application/javascript",
                        headers={"Service-Worker-Allowed": "/", "Cache-Control": "no-cache"})
