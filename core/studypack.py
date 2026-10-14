"""Per-course study pack → PDF → G:\\My Drive\\school-out\\<CODE>\\study-pack\\.

Tutoring itself runs on your Gemini app plan (no API quota spent): the pack
gives the Gemini app / NotebookLM the facts through @Google Drive, and the
"Study this" button hands over a ready prompt.

Rendering uses tools/md2pdf.py (Chromium via Playwright) and the
existing deliverables rule: copy via PowerShell, then md5-verify — locally on G:
and, once Drive has synced, on Google's side through the drive.readonly token.
"""
import hashlib
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .judge import COURSES
from .models import Item, Message, Proposal

STAGE = Path("/mnt/c/Temp/nebula")
POWERSHELL = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
LINK = re.compile(r"\[([^\]]+)\]\((https?://[^)\s]+)\)")


def materials(code):
    """(title, url) pairs from the hub's `## Materials (Drive)` section."""
    hub = settings.SCHOOL_DIR / "courses" / code / f"{code}.md"
    if not hub.exists():
        return []
    text = hub.read_text()
    m = re.search(r"^##\s+Materials \(Drive\)[^\n]*\n(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    links = LINK.findall(m.group(1)) if m else []
    # Study material only: skip helper scripts and prompt stubs that live alongside it.
    out = []
    for t, u in links:
        t = t.strip("`")
        if re.search(r"\.(py|md|json|sh)$", t, re.I):
            continue
        if "\\" in t or "/drive/folders/" in u:      # a Windows path breaks a markdown link
            t = f"{code} materials folder"
        out.append((t, u))
    return out


def tutor_prompt(code, focus=None):
    name = COURSES.get(code, code)
    tgt = f" for {focus}" if focus else " for my next assessment"
    return (f"You are my tutor for {code} ({name}){tgt}. Use @Google Drive: open "
            f"\"{code} course guide\" in school-out/{code}, then \"{code}-study-pack.pdf\" in "
            f"school-out/{code}/study/study-pack, and the module files in "
            f"school-out/{code}/materials/blackboard. First ask me 3 diagnostic questions, then teach only what I got wrong, then "
            f"quiz me with 10 exam-style questions one at a time and check each answer against "
            f"the module. Quote the module when you correct me. Don't just summarise.")


def build_markdown(code):
    now = timezone.now()
    name = COURSES.get(code, code)
    up = Item.objects.filter(course=code, submitted=False, due__gte=now).order_by("due")[:12]
    props = Proposal.objects.filter(course=code, date__gte=now.date()).exclude(
        state="rejected").order_by("date")[:12]
    notes = Message.objects.filter(course=code, when__gte=now - timedelta(days=21)).filter(
        judge_relevant=True).order_by("-when")[:15]
    L = [f"# {code} — {name} · study pack",
         f"_Built by Nebula {timezone.localtime():%a %d %b %Y %I:%M %p}. Facts only — every line "
         f"below comes from Blackboard, Classroom, Chat or own notes._", ""]
    L += ["## What is coming", ""]
    if not up and not props:
        L.append("- Nothing dated is on record for this course.")
    for i in up:
        L.append(f"- **{timezone.localtime(i.due):%a %d %b, %I:%M %p}** — {i.title}"
                 + (f" ({i.pts:g} pts)" if i.pts else ""))
    for p in props:
        when = f"{p.date:%a %d %b}" + (f", {p.time:%I:%M %p}" if p.time else "")
        L.append(f"- **{when}** — {p.get_kind_display()} {p.title} · {p.get_label_display()}")
    L += ["", "## What the instructor said recently", ""]
    for m in notes:
        L.append(f"- {timezone.localtime(m.when):%d %b} · {m.get_source_display()}: "
                 f"{m.judge_tag or m.title}")
    if not notes:
        L.append("- No judged announcements in the last 21 days.")
    mats = materials(code)
    L += ["", "## Module files to study (Google Drive)", ""]
    L += [f"- [{t}]({u})" for t, u in mats] or ["- No materials linked in the course hub yet."]
    L += ["", "## How to use this with Gemini", "",
          "Open the Gemini app and paste:", "", "> " + tutor_prompt(code), ""]
    return "\n".join(L)


def _md5(path):
    return hashlib.md5(Path(path).read_bytes()).hexdigest()


def render_pdf(code):
    out_dir = settings.DATA_DIR / "packs"
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / f"{code}-study-pack.md"
    pdf = out_dir / f"{code}-study-pack.pdf"
    md.write_text(build_markdown(code))
    env = dict(os.environ, LD_LIBRARY_PATH=str(settings.PWLIBS))
    p = subprocess.run([str(settings.LMS_PYTHON), str(settings.MD2PDF),
                        str(md), "-o", str(pdf)], capture_output=True, text=True, env=env,
                       timeout=180)
    if p.returncode != 0 or not pdf.exists():
        raise RuntimeError(f"md2pdf failed: {(p.stderr or p.stdout).strip()[-300:]}")
    return pdf


def publish(code):
    """Render, copy to Drive via PowerShell, md5-verify on G:. Returns a status dict."""
    pdf = render_pdf(code)
    STAGE.mkdir(parents=True, exist_ok=True)
    staged = STAGE / pdf.name
    staged.write_bytes(pdf.read_bytes())
    dest_dir = f"{settings.DRIVE_OUT}\\{code}\\study\\study-pack"
    ps = (f"$d='{dest_dir}'; New-Item -ItemType Directory -Force -Path $d | Out-Null; "
          f"Copy-Item -Force 'C:\\Temp\\nebula\\{pdf.name}' (Join-Path $d '{pdf.name}'); "
          f"(Get-FileHash -Algorithm MD5 (Join-Path $d '{pdf.name}')).Hash")
    p = subprocess.run([POWERSHELL, "-NoProfile", "-Command", ps], capture_output=True,
                       text=True, timeout=120)
    remote = (p.stdout.strip().splitlines() or [""])[-1].strip().lower()
    local = _md5(pdf)
    return {"pdf": str(pdf), "drive_path": f"{dest_dir}\\{pdf.name}", "md5": local,
            "copied": remote == local, "error": "" if remote == local else
            (p.stderr.strip()[-200:] or f"md5 mismatch {remote} != {local}")}


def drive_verified(code, md5):
    """True when Google Drive itself reports the same md5 (sync finished)."""
    import gws_auth
    from googleapiclient.discovery import build
    svc = build("drive", "v3", credentials=gws_auth.credentials(), cache_discovery=False)
    r = svc.files().list(q=f"name = '{code}-study-pack.pdf' and trashed = false",
                         fields="files(id,md5Checksum,modifiedTime)", pageSize=10).execute()
    return any(f.get("md5Checksum") == md5 for f in r.get("files", []))
