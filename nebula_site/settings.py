"""Nebula — the one school app. Settings.

Data lives OUTSIDE the code (~/.local/share/nebula by default) because the database holds
personal mail. Secrets live in ~/.config/nebula/ (gemini.env, nebula.env), never in the repo.
"""
import os
import secrets
import sys
from pathlib import Path

def _env_path(name, default):
    return Path(os.environ[name]).expanduser() if os.environ.get(name) else Path(default)


# Every machine-specific value below can be overridden from the environment (NEBULA_*);
# the defaults assume lms-deadline-sync and gws-mcp are cloned next to this repo.
BASE_DIR = Path(__file__).resolve().parent.parent
LMS_DIR = _env_path("NEBULA_LMS_DIR", BASE_DIR.parent / "lms-deadline-sync")
# Where the LMS toolchain keeps state/ledger.json (its LMS_STATE_DIR), and courses/<CODE>/<CODE>.md
SCHOOL_DIR = _env_path("NEBULA_SCHOOL_DIR", os.environ.get("LMS_STATE_DIR") or LMS_DIR.parents[1])
GWS_DIR = _env_path("NEBULA_GWS_DIR", BASE_DIR.parent / "gws-mcp")   # personal-Google token loader
DATA_DIR = Path(os.environ.get("NEBULA_DATA", Path.home() / ".local/share/nebula"))
CONF_DIR = Path(os.environ.get("NEBULA_CONF", Path.home() / ".config/nebula"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

# The LMS toolchain and the personal-Google token loader are imported in-process.
for p in (LMS_DIR, GWS_DIR):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))


def _load_env(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


_load_env(CONF_DIR / "gemini.env")
_NEBULA_ENV = CONF_DIR / "nebula.env"
_load_env(_NEBULA_ENV)
if not os.environ.get("NEBULA_SECRET_KEY"):
    # First run: mint a key and keep it (0600), so sessions survive restarts.
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    key = secrets.token_urlsafe(50)
    with open(_NEBULA_ENV, "a") as f:
        f.write(f"NEBULA_SECRET_KEY={key}\n")
    os.chmod(_NEBULA_ENV, 0o600)
    os.environ["NEBULA_SECRET_KEY"] = key

SECRET_KEY = os.environ["NEBULA_SECRET_KEY"]
DEBUG = os.environ.get("NEBULA_DEBUG") == "1"

TAILNET_HOST = os.environ.get("NEBULA_HOST", "nebula.local")
NEBULA_PORT_PUBLIC = int(os.environ.get("NEBULA_PUBLIC_PORT", "8444"))
NEBULA_PORT_LOCAL = int(os.environ.get("NEBULA_PORT", "8020"))
ALLOWED_HOSTS = ["127.0.0.1", "localhost", TAILNET_HOST, "testserver"]
CSRF_TRUSTED_ORIGINS = [f"https://{TAILNET_HOST}:{NEBULA_PORT_PUBLIC}",
                        f"http://127.0.0.1:{NEBULA_PORT_LOCAL}", f"http://localhost:{NEBULA_PORT_LOCAL}"]

INSTALLED_APPS = [
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "nebula_site.urls"
TEMPLATES = [{
    "BACKEND": "django.template.backends.django.DjangoTemplates",
    "DIRS": [],
    "APP_DIRS": True,
    "OPTIONS": {"context_processors": [
        "django.template.context_processors.request",
        "django.contrib.auth.context_processors.auth",
        "django.contrib.messages.context_processors.messages",
        "core.views.nav_context",
    ]},
}]
WSGI_APPLICATION = "nebula_site.wsgi.application"

DATABASES = {"default": {
    "ENGINE": "django.db.backends.sqlite3",
    "NAME": DATA_DIR / "nebula.sqlite3",
    "OPTIONS": {"timeout": 30},
}}

AUTH_PASSWORD_VALIDATORS = []
LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "today"
LOGOUT_REDIRECT_URL = "login"
SESSION_COOKIE_AGE = 60 * 60 * 24 * 90          # stay signed in on the phone for 90 days

LANGUAGE_CODE = "en-us"
TIME_ZONE = "Asia/Manila"
USE_I18N = False
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = DATA_DIR / "static"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # Hashed names (app.3f2a….css): the service worker caches /static/ forever, so a fixed
    # name meant a new stylesheet NEVER reached the phone.
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
if len(sys.argv) > 1 and sys.argv[1] == "test":
    STORAGES["staticfiles"] = {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"}

# ---- Nebula ------------------------------------------------------------------
GEMINI_API_KEY = os.environ.get("SCHOOL_GEMINI_API_KEY", "")
LMS_PYTHON = _env_path("NEBULA_LMS_PYTHON", sys.executable)                          # a Python with Playwright
PWLIBS = os.environ.get("NEBULA_PWLIBS", "")                  # extra LD_LIBRARY_PATH for Chromium, if any
EMIT_PATH = DATA_DIR / "items.jsonl"
MD2PDF = _env_path("NEBULA_MD2PDF", BASE_DIR / "tools" / "md2pdf.py")   # study pack renderer
DRIVE_OUT = os.environ.get("NEBULA_DRIVE_OUT", "G:\\My Drive\\school-out")   # a Windows path (Google Drive for desktop, WSL)
PERSONAL_ACCOUNT = os.environ.get("NEBULA_PERSONAL_ACCOUNT", "you@gmail.com")
LOGIN_USERNAME = os.environ.get("NEBULA_USERNAME", "student")                    # pre-filled on the sign-in page
# Demo mode: fictional data from `manage.py demo`, and nothing runs in the background (no
# sweep, judge or study pack), so a demo can never touch a real account. See core/demo.py.
DEMO = os.environ.get("NEBULA_DEMO") == "1"
_TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"      # tests' fake failures stay out of the real log
LOGGING = {
    "version": 1, "disable_existing_loggers": False,
    "handlers": {"file": {"class": "logging.NullHandler"} if _TESTING else
                 {"class": "logging.FileHandler", "filename": str(DATA_DIR / "nebula.log"),
                  "formatter": "ts"}},
    "formatters": {"ts": {"format": "%(asctime)s %(levelname)s %(message)s"}},
    "loggers": {"nebula": {"handlers": ["file"], "level": "INFO"}},
}
