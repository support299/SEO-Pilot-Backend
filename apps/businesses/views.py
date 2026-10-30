from django.contrib.auth import get_user_model
from django.db import transaction
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.pagination import DefaultPagination
from common.permissions import DenyViewerWrites, IsAccountMember, IsAgencyAdmin, accounts_for_user

from .models import Account, Business
from .serializers import BusinessSerializer, CustomerCreateSerializer


def serialize_customer(account: Account) -> dict:
    return {
        "id": account.id,
        "name": account.name,
        "is_paused": account.is_paused,
        "created_at": account.created_at.isoformat(),
        "businesses": [
            {
                "id": b.id,
                "name": b.name,
                "website": b.website,
                "is_paused": b.is_paused,
                "effective_paused": b.is_effective_paused,
            }
            for b in account.businesses.all()
        ],
        "members": [{"email": m.user.email, "role": m.role} for m in account.memberships.all()],
    }


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
        return Response([serialize_customer(account) for account in accounts])

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


class AgencyCustomerDetailView(APIView):
    """
    /api/v1/agency/customers/<int:pk>/ — agency admins only.

    GET, PATCH, DELETE for a customer account.
    """

    permission_classes = [IsAuthenticated, IsAgencyAdmin]

    def get_object(self, pk: int) -> Account:
        return get_object_or_404(
            Account.objects.prefetch_related("businesses", "memberships__user"),
            pk=pk,
        )

    def get(self, request, pk: int):
        account = self.get_object(pk)
        return Response(serialize_customer(account))

    def patch(self, request, pk: int):
        account = self.get_object(pk)
        if "name" in request.data:
            name = str(request.data["name"]).strip()
            if not name:
                return Response({"name": ["This field cannot be blank."]}, status=status.HTTP_400_BAD_REQUEST)
            account.name = name
        if "is_paused" in request.data:
            account.is_paused = bool(request.data["is_paused"])
        account.save()
        return Response(serialize_customer(account))

    @transaction.atomic
    def delete(self, request, pk: int):
        account = get_object_or_404(Account, pk=pk)
        viewer_user_ids = list(
            account.memberships.filter(role=account.memberships.model.Role.VIEWER).values_list("user_id", flat=True)
        )
        account.delete()

        User = get_user_model()
        for user_id in viewer_user_ids:
            orphan = User.objects.filter(id=user_id, is_agency_admin=False, is_staff=False).first()
            if orphan and not orphan.memberships.exists():
                orphan.delete()

        return Response(status=status.HTTP_204_NO_CONTENT)
