"""Seed fictional demo data (core/demo.py). Refuses unless NEBULA_DEMO=1."""
from django.core.management.base import BaseCommand, CommandError

from core import demo


class Command(BaseCommand):
    help = "Wipe this database and fill it with fictional demo data (needs NEBULA_DEMO=1)"

    def handle(self, *args, **opts):
        try:
            r = demo.seed()
        except RuntimeError as e:
            raise CommandError(str(e))
        self.stdout.write(f"demo data: {r['items']} items · {r['messages']} messages · "
                          f"{r['events']} calendar events · sign in as {r['user']} / {r['password']}")
