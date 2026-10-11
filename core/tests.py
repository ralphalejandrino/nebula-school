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
from .models import Capture, Message, Proposal, QuotaUse
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
