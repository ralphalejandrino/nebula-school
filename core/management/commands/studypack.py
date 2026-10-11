import json

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

from core import studypack


class Command(BaseCommand):
    help = "Build <CODE>'s study pack PDF and copy it to Drive (school-out/<CODE>/study-pack)."

    def add_arguments(self, p):
        p.add_argument("code")

    def handle(self, *a, **o):
        code = o["code"].upper()
        status_file = settings.DATA_DIR / "packs" / f"{code}.status.json"
        status_file.parent.mkdir(parents=True, exist_ok=True)
        try:
            st = studypack.publish(code)
            try:
                st["drive_verified"] = studypack.drive_verified(code, st["md5"])
            except Exception as e:
                st["drive_verified"] = None
                st["drive_note"] = f"{type(e).__name__}"
        except Exception as e:
            st = {"copied": False, "error": f"{type(e).__name__}: {e}"[:300]}
        st["built"] = timezone.localtime().strftime("%a %d %b %I:%M %p")
        status_file.write_text(json.dumps(st))
        self.stdout.write(json.dumps(st, indent=1))
