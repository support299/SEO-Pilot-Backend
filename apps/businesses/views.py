from rest_framework import viewsets
from rest_framework.permissions import IsAuthenticated

from common.pagination import DefaultPagination
from common.permissions import IsAccountMember, accounts_for_user

from .models import Business
from .serializers import BusinessSerializer


class BusinessViewSet(viewsets.ModelViewSet):
    """
    /api/v1/businesses/

    Tenant isolation is enforced twice, deliberately: get_queryset() means a
    user can never even *list* another account's businesses, and
    IsAccountMember means a direct-by-id request (GET /businesses/<id>/) for
    a business outside the user's accounts 404s instead of leaking existence.
    """

    serializer_class = BusinessSerializer
    permission_classes = [IsAuthenticated, IsAccountMember]
    pagination_class = DefaultPagination

    def get_queryset(self):
        return Business.objects.filter(account_id__in=accounts_for_user(self.request.user)).order_by("-created_at")
