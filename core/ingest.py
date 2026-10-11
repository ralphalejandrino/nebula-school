"""One sweep: LMS load → calendar apply → imports → judge.

The ingestion ENGINE is unchanged: `lms.sh load --json --emit-items` runs as a
subprocess, keeping its own flock and its exit-code contract (0 complete,
1 incomplete, 2 session dead). Every step's failure is recorded on the Run and
shown as 🔴 in SWEEP — never folded into "nothing new".
"""
import fcntl
import re
import json
import logging
import os
import subprocess
import traceback
from datetime import datetime, timedelta, timezone as dtz
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import CalEvent, Item, Message, Run

log = logging.getLogger("nebula")
LOCK = settings.DATA_DIR / "sweep.lock"


class Busy(RuntimeError):
    pass


def _lms_env():
    env = dict(os.environ)
    env.pop("DJANGO_SETTINGS_MODULE", None)
    return env


def _lms(*args, timeout=900):
    p = subprocess.run([str(settings.LMS_DIR / "lms.sh"), *args], cwd=settings.LMS_DIR,
                       capture_output=True, text=True, timeout=timeout, env=_lms_env())
    return p.returncode, p.stdout, p.stderr


def _dt(v):
    if not v:
        return None
    if isinstance(v, (int, float)) or (isinstance(v, str) and v.isdigit()):
        return datetime.fromtimestamp(int(v) / 1000, dtz.utc)
    d = parse_datetime(str(v).replace("Z", "+00:00"))
    if d is None:
        try:
            d = datetime.strptime(str(v)[:10], "%Y-%m-%d")
        except ValueError:
            return None
    return d if timezone.is_aware(d) else timezone.make_aware(d)


# ---------------------------------------------------------------- imports
# A logged-out Facebook scrape prefixes each post with volatile chrome ("UB Router",
# "about an hour ago", "Online status indicator Active 7 hours ago", "+68"), and fb_fetch
# hashes that text — so one post became ten rows across a day of sweeps (2026-10-11).
_FB_CHROME = re.compile(
    r"^(?:\s*(?:\+\d+|UB Router|Baguio City Public Information Office|Online status indicator|Active|"
    r"was live\.?|just now|yesterday(?: at \d{1,2}:\d{2}\s*[AP]M)?|(?:about )?(?:an?|\d+) "
    r"(?:minute|hour|day|week)s? ago|·)\s*)+", re.I)


def fb_clean(text):
    return _FB_CHROME.sub("", text or "").strip()


def fb_uid(page, text):
    import hashlib
    norm = re.sub(r"\s+", " ", fb_clean(text).lower())[:400]
    return f"fb:{page}:{hashlib.sha1(norm.encode()).hexdigest()[:16]}"


def import_emitted(path=None):
    """Upsert every message row from the --emit-items JSONL. Returns (n, errors)."""
    path = Path(path or settings.EMIT_PATH)
    lines = path.read_text().splitlines()
    head = json.loads(lines[0]).get("_run", {}) if lines else {}
    n, errors = 0, []
    for line in lines[1:]:
        r = json.loads(line)
        if r.get("error"):
            errors.append(f"{r.get('source')}: {r['error']}")
            continue
        if not r.get("id"):
            continue
        if r["source"] == "fb":
            page = (r.get("id") or "fb:?:").split(":")[1]
            r["id"], r["body"] = fb_uid(page, r.get("body")), fb_clean(r.get("body"))
        obj, created = Message.objects.get_or_create(uid=r["id"], defaults={"source": r["source"]})
        obj.source = r["source"]
        obj.course = r.get("course") or ""
        obj.title = (r.get("title") or "")[:300]
        obj.author = (r.get("author") or "").strip().strip('"').strip()[:200]
        obj.author_email = (r.get("author_email") or "")[:200]
        obj.when = _dt(r.get("when"))
        obj.url = (r.get("url") or "")[:600]
        new_body = r.get("body") or ""
        if not created and new_body != obj.body:
            obj.judged_at = None                    # edited upstream → judge it again
        obj.body = new_body
        obj.keyword_hit = bool(r.get("keyword_hit"))
        obj.instructor_thread = r.get("instructor_thread")
        if r.get("unread") is not None:
            obj.unread = r.get("unread")
        obj.rule_why = (r.get("rule_why") or "")[:200]
        obj.rule_actionable = r.get("rule_actionable")
        obj.save()
        n += 1
    return n, errors, head


def import_ledger():
    ledger = json.loads((settings.SCHOOL_DIR / "state" / "ledger.json").read_text())
    n = 0
    for i in ledger.get("items", []):
        uid = f"{i.get('cid')}:{i.get('column')}"
        Item.objects.update_or_create(uid=uid, defaults={
            "course": i.get("course") or "", "title": (i.get("title") or "")[:300],
            "due": _dt(i.get("due")), "pts": i.get("pts"), "state": (i.get("state") or "")[:120],
            "submitted": bool(i.get("submitted")), "source": i.get("source") or ""})
        n += 1
    return n


def import_personal_mail(limit=40):
    """Personal inbox, READ ONLY, snippet only. 🔒 source=personal_mail is never eligible."""
    import gws_auth
    from googleapiclient.discovery import build
    svc = build("gmail", "v1", credentials=gws_auth.credentials(), cache_discovery=False)
    who = svc.users().getProfile(userId="me").execute().get("emailAddress", "")
    if who.lower() != settings.PERSONAL_ACCOUNT.lower():
        raise RuntimeError(f"WRONG ACCOUNT: {who}")
    ids = svc.users().messages().list(userId="me", labelIds=["INBOX"],
                                      maxResults=limit).execute().get("messages", [])
    got = {}

    def _cb(rid, resp, err):
        if err is None and resp:
            got[resp["id"]] = resp
    batch = svc.new_batch_http_request(callback=_cb)
    for m in ids:
        batch.add(svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                             metadataHeaders=["From", "Subject"]))
    batch.execute()
    seen_threads, n = set(), 0
    for m in ids:
        msg = got.get(m["id"])
        if not msg or msg["threadId"] in seen_threads:
            continue
        seen_threads.add(msg["threadId"])
        h = {x["name"].lower(): x["value"] for x in msg.get("payload", {}).get("headers", [])}
        obj, _ = Message.objects.get_or_create(uid=f"pm:{msg['id']}",
                                               defaults={"source": "personal_mail"})
        obj.source = "personal_mail"
        obj.title = h.get("subject", "")[:300]
        obj.author = h.get("from", "").split("<")[0].strip().strip('"')[:200]
        obj.when = _dt(msg.get("internalDate"))
        obj.body = msg.get("snippet", "")
        obj.unread = "UNREAD" in (msg.get("labelIds") or [])
        obj.url = (f"https://mail.google.com/mail/u/?authuser={settings.PERSONAL_ACCOUNT}"
                   f"#inbox/{msg['threadId']}")
        obj.save()                                   # save() forces gemini_eligible=False
        n += 1
    return n


def import_calendar(days=45):
    """Snapshot primary calendar (yesterday → +days). Local only: the agenda shows 14 days,
    the judge's duplicate check needs the exam weeks further out."""
    import gcreds
    from googleapiclient.discovery import build
    cr = gcreds.credentials("calendar", quiet=True)
    svc = build("calendar", "v3", credentials=cr, cache_discovery=False)
    now = timezone.now()
    lo, hi = now - timedelta(days=1), now + timedelta(days=days)
    evs, tok = [], None
    while True:
        r = svc.events().list(calendarId="primary", timeMin=lo.isoformat(), timeMax=hi.isoformat(),
                              singleEvents=True, orderBy="startTime", maxResults=250,
                              pageToken=tok).execute()
        evs += r.get("items", [])
        tok = r.get("nextPageToken")
        if not tok:
            break
    keep = set()
    for e in evs:
        if e.get("status") == "cancelled":
            continue
        s, en = e.get("start", {}), e.get("end", {})
        all_day = "date" in s
        start = _dt(s.get("dateTime") or s.get("date"))
        end = _dt(en.get("dateTime") or en.get("date")) or start
        CalEvent.objects.update_or_create(gid=e["id"], defaults={
            "summary": (e.get("summary") or "(no title)")[:400], "start": start, "end": end,
            "all_day": all_day, "color": e.get("colorId") or "",
            "link": (e.get("htmlLink") or "")[:600], "location": (e.get("location") or "")[:300],
            # cal_sync's shape is exactly one hour ending ON the deadline; a hand-moved one
            # (the BIOS lab on its 2:00–5:00 PM block) keeps its marker but not that shape.
            "deadline_at_end": (e.get("description") or "").lstrip().startswith("bb:")
                               and not all_day and (end - start) == timedelta(hours=1)})
        keep.add(e["id"])
    CalEvent.objects.filter(start__gte=lo, start__lte=hi).exclude(gid__in=keep).delete()
    CalEvent.objects.filter(end__lt=lo).delete()
    return len(keep)


# ---------------------------------------------------------------- the sweep
def sweep(trigger="cli", judge_after=True, apply_calendar=True):
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK, "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        raise Busy("a sweep is already running")
    run = Run.objects.create(trigger=trigger)
    steps = {}

    def step(name, fn):
        try:
            steps[name] = "ok · " + str(fn())
        except Exception as e:
            steps[name] = f"ERR · {type(e).__name__}: {str(e)[:200]}"
            log.error("step %s failed\n%s", name, traceback.format_exc())
        run.steps = steps
        run.save(update_fields=["steps"])

    def _load():
        code, out, err = _lms("load", "--json", "--emit-items", str(settings.EMIT_PATH))
        run.load_exit = code
        try:
            art = json.loads(out)
        except json.JSONDecodeError:
            raise RuntimeError(f"exit {code}, no JSON: {err.strip()[-200:]}")
        run.artifact = art
        run.complete = bool(art.get("complete")) and code == 0
        run.coverage = art.get("coverage") or {}
        run.degraded = art.get("degraded") or []
        run.save()
        if code == 2 or art.get("ok") is False:
            raise RuntimeError(f"SESSION DEAD — {art.get('error')}")
        n, errs, _ = import_emitted()
        if errs:
            run.degraded = list(run.degraded) + errs
            run.save(update_fields=["degraded"])
        return f"exit {code} · {n} messages"

    try:
        step("lms_load", _load)
        if apply_calendar:
            def _apply():
                code, out, err = _lms("apply", "--apply", timeout=600)
                if code not in (0, 1):
                    raise RuntimeError(f"exit {code}: {(err or out).strip()[-200:]}")
                # The dry-run summary block: "  CREATE   2", "  HELD   5" … — counts only.
                counts = [" ".join(l.split()) for l in out.splitlines()
                          if re.match(r"^\s+[A-Z_]+\s+\d+\s*$", l)]
                return f"exit {code} · " + (", ".join(counts) or "no changes")
            step("calendar_apply", _apply)
        step("ledger", lambda: f"{import_ledger()} items")
        step("personal_mail", lambda: f"{import_personal_mail()} threads (🔒 never sent to AI)")
        step("calendar_read", lambda: f"{import_calendar()} events")
        if judge_after:
            from . import judge
            try:
                run.gemini = judge.run()
            except Exception as e:
                run.gemini = f"UNKNOWN · {type(e).__name__}: {str(e)[:200]}"
                log.error("judge failed\n%s", traceback.format_exc())
    finally:
        run.finished = timezone.now()
        run.save()
        fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()
    return run


def is_running():
    try:
        with open(LOCK, "w") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
        return False
    except BlockingIOError:
        return True
