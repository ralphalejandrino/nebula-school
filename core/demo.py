"""Fictional demo data — for screenshots and for trying Nebula without any account.

    NEBULA_DEMO=1 NEBULA_DATA=/tmp/nebula-demo manage.py migrate
    NEBULA_DEMO=1 NEBULA_DATA=/tmp/nebula-demo manage.py demo

🔴 Every name, message and grade below is invented. `seed()` WIPES the database it is pointed
at, so it refuses to run unless settings.DEMO is on (NEBULA_DEMO=1); demo mode also switches
off every background job (views._bg), so a demo can never reach a real account.
Dates are relative to "now", so the pages always look current.
"""
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth.models import User
from django.utils import timezone

from . import quota
from .judge import COURSES
from .models import (CalEvent, Capture, Clarification, CourseFacts, Item, Message, Proposal,
                     QuotaUse, Run)

PASSWORD = "demo"

# Invented instructors, one per course.
TEACHERS = {
    "PROGIT1": ("ANA LIM", "ana.lim"), "HUMCOM1": ("PAOLO GARCIA", "paolo.garcia"),
    "INTRCS1": ("MARIA SANTOS", "maria.santos"), "MATHMW1": ("JOSE VILLANUEVA", "jose.villanueva"),
    "LITERA1": ("ELENA DOMINGO", "elena.domingo"), "ARTAPP1": ("RAMON TAN", "ramon.tan"),
    "NSTPRO1": ("GRACE FLORES", "grace.flores"), "PATHFT1": ("NOEL CRUZ", "noel.cruz"),
    "CMPTRB1": ("IRENE BAUTISTA", "irene.bautista"), "SOCORN2": ("LUIS NAVARRO", "luis.navarro"),
}
DOMAIN = "faculty.example.edu"

# A fictional timetable: (weekday Mon=0, start, end, course, room)
CLASSES = [
    (0, "08:00", "09:00", "PROGIT1", "F212"), (0, "09:00", "10:00", "LITERA1", "F209"),
    (0, "11:00", "12:00", "INTRCS1", "F112"), (0, "14:00", "17:00", "CMPTRB1", "Lab 1"),
    (1, "08:00", "10:00", "PATHFT1", "Gym"), (1, "11:00", "12:30", "MATHMW1", "F204"),
    (1, "14:00", "17:00", "PROGIT1", "Lab 3"), (1, "17:00", "18:30", "NSTPRO1", "F203"),
    (2, "09:00", "10:00", "LITERA1", "F209"), (2, "11:00", "12:00", "INTRCS1", "F112"),
    (2, "14:00", "17:00", "HUMCOM1", "Lab 2"), (2, "17:00", "18:00", "ARTAPP1", "F203"),
    (3, "08:00", "09:30", "INTRCS1", "Lab 1"), (3, "11:00", "12:30", "MATHMW1", "F204"),
    (3, "14:00", "15:00", "SOCORN2", "F101"),
]


def _at(day, hhmm):
    h, m = map(int, hhmm.split(":"))
    return timezone.make_aware(datetime.combine(day, time(h, m)))


def seed(now=None):
    if not settings.DEMO:
        raise RuntimeError("Refusing to seed: NEBULA_DEMO=1 is not set. seed() wipes the "
                           "database it points at, and this one may be real.")
    now = timezone.localtime(now or timezone.now())
    today = now.date()
    for model in (Proposal, Clarification, Capture, Message, Item, CalEvent, Run, CourseFacts,
                  QuotaUse):
        model.objects.all().delete()
    User.objects.filter(username=settings.LOGIN_USERNAME).delete()
    User.objects.create_user(settings.LOGIN_USERNAME, password=PASSWORD)

    # ---- the last sweep: complete, every source read ------------------------------------
    by_design = {"classroom": {"PROGIT1", "SOCORN2", "CMPTRB1", "INTRCS1", "HUMCOM1", "LITERA1",
                               "ARTAPP1", "PATHFT1"},
                 "chat": {"MATHMW1", "NSTPRO1", "PATHFT1"}}
    cov = {c: {s: ("n/a" if c in by_design.get(s, ()) else "OK")
               for s in ("gradebook", "announcements", "contents", "classroom", "chat", "facebook")}
           for c in COURSES}
    run = Run.objects.create(trigger="timer", load_exit=0, complete=True, coverage=cov,
                             steps={"lms_load": "ok · exit 0 · 214 messages",
                                    "calendar_apply": "ok · exit 0 · CREATE 1, HELD 2",
                                    "ledger": "ok · 96 items",
                                    "personal_mail": "ok · 31 threads (never sent to AI)",
                                    "calendar_read": "ok · 142 events"},
                             gemini="ok · gemini-flash · 7 msg · 3 proposal(s) · 1 question(s)",
                             artifact={"held_by_you": [], "twins": []})
    Run.objects.filter(pk=run.pk).update(started=now - timedelta(minutes=41),
                                         finished=now - timedelta(minutes=39))

    # ---- graded work (the gradebook ledger) ---------------------------------------------
    d = lambda n, hhmm="23:59": _at(today + timedelta(days=n), hhmm)  # noqa: E731
    items = [
        ("PROGIT1", "Lab 4: Loops (While and Do-While)", d(-1), 40, False, "no attempt"),
        ("HUMCOM1", "Week 9 Group Report: Interaction Design Tools", d(0), 50, False, "no attempt"),
        ("MATHMW1", "Problem Set 3: Patterns and Sequences", d(1, "11:00"), 30, False, "no attempt"),
        ("INTRCS1", "Activity 13: Video Editing (group)", d(3, "17:00"), 45, False, "no attempt"),
        ("CMPTRB1", "MLW2: Disk Partitioning", d(4, "17:00"), 100, False, "no attempt"),
        ("LITERA1", "Reflection Paper: Digital Ethics", d(5), 20, False, "no attempt"),
        ("NSTPRO1", "Position Paper: Community Health", d(8, "16:00"), 40, False, "no attempt"),
        ("ARTAPP1", "Coursework 3: Still Life Photography", d(9), 50, False, "no attempt"),
        ("PROGIT1", "Quiz: Unit 6 Arrays", d(-3, "15:30"), 30, True, "Completed"),
        ("HUMCOM1", "Week 8 Summative Quiz: Emotional Interaction", d(-4), 25, True, "Completed"),
        ("INTRCS1", "Week 8 Binary Quiz", d(-5, "11:15"), 20, True, "Completed"),
        ("LITERA1", "Cybersecurity Case Study", d(-6), 100, True, "NeedsGrading"),
        ("SOCORN2", "Org Fair Reflection", d(-2, "17:00"), 30, True, "NeedsGrading"),
    ]
    for i, (code, title, due, pts, sub, state) in enumerate(items):
        Item.objects.create(uid=f"demo:{i}", course=code, title=title, due=due, pts=pts,
                            submitted=sub, state=state, source="gradebook")

    # ---- messages, one or more per channel ----------------------------------------------
    def msg(uid, source, course, title, body, ago_h, *, author="", email="", tag="",
            relevant=None, kw=False, instructor=None, unread=None, actionable=None, url=""):
        when = now - timedelta(hours=ago_h)
        m = Message(uid=f"demo:{uid}", source=source, course=course, title=title, body=body,
                    when=when, author=author, author_email=email, judge_tag=tag,
                    judge_relevant=relevant, keyword_hit=kw, instructor_thread=instructor,
                    unread=unread, rule_actionable=actionable, url=url,
                    judge_model="gemini-flash" if relevant is not None else "")
        m.judged_at = when + timedelta(minutes=30) if relevant is not None else None
        m.save()
        return m

    t = TEACHERS
    quiz = msg("chat-lit", "chat_space", "LITERA1", "LITERA1 space",
               "Quiz tomorrow at the start of class, so please do not be late. "
               "It covers the Digital Ethics module, lessons 1 to 3.", 3,
               author=t["LITERA1"][0], tag="Quiz tomorrow, do not be late", relevant=True, kw=True)
    exams = msg("bb-intrcs", "bb_announce", "INTRCS1", "Midterm examination schedule",
                "Good day, class.\n\nMidterm examinations will be held in the same rooms as our "
                "regular classes:\n\nLAB — next Thursday, 9:30 – 11:00 AM\nLECTURE — the Monday "
                "after, 11:00 AM – 12:00 noon\n\nIf you have a concern, please see me at the "
                "faculty office before the examination week.", 20,
                author=t["INTRCS1"][0], tag="Midterm lab exam next Thu; lecture exam the Mon after",
                relevant=True, kw=True)
    msg("bb-intrcs-mail", "school_mail", "", "New announcement: Midterm examination schedule",
        "A new announcement was posted in INTRCS1: Midterm examination schedule.", 20,
        author="Blackboard", email="do-not-reply@blackboard.example.edu",
        tag="Midterm lab exam next Thu; lecture exam the Mon after", relevant=True, unread=False,
        actionable=True)
    seat = msg("cls-nstp", "classroom", "NSTPRO1", "Classroom — NSTPRO1",
               "Seating arrangement for Saturday's General Assembly is posted under Classwork. "
               "Please be at the venue by 7:30 AM and bring your ID.", 30,
               author=t["NSTPRO1"][0], tag="General Assembly Sat 7:30 AM, bring ID",
               relevant=True, kw=False)
    msg("mail-hci", "school_mail", "HUMCOM1", "HUMAN COMPUTER INTERACTION: Week 9 group reports",
        "Hello class,\n\nGroups 1 to 3 present their Week 9 reports today. Upload your slides to "
        "the Week 9 folder before 11:59 PM; the rubric is attached to the item.\n\nThank you.", 9,
        author=f"{t['HUMCOM1'][0].title()}", email=f"{t['HUMCOM1'][1]}@{DOMAIN}",
        tag="Week 9 report slides due 11:59 PM today", relevant=True, unread=True,
        actionable=True)
    msg("dm-math", "chat_dm", "MATHMW1", "Direct message",
        "Your Problem Set 3 answers may be written by hand on one whole sheet. Bring it to class.",
        14, author=t["MATHMW1"][0], email=f"{t['MATHMW1'][1]}@{DOMAIN}",
        tag="Problem Set 3: handwritten, one whole sheet", relevant=True, instructor=True)
    msg("fb-sit", "fb", "", "Facebook — Student Body",
        "Org Fair registration is open until Friday! Visit the booths at the main lobby and sign "
        "up for the clubs you like.", 26, kw=True, relevant=False)
    msg("mail-receipt", "school_mail", "", "Submission received",
        "Your submission for Quiz: Unit 6 Arrays was received.", 50,
        author="Blackboard", email="do-not-reply@blackboard.example.edu", relevant=False,
        unread=False, actionable=False)
    msg("chat-prog", "chat_space", "PROGIT1", "PROGIT1 space",
        "If you missed a quiz for a valid reason, message me here with your preferred make-up "
        "schedule.", 40, author=t["PROGIT1"][0], kw=True, relevant=False)
    msg("pm-1", "personal_mail", "", "Your order has shipped",
        "Your order is on its way and should arrive in 2 to 3 days.", 5,
        author="Online Store", email="orders@shop.example.com", unread=True)
    msg("pm-2", "personal_mail", "", "Weekly newsletter",
        "This week's picks for you.", 30, author="Newsletter", email="news@example.com",
        unread=True)

    # ---- the judge's proposals and a question -------------------------------------------
    tomorrow_ = today + timedelta(days=1)
    lab_day = today + timedelta(days=(3 - today.weekday()) % 7 + 7)
    Proposal.objects.create(message=quiz, kind="quiz", course="LITERA1", title="LITERA1 Quiz",
                            date=tomorrow_, time=time(9, 0), end_time=time(10, 0), room="F209",
                            evidence="Quiz tomorrow at the start of class",
                            label="extrapolated", verified=True,
                            verify_note="time = your class slot", model="gemini-flash")
    Proposal.objects.create(message=exams, kind="exam", course="INTRCS1",
                            title="INTRCS1 Midterm Lab Exam", date=lab_day, time=time(9, 30),
                            end_time=time(11, 0),
                            evidence="LAB — next Thursday, 9:30 – 11:00 AM", label="stated",
                            verified=True, model="gemini-flash")
    Proposal.objects.create(message=seat, kind="event", course="NSTPRO1",
                            title="NSTP General Assembly",
                            date=today + timedelta(days=(5 - today.weekday()) % 7 or 7),
                            time=time(7, 30), evidence="be at the venue by 7:30 AM and bring "
                            "your ID", label="stated", verified=False,
                            verify_note="the quote is reworded, not verbatim", model="gemini-flash")
    Clarification.objects.create(
        question="Which group are you in for the HUMCOM1 Week 9 report, and does it present today?",
        context="Groups 1 to 3 present today.")
    Capture.objects.create(text="Lab exam is open notes, but no laptops", course="INTRCS1",
                           judged_at=now - timedelta(hours=2), outcome="nothing dated in it")
    rubric = Capture.objects.create(text="Bring a printed copy of the rubric on Wednesday",
                                    course="HUMCOM1", judged_at=now - timedelta(minutes=50),
                                    outcome="1 proposal")
    wed = today + timedelta(days=(2 - today.weekday()) % 7 or 7)
    Proposal.objects.create(capture=rubric, kind="buy", course="HUMCOM1",
                            title="Print the HUMCOM1 rubric", date=wed, time=time(14, 0),
                            evidence="Bring a printed copy of the rubric on Wednesday",
                            label="stated", verified=True, model="gemini-flash")
    Capture.objects.create(text="Group 2 presents the infographic first", course="NSTPRO1")
    Capture.objects.create(text="Answer to “Which group are you in for the HUMCOM1 Week 9 report?”: "
                                "Group 3", kind="answer", judged_at=now - timedelta(days=1),
                           outcome="kept as context")

    # ---- the calendar: classes, deadlines, and one work clash ---------------------------
    n = 0

    def ev(summary, start, end, color="", deadline=False, all_day=False):
        nonlocal n
        n += 1
        CalEvent.objects.create(gid=f"demo{n}", summary=summary, start=start, end=end,
                                color=color, deadline_at_end=deadline, all_day=all_day,
                                link="https://calendar.google.com/")
    for k in range(15):
        day = today + timedelta(days=k)
        for wd, a, b, code, room in CLASSES:
            if day.weekday() == wd:
                ev(f"{code} — {COURSES[code]}", _at(day, a), _at(day, b), "8")
    for code, title, due, pts, sub, _ in items:
        if not sub and due >= now - timedelta(days=2):
            pre = "📌 DUE" if due >= now else "📌 OVERDUE"
            ev(f"{pre}: {code} — {title} ({pts} pts)", due - timedelta(hours=1), due, "11",
               deadline=True)
    ev("📌 QUIZ: LITERA1 — Quiz · via Chat (Domingo)", _at(tomorrow_, "09:00"),
       _at(tomorrow_, "10:00"), "11")
    clash = next(day for day in (today + timedelta(days=k) for k in range(1, 8))
                 if day.weekday() == 1)
    ev("💼 WORK: client call", _at(clash, "14:30"), _at(clash, "15:30"), "3")
    ev("📌 EXAM: Midterm examinations week", _at(today + timedelta(days=11), "00:00"),
       _at(today + timedelta(days=12), "00:00"), "11", all_day=True)

    # ---- per-course facts and today's Gemini quota --------------------------------------
    for code, (name, _) in TEACHERS.items():
        CourseFacts.objects.create(code=code, channel="Blackboard + Chat",
                                   notes=f"Instructor: {name.title()}")
    day = quota.pacific_day()
    for (model, _rpm, _rpd), used in zip(quota.CHAIN, (7, 2, 0, 0, 0)):
        QuotaUse.objects.create(model=model, day=day, count=used)
    return {"items": len(items), "messages": Message.objects.count(),
            "events": CalEvent.objects.count(), "user": settings.LOGIN_USERNAME,
            "password": PASSWORD}
