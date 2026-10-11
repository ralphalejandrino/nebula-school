"""Nebula's data. SQLite in NEBULA_DATA (~/.local/share/nebula) — never in the repo."""
from django.db import models

from . import privacy


class Run(models.Model):
    """One sweep: the LMS load, the calendar apply, the imports and the judge."""
    started = models.DateTimeField(auto_now_add=True)
    finished = models.DateTimeField(null=True, blank=True)
    trigger = models.CharField(max_length=20, default="timer")   # timer | button | cli
    load_exit = models.IntegerField(null=True)                    # 0 complete · 1 incomplete · 2 dead
    complete = models.BooleanField(default=False)
    coverage = models.JSONField(default=dict, blank=True)
    degraded = models.JSONField(default=list, blank=True)
    artifact = models.JSONField(default=dict, blank=True)         # the full `load --json`
    steps = models.JSONField(default=dict, blank=True)            # step -> "ok" | "ERR …"
    gemini = models.CharField(max_length=300, blank=True)         # model used, or why UNKNOWN

    class Meta:
        ordering = ["-started"]

    @property
    def ok(self):
        return self.finished is not None and all(
            str(v).startswith("ok") for v in self.steps.values())


class Message(models.Model):
    SOURCES = [
        ("school_mail", "School mail"), ("personal_mail", "Personal mail"),
        ("bb_announce", "Blackboard"), ("classroom", "Classroom"),
        ("chat_space", "Chat"), ("chat_dm", "Chat DM"), ("fb", "Facebook"),
    ]
    uid = models.CharField(max_length=300, unique=True)
    source = models.CharField(max_length=20, choices=SOURCES)
    course = models.CharField(max_length=12, blank=True)
    title = models.CharField(max_length=300, blank=True)
    author = models.CharField(max_length=200, blank=True)
    author_email = models.CharField(max_length=200, blank=True)
    when = models.DateTimeField(null=True, blank=True)
    url = models.URLField(max_length=600, blank=True)
    body = models.TextField(blank=True)
    keyword_hit = models.BooleanField(default=False)               # the regex floor
    instructor_thread = models.BooleanField(null=True)            # chat_dm only
    gemini_eligible = models.BooleanField(default=False, editable=False)
    unread = models.BooleanField(null=True)
    rule_why = models.CharField(max_length=200, blank=True)        # mail_fetch's own reason
    rule_actionable = models.BooleanField(null=True)               # mail_fetch's own verdict
    first_seen = models.DateTimeField(auto_now_add=True)
    judged_at = models.DateTimeField(null=True, blank=True)
    judge_relevant = models.BooleanField(null=True)
    judge_tag = models.CharField(max_length=120, blank=True)
    judge_model = models.CharField(max_length=60, blank=True)

    class Meta:
        ordering = ["-when"]
        indexes = [models.Index(fields=["source", "when"]), models.Index(fields=["course"])]

    def save(self, *a, **kw):
        # 🔴 The privacy rule is decided HERE, from the source, on every save.
        # Nothing else may set gemini_eligible (editable=False keeps forms off it).
        self.gemini_eligible = privacy.eligible(self.source, self.instructor_thread)
        super().save(*a, **kw)

    @property
    def source_long(self):
        """Where it was said, in words: 'LITERA1 Google Chat space', 'Google Chat DM', …"""
        c = f"{self.course} " if self.course else ""
        return {"chat_space": f"{c}Google Chat space", "chat_dm": "Google Chat DM",
                "bb_announce": f"{c}Blackboard announcement", "classroom": f"{c}Google Classroom",
                "school_mail": "school email", "personal_mail": "personal email",
                "fb": "Facebook"}.get(self.source, self.get_source_display())

    @property
    def short_source(self):
        """'Chat (Domingo)' — fits in a calendar title."""
        kind = {"chat_space": "Chat", "chat_dm": "Chat DM", "bb_announce": "Blackboard",
                "classroom": "Classroom", "school_mail": "email", "fb": "Facebook"}.get(self.source, "")
        who = self.author.split()[-1].title() if self.author and self.source != "fb" else ""
        return f"{kind} ({who})" if who else kind

    @property
    def headline(self):
        """One line to show: the judge's tag, else the subject, else the start of the text.
        Facebook/Chat/Classroom titles are generic ("Facebook — SIT-SB"), so use their text."""
        if self.judge_tag:
            return self.judge_tag
        if self.source in ("fb", "chat_space", "chat_dm", "classroom") or not self.title:
            return " ".join(self.body.split())[:110]
        return self.title

    @property
    def shows_in_brief(self):
        """Keyword floor OR the judge said it matters. The model can add, never subtract."""
        return self.keyword_hit or bool(self.judge_relevant)


class Item(models.Model):
    """Dated graded work from the Blackboard/Classroom ledger (deterministic)."""
    uid = models.CharField(max_length=120, unique=True)
    course = models.CharField(max_length=12)
    title = models.CharField(max_length=300)
    due = models.DateTimeField(null=True, blank=True)
    pts = models.FloatField(null=True, blank=True)
    state = models.CharField(max_length=120, blank=True)
    submitted = models.BooleanField(default=False)
    source = models.CharField(max_length=40, blank=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["due"]


class CalEvent(models.Model):
    """A snapshot of Google Calendar for the agenda. LOCAL ONLY — never sent to the judge
    (💼 WORK events carry client names)."""
    gid = models.CharField(max_length=200, unique=True)
    summary = models.CharField(max_length=400)
    start = models.DateTimeField()
    end = models.DateTimeField()
    all_day = models.BooleanField(default=False)
    color = models.CharField(max_length=4, blank=True)
    link = models.URLField(max_length=600, blank=True)
    location = models.CharField(max_length=300, blank=True)
    # cal_sync's gradebook events (description "bb:<course>:<column>") are drawn as the
    # hour BEFORE the deadline — their END is the deadline. Everything else: start.
    deadline_at_end = models.BooleanField(default=False)

    class Meta:
        ordering = ["start"]

    @property
    def is_work(self):
        return self.summary.lstrip("✅ ").startswith("💼") or self.color in ("1", "3")

    @property
    def is_class(self):
        return self.color == "8"


class Capture(models.Model):
    """A note typed in Nebula (things said in class). Replaces thumb-typing into inbox.md."""
    KINDS = [("note", "note"), ("answer", "answer to a question")]
    text = models.TextField()
    course = models.CharField(max_length=12, blank=True)
    kind = models.CharField(max_length=8, choices=KINDS, default="note")
    created = models.DateTimeField(auto_now_add=True)
    judged_at = models.DateTimeField(null=True, blank=True)
    # What the judge made of it, in words ("nothing dated in it", "already tracked: …"), so
    # a note never just disappears. Proposals it produced hang off proposal_set.
    outcome = models.CharField(max_length=200, blank=True)

    class Meta:
        ordering = ["-created"]


class Proposal(models.Model):
    KINDS = [("deadline", "Due"), ("quiz", "Quiz"), ("exam", "Exam"),
             ("event", "Event"), ("buy", "Buy"), ("confirm", "Confirm"),
             ("notice", "Notice")]
    LABELS = [("stated", "stated"), ("extrapolated", "extrapolated"),
              ("unknown", "unknown")]
    STATES = [("pending", "pending"), ("approved", "approved"), ("written", "on calendar"),
              ("duplicate", "already on calendar"), ("rejected", "rejected"),
              ("failed", "write failed")]
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL)
    capture = models.ForeignKey(Capture, null=True, blank=True, on_delete=models.SET_NULL)
    kind = models.CharField(max_length=12, choices=KINDS)
    course = models.CharField(max_length=12, blank=True)
    title = models.CharField(max_length=200)
    date = models.DateField(null=True, blank=True)
    time = models.TimeField(null=True, blank=True)
    end_time = models.TimeField(null=True, blank=True)
    room = models.CharField(max_length=80, blank=True)
    evidence = models.TextField()
    label = models.CharField(max_length=14, choices=LABELS, default="stated")
    verified = models.BooleanField(default=False)                 # evidence found verbatim
    verify_note = models.CharField(max_length=200, blank=True)
    model = models.CharField(max_length=60, blank=True)
    state = models.CharField(max_length=10, choices=STATES, default="pending")
    event_id = models.CharField(max_length=200, blank=True)
    event_link = models.URLField(max_length=600, blank=True)
    error = models.TextField(blank=True)
    created = models.DateTimeField(auto_now_add=True)
    decided = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["date", "time"]


class Clarification(models.Model):
    question = models.TextField()
    context = models.TextField(blank=True)
    message = models.ForeignKey(Message, null=True, blank=True, on_delete=models.SET_NULL)
    created = models.DateTimeField(auto_now_add=True)
    answer = models.TextField(blank=True)
    answered = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["answered", "-created"]


class CourseFacts(models.Model):
    code = models.CharField(max_length=12, unique=True)
    group_no = models.CharField(max_length=40, blank=True)
    members = models.TextField(blank=True)
    room = models.CharField(max_length=80, blank=True)
    channel = models.CharField(max_length=120, blank=True)
    notebook_url = models.URLField(max_length=600, blank=True)     # NotebookLM for this course
    notes = models.TextField(blank=True)


class QuotaUse(models.Model):
    """Gemini requests per model per PACIFIC day (that is when Google resets RPD)."""
    model = models.CharField(max_length=60)
    day = models.DateField()
    count = models.IntegerField(default=0)
    recent = models.JSONField(default=list)                        # epoch seconds, last minute
    exhausted = models.BooleanField(default=False)                 # Google said 429 today

    class Meta:
        unique_together = [("model", "day")]
