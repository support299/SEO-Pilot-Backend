from django.http import HttpResponse
from django.utils.html import escape

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title></head>
<body style="font-family:system-ui,sans-serif;text-align:center;padding:48px 24px;color:#1d2939">
<h2 style="margin:0 0 8px">{title}</h2><p style="color:#667085">{detail}</p>
<script>setTimeout(function(){{window.close()}},1200)</script></body></html>"""


def popup_result_page(*, ok: bool, detail: str = "") -> HttpResponse:
    """Shown in the Google sign-in popup when it finishes. The app behind it notices the new connection by itself."""
    title = "Google connected" if ok else "Google was not connected"
    detail = detail or ("You can close this window and go back to SEO Pilot." if ok else "Close this window and try again from SEO Pilot.")
    return HttpResponse(PAGE.format(title=escape(title), detail=escape(detail)))
