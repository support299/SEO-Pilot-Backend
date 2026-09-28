from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Cookies are only sent over HTTPS in production (see prod.py). Locally, the
# frontend runs on plain http://localhost, so `Secure` must be off or the
# browser silently drops the refresh cookie.
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False

# Vite may hop off 5173 when the port is busy; keep login working either way.
CORS_ALLOWED_ORIGIN_REGEXES = [
    r"^http://localhost:\d+$",
    r"^http://127\.0\.0\.1:\d+$",
]
CSRF_TRUSTED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:5174",
    "http://127.0.0.1:5174",
]
