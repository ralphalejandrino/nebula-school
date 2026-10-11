from django import template
from django.utils import timezone

register = template.Library()


@register.filter
def get(d, k):
    try:
        return d.get(k, "")
    except AttributeError:
        return ""


@register.filter
def ampm(dt):
    """12-hour, never military (house rule)."""
    if not dt:
        return ""
    dt = timezone.localtime(dt) if timezone.is_aware(dt) else dt
    return dt.strftime("%I:%M %p").lstrip("0")


@register.filter
def dayname(d):
    # The Clashes card passes an event's START (a datetime). Comparing that to a date raised
    # TypeError, so a 💼 WORK/class overlap took down Today and Calendar (found 2026-10-11 by
    # the demo data — the first time the app had ever rendered a clash).
    if hasattr(d, "hour"):
        d = timezone.localtime(d).date() if timezone.is_aware(d) else d.date()
    today = timezone.localtime().date()
    if d == today:
        return "Today"
    if (d - today).days == 1:
        return "Tomorrow"
    return d.strftime("%a %d %b")


@register.filter
def when(dt):
    if not dt:
        return ""
    dt = timezone.localtime(dt)
    now = timezone.localtime()
    if dt.hour == 0 and dt.minute == 0:                # date-only source (Facebook): no fake 12:00 AM
        return "Today" if dt.date() == now.date() else dt.strftime("%a %d %b")
    if dt.date() == now.date():
        return dt.strftime("%I:%M %p").lstrip("0")
    if dt.year == now.year:
        return dt.strftime("%a %d %b")
    return dt.strftime("%d %b %Y")


@register.filter
def calcolor(c):
    return {"4": "flamingo", "7": "peacock", "2": "sage", "8": "graphite", "11": "tomato",
            "1": "lavender", "3": "grape"}.get(c or "", "default")


@register.filter
def dayname_dt(dt):
    """'Tomorrow, 11:59 PM' — a datetime as a friendly day plus 12-hour time."""
    if not dt:
        return ""
    loc = timezone.localtime(dt)
    return f"{dayname(loc.date())}, {ampm(loc)}"


# ---- icons (Icons8 Material Outlined, drawn as CSS masks — see app.css) ----------
from django.utils.html import format_html  # noqa: E402

STATUS = {"done", "overdue", "due", "warn", "na"}


@register.simple_tag
def icon(name, extra=""):
    """{% icon "lock" %} → a masked Icons8 icon that takes the text colour."""
    return format_html('<i class="ico ico-{} {}" aria-hidden="true"></i>', name, extra)


@register.filter
def srcico(source):
    """A message source ('chat_space', 'fb', …) → its icon."""
    return format_html('<i class="ico ico-src-{}" aria-hidden="true" title="{}"></i>', source, source)


@register.filter
def stico(status):
    """A status ('done', 'overdue', 'due', 'warn', 'na') → its COLOURED mark."""
    s = status if status in STATUS else "na"
    return format_html('<i class="ico st ico-st-{} st-{}" aria-hidden="true"></i>', s, s)


@register.filter
def labelst(label):
    """A proposal's confidence label → the status it is drawn as."""
    return {"stated": "done", "extrapolated": "warn"}.get(label, "na")


# A calendar title's emoji prefix → Nebula's own mark (the title in Google is untouched).
_CAL_MARKS = (("✅ 💼", "done"), ("💼", "work"), ("📌", "due"), ("✅", "done"), ("✖", "overdue"),
              ("🔴", "overdue"))


@register.filter
def cleantitle(s):
    """A gradebook name without the instructor's date stamp: see core/present.py."""
    from core.present import clean_title
    return clean_title(s)


@register.filter
def calsum(summary):
    """'📌 DUE: X' → <red pin> 'DUE: X';  '✅ SUBMITTED: X' → <green check> 'SUBMITTED: X'."""
    from core.present import clean_event
    s = clean_event((summary or "").strip())
    for prefix, kind in _CAL_MARKS:
        if s.startswith(prefix):
            rest = s[len(prefix):].lstrip(" ️")
            mark = (format_html('<i class="ico ico-work" aria-hidden="true"></i>') if kind == "work"
                    else stico(kind))
            if prefix == "✅ 💼":
                mark = format_html("{}{}", stico("done"), format_html('<i class="ico ico-work" aria-hidden="true"></i>'))
            return format_html("{} {}", mark, rest)
    return s
