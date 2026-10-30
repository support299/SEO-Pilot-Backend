from django.urls import path
from rest_framework.routers import DefaultRouter

from .views import AgencyCustomerDetailView, AgencyCustomersView, BusinessViewSet

app_name = "businesses"

router = DefaultRouter()
router.register("businesses", BusinessViewSet, basename="business")

urlpatterns = [
    path("agency/customers/", AgencyCustomersView.as_view(), name="agency-customers"),
    path("agency/customers/<int:pk>/", AgencyCustomerDetailView.as_view(), name="agency-customer-detail"),
    *router.urls,
]
