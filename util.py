"""
Place to put random stuff that isn't strictly database related
"""

from datetime import datetime, UTC


def get_current_timestamp():
    """
    Returns current UTC time in ISO format with Z suffix.
    """
    current_utc_aware = datetime.now(UTC)
    return current_utc_aware.isoformat().replace("+00:00", "Z")
