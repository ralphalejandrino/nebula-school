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
from .models import CalEvent, Capture, ChatTurn, Clarification, DoneMark, Item, Message, Proposal, Run
from . import done as donemod, tutor
from .present import clean_title, settled, upcoming
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
            "nav_last": Run.objects.filter(finished__isnull=False).first(),
            "nav_dead": ingest.dead_runs()}


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
    done_by_you = settled(run)
    marked = donemod.done_keys()             # ✓ tapped in Nebula (2026-10-12)
    due_today = [i for i in due_today if i.uid not in marked]
    overdue = [i for i in Item.objects.filter(submitted=False, due__lt=now,
                                              due__gte=now - timedelta(days=7)).order_by("-due")
               if (i.course, i.title) not in done_by_you and i.uid not in marked]
    tomorrow_items = [i for i in Item.objects.filter(submitted=False, due__gte=hi, due__lt=hi + timedelta(days=1))
                      if i.uid not in marked]
    tasks = []
    from .judge import similar

    def _cal_pins(lo_, hi_, items):
        """The calendar's 📌 school items in [lo_, hi_) — quizzes, labs and videos with no
        gradebook column live only there. Skips ones already listed from the gradebook."""
        out_ = []
        for e in CalEvent.objects.filter(start__gte=lo_, start__lt=hi_).order_by("start"):
            if not e.summary.startswith("📌") or e.is_work or e.gid in marked:
                continue
            code = next((c for c in COURSES if c in e.summary), "")
            if any(i.course == code and similar(code, i.title, e.summary) for i in items):
                continue
            out_.append(e)
        return out_

    def _label(e):
        return clean_title(e.summary.split(":", 1)[-1].split(" · via")[0].strip())

    def _due(e):
        # Only cal_sync's gradebook events end ON the deadline; a class-slot 📌 starts on time.
        return e.end if e.deadline_at_end else e.start
    for i in overdue:
        tasks.append({"pri": "overdue", "text": f"{i.course} {clean_title(i.title)}", "due": i.due, "tag": "overdue",
                      "prompt": tutor.for_item(i), "key": i.uid, "choices": donemod.choices()})
    for i in due_today:
        tasks.append({"pri": "due", "text": f"{i.course} {clean_title(i.title)}", "due": i.due, "tag": "today",
                      "prompt": tutor.for_item(i), "key": i.uid, "choices": donemod.choices()})
    for e in _cal_pins(lo, hi, overdue + due_today):
        code = next((c for c in COURSES if c in e.summary), "")
        title = e.summary.split("—", 1)[-1].split(" · via")[0].strip()
        tasks.append({"pri": "due", "text": _label(e), "due": _due(e), "tag": "today",
                      "prompt": tutor.prompt(code, title, due=e.start) if code else "",
                      "key": e.gid, "choices": donemod.choices(e.summary)})
    # Tomorrow: every 📌 on the calendar, plus gradebook items not already among them.
    pins_tmr = _cal_pins(hi, hi + timedelta(days=1), [])
    # ✨ on tomorrow's rows too (2026-10-13, the student: the night before a quiz is when it's used).
    def _pin_prompt(e):
        code = next((c for c in COURSES if c in e.summary), "")
        return tutor.prompt(code, e.summary.split("—", 1)[-1].split(" · via")[0].strip(),
                            due=e.start) if code else ""
    tomorrow = [{"text": _label(e), "due": _due(e), "key": e.gid, "choices": donemod.choices(e.summary),
                 "prompt": _pin_prompt(e)} for e in pins_tmr] + [
        {"text": f"{i.course} — {clean_title(i.title)}", "due": i.due, "key": i.uid,
         "choices": donemod.choices(), "prompt": tutor.for_item(i)} for i in tomorrow_items
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
        "recently_done": DoneMark.objects.filter(created__gte=now - timedelta(hours=24)),
    }
    return render(request, "core/today.html", ctx)


def _group_by_day(events):
    days = OrderedDict()
    for e in events:
        days.setdefault(timezone.localtime(e.start).date(), []).append(e)
    return days


PAGE = 50


@login_required
def inbox(request):
    """School first: personal mail (job alerts, sign-in notices) buried every school message
    under "All" (review 2026-10-12), so it now has its own chip and stays out of the default."""
    src, course = request.GET.get("src", ""), request.GET.get("course", "")
    qs = Message.objects.all()
    if src:
        qs = qs.filter(source=src)
    elif request.GET.get("only") != "everything":
        qs = qs.exclude(source="personal_mail")
    if course:
        qs = qs.filter(course=course)
    only = request.GET.get("only")
    if only == "matters":
        from django.db.models import Q
        qs = qs.filter(Q(keyword_hit=True) | Q(judge_relevant=True))
    try:
        page = max(1, int(request.GET.get("page", 1)))
    except ValueError:
        page = 1
    rows = list(qs.order_by("-when")[(page - 1) * PAGE: page * PAGE + 1])
    more = len(rows) > PAGE
    rows = rows[:PAGE]
    keep = request.GET.copy()
    keep.pop("page", None)
    keep.pop("m", None)
    base = keep.urlencode()
    sel = None
    if request.GET.get("m"):
        sel = Message.objects.filter(pk=request.GET["m"]).first()
    return render(request, "core/inbox.html", {
        "rows": rows, "src": src, "course": course, "only": only, "sel": sel,
        "page": page, "more": more, "base": base,
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
    done_by_you = settled(Run.objects.filter(finished__isnull=False).first())
    items = [i for i in Item.objects.filter(submitted=False, due__gte=now)
             if (i.course, i.title) not in done_by_you]
    pins = list(CalEvent.objects.filter(end__gte=now, summary__startswith="📌"))
    cards = []
    for code, name in COURSES.items():
        ahead = upcoming(code, now, items, pins)
        cards.append({"code": code, "name": name, "next": ahead[0] if ahead else None,
                      "open": len(ahead),
                      "recent": Message.objects.filter(course=code,
                                                       when__gte=now - timedelta(days=7)).count()})
    return render(request, "core/courses.html", {"cards": cards})


@login_required
def course(request, code):
    if code not in COURSES:
        raise Http404
    now = timezone.now()
    done_by_you = settled(Run.objects.filter(finished__isnull=False).first())
    marks = {d.key: d for d in DoneMark.objects.filter(course=code)}
    items = []
    for i in Item.objects.filter(course=code).filter(due__gte=now - timedelta(days=14)):
        # Work you marked done on the calendar is done here too — Today already said so.
        mark = marks.get(i.uid)
        i.done = i.submitted or (i.course, i.title) in done_by_you or mark is not None
        i.done_word = mark.word if mark else ""
        i.choices = donemod.choices()
        items.append(i)
    items.sort(key=lambda i: (i.done, i.due < now, i.due if i.due >= now else now - (i.due - now)))
    # Messages the judge or the keyword floor flagged first; classmates' chatter folds away.
    recent = list(Message.objects.filter(course=code).order_by("-when")[:60])
    flagged = [m for m in recent if m.shows_in_brief][:15]
    return render(request, "core/course.html", {
        "code": code, "name": COURSES[code],
        "items": [(i, tutor.for_item(i) if not i.done else "") for i in items],
        "flagged": flagged, "others": [m for m in recent if m not in flagged][:25],
        # Proposals that are already on the calendar are the calendar's now; list the rest.
        "props": [(p, tutor.for_proposal(p)) for p in
                  Proposal.objects.filter(course=code, state__in=["pending", "approved", "written", "failed"])
                  .exclude(date__lt=now.date() - timedelta(days=1)).order_by("date")[:10]],
        "materials": materials(code), "prompt": tutor_prompt(code),
        "pack": _pack_status(code),
        "icons": SOURCE_ICON, "now": now})


def _pack_status(code):
    """The study pack's status file. "Drive still syncing" was only ever checked at build
    time, so it said that forever; re-ask Drive (one call) until it confirms, then remember."""
    f = settings.DATA_DIR / "packs" / f"{code}.status.json"
    if not f.exists():
        return None
    st = json.loads(f.read_text())
    if st.get("copied") and st.get("drive_verified") is not True and not settings.DEMO:
        try:
            from .studypack import drive_verified
            if drive_verified(code, st["md5"]):
                st["drive_verified"] = True
                f.write_text(json.dumps(st))
        except Exception:
            pass                                    # offline / token: leave it as it was
    return st


def class_now(now=None):
    """The course whose class is on now: started up to 15 min from now, or ended under
    90 min ago (a note is usually typed during or just after class). From the calendar
    snapshot's class events, so it follows the real timetable, moves and suspensions."""
    now = now or timezone.now()
    best = None
    for e in CalEvent.objects.filter(color="8", start__lte=now + timedelta(minutes=15),
                                     end__gte=now - timedelta(minutes=90)):
        code = next((c for c in COURSES if c in e.summary), "")
        if code and (best is None or e.start > best[0]):
            best = (e.start, code)
    return best[1] if best else ""


@login_required
def capture(request):
    """The Capture chat (2026-10-13). the student's line → a Capture the next sweep judges; Nebula
    answers at once on Flash-Lite (core/chat.py). Nothing is judged here unless he taps Check now."""
    from . import chat
    if request.method == "POST":
        text = request.POST.get("text", "").strip()
        course = request.POST.get("course", "")
        course = course if course in COURSES else ""
        if not text:
            flash.error(request, "Type something first.")
            return redirect("capture")
        c = Capture.objects.create(text=text, course=course)
        ChatTurn.objects.create(role="me", text=text, capture=c)
        if not settings.DEMO:
            chat.reply(text, course)
        return redirect("capture")
    turns = list(ChatTurn.objects.select_related("capture").order_by("-created")[:40])[::-1]
    caps = {t.capture_id for t in turns if t.capture_id}
    props = {}
    for p in Proposal.objects.filter(capture_id__in=caps):
        props.setdefault(p.capture_id, []).append(p)
    for t in turns:
        t.props = props.get(t.capture_id, [])
    lite = quota.summary()[-1]
    return render(request, "core/capture.html", {
        "turns": turns, "courses": list(COURSES), "guess": class_now(),
        "sweep": chat.next_sweep(), "lite": lite,
        "answers": Capture.objects.filter(kind="answer")[:20],
        "waiting": Capture.objects.filter(kind="note", judged_at__isnull=True).count()})


@login_required
@require_POST
def chat_check(request, pk):
    """Check now: judge the waiting notes immediately, leaving 10 requests per Flash model for
    the sweeps. Only offered when Flash-Lite flagged a deadline before the next sweep."""
    t = get_object_or_404(ChatTurn, pk=pk, role="nebula")
    if ingest.is_running():
        flash.info(request, "A sweep is running right now — it will check your note in a few minutes.")
    elif _bg("judge", "--captures-only", "--reserve", "10"):
        t.checked_now = True
        t.save(update_fields=["checked_now"])
        flash.success(request, "Checking now — anything dated will show here and in To approve.")
    return redirect("capture")


@login_required
@require_POST
def capture_delete(request, pk):
    c = get_object_or_404(Capture, pk=pk)
    ChatTurn.objects.filter(capture=c).delete()      # the chat line goes with the note
    c.delete()                      # its proposals stay (FK SET_NULL); the calendar is untouched
    flash.info(request, "Note deleted.")
    return redirect("capture")


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
            if p.moved_done:
                msg += f" · old {p.replaces_date:%a %m-%d} event marked ↪ MOVED"
            elif p.replaces_event_id and "NOT marked moved" in p.error:
                msg += f" · ⚠ {p.error}"
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
        Capture.objects.create(text=f"Answer to “{c.question}”: {c.answer}", kind="answer")
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
                         "captures_waiting": Capture.objects.filter(judged_at__isnull=True).count(),
                         "last": run.started.isoformat() if run else None,
                         "finished": bool(run and run.finished)})


# ---------------------------------------------------------------- PWA
def manifest(request):
    data = {"id": "/", "name": "Nebula", "short_name": "Nebula", "start_url": "/", "scope": "/",
            "display": "standalone", "background_color": "#000000", "theme_color": "#000000",
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


@login_required
@require_POST
def mark_done(request):
    """✓ — the student says it's done: close the calendar event (calendar skill contract), remember it."""
    key, word = request.POST.get("key", ""), request.POST.get("word", "DONE")
    nxt = request.POST.get("next") or "today"
    if not key:
        raise Http404
    try:
        dm, msg = donemod.mark(key, word)
        flash.success(request, f"{msg}: {clean_title(dm.title)}")
    except Exception as e:
        flash.error(request, f"Not marked — the calendar write failed ({type(e).__name__}: {e})"[:300])
    return redirect(nxt)


@login_required
@require_POST
def undo_done(request, pk):
    dm = get_object_or_404(DoneMark, pk=pk)
    try:
        donemod.undo(dm)
        flash.info(request, f"Undone — back on the calendar as it was: {clean_title(dm.title)}")
    except Exception as e:
        flash.error(request, f"Undo failed ({type(e).__name__}: {e})"[:300])
    return redirect(request.POST.get("next") or "today")
