from django.core.management.base import BaseCommand

from core import ingest


class Command(BaseCommand):
    help = "One full sweep: LMS load + calendar apply + imports + Gemini judge."

    def add_arguments(self, p):
        p.add_argument("--trigger", default="cli")
        p.add_argument("--no-judge", action="store_true")
        p.add_argument("--no-apply", action="store_true", help="skip ./lms.sh apply --apply")

    def handle(self, *a, **o):
        try:
            run = ingest.sweep(trigger=o["trigger"], judge_after=not o["no_judge"],
                               apply_calendar=not o["no_apply"])
        except ingest.Busy as e:
            self.stdout.write(f"skipped: {e}")
            return
        for k, v in run.steps.items():
            self.stdout.write(f"{k:16} {v}")
        self.stdout.write(f"{'gemini':16} {run.gemini}")
        self.stdout.write(f"complete={run.complete} load_exit={run.load_exit}")
