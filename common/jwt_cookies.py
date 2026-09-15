"""
The refresh token lives only in an httpOnly cookie — it is never present in a
JSON response body, so client-side JavaScript (and therefore an XSS payload)
can't read it. The short-lived access token is returned in the response body
for the frontend to hold in memory (never localStorage — see the frontend
apiClient for why).
"""

from datetime import timedelta

from django.conf import settings

REFRESH_COOKIE_NAME = "refresh_token"


def set_refresh_cookie(response, refresh_token: str) -> None:
    lifetime: timedelta = settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"]
    response.set_cookie(
        REFRESH_COOKIE_NAME,
        str(refresh_token),
        max_age=int(lifetime.total_seconds()),
        httponly=True,
        secure=not settings.DEBUG,
        samesite="Lax",
        path="/api/v1/auth/",
    )


def clear_refresh_cookie(response) -> None:
    response.delete_cookie(REFRESH_COOKIE_NAME, path="/api/v1/auth/")
