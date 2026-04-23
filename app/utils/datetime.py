from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def lookback_window(hours: int) -> tuple[datetime, datetime]:
    end = utcnow()
    start = end - timedelta(hours=hours)
    return start, end
