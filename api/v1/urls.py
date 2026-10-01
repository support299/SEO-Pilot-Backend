"""
/api/v1/ — every app's URLs get included here, under a namespaced prefix. New
apps (search_console, analytics, wordpress, ...) plug in the same way; this
file is the only place that needs to change to expose a new app's API.
"""

from django.urls import include, path

urlpatterns = [
    path("auth/", include("apps.accounts.urls")),
    path("", include("apps.businesses.urls")),
    path("", include("apps.search_console.urls")),
    path("", include("apps.site_health.urls")),
]
