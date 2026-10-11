from django.core.management.base import BaseCommand

from core import judge
from core.models import Run


class Command(BaseCommand):
    help = "Send pending school messages / notes to free Gemini (no LMS pull)."

    def add_arguments(self, p):
        p.add_argument("--captures-only", action="store_true",
                       help="judge only your typed notes (cheap; one request)")
        p.add_argument("--days", type=int, default=judge.MAX_AGE_DAYS)

    def handle(self, *a, **o):
        if o["captures_only"]:
            status = judge.run(include_messages=False)
        else:
            status = judge.run(max_age_days=o["days"])
        last = Run.objects.filter(finished__isnull=False).first()
        if last and not o["captures_only"]:
            last.gemini = status
            last.save(update_fields=["gemini"])
        self.stdout.write(status)
