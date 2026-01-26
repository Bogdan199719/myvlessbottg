import pytz
from datetime import datetime
import logging

# Define MSK timezone
MSK_TZ = pytz.timezone('Europe/Moscow')

logger = logging.getLogger(__name__)

def get_msk_now() -> datetime:
    """Returns the current aware datetime in Moscow timezone."""
    return datetime.now(MSK_TZ)

def to_msk(dt: datetime) -> datetime:
    """
    Converts a datetime object to MSK.
    If naive, assumes it was meant to be MSK (or system local which we treat as source of truth for MSK context).
    If aware, converts to MSK.
    """
    if dt is None:
        return None
    
    if dt.tzinfo is None:
        # If naive, localize to MSK directly (assuming stored implementation was creating naive times intended for this zone)
        # OR if naive came from datetime.now() on a server with different time, this forces it to MSK frame.
        # Given the requirements, we force it to be interpreted as MSK.
        return MSK_TZ.localize(dt)
    
    return dt.astimezone(MSK_TZ)

def get_timestamp_ms(dt: datetime) -> int:
    """
    Returns UTC timestamp in milliseconds for a given datetime.
    Ensures input is treated as MSK if naive.
    """
    if dt is None:
        return 0
        
    msk_dt = to_msk(dt)
    return int(msk_dt.timestamp() * 1000)

def format_msk(dt: datetime, fmt: str = '%d.%m.%Y %H:%M') -> str:
    """Formats a datetime in MSK."""
    if dt is None:
        return ""
    
    msk_dt = to_msk(dt)
    return msk_dt.strftime(fmt)

def from_timestamp_ms(ts_ms: int) -> datetime:
    """Converts a UTC timestamp (ms) to an MSK datetime."""
    if ts_ms is None:
        return None
    
    utc_dt = datetime.fromtimestamp(ts_ms / 1000, pytz.UTC)
    return utc_dt.astimezone(MSK_TZ)

def parse_iso_to_msk(date_str: str) -> datetime | None:
    """
    Parses an ISO date string (likely from DB).
    If naive, assumes UTC (to correct for DB storage issues).
    Returns MSK aware datetime.
    """
    if not date_str:
        return None
    try:
        dt = datetime.fromisoformat(date_str)
        if dt.tzinfo is None:
            # Assume UTC if naive (matches observed behavior where 08:30 stored -> 11:30 target)
            dt = dt.replace(tzinfo=pytz.UTC)
        return dt.astimezone(MSK_TZ)
    except Exception as e:
        logger.error(f"Error parsing date {date_str}: {e}")
        return None
