"""
Django settings for reference server.
"""
import os
import environ
from django.utils.translation import gettext_lazy as _

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load environment variables from .env
env = environ.Env()
env_file = os.path.join(BASE_DIR, ".env")
if os.path.exists(env_file):
    environ.Env.read_env(env_file)

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = env("DJANGO_SECRET_KEY")

MULT_ASSET_ADDITIONAL_SIGNING_SEED = env(
    "MULT_ASSET_ADDITIONAL_SIGNING_SEED", default=None
)

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = env.bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env.list(
    "DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "[::1]", "0.0.0.0"]
)

# Application definition

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "corsheaders",
    "server",
    "polaris",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "corsheaders.middleware.CorsMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "polaris.middleware.TimezoneMiddleware",
]

local_mode = env.bool("LOCAL_MODE", default=False)

# Ensure SEP-24 session cookies have the secure flag
SESSION_COOKIE_SECURE = not local_mode

# Redirect HTTP to HTTPS if not in local mode
SECURE_SSL_REDIRECT = not local_mode
if SECURE_SSL_REDIRECT:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


APPEND_SLASH = False

ROOT_URLCONF = "server.urls"

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

WSGI_APPLICATION = "server.wsgi.application"

# Database
# https://docs.djangoproject.com/en/2.2/ref/settings/#databases

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="sqlite:///" + os.path.join(BASE_DIR, "data/db.sqlite3"),
    )
}
DEFAULT_AUTO_FIELD = "django.db.models.AutoField"

# Internationalization
# https://docs.djangoproject.com/en/2.2/topics/i18n/

LANGUAGE_CODE = "en-us"
TIME_ZONE = "America/Los_Angeles"
USE_I18N = True
USE_L10N = True
USE_TZ = True
USE_THOUSAND_SEPARATOR = True
LANGUAGES = [
    ("en", _("English")),
    ("pt", _("Portuguese")),
    ("id", _("Bahasa Indonesia")),
]

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/2.2/howto/static-files/

FORM_RENDERER = "django.forms.renderers.TemplatesSetting"

STATIC_ROOT = os.path.join(BASE_DIR, "server/collectstatic")
STATIC_URL = "/static/"
STATICFILES_STORAGE = "whitenoise.storage.CompressedManifestStaticFilesStorage"

# Django Rest Framework Settings:

REST_FRAMEWORK = {
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
        "rest_framework.renderers.TemplateHTMLRenderer",
    ],
    "PAGE_SIZE": 10,
}

# Email Settings

EMAIL_HOST = "smtp.gmail.com"
EMAIL_HOST_USER = env("EMAIL_HOST_USER", default=None)
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", default=None)
EMAIL_USE_TLS = True
EMAIL_PORT = 587

# CORS configuration

CORS_ORIGIN_ALLOW_ALL = True

# Logging

LOGGING = {
    "version": 1,
    "disable_existing_loggers": True,
    "formatters": {
        "verbose": {"format": "{asctime} - {levelname}: {message}", "style": "{",},
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "level": "DEBUG",
            "formatter": "verbose",
        },
    },
    "loggers": {
        "server": {"handlers": ["console"], "propogate": False, "level": "INFO"},
        "polaris": {"handlers": ["console"], "propogate": False, "level": "DEBUG"},
        "django": {"handlers": ["console"], "propogate": False, "level": "INFO"},
    },
}
