"""
Authentication: short-lived access JWT in the response body (kept in memory by
the SPA), refresh JWT in an httpOnly cookie scoped to /api/auth/.

- Refresh tokens rotate on every use; the old one is blacklisted.
- Cookie endpoints (refresh, logout) reject requests whose Origin isn't an
  allowed frontend origin, on top of the SameSite cookie attribute.
"""

import time

from django.conf import settings
from django.contrib.auth.tokens import default_token_generator
from django.core.cache import cache
from django.core.mail import send_mail
from django.utils.encoding import force_bytes, force_str
from django.utils.http import urlsafe_base64_decode, urlsafe_base64_encode
from drf_spectacular.utils import extend_schema, inline_serializer
from rest_framework import generics, permissions, serializers, status
from rest_framework.exceptions import (
    AuthenticationFailed,
    PermissionDenied,
    ValidationError,
)
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenBackendError, TokenError
from rest_framework_simplejwt.serializers import TokenRefreshSerializer
from rest_framework_simplejwt.state import token_backend
from rest_framework_simplejwt.token_blacklist.models import (
    BlacklistedToken,
    OutstandingToken,
)
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User
from accounts.serializers import (
    LoginSerializer,
    PasswordChangeSerializer,
    PasswordResetConfirmSerializer,
    PasswordResetRequestSerializer,
    RegisterSerializer,
    UserSerializer,
    validate_new_password,
)

ROTATION_GRACE_SECONDS = 30

AccessResponse = inline_serializer("AccessResponse", {"access": serializers.CharField(), "user": UserSerializer(required=False)})


def _set_refresh_cookie(response, refresh, remember):
    cookie = settings.REFRESH_COOKIE
    response.set_cookie(
        cookie["name"],
        refresh,
        max_age=int(settings.SIMPLE_JWT["REFRESH_TOKEN_LIFETIME"].total_seconds()) if remember else None,
        path=cookie["path"],
        domain=cookie["domain"],
        secure=cookie["secure"],
        httponly=True,
        samesite=cookie["samesite"],
    )


def _clear_refresh_cookie(response):
    cookie = settings.REFRESH_COOKIE
    response.delete_cookie(cookie["name"], path=cookie["path"], domain=cookie["domain"], samesite=cookie["samesite"])


def _check_origin(request):
    """Cookie-authenticated endpoints only accept browser requests from allowed origins."""
    origin = request.headers.get("Origin")
    if not origin:
        return  # not a cross-site browser request
    own = f"{request.scheme}://{request.get_host()}"
    if origin != own and origin not in settings.CORS_ALLOWED_ORIGINS:
        raise PermissionDenied("Origin not allowed.")


def revoke_all_sessions(user):
    """Blacklist every refresh token issued to `user` (password changes and resets)."""
    _mark_revoked(user.pk)
    for token in OutstandingToken.objects.filter(user=user).exclude(blacklistedtoken__isnull=False):
        BlacklistedToken.objects.get_or_create(token=token)


def _rotated_key(raw):
    try:
        return f"auth:rotated:{RefreshToken(raw, verify=False)['jti']}"
    except (TokenError, KeyError):
        return "auth:rotated:invalid"


def _revoked_key(user_id):
    return f"auth:revoked-before:{user_id}"


def _mark_revoked(user_id):
    """Close the rotation grace window for every token of `user_id` issued until now."""
    cache.set(_revoked_key(user_id), int(time.time()), ROTATION_GRACE_SECONDS * 2)


def _reuse_within_grace(raw):
    """
    A refresh token rotated less than ROTATION_GRACE_SECONDS ago may be presented
    again (a lost Set-Cookie when the user reloads mid-request, parallel tabs).
    It must still be correctly signed and unexpired; a new token is issued.
    Anything else (reuse after the window, sessions revoked by logout or a
    password change) stays rejected.
    """
    try:
        payload = token_backend.decode(raw, verify=True)
    except TokenBackendError:
        return None
    key = f"auth:rotated:{payload.get('jti')}"
    if not cache.get(key) or payload.get("token_type") != "refresh":
        return None
    # Sessions revoked after this token was issued (logout, password change) stay revoked
    revoked_at = cache.get(_revoked_key(payload.get("user_id")))
    if revoked_at is not None and payload.get("iat", 0) <= revoked_at:
        return None
    user = User.objects.filter(pk=payload.get("user_id"), is_active=True).first()
    if user is None:
        return None
    replacement = RefreshToken.for_user(user)
    replacement["remember"] = payload.get("remember", True)
    return replacement


class AuthThrottleMixin:
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "auth"


class RegisterView(AuthThrottleMixin, generics.CreateAPIView):
    """Creates an account. Does not sign in; the client logs in afterwards."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    serializer_class = RegisterSerializer


class LoginView(AuthThrottleMixin, APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=LoginSerializer, responses=AccessResponse)
    def post(self, request):
        serializer = LoginSerializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except AuthenticationFailed:
            # Explicit 401 (DRF would send 403 from a view without authenticators)
            return Response({"detail": "Incorrect email or password."}, status=status.HTTP_401_UNAUTHORIZED)
        data = serializer.validated_data
        response = Response({"access": data["access"], "user": data["user"]})
        _set_refresh_cookie(response, data["refresh"], data["remember"])
        return response


class RefreshView(APIView):
    """Exchanges the refresh cookie for a new access token and rotates the cookie."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "refresh"

    @extend_schema(request=None, responses=AccessResponse)
    def post(self, request):
        _check_origin(request)
        raw = request.COOKIES.get(settings.REFRESH_COOKIE["name"])
        if not raw:
            return Response({"detail": "No active session."}, status=status.HTTP_401_UNAUTHORIZED)
        serializer = TokenRefreshSerializer(data={"refresh": raw})
        try:
            serializer.is_valid(raise_exception=True)
        except (TokenError, ValidationError, AuthenticationFailed):
            replacement = _reuse_within_grace(raw)
            if replacement is None:
                response = Response({"detail": "Your session has expired."}, status=status.HTTP_401_UNAUTHORIZED)
                _clear_refresh_cookie(response)
                return response
            response = Response({"access": str(replacement.access_token)})
            _set_refresh_cookie(response, str(replacement), replacement.get("remember", True))
            return response
        data = serializer.validated_data
        # Remember the rotation briefly so a concurrent/interrupted refresh with the
        # previous cookie (two tabs, a reload mid-request) doesn't end the session.
        cache.set(_rotated_key(raw), True, ROTATION_GRACE_SECONDS)
        response = Response({"access": data["access"]})
        rotated = data.get("refresh", raw)
        _set_refresh_cookie(response, rotated, RefreshToken(rotated, verify=False).get("remember", True))
        return response


class LogoutView(APIView):
    """Blacklists the refresh token and clears the cookie. Works with an expired access token."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=None, responses={204: None})
    def post(self, request):
        _check_origin(request)
        raw = request.COOKIES.get(settings.REFRESH_COOKIE["name"])
        if raw:
            try:
                _mark_revoked(RefreshToken(raw, verify=False)["user_id"])
            except (TokenError, KeyError):
                pass
            try:
                RefreshToken(raw).blacklist()
            except TokenError:
                pass  # already invalid: nothing to revoke
        response = Response(status=status.HTTP_204_NO_CONTENT)
        _clear_refresh_cookie(response)
        return response


class MeView(generics.RetrieveUpdateAPIView):
    """The signed-in user's profile (name and title are editable)."""

    serializer_class = UserSerializer
    http_method_names = ["get", "patch", "head", "options"]

    def get_object(self):
        return self.request.user


class PasswordChangeView(APIView):
    @extend_schema(request=PasswordChangeSerializer, responses=AccessResponse)
    def post(self, request):
        serializer = PasswordChangeSerializer(data=request.data, context={"request": request})
        serializer.is_valid(raise_exception=True)
        request.user.set_password(serializer.validated_data["new_password"])
        request.user.save(update_fields=["password"])
        # Sign out every other session; this one gets a fresh refresh cookie
        revoke_all_sessions(request.user)
        refresh = RefreshToken.for_user(request.user)
        response = Response({"access": str(refresh.access_token)})
        _set_refresh_cookie(response, str(refresh), True)
        return response


class PasswordResetView(APIView):
    """Emails a reset link. Responds identically whether or not the account exists."""

    permission_classes = [permissions.AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "password_reset"

    @extend_schema(request=PasswordResetRequestSerializer, responses={204: None})
    def post(self, request):
        serializer = PasswordResetRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = User.objects.filter(email__iexact=serializer.validated_data["email"], is_active=True).first()
        if user is not None and user.has_usable_password():
            uid = urlsafe_base64_encode(force_bytes(user.pk))
            token = default_token_generator.make_token(user)
            link = f"{settings.FRONTEND_URL}/reset-password/{uid}/{token}"
            send_mail(
                "Reset your TaskPilot password",
                f"Someone asked to reset the password for {user.email}.\n\n"
                f"Choose a new password: {link}\n\nIf it wasn't you, ignore this email.",
                None,
                [user.email],
                fail_silently=True,
            )
        return Response(status=status.HTTP_204_NO_CONTENT)


class PasswordResetConfirmView(AuthThrottleMixin, APIView):
    permission_classes = [permissions.AllowAny]
    authentication_classes = []

    @extend_schema(request=PasswordResetConfirmSerializer, responses={204: None})
    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        try:
            user = User.objects.get(pk=force_str(urlsafe_base64_decode(data["uid"])), is_active=True)
        except (User.DoesNotExist, ValueError, TypeError, OverflowError):
            user = None
        if user is None or not default_token_generator.check_token(user, data["token"]):
            raise ValidationError({"detail": "This reset link is invalid or has expired."})
        try:
            validate_new_password(data["password"], user)
        except ValidationError as error:
            raise ValidationError({"password": error.detail}) from None
        user.set_password(data["password"])
        user.save(update_fields=["password"])
        revoke_all_sessions(user)
        return Response(status=status.HTTP_204_NO_CONTENT)
