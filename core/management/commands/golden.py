"""Golden set: REAL messages that were once missed or late, replayed through the live judge.

Each group runs with "today" set just after its messages were sent (so the judge's
"no past events" rule does not hide them), with an empty KNOWN list, and NOTHING is
stored — the replies are scored in memory and a report is printed (and appended to
school/context/history/gemini-trial.md with --record). Costs one request per group.
"""
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import gemini, judge, quota
from core.models import Message
from core.privacy import eligible

MNL = ZoneInfo("Asia/Manila")


def at(s):
    return datetime.fromisoformat(s).replace(tzinfo=MNL)


def synthetic(pk, source, course, body, when):
    m = Message(pk=pk, uid=f"golden:{pk}", source=source, course=course, body=body, when=when,
                author="Instructor")
    m.gemini_eligible = eligible(source)
    return m


def check_props(props, ok_fn):
    return any(ok_fn(p) for p in props)


GROUPS = [
    {"name": "PROGIT1 exam room+time, missed 32 h (2026-09-15)", "today": "2026-09-15T10:45",
     "pks": [6],
     "checks": [
         ("lec exam Tue 9/15 2:00 PM @F212",
          lambda p: p["date"] == "2026-09-15" and p["time"].startswith("14") and "F212" in p["room"]),
         ("lab exam Thu 9/17 2:00 PM", lambda p: p["date"] == "2026-09-17" and p["time"].startswith("14")),
         ("never invents a room for the lab exam",
          lambda p: not (p["date"] == "2026-09-17" and p["room"] and "lab" not in p["room"].lower())),
     ]},
    {"name": "Instructor DM: exam stays 5-6 PM (2026-09-17)", "today": "2026-09-17T20:40",
     "pks": [239, 238, 237],
     "checks": [("exam Fri 9/18 5:00 PM", lambda p: p["date"] == "2026-09-18" and p["time"].startswith("17"))]},
    {"name": "MATHMW1 coursework + faculty evaluation (2026-09-25)", "today": "2026-09-25T15:00",
     "pks": [23, 1],
     "checks": [("MATHMW1 due Tue 9/29", lambda p: p["date"] == "2026-09-29" and p["course"] == "MATHMW1"),
                ("faculty evaluation by 10/31", lambda p: p["date"] == "2026-10-31")]},
    {"name": "soft wording must not harden (synthetic)", "today": "2026-10-05T09:00",
     "synthetic": [("bb_announce", "HUMCOM1",
                    "Good morning class! Kindly submit your reflection paper preferably this week. "
                    "Thank you and God bless.", "2026-10-05T08:30")],
     "checks": [("no STATED+VERIFIED hard deadline",
                 "NOT", lambda p: p["label"] == "stated" and p["_verified"])]},
    # #2 (2026-10-14): the instructor re-posted the schedule with the LEC moved Mon 10-26 → Wed 10-21.
    {"name": "INTRCS1 lecture exam moved 10-26 → 10-21 (2026-10-14)", "today": "2026-10-14T18:40",
     "pks": [607],
     "known": "- INTRCS1 | Exam Lecture Exam | 2026-10-26 11:00:00\n- INTRCS1 | Exam Lab Exam | 2026-10-22 09:30:00",
     "checks": [("lecture exam Wed 10/21 with moved_from 10/26",
                 lambda p: p["date"] == "2026-10-21" and p.get("moved_from") == "2026-10-26"),
                ("does not move the lab exam", "NOT",
                 lambda p: p["date"] == "2026-10-22" and p.get("moved_from"))]},
    # #1 (2026-10-14): the student rejected 13 of 14 Facebook proposals in three days — games,
    # a colloquium, a symposium, a booth, an ASEAN celebration. All real posts.
    {"name": "Facebook PR is not a proposal (rejected 10-12 → 10-14)", "today": "2026-10-14T10:00",
     "pks": [457, 487, 496, 544, 571],
     "checks": [("no proposal from any of the five posts", "NOT", lambda p: True)]},
    {"name": "Facebook class suspension IS a proposal (synthetic)", "today": "2026-10-14T19:00",
     "synthetic": [("fb", "",
                    "ADVISORY | SUSPENSION OF CLASSES\nDue to Tropical Storm Ompong, face-to-face and "
                    "online classes at all levels of the University of Baguio are suspended on "
                    "Thursday, October 15, 2026. Stay safe, UBians!", "2026-10-14T18:30")],
     "checks": [("a proposal for Thu 10/15", lambda p: p["date"] == "2026-10-15")]},
]


class Command(BaseCommand):
    help = "Replay the golden set of real past misses through the live free-Gemini judge."

    def add_arguments(self, p):
        p.add_argument("--record", action="store_true", help="append the report to the trial ledger")
        p.add_argument("--only", default="", help="run only groups whose name contains this text")

    def handle(self, *a, **o):
        lines = [f"### Golden set — {timezone.localtime():%a %Y-%m-%d %I:%M %p}", ""]
        passed = total = 0
        for g in [g for g in GROUPS if o["only"].lower() in g["name"].lower()]:
            msgs = list(Message.objects.filter(pk__in=g.get("pks", [])))
            for i, (src, course, body, when) in enumerate(g.get("synthetic", [])):
                msgs.append(synthetic(900000 + i, src, course, body, at(when)))
            system, user, idmap = judge.build_payload(msgs, [], today=at(g["today"]), known=g.get("known", "(none)"))
            try:
                reply, model = judge.call(system, user)
            except judge.NoModel as e:
                total += len(g["checks"])                  # an unanswered group FAILS its checks
                lines.append(f"- 🔴 **{g['name']}** — UNKNOWN: {str(e)[:200]}")
                continue
            props = reply.get("proposals", [])
            for p in props:
                src = idmap.get(p.get("source_id"))
                p.setdefault("room", "")
                p["_verified"] = judge.verify(p.get("evidence", ""), src.body if src else "",
                                              bool(p.get("date")))[0]
            lines.append(f"- **{g['name']}** ({model}) — {len(props)} proposal(s):")
            for p in props:
                lines.append(f"  - {p['kind']} {p['course'] or '-'} {p['date']} {p['time'] or ''} "
                             f"{('(moved from ' + p['moved_from'] + ') ') if p.get('moved_from') else ''}"
                             f"{('@' + p['room']) if p['room'] else ''} · {p['label']} · "
                             f"{'✅ verified' if p['_verified'] else '⚠ unverified'} — “{p['evidence'][:90]}”")
            for chk in g["checks"]:
                total += 1
                if len(chk) == 3:                      # negative check: must NOT happen
                    label, _, fn = chk
                    ok = not check_props(props, fn)
                else:
                    label, fn = chk
                    ok = check_props(props, fn)
                passed += ok
                lines.append(f"  - {'✅' if ok else '🔴'} expected: {label}")
        lines.insert(1, f"**{passed}/{total} checks passed** · nothing stored")
        report = "\n".join(lines) + "\n"
        self.stdout.write(report)
        if o["record"]:
            ledger = settings.SCHOOL_DIR / "context" / "history" / "gemini-trial.md"
            with ledger.open("a") as f:
                f.write("\n" + report)
