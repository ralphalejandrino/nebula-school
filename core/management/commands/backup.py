from django.core.management.base import BaseCommand

from core import backup


class Command(BaseCommand):
    help = "Copy the database to NEBULA_DATA/backups (once per day; keeps the newest 14)."

    def handle(self, *a, **o):
        f = backup.daily()
        self.stdout.write(f"backed up → {f}" if f else "today's backup already exists")
