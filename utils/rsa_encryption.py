import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

_RSA_KEY_SIZE = 2048
_RSA_PUBLIC_EXPONENT = 65537
_PRIVATE_KEY_FILENAME = ".rsa_private_key.pem"
_PUBLIC_KEY_FILENAME = ".rsa_public_key.pem"

_cached_private_key = None
_cached_public_key = None


def _key_dir() -> Path:
    from utils.file_system import fs_util
    return Path(fs_util._get_full_path("credentials"))


def _load_or_create_key_pair():
    global _cached_private_key, _cached_public_key
    if _cached_private_key is not None:
        return _cached_private_key, _cached_public_key

    key_dir = _key_dir()
    private_key_path = key_dir / _PRIVATE_KEY_FILENAME
    public_key_path = key_dir / _PUBLIC_KEY_FILENAME

    if private_key_path.exists():
        with open(private_key_path, "rb") as f:
            _cached_private_key = serialization.load_pem_private_key(f.read(), password=None)
        with open(public_key_path, "rb") as f:
            _cached_public_key = serialization.load_pem_public_key(f.read())
    else:
        _cached_private_key = rsa.generate_private_key(
            public_exponent=_RSA_PUBLIC_EXPONENT,
            key_size=_RSA_KEY_SIZE,
        )
        _cached_public_key = _cached_private_key.public_key()

        key_dir.mkdir(parents=True, exist_ok=True)
        with open(private_key_path, "wb") as f:
            f.write(_cached_private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=serialization.NoEncryption(),
            ))
        # Restrict permissions on private key file (owner read/write only)
        os.chmod(private_key_path, 0o600)

        with open(public_key_path, "wb") as f:
            f.write(_cached_public_key.public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            ))

    return _cached_private_key, _cached_public_key


def get_public_key_pem() -> str:
    """Return the server RSA public key in PEM format."""
    _, public_key = _load_or_create_key_pair()
    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()


def decrypt_credential_value(encrypted_b64: str) -> str:
    """Decrypt a base64-encoded RSA-OAEP encrypted credential value."""
    private_key, _ = _load_or_create_key_pair()
    try:
        encrypted_bytes = base64.b64decode(encrypted_b64)
    except Exception:
        raise ValueError("Invalid base64 encoding in encrypted credential value")
    decrypted = private_key.decrypt(
        encrypted_bytes,
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return decrypted.decode()


def decrypt_credentials(credentials: dict) -> dict:
    """Decrypt all string values in the credentials dict using RSA-OAEP."""
    decrypted = {}
    for key, value in credentials.items():
        if isinstance(value, str):
            decrypted[key] = decrypt_credential_value(value)
        else:
            decrypted[key] = value
    return decrypted
