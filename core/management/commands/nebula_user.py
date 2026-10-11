from django.conf import settings
import getpass

from django.contrib.auth.models import User
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Create or reset the single Nebula login."

    def add_arguments(self, p):
        p.add_argument("--username", default=settings.LOGIN_USERNAME)
        p.add_argument("--password-file", help="read the password from this file (0600)")

    def handle(self, *a, **o):
        pw = (open(o["password_file"]).read().strip() if o["password_file"]
              else getpass.getpass("Nebula password: "))
        u, created = User.objects.get_or_create(username=o["username"])
        u.set_password(pw)
        u.is_active = True
        u.save()
        self.stdout.write(("created" if created else "updated") + f" user {u.username}")
