"""InfluxDB line protocol: one point per line, `table,tag=value field=value timestamp_ns`."""
import math
from typing import Any, Dict, Optional


def escape(value: Any) -> str:
    """Escape a table name, tag key, tag value or field key."""
    return str(value).replace("\\", "\\\\").replace(",", "\\,").replace("=", "\\=").replace(" ", "\\ ")


def to_line(table: str, tags: Dict[str, Any], fields: Dict[str, Any], timestamp_ns: int) -> Optional[str]:
    """One point, or None when no field has a usable value. Empty tags are left out."""
    field_parts = []
    for key, value in fields.items():
        if isinstance(value, bool):
            field_parts.append(f"{escape(key)}={'true' if value else 'false'}")
        elif isinstance(value, int):
            field_parts.append(f"{escape(key)}={value}i")
        elif isinstance(value, float) and math.isfinite(value):
            field_parts.append(f"{escape(key)}={value!r}")
        elif isinstance(value, str):
            quoted = value.replace("\\", "\\\\").replace('"', '\\"')
            field_parts.append(f'{escape(key)}="{quoted}"')
    if not field_parts:
        return None
    tag_parts = [f"{escape(key)}={escape(value)}" for key, value in sorted(tags.items()) if value not in (None, "")]
    return f"{','.join([escape(table)] + tag_parts)} {','.join(field_parts)} {timestamp_ns}"
