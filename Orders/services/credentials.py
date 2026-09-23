from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def _fernet():
    key = getattr(settings, "RESTAURANT_CREDENTIAL_ENCRYPTION_KEY", "")
    if not key:
        raise ImproperlyConfigured(
            "Set RESTAURANT_CREDENTIAL_ENCRYPTION_KEY before storing restaurant credentials."
        )
    try:
        return Fernet(key.encode())
    except (TypeError, ValueError) as error:
        raise ImproperlyConfigured(
            "RESTAURANT_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key."
        ) from error


def encrypt_app_password(app_password):
    if not app_password:
        raise ValueError("An app password is required.")
    return _fernet().encrypt(app_password.encode()).decode()


def decrypt_app_password(encrypted_app_password):
    if not encrypted_app_password:
        return ""
    try:
        return _fernet().decrypt(encrypted_app_password.encode()).decode()
    except InvalidToken as error:
        raise ImproperlyConfigured("Stored restaurant credential cannot be decrypted.") from error
