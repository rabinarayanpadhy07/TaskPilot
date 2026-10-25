from django.contrib.auth import password_validation
from django.contrib.auth.models import update_last_login
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import serializers
from rest_framework_simplejwt.serializers import TokenObtainSerializer
from rest_framework_simplejwt.settings import api_settings
from rest_framework_simplejwt.tokens import RefreshToken

from accounts.models import User


def validate_new_password(password, user):
    try:
        password_validation.validate_password(password, user)
    except DjangoValidationError as error:
        raise serializers.ValidationError(list(error.messages)) from None
    return password


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "full_name", "title"]
        read_only_fields = ["id", "email"]


class RegisterSerializer(serializers.ModelSerializer):
    password = serializers.CharField(write_only=True, trim_whitespace=False, max_length=128)
    full_name = serializers.CharField(max_length=150)

    class Meta:
        model = User
        fields = ["id", "email", "full_name", "password"]

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("A user with this email already exists.")
        return value

    def validate(self, attrs):
        user = User(email=attrs["email"], full_name=attrs.get("full_name", ""))
        try:
            validate_new_password(attrs["password"], user)
        except serializers.ValidationError as error:
            raise serializers.ValidationError({"password": error.detail}) from None
        return attrs

    def create(self, validated_data):
        return User.objects.create_user(**validated_data)


class LoginSerializer(TokenObtainSerializer):
    """Email + password. `remember` decides whether the refresh cookie outlives the browser session."""

    token_class = RefreshToken
    remember = serializers.BooleanField(required=False, default=True)

    def validate(self, attrs):
        remember = attrs.pop("remember", True)
        attrs[self.username_field] = attrs[self.username_field].lower()
        super().validate(attrs)  # authenticates and sets self.user
        if api_settings.UPDATE_LAST_LOGIN:
            update_last_login(None, self.user)
        refresh = self.get_token(self.user)
        refresh["remember"] = bool(remember)  # copied into rotated tokens
        return {
            "access": str(refresh.access_token),
            "refresh": str(refresh),
            "remember": bool(remember),
            "user": UserSerializer(self.user).data,
        }


class PasswordResetRequestSerializer(serializers.Serializer):
    email = serializers.EmailField()


class PasswordResetConfirmSerializer(serializers.Serializer):
    uid = serializers.CharField()
    token = serializers.CharField()
    password = serializers.CharField(trim_whitespace=False, max_length=128)


class PasswordChangeSerializer(serializers.Serializer):
    current_password = serializers.CharField(trim_whitespace=False)
    new_password = serializers.CharField(trim_whitespace=False, max_length=128)

    def validate_current_password(self, value):
        if not self.context["request"].user.check_password(value):
            raise serializers.ValidationError("Your current password is incorrect.")
        return value

    def validate_new_password(self, value):
        return validate_new_password(value, self.context["request"].user)
