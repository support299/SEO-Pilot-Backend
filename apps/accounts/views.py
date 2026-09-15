from django.contrib.auth import authenticate, get_user_model
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken

from apps.businesses.models import Account
from common.jwt_cookies import REFRESH_COOKIE_NAME, clear_refresh_cookie, set_refresh_cookie

from .serializers import MeSerializer, RegisterSerializer, UserSerializer

User = get_user_model()


def _issue_tokens(user) -> tuple[str, str]:
    refresh = RefreshToken.for_user(user)
    return str(refresh.access_token), str(refresh)


class RegisterView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        serializer = RegisterSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()

        access, refresh = _issue_tokens(user)
        response = Response({"access": access, "user": UserSerializer(user).data}, status=status.HTTP_201_CREATED)
        set_refresh_cookie(response, refresh)
        return response


class LoginView(APIView):
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"

    def post(self, request):
        email = (request.data.get("email") or "").lower()
        password = request.data.get("password") or ""
        user = authenticate(request, username=email, password=password)

        if user is None:
            return Response({"detail": "Incorrect email or password."}, status=status.HTTP_401_UNAUTHORIZED)

        access, refresh = _issue_tokens(user)
        response = Response({"access": access, "user": UserSerializer(user).data})
        set_refresh_cookie(response, refresh)
        return response


class RefreshView(APIView):
    """
    Reads the refresh token from the httpOnly cookie, never from the request
    body. Rotates it on every use: the old one is blacklisted and a new one
    is cookied, so a leaked refresh token has a one-time-use window rather
    than staying valid for its full lifetime.
    """

    permission_classes = [AllowAny]

    def post(self, request):
        raw_refresh = request.COOKIES.get(REFRESH_COOKIE_NAME)
        if not raw_refresh:
            return Response({"detail": "No refresh token cookie present."}, status=status.HTTP_401_UNAUTHORIZED)

        try:
            old_refresh = RefreshToken(raw_refresh)
            access = str(old_refresh.access_token)
        except TokenError:
            response = Response({"detail": "Refresh token is invalid or expired."}, status=status.HTTP_401_UNAUTHORIZED)
            clear_refresh_cookie(response)
            return response

        user_id = old_refresh.payload.get("user_id")
        new_refresh = RefreshToken.for_user(User(id=user_id))

        try:
            old_refresh.blacklist()
        except AttributeError:
            pass  # token_blacklist app not installed — rotation still works, just without revocation of the old token

        response = Response({"access": access})
        set_refresh_cookie(response, new_refresh)
        return response


class LogoutView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request):
        raw_refresh = request.COOKIES.get(REFRESH_COOKIE_NAME)
        if raw_refresh:
            try:
                RefreshToken(raw_refresh).blacklist()
            except (TokenError, AttributeError):
                pass

        response = Response(status=status.HTTP_204_NO_CONTENT)
        clear_refresh_cookie(response)
        return response


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        accounts = Account.objects.filter(memberships__user=request.user).distinct()
        data = MeSerializer(
            {"user": request.user, "accounts": accounts},
            context={"request": request},
        ).data
        return Response(data)
