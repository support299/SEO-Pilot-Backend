from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.pagination import DefaultPagination
from common.permissions import DenyViewerWrites, IsAccountMember, IsAgencyAdmin, accounts_for_user

from .models import Account, Business
from .serializers import BusinessSerializer, CustomerCreateSerializer


class BusinessViewSet(viewsets.ModelViewSet):
    """
    /api/v1/businesses/

    Tenant isolation is enforced twice, deliberately: get_queryset() means a
    user can never even *list* another account's businesses, and
    IsAccountMember means a direct-by-id request (GET /businesses/<id>/) for
    a business outside the user's accounts 404s instead of leaking existence.
    """

    serializer_class = BusinessSerializer
    permission_classes = [IsAuthenticated, DenyViewerWrites, IsAccountMember]
    pk_is_business = True  # lets DenyViewerWrites resolve the account from the URL's pk
    pagination_class = DefaultPagination

    def get_queryset(self):
        return Business.objects.filter(account_id__in=accounts_for_user(self.request.user)).order_by("-created_at")


class AgencyCustomersView(APIView):
    """
    /api/v1/agency/customers/ — agency admins only.

    GET lists every customer account with its businesses and logins; POST
    onboards a new one (account + first business + optional read-only login).
    """

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def get(self, request):
        accounts = Account.objects.prefetch_related("businesses", "memberships__user").order_by("-created_at")
        return Response(
            [
                {
                    "id": account.id,
                    "name": account.name,
                    "created_at": account.created_at.isoformat(),
                    "businesses": [{"id": b.id, "name": b.name, "website": b.website} for b in account.businesses.all()],
                    "members": [{"email": m.user.email, "role": m.role} for m in account.memberships.all()],
                }
                for account in accounts
            ]
        )

    def post(self, request):
        serializer = CustomerCreateSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        created = serializer.save()
        return Response(
            {
                "account": {"id": created["account"].id, "name": created["account"].name},
                "business": {"id": created["business"].id, "name": created["business"].name, "website": created["business"].website},
                "login_email": created["login_email"],
            },
            status=status.HTTP_201_CREATED,
        )
