from .base import *  # noqa: F403

DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

# Cookies are only sent over HTTPS in production (see prod.py). Locally, the
# frontend runs on plain http://localhost, so `Secure` must be off or the
# browser silently drops the refresh cookie.
CSRF_COOKIE_SECURE = False
SESSION_COOKIE_SECURE = False
