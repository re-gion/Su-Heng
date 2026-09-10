from __future__ import annotations

from typing import Any


def repair_unicode_scalars(value: Any) -> Any:
    """Replace isolated UTF-16 surrogates while preserving valid non-BMP text."""
    if isinstance(value, str):
        return value.encode("utf-16-le", "surrogatepass").decode("utf-16-le", "replace")
    if isinstance(value, list):
        return [repair_unicode_scalars(item) for item in value]
    if isinstance(value, dict):
        return {
            repair_unicode_scalars(key): repair_unicode_scalars(item) for key, item in value.items()
        }
    return value
