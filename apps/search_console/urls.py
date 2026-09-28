from django.urls import path

from . import views

app_name = "search_console"

urlpatterns = [
    path("search-console/callback/", views.CallbackView.as_view(), name="callback"),
    path("search-console/<int:business_id>/status/", views.ConnectionStatusView.as_view(), name="status"),
    path("search-console/<int:business_id>/authorize-url/", views.AuthorizeUrlView.as_view(), name="authorize-url"),
    path("search-console/<int:business_id>/disconnect/", views.DisconnectView.as_view(), name="disconnect"),
    path("search-console/<int:business_id>/sync/", views.SyncView.as_view(), name="sync"),
    path("search-console/<int:business_id>/overview/", views.OverviewView.as_view(), name="overview"),
    path("search-console/<int:business_id>/top-queries/", views.TopQueriesView.as_view(), name="top-queries"),
    path("search-console/<int:business_id>/top-pages/", views.TopPagesView.as_view(), name="top-pages"),
]
