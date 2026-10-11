"""🔴 THE PRIVACY RULE — (2026-10-11)
"I wouldn't want it touching my personal mail, only my school mail."

The free Gemini tier may use prompts to improve Google's products. So ONLY school
data is ever sent. This module is the single place that decides it; `Message.save()`
calls `eligible()` and `judge.build_payload()` re-checks every row before sending.

| source        | sent? |
|---------------|-------|
| school_mail, bb_announce, chat_space, classroom, fb | yes |
| chat_dm       | ONLY an instructor thread (an @e.ubaguio.edu sender) |
| personal_mail | NEVER — including the label:School copies |
| calendar      | NEVER — 💼 WORK events carry client names |
"""

SCHOOL_SOURCES = frozenset({"school_mail", "bb_announce", "chat_space", "classroom", "fb"})
NEVER = frozenset({"personal_mail", "calendar"})


class PrivacyViolation(RuntimeError):
    """Raised when something not eligible reaches the outgoing payload. Fail closed."""


def eligible(source, instructor_thread=None):
    if source in NEVER:
        return False
    if source == "chat_dm":
        return instructor_thread is True
    return source in SCHOOL_SOURCES
