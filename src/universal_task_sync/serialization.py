import json
import re
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

# ISO 8601 Duration regex: P[n]DT[n]H[n]M[n]S
ISO_8601_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?$"
)


class TaskJSONEncoder(json.JSONEncoder):
    """
    Standardizes TaskCIR types for JSON files.
    - datetimes -> ISO 8601 strings
    - timedeltas -> ISO 8601 duration strings
    - Enums -> their underlying values
    """

    def default(self, obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()

        if isinstance(obj, timedelta):
            # Formats to P[d]DT[h]H[m]M[s]S
            days = obj.days
            hours = obj.seconds // 3600
            minutes = (obj.seconds // 60) % 60
            seconds = obj.seconds % 60
            return f"P{days}DT{hours}H{minutes}M{seconds}S"

        if isinstance(obj, Enum):
            return obj.value

        return super().default(obj)


def parse_iso_duration(duration_str: str) -> Optional[timedelta]:
    """Parses ISO 8601 duration strings back to timedelta."""
    if not duration_str or duration_str == "None":
        return None

    match = ISO_8601_DURATION_RE.match(duration_str)
    if not match:
        return None

    parts = {k: int(v) for k, v in match.groupdict().items() if v}
    return timedelta(**parts)


def parse_iso_datetime(dt_str: str) -> Optional[datetime]:
    """Parses ISO 8601 datetime strings back to datetime objects."""
    if not dt_str or dt_str == "None":
        return None
    try:
        return datetime.fromisoformat(dt_str)
    except ValueError:
        return None
