"""What Nebula knows right now, compact — for a Claude study load to compare against
during the trial (school/context/history/gemini-trial.md).

Usage: python manage.py trial_report [--days 3]
"""
from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from core import quota
from core.models import Clarification, Message, Proposal, Run


class Command(BaseCommand):
    help = "Nebula's latest run, proposals, questions and judged notices (for the trial)."

    def add_arguments(self, p):
        p.add_argument("--days", type=int, default=3)

    def handle(self, *a, **o):
        w = self.stdout.write
        run = Run.objects.filter(finished__isnull=False).first()
        if not run:
            w("🔴 Nebula has never finished a sweep.")
            return
        age = timezone.now() - run.finished
        w(f"# Nebula — last sweep {timezone.localtime(run.finished):%a %d %b %I:%M %p} "
          f"({int(age.total_seconds() // 3600)} h ago) · "
          f"{'✅ complete' if run.complete else '🔴 INCOMPLETE'} · trigger {run.trigger}")
        for k, v in run.steps.items():
            w(f"  {'✅' if str(v).startswith('ok') else '🔴'} {k}: {str(v).split('·', 1)[-1].strip()}")
        w(f"  {'✅' if run.gemini.startswith('ok') else '🔴'} gemini: {run.gemini}")
        w("  quota: " + ", ".join(f"{q['model'].replace('gemini-', '')} {q['used']}/{q['limit']}"
                                 for q in quota.summary()))
        w("\n## Proposals (newest 25)")
        for p in Proposal.objects.order_by("-created")[:25]:
            w(f"- [{p.get_state_display()}] {p.get_kind_display()} {p.course or '-'} {p.title} · "
              f"{p.date or 'no date'} {p.time or ''} · {p.label}{'' if p.verified else ' ⚠ unverified'}")
        w("\n## Open questions")
        for c in Clarification.objects.filter(answered__isnull=True):
            w(f"- {c.question}")
        since = timezone.now() - timedelta(days=o["days"])
        w(f"\n## Judged relevant, last {o['days']} days (school only)")
        for m in Message.objects.filter(judge_relevant=True, when__gte=since).order_by("-when"):
            w(f"- {timezone.localtime(m.when):%a %d %b} {m.get_source_display()} {m.course or '-'}: {m.judge_tag}")
        w(f"\n## Keyword-only (the judge said no, the floor still shows them)")
        for m in Message.objects.filter(keyword_hit=True, judge_relevant=False, when__gte=since).exclude(
                source="personal_mail").order_by("-when")[:20]:
            w(f"- {timezone.localtime(m.when):%a %d %b} {m.get_source_display()} {m.course or '-'}: "
              f"{m.headline[:90]}")
