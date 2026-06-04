from pathlib import Path
import os

BASE_DIR = Path(__file__).resolve().parent.parent

# Change this in production!
SECRET_KEY = "django-insecure-coq-translation-editor-change-me-in-prod"

DEBUG = True

ALLOWED_HOSTS = ["*"]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "translations",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "editor_project.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "editor_project.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE":   "django.db.backends.postgresql",
        "NAME":     os.environ.get("DB_NAME",     "coq_translate"),
        "USER":     os.environ.get("DB_USER",     "coq"),
        "PASSWORD": os.environ.get("DB_PASSWORD", "coq"),
        "HOST":     os.environ.get("DB_HOST",     "db"),
        "PORT":     os.environ.get("DB_PORT",     "5432"),
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# ── LLM / Translation config ──────────────────────────────────────────────────
# All values read from env vars (.env / Docker env_file); defaults shown here.

LLM_ENDPOINT     = os.environ.get("LLM_ENDPOINT",     "http://llama-server:8080/v1")
LLM_MODEL        = os.environ.get("LLM_MODEL",        "local-model")
TARGET_LANGUAGE  = os.environ.get("TARGET_LANGUAGE",  "Spanish")
LANG_CODE        = os.environ.get("LANG_CODE",        "es")
MOD_NAME         = os.environ.get("MOD_NAME",         "SpanishLanguage")
MOD_DISPLAY_NAME = os.environ.get("MOD_DISPLAY_NAME", "Español")
MOD_AUTHOR       = os.environ.get("MOD_AUTHOR",       "")
TEMPERATURE      = float(os.environ.get("TEMPERATURE",    0.6))
MAX_TOKENS       = int(os.environ.get("MAX_TOKENS",       256))
REQUEST_TIMEOUT  = int(os.environ.get("REQUEST_TIMEOUT",  120))
REQUEST_DELAY    = float(os.environ.get("REQUEST_DELAY",  0.0))
MAX_RETRIES      = int(os.environ.get("MAX_RETRIES",      3))
RETRY_DELAY      = int(os.environ.get("RETRY_DELAY",      5))
BATCH_SIZE       = int(os.environ.get("BATCH_SIZE",       10))
WORKERS          = int(os.environ.get("WORKERS",          4))
TOP_K            = int(os.environ.get("TOP_K"))    if os.environ.get("TOP_K")    else None
TOP_P            = float(os.environ.get("TOP_P"))  if os.environ.get("TOP_P")    else None
MIN_P            = float(os.environ.get("MIN_P"))  if os.environ.get("MIN_P")    else None
REPEAT_PENALTY   = float(os.environ.get("REPEAT_PENALTY")) if os.environ.get("REPEAT_PENALTY") else None
STREAMING_ASSETS_DIR = os.environ.get("STREAMING_ASSETS_DIR", "")


def _load_prompt(name: str) -> str:
    p = BASE_DIR.parent / "prompts" / name
    return p.read_text("utf-8").strip() if p.exists() else ""


_LLM_TONE      = _load_prompt("tone_rules.txt")
LLM_SYS_SINGLE = _load_prompt("sys_single.txt").replace("{tone_rules}", _LLM_TONE)
LLM_SYS_BATCH  = _load_prompt("sys_batch.txt").replace("{tone_rules}", _LLM_TONE)
