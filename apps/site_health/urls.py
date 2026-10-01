from django.urls import path

from . import views

app_name = "site_health"

urlpatterns = [
    path("site-health/<int:business_id>/", views.SiteHealthView.as_view(), name="report"),
    path("site-health/<int:business_id>/crawl/", views.StartCrawlView.as_view(), name="crawl"),
]
