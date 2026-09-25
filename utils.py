"""
General utilities: string sanitization, safe conversions, hashing.
"""

import hashlib
import re
from typing import Any


def sanitize_text(value: Any, max_length: int | None = None, remove_emojis: bool = False) -> str:
    """
    Sanitize text for XML-RPC and database safety.
    - Strips XML-RPC invalid control characters (ASCII 0-8, 11-12, 14-31).
    - Optionally removes 4-byte characters (emojis / surrogate pairs) if requested.
    - Truncates to max_length if specified.
    """
    if value is None:
        return ""

    text = str(value)

    # Strip XML-RPC 1.0 illegal control characters
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)

    if remove_emojis:
        # Strip characters outside BMP (Basic Multilingual Plane: > 0xFFFF)
        text = re.sub(r"[\U00010000-\U0010ffff]", "", text)

    text = text.strip()

    if max_length and len(text) > max_length:
        text = text[:max_length].strip()

    return text


def safe_float(val: Any, default: float = 0.0) -> float:
    """Safely convert value to float, handling strings, None, and commas."""
    if val is None:
        return default
    try:
        if isinstance(val, str):
            val = val.replace(",", "").strip()
        return float(val)
    except (ValueError, TypeError):
        return default


def safe_int(val: Any, default: int = 0) -> int:
    """Safely convert value to integer."""
    if val is None:
        return default
    try:
        return int(safe_float(val, default=float(default)))
    except (ValueError, TypeError):
        return default


def compute_md5(data: bytes | str) -> str:
    """Compute MD5 hex digest of string or bytes."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.md5(data).hexdigest()