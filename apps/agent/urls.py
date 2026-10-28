from django.urls import path

from . import views

app_name = "agent"

urlpatterns = [
    path("businesses/", views.BusinessListView.as_view(), name="businesses"),
    path("businesses/<int:business_id>/context/", views.BusinessContextView.as_view(), name="context"),
    path("businesses/<int:business_id>/wordpress/pages/", views.WordPressInventoryView.as_view(), name="inventory"),
    path("businesses/<int:business_id>/local-visibility/", views.LocalVisibilityView.as_view(), name="local-visibility"),
    path("businesses/<int:business_id>/proposals/", views.ProposalsView.as_view(), name="proposals"),
]
