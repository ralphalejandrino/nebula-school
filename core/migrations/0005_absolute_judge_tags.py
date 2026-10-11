from django.db import migrations


def pin_relative_days(apps, schema_editor):
    """Tags written before 2026-10-12 say "tomorrow"; pin them to the message's own date."""
    from core.present import absolute_tag
    Message = apps.get_model("core", "Message")
    for m in Message.objects.exclude(judge_tag="").exclude(when__isnull=True):
        new = absolute_tag(m.judge_tag, m.when)
        if new != m.judge_tag:
            m.judge_tag = new
            m.save(update_fields=["judge_tag"])


class Migration(migrations.Migration):
    dependencies = [("core", "0004_capture_kind_outcome")]
    operations = [migrations.RunPython(pin_relative_days, migrations.RunPython.noop)]
