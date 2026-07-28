from decimal import Decimal
from enum import Enum
from typing import Any, Dict, Optional


def extract_masked_credential_parameters(config_map) -> Dict[str, Any]:
    parameters: Dict[str, Any] = {}

    for traversal_item in config_map.traverse():
        config_path = getattr(traversal_item, "config_path", None)
        attr_name = getattr(traversal_item, "attr", None)
        if not config_path or config_path == "connector":
            continue

        normalized_value = normalize_credential_value(getattr(traversal_item, "value", None), attr_name)
        if normalized_value is None:
            continue

        parameters[config_path] = normalized_value

    return parameters


def normalize_credential_value(value: Any, key_name: Optional[str] = None) -> Any:
    if hasattr(value, "get_secret_value"):
        value = value.get_secret_value()

    if isinstance(value, Enum):
        value = value.value

    if isinstance(value, Decimal):
        value = float(value)

    if isinstance(value, dict):
        return {
            key: normalize_credential_value(item, key)
            for key, item in value.items()
        }

    if isinstance(value, (list, tuple, set)):
        return [normalize_credential_value(item, key_name) for item in value]

    if value is None or isinstance(value, (bool, int, float)):
        return value

    if isinstance(value, str):
        return mask_credential_value(key_name or "", value)

    return None


def mask_credential_value(key_name: str, value: str) -> str:
    lower_key = key_name.lower()
    if value == "":
        return value

    fully_masked_fields = [
        "secret",
        "password",
        "passphrase",
        "private_key",
        "privatekey",
        "mnemonic",
    ]
    partially_masked_fields = [
        "api_key",
        "access_key",
        "key",
        "token",
    ]

    if any(field in lower_key for field in fully_masked_fields):
        return "********"

    if any(field in lower_key for field in partially_masked_fields):
        return partially_mask_value(value)

    return value


def partially_mask_value(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    if len(value) <= 8:
        return f"{value[:2]}{'*' * (len(value) - 4)}{value[-2:]}"
    return f"{value[:4]}{'*' * (len(value) - 8)}{value[-4:]}"