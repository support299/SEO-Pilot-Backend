from django.urls import path

from . import views

app_name = "analytics"

urlpatterns = [
    path("analytics/callback/", views.CallbackView.as_view(), name="callback"),
    path("analytics/<int:business_id>/status/", views.ConnectionStatusView.as_view(), name="status"),
    path("analytics/<int:business_id>/authorize-url/", views.AuthorizeUrlView.as_view(), name="authorize-url"),
    path("analytics/<int:business_id>/disconnect/", views.DisconnectView.as_view(), name="disconnect"),
    path("analytics/<int:business_id>/sync/", views.SyncView.as_view(), name="sync"),
    path("analytics/<int:business_id>/overview/", views.OverviewView.as_view(), name="overview"),
]
