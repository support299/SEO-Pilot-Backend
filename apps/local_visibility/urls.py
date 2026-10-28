from django.urls import path

from . import views

app_name = "local_visibility"

urlpatterns = [
    path("local-visibility/<int:business_id>/", views.OverviewView.as_view(), name="overview"),
    path("local-visibility/<int:business_id>/profile/", views.ProfileView.as_view(), name="profile"),
    path("local-visibility/<int:business_id>/keywords/", views.KeywordsView.as_view(), name="keywords"),
    path("local-visibility/<int:business_id>/keywords/<int:keyword_id>/", views.KeywordDetailView.as_view(), name="keyword-detail"),
    path("local-visibility/<int:business_id>/keywords/<int:keyword_id>/scan/", views.KeywordScanView.as_view(), name="keyword-scan"),
]
