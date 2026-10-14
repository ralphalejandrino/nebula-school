"""Nebula tests. Run: ~/.venv-school/bin/python manage.py test core

Offline: the Gemini call site (gemini._post_json) and the Calendar service are faked,
so no test opens a socket. The privacy tests are the reason this file exists.
"""
import json
import tempfile
from datetime import date, datetime, time, timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.utils import timezone

from . import calwrite, gemini, ingest, judge, quota
from .models import CalEvent, Capture, Message, Proposal, QuotaUse
from .privacy import PrivacyViolation, eligible

FAKE_KEY = override_settings(GEMINI_API_KEY="test-key")


def msg(source, body="Quiz on Monday 10/13 at 9:00 AM in F209", **kw):
    kw.setdefault("uid", f"{source}:{Message.objects.count()}")
    kw.setdefault("when", timezone.now() - timedelta(hours=1))
    return Message.objects.create(source=source, body=body, **kw)


def reply(proposals=(), msgs=None, questions=()):
    return {"candidates": [{"content": {"parts": [{"text": json.dumps({
        "messages": msgs or [], "proposals": list(proposals),
        "clarifications": list(questions)})}]}}]}


class Privacy(TestCase):
    def test_rule_table(self):
        self.assertTrue(eligible("school_mail"))
        self.assertTrue(eligible("bb_announce"))
        self.assertTrue(eligible("chat_dm", True))
        self.assertFalse(eligible("chat_dm", False))
        self.assertFalse(eligible("chat_dm", None))
        self.assertFalse(eligible("personal_mail"))
        self.assertFalse(eligible("calendar"))
        self.assertFalse(eligible("anything_new"))       # unknown sources fail closed

    def test_save_sets_eligibility_from_source(self):
        self.assertFalse(msg("personal_mail").gemini_eligible)
        self.assertFalse(msg("chat_dm", instructor_thread=False).gemini_eligible)
        self.assertTrue(msg("chat_dm", instructor_thread=True).gemini_eligible)
        self.assertTrue(msg("school_mail").gemini_eligible)

    def test_payload_refuses_a_forced_personal_row(self):
        """🔴 Negative control: even if a row's flag is forced True behind save()'s back,
        build_payload re-checks the SOURCE and fails closed."""
        p = msg("personal_mail", body="Job offer from a client — CONFIDENTIAL")
        Message.objects.filter(pk=p.pk).update(gemini_eligible=True)
        p.refresh_from_db()
        with self.assertRaises(PrivacyViolation):
            judge.build_payload([p], [])

    def test_pending_work_never_selects_personal_or_classmate(self):
        msg("personal_mail", body="PERSONAL-SECRET")
        msg("chat_dm", body="CLASSMATE-SECRET", instructor_thread=False)
        msg("school_mail", body="school quiz")
        msgs, _ = judge.pending_work()
        self.assertEqual([m.source for m in msgs], ["school_mail"])

    @FAKE_KEY
    def test_outgoing_request_body_has_no_personal_text(self):
        msg("personal_mail", body="PERSONAL-SECRET")
        msg("chat_dm", body="CLASSMATE-SECRET", instructor_thread=False)
        s = msg("school_mail", body="Quiz moved to Wed 10:00 AM")
        sent = []
        with mock.patch.object(gemini, "_post_json",
                               side_effect=lambda u, p, k: sent.append(json.dumps(p)) or reply(
                                   msgs=[{"id": f"M{s.pk}", "relevant": True, "tag": "quiz moved"}])):
            status = judge.run()
        self.assertTrue(status.startswith("ok"), status)
        self.assertEqual(len(sent), 1)
        self.assertNotIn("PERSONAL-SECRET", sent[0])
        self.assertNotIn("CLASSMATE-SECRET", sent[0])
        self.assertIn("Quiz moved", sent[0])


class Quota(TestCase):
    def test_chain_falls_through_on_daily_cap(self):
        day = quota.pacific_day()
        QuotaUse.objects.create(model="gemini-3.8-flash", day=day, count=20)
        self.assertEqual(quota.next_model(), "gemini-3.7-flash")

    def test_rpm_window(self):
        now = 1_000_000.0
        for i in range(5):
            quota.record("gemini-3.8-flash", now_s=now + i)
        self.assertEqual(quota.next_model(now_s=now + 10), "gemini-3.7-flash")
        self.assertEqual(quota.next_model(now_s=now + 120), "gemini-3.8-flash")

    def test_google_429_is_believed(self):
        quota.mark_exhausted("gemini-3.8-flash")
        self.assertEqual(quota.next_model(), "gemini-3.7-flash")

    @FAKE_KEY
    def test_everything_exhausted_is_UNKNOWN_not_nothing(self):
        for m, _, rpd in quota.CHAIN:
            QuotaUse.objects.create(model=m, day=quota.pacific_day(), count=rpd)
        msg("school_mail")
        status = judge.run()
        self.assertTrue(status.startswith("UNKNOWN"), status)
        self.assertEqual(Message.objects.filter(judged_at__isnull=False).count(), 0)

    @FAKE_KEY
    def test_429_moves_to_next_model(self):
        s = msg("school_mail")
        ok = json.loads(reply(msgs=[{"id": f"M{s.pk}", "relevant": False, "tag": ""}])
                        ["candidates"][0]["content"]["parts"][0]["text"])

        def fake(model, *a, **k):
            if model == "gemini-3.8-flash":
                raise gemini.GeminiError("HTTP 429", status=429, quota=True)
            return ok, {}
        with mock.patch.object(gemini, "generate_json", side_effect=fake):
            status = judge.run()
        self.assertIn("gemini-3.7-flash", status)
        self.assertTrue(QuotaUse.objects.get(model="gemini-3.8-flash").exhausted)


class Overload(TestCase):
    @FAKE_KEY
    def test_503_moves_on_without_burning_the_day(self):
        s = msg("school_mail")
        ok = json.loads(reply(msgs=[{"id": f"M{s.pk}", "relevant": False, "tag": ""}])
                        ["candidates"][0]["content"]["parts"][0]["text"])

        def fake(model, *a, **k):
            if model == "gemini-3.8-flash":
                raise gemini.GeminiError("HTTP 503 high demand", status=503)
            return ok, {}
        with mock.patch.object(gemini, "generate_json", side_effect=fake):
            status = judge.run()
        self.assertIn("gemini-3.7-flash", status)
        self.assertFalse(QuotaUse.objects.get(model="gemini-3.8-flash").exhausted)

    # #4 (2026-10-14): 59 × 503 and 0 × 429 in three days — every Flash model refused within
    # seconds of each other and Flash-Lite answered most sweeps. Wait ONCE, then retry the chain.
    def _ok(self, s):
        return json.loads(reply(msgs=[{"id": f"M{s.pk}", "relevant": False, "tag": ""}])
                          ["candidates"][0]["content"]["parts"][0]["text"])

    @FAKE_KEY
    def test_all_flash_overloaded_waits_once_then_gets_the_best_model(self):
        s, calls = msg("school_mail"), []

        def fake(model, *a, **k):
            calls.append(model)
            if model != "gemini-3.5-flash-lite" and calls.count(model) == 1:
                raise gemini.GeminiError("HTTP 503 high demand", status=503)
            return self._ok(s), {}
        with mock.patch.object(gemini, "generate_json", side_effect=fake), \
                mock.patch("core.judge._sleep") as slept:
            status = judge.run()
        self.assertIn("gemini-3.8-flash", status)
        slept.assert_called_once_with(judge.RETRY_WAIT)
        self.assertNotIn("gemini-3.5-flash-lite", calls)

    @FAKE_KEY
    def test_no_wait_when_a_flash_model_answers(self):
        s = msg("school_mail")

        def fake(model, *a, **k):
            if model == "gemini-3.8-flash":
                raise gemini.GeminiError("HTTP 503 high demand", status=503)
            return self._ok(s), {}
        with mock.patch.object(gemini, "generate_json", side_effect=fake), \
                mock.patch("core.judge._sleep") as slept:
            judge.run()
        slept.assert_not_called()

    @FAKE_KEY
    def test_still_overloaded_after_the_wait_falls_to_lite(self):
        s = msg("school_mail")

        def fake(model, *a, **k):
            if model != "gemini-3.5-flash-lite":
                raise gemini.GeminiError("HTTP 503 high demand", status=503)
            return self._ok(s), {}
        with mock.patch.object(gemini, "generate_json", side_effect=fake), \
                mock.patch("core.judge._sleep") as slept:
            status = judge.run()
        self.assertIn("gemini-3.5-flash-lite", status)
        self.assertEqual(slept.call_count, 1)

    @FAKE_KEY
    def test_bad_request_stops_with_UNKNOWN(self):
        msg("school_mail")
        with mock.patch.object(gemini, "generate_json",
                               side_effect=gemini.GeminiError("HTTP 400 bad", status=400)):
            status = judge.run()
        self.assertTrue(status.startswith("UNKNOWN"), status)


class Evidence(TestCase):
    def test_verbatim_quote_passes(self):
        ok, _ = judge.verify("LEC-Monday October 26,2026 11-12 noon",
                             "Examination Schedule - LEC-Monday October 26,2026 11-12 noon LAB-...", True)
        self.assertTrue(ok)

    def test_paraphrase_fails(self):
        ok, note = judge.verify("exam on Monday the 26th", "LEC-Monday October 26,2026 11-12 noon", True)
        self.assertFalse(ok)
        self.assertIn("NOT found", note)

    def test_preferably_this_week_is_not_a_date(self):
        """🔴 'preferably this week' once became a hard deadline (2026-09-06)."""
        ok, note = judge.verify("submit it preferably this week",
                                "Please submit it preferably this week, thank you.", True)
        self.assertFalse(ok)
        self.assertIn("no date", note)

    def test_curly_quotes_and_spacing_normalised(self):
        ok, _ = judge.verify("bring your  ‘calculator’ on Wed", "Please bring your 'calculator' on Wed.", True)
        self.assertTrue(ok)


class Store(TestCase):
    def test_proposal_created_verified_and_dupe_suppressed(self):
        s = msg("chat_space", course="INTRCS1",
                body="Examination Schedule LEC-Monday October 26,2026 11-12 noon same room")
        idmap = {f"M{s.pk}": s}
        prop = {"source_id": f"M{s.pk}", "kind": "exam", "course": "INTRCS1",
                "title": "Midterm lecture exam", "date": "2026-10-26", "time": "11:00",
                "end_time": "12:00", "room": "", "evidence": "LEC-Monday October 26,2026 11-12 noon",
                "label": "stated"}
        judge.store({"messages": [], "proposals": [prop], "clarifications": []}, idmap, "m")
        judge.store({"messages": [], "proposals": [prop], "clarifications": []}, idmap, "m")
        self.assertEqual(Proposal.objects.count(), 1)
        p = Proposal.objects.get()
        self.assertTrue(p.verified)
        self.assertEqual(p.time, time(11, 0))

    def test_unknown_course_is_blanked(self):
        s = msg("fb", body="No classes tomorrow Oct 12 due to the typhoon")
        judge.store({"messages": [], "clarifications": [], "proposals": [{
            "source_id": f"M{s.pk}", "kind": "notice", "course": "NOTACOURSE", "title": "Suspension",
            "date": "2026-10-12", "time": "", "evidence": "No classes tomorrow Oct 12",
            "label": "stated"}]}, {f"M{s.pk}": s}, "m")
        self.assertEqual(Proposal.objects.get().course, "")

    def test_capture_becomes_judged(self):
        c = Capture.objects.create(text="Ms. Santos: quiz next meeting Wed 10:00 AM", course="INTRCS1")
        judge.store({"messages": [], "proposals": [], "clarifications": []}, {f"N{c.pk}": c}, "m")
        c.refresh_from_db()
        self.assertIsNotNone(c.judged_at)


class FakeCal:
    def __init__(self, existing=(), echo_color=None):
        self.existing, self.echo_color, self.inserted = list(existing), echo_color, []

    def events(self):
        return self

    def list(self, **kw):
        return _X({"items": self.existing})

    def insert(self, calendarId, body):
        self.inserted.append(body)
        return _X({"id": "ev1", "htmlLink": "https://cal/ev1", **body})

    def get(self, calendarId, eventId):
        b = dict(self.inserted[-1])
        if self.echo_color:
            b["colorId"] = self.echo_color
        return _X(b)


class _X:
    def __init__(self, v): self.v = v
    def execute(self): return self.v


class CalWrite(TestCase):
    def _p(self, **kw):
        d = dict(kind="quiz", course="INTRCS1", title="Binary to decimal quiz", date=date(2026, 10, 14),
                 time=time(11, 15), evidence="Quiz Start Time: Exactly 11:15 AM", label="stated",
                 verified=True)
        d.update(kw)
        return Proposal.objects.create(**d)

    def test_conventions(self):
        p = self._p()
        self.assertEqual(calwrite.title_for(p), "📌 QUIZ: INTRCS1 — Binary to decimal quiz")
        self.assertEqual(calwrite.color_for(p), "4")
        self.assertEqual([o["minutes"] for o in calwrite.reminders_for(p)["overrides"]], [2880, 480])
        ev = self._p(kind="event", title="Orientation")
        self.assertEqual(calwrite.color_for(ev), "7")
        self.assertEqual([o["minutes"] for o in calwrite.reminders_for(ev)["overrides"]], [840])

    def test_write_and_read_back(self):
        cal = FakeCal()
        p = calwrite.write(self._p(), svc=cal)
        self.assertEqual(p.state, "written")
        body = cal.inserted[0]
        self.assertIn(f"nebula:{p.pk}", body["description"])
        self.assertIn("Quiz Start Time: Exactly 11:15 AM", body["description"])
        self.assertEqual(body["start"]["dateTime"][:16], "2026-10-14T11:15")

    def test_existing_event_is_not_duplicated(self):
        cal = FakeCal(existing=[{"id": "x", "summary": "📌 QUIZ: INTRCS1 — Binary to Decimal quiz",
                                 "htmlLink": "l"}])
        p = calwrite.write(self._p(), svc=cal)
        self.assertEqual(p.state, "duplicate")
        self.assertEqual(cal.inserted, [])

    def test_read_back_mismatch_is_a_failure(self):
        p = calwrite.write(self._p(), svc=FakeCal(echo_color="9"))
        self.assertEqual(p.state, "failed")
        self.assertIn("colorId", p.error)


class MoveCal(FakeCal):
    """FakeCal that also knows events by id and records patches (moved-date tests)."""
    def __init__(self, events=(), **kw):
        super().__init__(**kw)
        self.by_id, self.patched = {e["id"]: dict(e) for e in events}, []

    def get(self, calendarId, eventId):
        if eventId in self.by_id:
            return _X(dict(self.by_id[eventId]))
        return super().get(calendarId, eventId)

    def patch(self, calendarId, eventId, body):
        self.patched.append((eventId, body))
        self.by_id[eventId].update(body)
        return _X(dict(self.by_id[eventId]))


class Moves(TestCase):
    """#2 (2026-10-14): the instructor moved the INTRCS1 lecture exam Mon 10-26 → Wed 10-21. It arrived
    as a NEW exam and the 10-26 event sat beside it until a Claude load retitled it by hand."""
    TODAY = date(2026, 10, 14)

    def ev(self, gid, summary, d, **kw):
        start = timezone.make_aware(datetime.combine(d, time(11, 0)))
        return CalEvent.objects.create(gid=gid, summary=summary, start=start,
                                       end=start + timedelta(hours=1), color=kw.pop("color", "4"), **kw)

    def setUp(self):
        self.ev("lec", "📌 EXAM: INTRCS1 — Midterm Lecture Exam", date(2026, 10, 26))
        self.ev("lab", "📌 EXAM: INTRCS1 — Midterm Lab Exam", date(2026, 10, 22))

    def store(self, title, d, kind="exam", course="INTRCS1", moved_from=""):
        s = msg("chat_space", course=course, body=f"LEC-Wednesday October 21, 2026 11-12 noon {title}")
        with mock.patch("core.judge.timezone.localdate", return_value=self.TODAY):
            judge.store({"messages": [], "clarifications": [], "proposals": [{
                "source_id": f"M{s.pk}", "kind": kind, "course": course, "title": title,
                "date": d, "time": "11:00", "evidence": "LEC-Wednesday October 21, 2026 11-12 noon",
                "label": "stated", "moved_from": moved_from}]}, {f"M{s.pk}": s}, "m")
        return Proposal.objects.order_by("-pk").first()

    def test_the_real_case_links_the_old_event(self):
        p = self.store("Lecture Exam", "2026-10-21")
        self.assertEqual(p.replaces_event_id, "lec")               # not the lab exam
        self.assertEqual(p.replaces_date, date(2026, 10, 26))

    def test_judge_says_moved_from(self):
        p = self.store("Exam", "2026-10-21", moved_from="2026-10-22")
        self.assertEqual(p.replaces_event_id, "lab")

    def test_negative_controls(self):
        CalEvent.objects.all().delete()
        self.ev("q2", "📌 QUIZ: INTRCS1 — Quiz 2", date(2026, 10, 19))
        self.ev("old", "📌 EXAM: INTRCS1 — Midterm Lecture Exam", date(2026, 10, 10))       # past
        self.ev("done", "✅ DONE: INTRCS1 — Lecture Exam Review", date(2026, 10, 27))       # closed
        self.ev("bb", "📌 DUE: INTRCS1 — Lecture Exam Reflection", date(2026, 10, 28),
                deadline_at_end=True)                                                      # cal_sync owns it
        self.ev("other", "📌 EXAM: HUMCOM1 — Midterm Lecture Exam", date(2026, 10, 26))    # other course
        self.assertEqual(self.store("Quiz 3", "2026-10-21", kind="quiz").replaces_event_id, "")
        self.assertEqual(self.store("Midterm Lecture Exam", "2026-10-21").replaces_event_id, "")

    def test_approve_writes_new_and_marks_old_moved(self):
        p = self.store("Lecture Exam", "2026-10-21")
        old = {"id": "lec", "summary": "📌 EXAM: INTRCS1 — Midterm Lecture Exam", "colorId": "4",
               "description": "Instructor, Chat Sat 10-10"}
        cal = MoveCal(events=[old])
        calwrite.write(p, svc=cal)
        self.assertEqual(p.state, "written")
        eid, body = cal.patched[0]
        self.assertEqual(eid, "lec")
        self.assertEqual(body["summary"], "↪ MOVED → Wed 10-21: INTRCS1 — Midterm Lecture Exam")
        self.assertEqual(body["colorId"], "8")
        self.assertEqual(body["reminders"], {"useDefault": False, "overrides": []})
        self.assertIn("Instructor, Chat Sat 10-10", body["description"])          # kept, never lost
        self.assertTrue(p.moved_done)

    def test_new_date_already_on_calendar_still_moves_old(self):
        # tonight's exact shape: the 10-21 event existed, the 10-26 one was left behind
        p = self.store("Lecture Exam", "2026-10-21")
        new = {"id": "new", "summary": "📌 EXAM: INTRCS1 — Lecture Exam", "htmlLink": "l"}
        old = {"id": "lec", "summary": "📌 EXAM: INTRCS1 — Midterm Lecture Exam", "colorId": "4"}
        cal = MoveCal(events=[old], existing=[new])
        calwrite.write(p, svc=cal)
        self.assertEqual(p.state, "duplicate")
        self.assertEqual([e for e, _ in cal.patched], ["lec"])

    def test_old_event_already_closed_is_left_alone(self):
        p = self.store("Lecture Exam", "2026-10-21")
        cal = MoveCal(events=[{"id": "lec", "summary": "✅ DONE: INTRCS1 — Midterm Lecture Exam"}])
        calwrite.write(p, svc=cal)
        self.assertEqual(cal.patched, [])
        self.assertFalse(p.moved_done)


class ClassSlot(TestCase):
    """(2026-10-11) "the tickets are not on the class slot"."""
    def setUp(self):
        from .models import CalEvent
        st = timezone.make_aware(datetime(2026, 10, 12, 9, 0))
        CalEvent.objects.create(gid="c1", summary="LITERA1 — Living in the IT Era", start=st,
                                end=st + timedelta(hours=1), color="8")
        CalEvent.objects.create(gid="c2", summary="📌 DUE: LITERA1 — Group video", start=st,
                                end=st + timedelta(hours=1), color="4")
        self.m = msg("chat_space", course="LITERA1", author="ELENA DOMINGO",
                     body="Quiz ahead po. Please do not be late tomorrow.")

    def _p(self, **kw):
        d = dict(kind="quiz", course="LITERA1", title="Quiz", date=date(2026, 10, 12),
                 evidence="Quiz ahead po", label="extrapolated", verified=True, message=self.m)
        d.update(kw)
        return Proposal.objects.create(**d)

    def test_untimed_quiz_snaps_to_the_class(self):
        p = self._p()
        self.assertTrue(calwrite.apply_class_slot(p))
        self.assertEqual((p.time, p.end_time), (time(9, 0), time(10, 0)))
        self.assertIn("class slot", p.verify_note)

    def test_stated_time_is_never_moved(self):
        p = self._p(time=time(13, 0))
        self.assertFalse(calwrite.apply_class_slot(p))
        self.assertEqual(p.time, time(13, 0))

    def test_buy_and_no_class_day_stay_untimed(self):
        self.assertFalse(calwrite.apply_class_slot(self._p(kind="buy")))
        self.assertFalse(calwrite.apply_class_slot(self._p(date=date(2026, 10, 13))))

    def test_title_and_notes_name_the_source(self):
        p = self._p()
        self.assertEqual(calwrite.title_for(p), "📌 QUIZ: LITERA1 — Quiz · via Chat (Domingo)")
        first = calwrite.body_for(p).splitlines()[0]
        self.assertIn("LITERA1 Google Chat space — ELENA DOMINGO", first)

    def test_write_lands_on_the_slot(self):
        cal = FakeCal()
        p = calwrite.write(self._p(), svc=cal)
        self.assertEqual(p.state, "written")
        self.assertEqual(cal.inserted[0]["start"]["dateTime"][:16], "2026-10-12T09:00")
        self.assertIn("placed on your LITERA1 class slot", cal.inserted[0]["description"])


class Tutor(TestCase):
    """(2026-10-11) tap a task → Gemini gets all the context it needs."""
    def test_item_prompt_carries_the_context(self):
        from .models import Item
        from . import tutor
        msg("bb_announce", course="HUMCOM1", title="Week 9 Group Reporting",
            body="Week 9 group reporting: present Interaction Design Tools, 10 minutes per group.")
        i = Item.objects.create(uid="x:1", course="HUMCOM1", title="Week 9- Group Reporting-Interaction Design Tools",
                                due=timezone.make_aware(datetime(2026, 10, 12, 23, 59)), pts=50)
        p = tutor.for_item(i)
        self.assertIn("HUMCOM1 (Human Computer Interaction)", p)
        self.assertIn("Worth: 50 points", p)
        self.assertIn("Mon 12 Oct, 11:59 PM", p)
        self.assertIn("10 minutes per group", p)          # the instructor's own words, inline

    # #5 (2026-10-14): Gemini quoted 28–29 Sep "QUIZ next LEC MEETING" as if it were about the
    # next day. The dates were there; nothing said how OLD they were or what "today" is.
    def test_prompt_says_today_and_message_age(self):
        from . import tutor
        old = msg("chat_space", course="PROGIT1", body="QUIZ next LEC MEETING on loops and switch",
                  when=timezone.now() - timedelta(days=16))
        new = msg("chat_space", course="PROGIT1", body="Loops and switch reviewer is posted",
                  when=timezone.now() - timedelta(hours=2))
        p = tutor.prompt("PROGIT1", "Loops and switch quiz")
        self.assertIn(f"Today is {timezone.localtime():%A %d %B %Y}", p)
        self.assertIn("16 days ago ⚠ old", p)
        self.assertRegex(p, r"today\)|today,|— today")
        self.assertNotIn("today ⚠ old", p)                                 # negative control
        self.assertIn("already past", p)                                   # the rule itself

    def test_guide_lines_show_message_age(self):
        from . import drive
        m = msg("chat_space", course="PROGIT1", body="QUIZ next LEC MEETING", judge_relevant=True,
                when=timezone.now() - timedelta(days=10))
        text = "\n".join(t for _, t in drive.guide_lines("PROGIT1"))
        self.assertIn("10 days ago ⚠ old", text)
        self.assertIn(f"Today is {timezone.localtime():%A %d %B %Y}", text)

    def test_personal_mail_never_quoted(self):
        from . import tutor
        msg("personal_mail", course="HUMCOM1", body="Week 9 group reporting PERSONAL-SECRET")
        self.assertNotIn("PERSONAL-SECRET", tutor.prompt("HUMCOM1", "Week 9 group reporting"))


class CalendarMarks(TestCase):
    """(2026-10-11) "the calendar section still uses the old red pin"."""
    def test_prefix_emoji_become_icons(self):
        from .templatetags.nebula import calsum
        due = calsum("📌 DUE: HUMCOM1 — Week 9")
        self.assertNotIn("📌", due)
        self.assertIn("st-due", due)
        self.assertIn("DUE: HUMCOM1 — Week 9", due)
        self.assertIn("st-done", calsum("✅ SUBMITTED: LITERA1 — Ethics"))
        self.assertIn("st-overdue", calsum("✖ MISSED: PROGIT1 — Q1"))
        work = calsum("💼 WORK: client call")
        self.assertIn("ico-work", work)
        self.assertNotIn("💼", work)
        self.assertEqual(calsum("PROGIT1 — Computer Programming (Lec)"), "PROGIT1 — Computer Programming (Lec)")


class Ingest(TestCase):
    def test_import_emitted_and_error_rows(self):
        rows = [{"_run": {"rows": 3}},
                {"source": "chat_dm", "id": "dm1", "body": "group 4", "instructor_thread": False},
                {"source": "school_mail", "id": "gm:1", "body": "quiz", "when": "1791652864000",
                 "keyword_hit": True},
                {"source": "school_mail", "error": "HttpError 500"}]
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "items.jsonl"
            p.write_text("\n".join(json.dumps(r) for r in rows))
            n, errs, _ = ingest.import_emitted(p)
        self.assertEqual(n, 2)
        self.assertEqual(errs, ["school_mail: HttpError 500"])
        self.assertFalse(Message.objects.get(uid="dm1").gemini_eligible)
        self.assertIsNotNone(Message.objects.get(uid="gm:1").when)

    def test_edited_upstream_is_rejudged(self):
        m = msg("bb_announce", uid="bb:1", body="old")
        Message.objects.filter(pk=m.pk).update(judged_at=timezone.now())
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "items.jsonl"
            p.write_text(json.dumps({"_run": {}}) + "\n" +
                         json.dumps({"source": "bb_announce", "id": "bb:1", "body": "NEW TEXT"}))
            ingest.import_emitted(p)
        self.assertIsNone(Message.objects.get(uid="bb:1").judged_at)

    # B1 (2026-10-14): a SHARED post puts the sharer's name before the relative time, so the
    # start-anchored strip missed it and "4 hours ago" → "7 hours ago" minted a new row every
    # sweep (85 FB rows were 73 posts + 12 copies, every copy re-judged).
    SHARED = "Office of Student Affairs - University of Baguio\n{}\nPROJECT D.I.G. | ASEAN Month Celebration\nOct 19"

    def test_fb_shared_post_is_stable_across_sweeps(self):
        a, b = self.SHARED.format("4 hours ago"), self.SHARED.format("a day ago")
        self.assertEqual(ingest.fb_uid("SIT-SB", a), ingest.fb_uid("SIT-SB", b))
        self.assertEqual(ingest.fb_clean(a), ingest.fb_clean(b))
        self.assertIn("Office of Student Affairs", ingest.fb_clean(a))   # the sharer is kept
        self.assertNotIn("ago", ingest.fb_clean(a))

    def test_fb_reimport_with_new_relative_time_is_one_row_not_rejudged(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "items.jsonl"
            for when in ("4 hours ago", "7 hours ago"):
                p.write_text(json.dumps({"_run": {}}) + "\n" + json.dumps(
                    {"source": "fb", "id": "fb:SIT-SB:x", "body": self.SHARED.format(when)}))
                ingest.import_emitted(p)
                Message.objects.filter(source="fb").update(judged_at=timezone.now())
            judged = Message.objects.get(source="fb").judged_at
            ingest.import_emitted(p)
        self.assertEqual(Message.objects.filter(source="fb").count(), 1)
        self.assertEqual(Message.objects.get(source="fb").judged_at, judged)

    def test_fb_negative_controls(self):
        # a different post is a different row
        self.assertNotEqual(ingest.fb_uid("UB", self.SHARED.format("4 hours ago")),
                            ingest.fb_uid("UB", self.SHARED.format("4 hours ago").replace("Oct 19", "Oct 20")))
        # "N days ago" inside the post itself is content, not chrome
        body = "Title\nLine\nMore\nStill more\nThe deadline passed 3 days ago"
        self.assertIn("3 days ago", ingest.fb_clean(body))
        # the old start-of-post case still works
        self.assertEqual(ingest.fb_uid("UB", "+3\nUB Router\n21 hours ago\nMAHAL KITA"),
                         ingest.fb_uid("UB", "14 hours ago\nMAHAL KITA"))

    def test_fb_short_relative_time(self):
        # the PIO page writes "3m" / "1h" (rows 549 + 555 were one post, 2026-10-14)
        self.assertEqual(ingest.fb_uid("PIO", "3m LAO-LAN Arnis team dominates"),
                         ingest.fb_uid("PIO", "1h LAO-LAN Arnis team dominates"))
        self.assertEqual(ingest.fb_clean("2d\nNO CLASSES TOMORROW"), "NO CLASSES TOMORROW")
        # negative controls: a brand, a quantity and a capital M are content
        self.assertEqual(ingest.fb_clean("3M Philippines donates"), "3M Philippines donates")
        self.assertEqual(ingest.fb_clean("5km fun run on Sunday"), "5km fun run on Sunday")


class RejectedSticks(TestCase):
    """B2 (2026-10-14): a rejected proposal must block a near-identical one on the same date —
    the Research Colloquium was rejected, then proposed again twice more the next day."""
    D = date(2026, 10, 15)

    def test_rejected_blocks_the_same_thing(self):
        Proposal.objects.create(kind="event", course="", title="4th Regional Student Research Colloquium",
                                date=self.D, evidence="x", state="rejected")
        self.assertTrue(judge._duplicate("", "event", self.D, "4th Regional Student Research Colloquium"))
        self.assertTrue(judge._duplicate("", "event", self.D, "Regional Research Colloquium (UB)"))

    def test_negative_controls(self):
        Proposal.objects.create(kind="event", course="", title="4th Regional Student Research Colloquium",
                                date=self.D, evidence="x", state="rejected")
        # another date, another course, or an unrelated title is NOT blocked
        self.assertFalse(judge._duplicate("", "event", self.D + timedelta(days=1), "4th Regional Student Research Colloquium"))
        self.assertFalse(judge._duplicate("INTRCS1", "exam", self.D, "4th Regional Student Research Colloquium"))
        self.assertFalse(judge._duplicate("", "event", self.D, "Global Handwashing Day Booth"))


class Views(TestCase):
    def setUp(self):
        self.u = User.objects.create_user("student", password="pw")

    def test_login_required(self):
        for url in ("/", "/inbox/", "/calendar/", "/courses/", "/capture/", "/courses/PROGIT1/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 302, url)

    def test_pages_render(self):
        self.client.login(username="student", password="pw")
        msg("school_mail", title="Quiz", rule_why="graded work", unread=True)
        msg("personal_mail", body="hello")
        for url in ("/", "/inbox/", "/inbox/?src=personal_mail", "/calendar/", "/courses/",
                    "/courses/INTRCS1/", "/capture/", "/manifest.webmanifest", "/sw.js", "/status.json"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
        self.assertContains(self.client.get("/inbox/?src=personal_mail"), "ico-lock")     # 🔒 never sent
        self.assertNotContains(self.client.get("/courses/INTRCS1/"), "Facts only you know")
        pm = Message.objects.get(source="personal_mail")
        self.assertNotContains(self.client.get(f"/inbox/{pm.pk}/"), "Ask Gemini")      # 🔒 no handoff

    # #3 (2026-10-14): systemd killed the Tue 6:33 AM sweep at its 30-min limit, so `finished`
    # was never written — and every page only ever looked at FINISHED runs. It showed nowhere.
    def test_dead_sweep_is_shown(self):
        from .models import Run
        self.client.login(username="student", password="pw")
        r = Run.objects.create(trigger="timer")
        Run.objects.filter(pk=r.pk).update(started=timezone.now() - timedelta(hours=2))
        self.assertContains(self.client.get("/"), "never finished")
        self.assertContains(self.client.get("/inbox/"), "never finished")      # every page

    def test_questions_sit_right_under_today(self):
        # (2026-10-14) "put up the clarifications section under today"
        self.client.login(username="student", password="pw")
        html = self.client.get("/").content.decode()
        heads = [h for h in ("Today</h2>", "Questions for you", "Notices", "Emails") if h in html]
        self.assertEqual(heads, ["Today</h2>", "Questions for you", "Notices", "Emails"])
        self.assertLess(html.index("Questions for you"), html.index("Notices"))
        self.assertLess(html.index("Today</h2>"), html.index("Questions for you"))

    def test_dead_sweep_negative_controls(self):
        from .models import Run
        self.client.login(username="student", password="pw")
        Run.objects.create(trigger="timer")                                        # still running
        done = Run.objects.create(trigger="timer", finished=timezone.now())
        Run.objects.filter(pk=done.pk).update(started=timezone.now() - timedelta(hours=2))
        old = Run.objects.create(trigger="timer")                                  # > 24 h ago
        Run.objects.filter(pk=old.pk).update(started=timezone.now() - timedelta(hours=30))
        self.assertNotContains(self.client.get("/"), "never finished")

    def test_reject_and_unknown_verdict(self):
        self.client.login(username="student", password="pw")
        p = Proposal.objects.create(kind="quiz", title="x", evidence="y", date=date(2026, 10, 14))
        self.assertEqual(self.client.post(f"/proposal/{p.pk}/delete/").status_code, 404)
        self.client.post(f"/proposal/{p.pk}/reject/")
        p.refresh_from_db()
        self.assertEqual(p.state, "rejected")

    def test_capture_saves(self):
        self.client.login(username="student", password="pw")
        with mock.patch("core.views._bg"):
            self.client.post("/capture/", {"text": "quiz next meeting", "course": "INTRCS1"})
        self.assertEqual(Capture.objects.count(), 1)


class Demo(TestCase):
    """manage.py demo WIPES the database, so it must refuse unless demo mode is on, and demo
    mode must never start a background job (sweep, judge, study pack) that could reach a real
    account."""

    def test_seed_refuses_outside_demo_mode(self):
        from . import demo
        Message.objects.create(uid="real:1", source="school_mail", body="real data")
        with override_settings(DEMO=False), self.assertRaises(RuntimeError):
            demo.seed()
        self.assertTrue(Message.objects.filter(uid="real:1").exists())      # nothing wiped

    @override_settings(DEMO=True, LOGIN_USERNAME="student")
    def test_seed_fills_every_page(self):
        from . import demo
        r = demo.seed()
        self.assertGreater(r["messages"], 5)
        self.client.login(username="student", password=demo.PASSWORD)
        for url in ("/", "/inbox/", "/calendar/", "/courses/", "/courses/HUMCOM1/", "/capture/"):
            self.assertEqual(self.client.get(url).status_code, 200, url)
        today = self.client.get("/")
        self.assertContains(today, "Week 9 Group Report")                  # due today
        self.assertContains(today, "Lab 4: Loops")                         # overdue
        self.assertContains(today, "To approve")                           # pending proposals
        self.assertContains(today, 'class="demo"')                         # the badge shows

    @override_settings(DEMO=True)
    def test_demo_mode_starts_no_background_job(self):
        self.client.force_login(User.objects.create_user("x", password="pw"))
        with mock.patch("core.views.subprocess.Popen") as popen:
            self.client.post("/sweep/")
            self.client.post("/capture/", {"text": "quiz next meeting", "course": "INTRCS1"})
            self.client.post("/courses/INTRCS1/pack/")
        popen.assert_not_called()

    def test_control_outside_demo_mode_a_sweep_does_start(self):
        # Negative control: the assertion above must be able to fail.
        self.client.force_login(User.objects.create_user("x", password="pw"))
        with override_settings(DEMO=False), mock.patch("core.views.subprocess.Popen") as popen, \
                mock.patch("core.views.ingest.is_running", return_value=False), \
                mock.patch("builtins.open", mock.mock_open()):
            self.client.post("/sweep/")
        popen.assert_called_once()


class Clash(TestCase):
    def test_a_work_class_overlap_renders(self):
        """A 💼 WORK event overlapping a class used to 500 Today and Calendar (dayname got a
        datetime)."""
        from .models import CalEvent
        self.client.force_login(User.objects.create_user("x", password="pw"))
        start = timezone.now() + timedelta(days=1)
        CalEvent.objects.create(gid="a", summary="💼 WORK: call", start=start,
                                end=start + timedelta(hours=1), color="3")
        CalEvent.objects.create(gid="b", summary="PROGIT1 — Lab", start=start,
                                end=start + timedelta(hours=2), color="8")
        for url in ("/", "/calendar/"):
            r = self.client.get(url)
            self.assertEqual(r.status_code, 200, url)
            self.assertContains(r, "Clashes")


class CaptureTab(TestCase):
    """The Capture tab, cleaned up 2026-10-12: a note always says what became of it, the
    course defaults to the class on now, and answers are kept apart from notes."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("x", password="pw"))

    def test_save_and_list(self):
        """Chat (2026-10-13): the note is saved for the SWEEP's judge — nothing judged on save —
        and Nebula replies on Flash-Lite."""
        from .models import ChatTurn
        with mock.patch("core.views._bg") as bg, mock.patch("core.chat.reply") as rep:
            self.client.post("/capture/", {"text": "quiz next meeting", "course": "INTRCS1"})
        c = Capture.objects.get()
        self.assertEqual((c.course, c.kind), ("INTRCS1", "note"))
        bg.assert_not_called()                                    # judgement waits for the sweep
        rep.assert_called_once_with("quiz next meeting", "INTRCS1")
        self.assertEqual(ChatTurn.objects.get().capture, c)
        r = self.client.get("/capture/")
        self.assertContains(r, "quiz next meeting")
        self.assertContains(r, "⏳ checked at")

    def test_unknown_course_and_empty_text(self):
        with mock.patch("core.views._bg"):
            self.client.post("/capture/", {"text": "x", "course": "NOPE1"})
            self.client.post("/capture/", {"text": "   ", "course": "INTRCS1"})
        self.assertEqual(list(Capture.objects.values_list("course", flat=True)), [""])

    def test_course_defaults_to_the_class_on_now(self):
        from .models import CalEvent
        from .views import class_now
        now = timezone.now()
        CalEvent.objects.create(gid="c", summary="LITERA1 — Living in the IT Era", color="8",
                                start=now - timedelta(minutes=30), end=now + timedelta(minutes=30))
        CalEvent.objects.create(gid="w", summary="📌 DUE: PROGIT1 — Lab", color="11",
                                start=now - timedelta(minutes=10), end=now + timedelta(minutes=50))
        self.assertEqual(class_now(now), "LITERA1")               # a class, not a deadline
        self.assertEqual(class_now(now + timedelta(hours=3)), "")  # long over → no guess
        r = self.client.get("/capture/")
        self.assertContains(r, 'value="LITERA1" checked')

    def test_outcome_is_recorded(self):
        c = Capture.objects.create(text="Lab exam is open notes", course="INTRCS1")
        a = Capture.objects.create(text="Answer to “Which group?”: 1", kind="answer")
        q = Capture.objects.create(text="Quiz Wed 10:00 AM in F209", course="INTRCS1")
        judge.store({"messages": [], "clarifications": [], "proposals": [
            {"source_id": f"N{q.pk}", "kind": "quiz", "course": "INTRCS1", "title": "INTRCS1 Quiz",
             "date": "2026-10-14", "time": "10:00", "evidence": "Quiz Wed 10:00 AM in F209",
             "label": "stated"}]}, {f"N{c.pk}": c, f"N{a.pk}": a, f"N{q.pk}": q}, "m")
        for x in (c, a, q):
            x.refresh_from_db()
        self.assertEqual(c.outcome, "nothing dated in it")
        self.assertEqual(a.outcome, "kept as context")
        self.assertEqual(q.outcome, "1 proposal")
        from .models import ChatTurn
        ChatTurn.objects.create(role="me", text=q.text, capture=q)
        self.assertContains(self.client.get("/capture/"), "INTRCS1 Quiz")

    def test_duplicate_says_already_tracked(self):
        from .models import Item
        due = timezone.make_aware(datetime(2026, 10, 14, 23, 59))
        Item.objects.create(uid="i", course="HUMCOM1", title="Week 9 Group Reporting", due=due)
        c = Capture.objects.create(text="Week 9 group reporting due Wed", course="HUMCOM1")
        judge.store({"messages": [], "clarifications": [], "proposals": [
            {"source_id": f"N{c.pk}", "kind": "deadline", "course": "HUMCOM1",
             "title": "Week 9 Group Reporting", "date": "2026-10-14", "time": "",
             "evidence": "due Wed", "label": "stated"}]}, {f"N{c.pk}": c}, "m")
        c.refresh_from_db()
        self.assertEqual(c.outcome, "already tracked: Week 9 Group Reporting")

    def test_answers_are_kept_apart(self):
        from .models import Clarification
        q = Clarification.objects.create(question="Which group?")
        self.client.post(f"/question/{q.pk}/", {"answer": "Group 1"})
        a = Capture.objects.get()
        self.assertEqual(a.kind, "answer")
        r = self.client.get("/capture/")
        self.assertContains(r, "No messages yet")                 # not listed as a chat line
        self.assertContains(r, "Your answers to questions (1)")

    def test_delete(self):
        c = Capture.objects.create(text="typo note")
        p = Proposal.objects.create(kind="quiz", title="x", evidence="y", capture=c)
        self.assertEqual(self.client.get(f"/capture/{c.pk}/delete/").status_code, 405)   # POST only
        self.client.post(f"/capture/{c.pk}/delete/")
        self.assertFalse(Capture.objects.exists())
        self.assertTrue(Proposal.objects.filter(pk=p.pk).exists())  # the proposal stays

    def test_status_counts_waiting_notes(self):
        Capture.objects.create(text="a")
        Capture.objects.create(text="b", judged_at=timezone.now())
        self.assertEqual(self.client.get("/status.json").json()["captures_waiting"], 1)

    def test_a_note_saved_during_a_sweep_is_judged_at_its_end(self):
        Capture.objects.create(text="quiz next meeting")
        with mock.patch.object(ingest, "_lms", return_value=(2, '{"ok": false}', "")), \
                mock.patch.object(ingest, "import_ledger", return_value=0), \
                mock.patch.object(ingest, "import_personal_mail", return_value=0), \
                mock.patch.object(ingest, "import_calendar", return_value=0), \
                mock.patch.object(judge, "run", return_value="ok") as jr:
            ingest.sweep(trigger="test", apply_calendar=False)
        self.assertEqual([c.kwargs for c in jr.call_args_list], [{}, {"include_messages": False}])


class Review(TestCase):
    """The 2026-10-12 review: each test is a thing a page really showed wrong."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("x", password="pw"))

    def test_clean_title(self):
        from .present import clean_event, clean_title
        raw = "Week 9- Group Reporting-Interaction Design Tools/Ethical&SocialImplications-10122026"
        self.assertEqual(clean_title(raw),
                         "Week 9 - Group Reporting-Interaction Design Tools / Ethical & Social Implications")
        for keep in ("M LAB FA2 (If-Else, If-Else-If, Challenge: While; Code: A)", "PowerPoint Basics",
                     "Lesson1.2-Self-Check Assessment", "MLW1: BIOS"):
            self.assertEqual(clean_title(keep), keep)               # real hyphens and words untouched
        self.assertEqual(clean_title("Week 6- Activity 1- 9/21&23/26"), "Week 6 - Activity 1")
        self.assertEqual(clean_title("M LAB FA1 (if-else) [9/22,24]"), "M LAB FA1 (if-else)")
        self.assertEqual(clean_event("📌 DUE: HUMCOM1 — Week 9- Report-10122026 (50 pts)"),
                         "📌 DUE: HUMCOM1 — Week 9 - Report (50 pts)")

    def test_absolute_tag(self):
        from .present import absolute_tag
        sun = timezone.make_aware(datetime(2026, 10, 11, 20, 38))
        self.assertEqual(absolute_tag("Quiz tomorrow, do not be late", sun), "Quiz Mon 12 Oct, do not be late")
        self.assertEqual(absolute_tag("Upload the video tonight", sun), "Upload the video Sun 11 Oct night")
        self.assertEqual(absolute_tag("Paper due next meeting", sun), "Paper due next meeting")

    def test_judge_stores_absolute_tags(self):
        m = msg("chat_space", body="Quiz tomorrow", when=timezone.make_aware(datetime(2026, 10, 11, 20, 38)))
        judge.store({"messages": [{"id": f"M{m.pk}", "relevant": True, "tag": "Quiz tomorrow"}],
                     "proposals": [], "clarifications": []}, {f"M{m.pk}": m}, "x")
        m.refresh_from_db()
        self.assertEqual(m.judge_tag, "Quiz Mon 12 Oct")

    def _run_with_held(self, course, title):
        from .models import Run
        Run.objects.create(finished=timezone.now(), complete=True,
                           artifact={"held_by_you": [{"course": course, "title": title}]})

    def test_course_page_respects_done_by_you(self):
        from .models import Item
        Item.objects.create(uid="a", course="LITERA1", title="Cybersecurity",
                            due=timezone.now() - timedelta(days=3), pts=100)
        self._run_with_held("LITERA1", "Cybersecurity")
        r = self.client.get("/courses/LITERA1/")
        self.assertContains(r, "marked done by you")
        self.assertNotContains(r, "ico-st-overdue")

    def test_courses_page_counts_calendar_items(self):
        from .models import CalEvent
        start = timezone.now() + timedelta(hours=8)
        CalEvent.objects.create(gid="q", summary="📌 QUIZ: LITERA1 — Quiz · via Chat (Domingo)",
                                start=start, end=start + timedelta(hours=1), color="11")
        r = self.client.get("/courses/")
        self.assertContains(r, "Next: <b>Quiz</b>")
        self.assertContains(r, "1 ahead")

    def test_tomorrows_quiz_has_a_gemini_button(self):
        # 2026-10-13, the student's screenshot: Tomorrow's two quizzes had ✓ but no ✨ — the night
        # before a quiz is exactly when he reaches for it.
        from datetime import datetime, time
        from .models import CalEvent
        start = timezone.make_aware(datetime.combine(timezone.localdate() + timedelta(days=1), time(8, 0)))
        CalEvent.objects.create(gid="tq", summary="📌 QUIZ: PROGIT1 — Quiz (topic not given)",
                                start=start, end=start + timedelta(hours=1), color="11")
        r = self.client.get("/")
        tmr = r.content.decode().split('class="tmr"', 1)[1].split("</div></div>", 1)[0]
        self.assertIn('class="btn small gem"', tmr)
        self.assertIn("PROGIT1 (Computer Programming)", tmr)

    def test_course_proposals_read_cleanly(self):
        Proposal.objects.create(kind="quiz", course="LITERA1", title="Quiz", evidence="e",
                                date=timezone.localdate() + timedelta(days=1), state="written")
        Proposal.objects.create(kind="quiz", course="LITERA1", title="Old dup", evidence="e",
                                date=timezone.localdate() + timedelta(days=1), state="duplicate")
        r = self.client.get("/courses/LITERA1/")
        self.assertNotContains(r, "Quiz Quiz")
        self.assertContains(r, "Tomorrow")
        self.assertNotContains(r, "Old dup")                        # the calendar's now

    def test_inbox_is_school_first_and_paged(self):
        for k in range(55):
            msg("school_mail", title=f"school {k}", when=timezone.now() - timedelta(minutes=k))
        msg("personal_mail", title="Job alert", when=timezone.now())
        r = self.client.get("/inbox/")
        self.assertNotContains(r, "Job alert")
        self.assertContains(r, "Older ›")
        self.assertContains(self.client.get("/inbox/?src=personal_mail"), "Job alert")
        self.assertContains(self.client.get("/inbox/?only=everything"), "Job alert")
        p2 = self.client.get("/inbox/?page=2")
        self.assertContains(p2, "‹ Newer")
        self.assertContains(p2, "school 54")

    def test_pack_status_rechecks_drive(self):
        from django.conf import settings
        from . import studypack
        with tempfile.TemporaryDirectory() as d, override_settings(DATA_DIR=Path(d)):
            (Path(d) / "packs").mkdir()
            f = Path(d) / "packs" / "LITERA1.status.json"
            f.write_text(json.dumps({"copied": True, "md5": "abc", "drive_verified": False, "built": "x"}))
            with mock.patch.object(studypack, "drive_verified", return_value=True) as dv:
                self.client.get("/courses/LITERA1/")
                self.client.get("/courses/LITERA1/")
            self.assertEqual(dv.call_count, 1)                       # remembered after the first yes
            self.assertTrue(json.loads(f.read_text())["drive_verified"])

    def test_backup_daily_and_rotated(self):
        import sqlite3
        from . import backup
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "db.sqlite3"
            sqlite3.connect(str(db)).execute("create table t (x)").connection.commit()
            from django.db import connection
            with override_settings(DATA_DIR=Path(d)), \
                    mock.patch.dict(connection.settings_dict, {"NAME": str(db)}):
                (Path(d) / "backups").mkdir()
                for k in range(16):
                    (Path(d) / "backups" / f"nebula-2026-09-{k + 1:02d}.sqlite3").write_text("old")
                f = backup.daily()
                self.assertIsNotNone(f)
                self.assertIsNone(backup.daily())                    # once per day
                self.assertEqual(oct(f.stat().st_mode & 0o777), "0o600")
                self.assertEqual(sqlite3.connect(str(f)).execute(
                    "select name from sqlite_master").fetchall(), [("t",)])
                self.assertEqual(len(list((Path(d) / "backups").glob("nebula-*.sqlite3"))), 14)


class PatchCal:
    """A calendar that stores events by id and supports list / get / patch (the ✓ path)."""
    def __init__(self, events, drop=()):
        self.ev = {e["id"]: dict(e) for e in events}
        self.drop, self.patches = set(drop), []

    def events(self):
        return self

    def list(self, **kw):
        return _X({"items": list(self.ev.values())})

    def get(self, calendarId, eventId):
        return _X(dict(self.ev[eventId]))

    def patch(self, calendarId, eventId, body):
        self.patches.append(body)
        kept = {k: v for k, v in body.items() if k not in self.drop}   # drop = a write that didn't stick
        self.ev[eventId].update(kept)
        return _X(self.ev[eventId])


class MarkDone(TestCase):
    """✓ in Nebula (2026-10-12) closes the event by the calendar skill's contract."""

    def _item(self, uid="_808_1:_30474_1"):
        from .models import Item
        # One minute out: due TODAY (two hours out crosses midnight in the evening and lands
        # on Tomorrow, which made the Today test pass with its filter removed).
        return Item.objects.create(uid=uid, course="HUMCOM1", title="Week 9- Group Reporting",
                                   due=timezone.now() + timedelta(minutes=1), pts=50)

    def test_gradebook_item_closes_its_bb_event_as_submitted_and_is_held(self):
        from . import done
        it = self._item()
        cal = PatchCal([{"id": "e1", "summary": "📌 DUE: HUMCOM1 — Week 9 (50 pts)", "colorId": "4",
                         "description": "Due 11:59 PM\nbb:_808_1:_30474_1"},
                        {"id": "e2", "summary": "📌 DUE: HUMCOM1 — other", "colorId": "4",
                         "description": "bb:_808_1:_99999_1"}])
        dm, _ = done.mark(it.uid, "SUBMITTED", svc=cal)
        e = cal.ev["e1"]
        self.assertEqual(e["summary"], "✅ SUBMITTED: HUMCOM1 — Week 9 (50 pts)")
        self.assertEqual(e["colorId"], "2")
        self.assertIn("\nhuman: done\n", e["description"])     # cal_sync.human_done HOLDS it
        self.assertIn("bb:_808_1:_30474_1", e["description"])   # the marker survives
        self.assertTrue(cal.ev["e2"]["summary"].startswith("📌"))  # the other event untouched
        self.assertEqual(dm.event_id, "e1")

    def test_cal_sync_reads_the_close_as_human_done(self):
        """Negative-controlled against the real reader: before ✓ it is NOT held, after it IS."""
        import sys
        from django.conf import settings
        sys.path.insert(0, str(settings.LMS_DIR))
        from cal_sync import human_done
        from . import done
        it = self._item()
        ev0 = {"id": "e1", "summary": "📌 DUE: HUMCOM1 — Week 9 (50 pts)", "colorId": "4",
               "description": "bb:_808_1:_30474_1"}
        self.assertFalse(human_done(dict(ev0)))
        cal = PatchCal([ev0])
        done.mark(it.uid, "SUBMITTED", svc=cal)
        self.assertTrue(human_done(cal.ev["e1"]))

    def test_event_attended_keeps_peacock(self):
        from . import done
        cal = PatchCal([{"id": "g1", "summary": "📌 EVENT: NSTP General Assembly", "colorId": "7"}])
        done.mark("g1", "DONE", svc=cal)                         # word corrected to ATTENDED
        self.assertEqual(cal.ev["g1"]["summary"], "✅ ATTENDED: NSTP General Assembly")
        self.assertEqual(cal.ev["g1"]["colorId"], "7")

    def test_quiz_closes_to_done(self):
        from . import done
        cal = PatchCal([{"id": "q1", "summary": "📌 QUIZ: INTRCS1 — Hexa to binary quiz", "colorId": "4"}])
        done.mark("q1", "SUBMITTED", svc=cal)
        self.assertEqual(cal.ev["q1"]["summary"], "✅ DONE: INTRCS1 — Hexa to binary quiz")
        self.assertEqual(cal.ev["q1"]["colorId"], "2")

    def test_failed_write_records_nothing(self):
        from . import done
        from .models import DoneMark
        cal = PatchCal([{"id": "q1", "summary": "📌 QUIZ: INTRCS1 — x", "colorId": "4"}], drop={"colorId"})
        with self.assertRaises(RuntimeError):
            done.mark("q1", "DONE", svc=cal)
        self.assertFalse(DoneMark.objects.exists())             # a failure never looks done

    def test_item_without_event_is_recorded_not_created(self):
        from . import done
        it = self._item("_811_1:_1_1")
        cal = PatchCal([])
        dm, msg = done.mark(it.uid, "DONE", svc=cal)
        self.assertEqual(dm.event_id, "")
        self.assertEqual(cal.patches, [])
        self.assertIn("no calendar event", msg)

    def test_undo_restores_exactly(self):
        from . import done
        from .models import DoneMark
        ev0 = {"id": "e1", "summary": "📌 DUE: MATHMW1 — Coursework", "colorId": "4", "description": "x"}
        cal = PatchCal([ev0])
        dm, _ = done.mark("e1", "DONE", svc=cal)
        done.undo(dm, svc=cal)
        self.assertEqual({k: cal.ev["e1"][k] for k in ("summary", "colorId", "description")},
                         {"summary": "📌 DUE: MATHMW1 — Coursework", "colorId": "4", "description": "x"})
        self.assertFalse(DoneMark.objects.exists())

    def test_marked_item_leaves_today(self):
        from .models import DoneMark
        u = User.objects.create_user("student", password="pw")
        self.client.login(username="student", password="pw")
        it = self._item()
        self.assertContains(self.client.get("/"), "Week 9")
        DoneMark.objects.create(key=it.uid, course="HUMCOM1", title=it.title, word="SUBMITTED")
        r = self.client.get("/")
        self.assertContains(r, "Marked done")
        self.assertNotContains(r, 'value="_808_1:_30474_1"')   # no ✓ left for it


class DriveContext(TestCase):
    """Drive organised for Gemini (2026-10-12): the guide + the per-task Blackboard files."""

    def setUp(self):
        from django.conf import settings
        d = settings.DATA_DIR / "materials" / "MATHMW1"
        d.mkdir(parents=True, exist_ok=True)
        (d / "_manifest.json").write_text(json.dumps({"files": [
            {"path": "Week 1/MATHMW1 SYLLABUS.pdf", "title": "Syllabus", "kind": "file",
             "created": "2026-08-10T01:00:00Z"},
            {"path": "Midterms/Relations and Functions.pptx", "title": "Relations and Functions",
             "kind": "file", "created": "2026-10-08T03:25:00Z"},
            {"path": "Midterms/Quiz 2_Midterms.pdf", "title": "Quiz 2_Midterms", "kind": "file",
             "created": "2026-10-08T03:34:00Z"}],
            "links": [{"title": "GeoGebra", "url": "https://geogebra.org", "folder": ""}]}))

    def tearDown(self):
        from django.conf import settings
        import shutil
        shutil.rmtree(settings.DATA_DIR / "materials", ignore_errors=True)

    def test_task_prompt_names_the_guide_and_the_newest_files(self):
        from . import tutor
        p = tutor.prompt("MATHMW1", "Coursework activity — answers on 1 whole sheet")
        self.assertIn('"MATHMW1 course guide"', p)
        self.assertIn("Midterms/Quiz 2_Midterms.pdf", p)            # no shared word, but newest
        self.assertLess(p.index("Quiz 2_Midterms"), p.index("SYLLABUS"))

    def test_word_match_beats_recency(self):
        from . import tutor
        self.assertEqual(tutor.bb_files("MATHMW1", "Syllabus quiz", limit=1), ["Week 1/MATHMW1 SYLLABUS.pdf"])

    def _cache(self, rows):
        from . import drive
        drive.listing_cache().write_text(json.dumps({"MATHMW1": rows}))
        self.addCleanup(drive.listing_cache().unlink, missing_ok=True)

    def test_prompt_names_my_newest_reviewer_and_nothing_older(self):
        # 2026-10-13: two quiz reviewers sat in study\ and the ✨ prompt never named them.
        from . import tutor
        self._cache([["study\\week4-cram.pdf", "2026-09-10T02:00:00Z"],
                     ["study\\quiz-functions-review.pdf", "2026-10-13T12:53:00Z"],
                     ["my-work\\Pattern_20264316.pdf", "2026-10-13T13:00:00Z"],
                     ["materials\\blackboard\\x.pdf", "2026-10-13T13:00:00Z"]])
        p = tutor.prompt("MATHMW1", "Quiz (topic not given)")
        self.assertIn("study/quiz-functions-review.pdf", p)
        self.assertNotIn("week4-cram", p)          # no shared word: the newest only, no padding
        self.assertNotIn("Pattern_20264316", p.split("My own reviewers")[1].split("Blackboard files")[0])
        self.assertLess(p.index("My own reviewers"), p.index("Blackboard files most likely"))

    def test_reviewer_word_match_beats_recency(self):
        from . import tutor
        self._cache([["study\\relations-review.pdf", "2026-09-01T00:00:00Z"],
                     ["study\\newest-other.pdf", "2026-10-13T00:00:00Z"]])
        self.assertEqual(tutor.study_files("MATHMW1", "Relations quiz", limit=1),
                         ["study/relations-review.pdf"])

    def test_a_word_prefix_counts_as_shared(self):
        # 2026-10-13: the ticket said "Hexa", the lessons say "Hexadecimal".
        from . import tutor
        self._cache([["study\\hexadecimal-cram.pdf", "2026-09-01T00:00:00Z"],
                     ["study\\binary-cram.pdf", "2026-09-02T00:00:00Z"],
                     ["study\\newest-other.pdf", "2026-10-13T00:00:00Z"]])
        self.assertEqual(tutor.study_files("MATHMW1", "Hexa to binary quiz"),
                         ["study/binary-cram.pdf", "study/hexadecimal-cram.pdf"])

    def test_no_cache_no_reviewer_section(self):
        from . import tutor
        self.assertEqual(tutor.study_files("MATHMW1", "anything"), [])
        self.assertNotIn("My own reviewers", tutor.prompt("MATHMW1", "anything"))

    def test_listing_is_cached_with_stamps_and_a_failed_walk_keeps_it(self):
        from . import drive
        good = mock.Mock(returncode=0, stdout=f"MATHMW1\\study\\r.pdf|2026-10-13T12:53:00Z\n"
                                              f"MATHMW1\\desktop.ini|2026-10-13T12:53:00Z\n")
        with mock.patch("core.drive.subprocess.run", return_value=good):
            self.assertEqual(drive.drive_listing(), {"MATHMW1": ["study\\r.pdf"]})
        self.addCleanup(drive.listing_cache().unlink, missing_ok=True)
        cached = json.loads(drive.listing_cache().read_text())
        self.assertEqual(cached, {"MATHMW1": [["study\\r.pdf", "2026-10-13T12:53:00Z"]]})
        with mock.patch("core.drive.subprocess.run", return_value=mock.Mock(returncode=1, stdout="")):
            drive.drive_listing()
        self.assertEqual(json.loads(drive.listing_cache().read_text()), cached)

    def test_guide_lists_every_file_folder_and_due_item(self):
        from . import drive
        from .models import Item
        Item.objects.create(uid="_811_1:_9_1", course="MATHMW1", title="Coursework activity",
                            due=timezone.now() + timedelta(days=1))
        Item.objects.create(uid="_811_1:_8_1", course="MATHMW1", title="Already in",
                            due=timezone.now() + timedelta(days=1), submitted=True)
        text = "\n".join(t for _, t in drive.guide_lines("MATHMW1", ["my-work\\Pattern_20264316.pdf",
                                                                      "study\\cram.pdf"]))
        self.assertIn("Coursework activity", text)
        self.assertNotIn("Already in", text)
        self.assertIn("Midterms\\Quiz 2_Midterms.pdf", text)
        self.assertIn("my-work\\Pattern_20264316.pdf", text)
        self.assertIn("https://geogebra.org", text)

    def test_guide_never_carries_personal_mail(self):
        from . import drive
        msg("personal_mail", body="SECRET bank OTP 123456", course="MATHMW1", judge_relevant=True)
        text = "\n".join(t for _, t in drive.guide_lines("MATHMW1", []))
        self.assertNotIn("SECRET", text)

    def test_docx_is_written(self):
        from django.conf import settings
        from . import drive
        out = drive.write_docx(drive.guide_lines("MATHMW1", []), settings.DATA_DIR / "g" / "x.docx")
        from docx import Document
        self.assertIn("MATHMW1", Document(str(out)).paragraphs[0].text)


class DoneLabels(TestCase):
    def test_hand_written_labels_close_cleanly(self):
        from . import done
        cal = PatchCal([{"id": "s1", "summary": "📌 NO DUE DATE: SOCORN2 — Mental Health (30 pts)", "colorId": "4"},
                        {"id": "n1", "summary": "📌 NOTICE: PATHFIT1 — no class", "colorId": "4"}])
        done.mark("s1", "DONE", svc=cal)
        done.mark("n1", "DONE", svc=cal)                       # word corrected to READ
        self.assertEqual(cal.ev["s1"]["summary"], "✅ DONE: SOCORN2 — Mental Health (30 pts)")
        self.assertEqual(cal.ev["n1"]["summary"], "✅ READ: PATHFIT1 — no class")


class SnapshotNow(TestCase):
    def test_close_updates_the_local_agenda_without_a_sweep(self):
        from . import done
        from .models import CalEvent
        st = {"dateTime": "2026-10-14T08:00:00+08:00"}; en = {"dateTime": "2026-10-14T09:00:00+08:00"}
        CalEvent.objects.create(gid="q9", summary="📌 QUIZ: PROGIT1 — Quiz", color="4",
                                start=timezone.now(), end=timezone.now())
        cal = PatchCal([{"id": "q9", "summary": "📌 QUIZ: PROGIT1 — Quiz", "colorId": "4",
                         "start": st, "end": en}])
        done.mark("q9", "DONE", svc=cal)
        self.assertEqual(CalEvent.objects.get(gid="q9").summary, "✅ DONE: PROGIT1 — Quiz")


class Chat(TestCase):
    """Capture chat: Flash-Lite talks, the sweep judges, Check now leaves the sweeps their quota."""

    def setUp(self):
        self.client.force_login(User.objects.create_user("x", password="pw"))

    @FAKE_KEY
    def test_reply_uses_only_flash_lite_and_only_school_data(self):
        from . import chat
        from .models import CalEvent, Item
        Item.objects.create(uid="i1", course="PROGIT1", title="M LAB FA5", due=timezone.now() + timedelta(days=1))
        msg("personal_mail", body="SECRET bank OTP 99", judge_relevant=True, judge_tag="SECRET")
        msg("chat_dm", body="classmate gossip SECRET2", judge_relevant=True, judge_tag="SECRET2")
        CalEvent.objects.create(gid="g", summary="💼 WORK: CALSECRET", start=timezone.now(), end=timezone.now())
        seen = {}

        def fake(url, payload, key, timeout=300):
            seen["url"], seen["body"] = url, json.dumps(payload)
            return {"candidates": [{"content": {"parts": [{"text": json.dumps(
                {"reply": "Noted — checked at the next sweep.", "urgent": True,
                 "urgent_why": "PROGIT1 quiz 8:00 AM tomorrow"})}]}}]}
        with mock.patch.object(gemini, "_post_json", side_effect=fake):
            t = chat.reply("quiz tomorrow 8am progit", "PROGIT1")
        self.assertIn("gemini-3.5-flash-lite", seen["url"])
        self.assertIn("M LAB FA5", seen["body"])                   # school data is there
        for secret in ("SECRET", "SECRET2", "CALSECRET"):
            self.assertNotIn(secret, seen["body"])                 # 🔒 never sent
        self.assertTrue(t.urgent)
        used = {q["model"]: q["used"] for q in quota.summary()}
        self.assertEqual(used["gemini-3.8-flash"], 0)              # the judge's bucket untouched

    @FAKE_KEY
    def test_failure_still_keeps_the_note(self):
        from . import chat
        with mock.patch.object(gemini, "_post_json", side_effect=TimeoutError("slow")):
            t = chat.reply("hello")
        self.assertIn("note is saved", t.text)

    def test_check_now_offered_only_when_urgent_and_runs_with_a_reserve(self):
        from .models import ChatTurn
        c = Capture.objects.create(text="quiz 8am tomorrow")
        ChatTurn.objects.create(role="me", text=c.text, capture=c)
        calm = ChatTurn.objects.create(role="nebula", text="ok")
        self.assertNotContains(self.client.get("/capture/"), "Check now")
        hot = ChatTurn.objects.create(role="nebula", text="ok", urgent=True, urgent_why="quiz 8 AM")
        self.assertContains(self.client.get("/capture/"), "Check now")
        with mock.patch("core.views._bg", return_value=True) as bg:
            self.client.post(f"/capture/check/{hot.pk}/")
        bg.assert_called_once_with("judge", "--captures-only", "--reserve", "10")

    def test_reserve_keeps_ten_for_the_sweeps(self):
        from .models import QuotaUse
        d = quota.pacific_day()
        for m, _, rpd in quota.CHAIN[:-1]:
            QuotaUse.objects.create(model=m, day=d, count=rpd - 10)  # exactly 10 left on each
        self.assertEqual(quota.next_model(reserve=10), "gemini-3.5-flash-lite")
        self.assertEqual(quota.next_model(), "gemini-3.8-flash")    # the sweep itself may use them


class ChatFilesOnDrive(TestCase):
    def test_chat_files_staged_to_materials_chat_and_listed(self):
        from django.conf import settings
        from . import drive
        d = settings.DATA_DIR / "materials" / "LITERA1"
        (d / "_chat").mkdir(parents=True, exist_ok=True)
        (d / "_chat" / "2026-10-07 brief.pdf").write_bytes(b"x")
        (d / "_chat_manifest.json").write_text(json.dumps({"files": [
            {"path": "_chat/2026-10-07 brief.pdf", "author": "Elena Domingo", "posted": "2026-10-07",
             "message": "Policy brief format"}], "drive_only": [{"file": "Rubric", "posted": "2026-10-08"}]}))
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.object(drive, "STAGE", tmp):
            self.assertEqual(drive.stage_mirror("LITERA1"), 1)
        self.assertTrue((tmp / "LITERA1" / "materials" / "chat" / "2026-10-07 brief.pdf").exists())
        self.assertFalse((tmp / "LITERA1" / "materials" / "blackboard" / "_chat").exists())
        text = "\n".join(t for _, t in drive.guide_lines("LITERA1", []))
        self.assertIn("2026-10-07 brief.pdf", text)
        self.assertIn("Policy brief format", text)
        self.assertIn("(Drive link, open in Chat) Rubric", text)
