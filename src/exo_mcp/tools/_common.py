"""Shared helpers for the tool layer: field projection and size parsing.

Exchange cmdlets return well over a hundred properties per object, each with an
`@odata.type` companion key. Tools project a whitelist instead, so an agent's
context pays only for the fields it can act on.
"""

import re

from .._json import error_envelope
from ..api_client import strip_odata

NO_CREDENTIALS = error_envelope(
    "not_configured",
    "No Exchange Online credentials. Send the X-Exo-Tenant-Id, X-Exo-Client-Id and "
    "X-Exo-Certificate headers.",
    False,
)

# A shared mailbox stays licence-free only below 50 GB.
SHARED_MAILBOX_LIMIT_BYTES = 50 * 1024**3

_BYTES_IN_PARENS = re.compile(r"\(([\d,\.\s]+)\s*bytes\)")
_SIZE_WITH_UNIT = re.compile(r"^\s*([\d\.,]+)\s*(B|KB|MB|GB|TB)\b", re.IGNORECASE)
_UNIT_FACTOR = {"B": 1, "KB": 1024, "MB": 1024**2, "GB": 1024**3, "TB": 1024**4}


def project(record: dict, fields: dict[str, str]) -> dict:
    """Map cmdlet properties to snake_case output keys, dropping absent ones."""
    clean = strip_odata(record)
    out: dict[str, object] = {}
    for source, target in fields.items():
        if source in clean and clean[source] is not None:
            out[target] = _scalarize(clean[source])
    return out


def _scalarize(value: object) -> object:
    """Flatten the admin endpoint's occasional {"Value": ...} wrappers."""
    if isinstance(value, dict) and set(value) <= {"Value", "value"}:
        return value.get("Value", value.get("value"))
    return value


def parse_size_bytes(size: object) -> int | None:
    """Read a byte count out of an Exchange size string.

    Sizes arrive as `"1.66 GB (1,782,579,200 bytes)"`; the parenthesised exact
    count is preferred, with the human unit as a fallback.
    """
    if isinstance(size, (int, float)):
        return int(size)
    text = _scalarize(size)
    if not isinstance(text, str):
        return None
    match = _BYTES_IN_PARENS.search(text)
    if match:
        digits = re.sub(r"[,\s]", "", match.group(1)).split(".")[0]
        if digits.isdigit():
            return int(digits)
    match = _SIZE_WITH_UNIT.match(text)
    if match:
        amount = float(match.group(1).replace(",", ""))
        return int(amount * _UNIT_FACTOR[match.group(2).upper()])
    return None
