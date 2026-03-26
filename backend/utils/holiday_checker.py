import requests
from datetime import datetime
from typing import Optional
from config import settings

_HOLIDAY_CACHE: dict = {}


def _fetch_holidays(year: int) -> list:
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    try:
        url = f"https://date.nager.at/api/v3/PublicHolidays/{year}/{settings.HOLIDAY_COUNTRY_CODE}"
        resp = requests.get(url, timeout=10)
        resp.raise_for_status()
        _HOLIDAY_CACHE[year] = resp.json()
    except Exception:
        _HOLIDAY_CACHE[year] = []
    return _HOLIDAY_CACHE[year]


def check_date_warning(date_str: str) -> Optional[str]:
    """
    Returns a warning string if the date is a weekoff or public holiday.
    date_str must be in DD-MM-YYYY format.
    Returns None if the date is a normal working day.
    """
    if not date_str:
        return None

    try:
        dt = datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        return None

    weekoff_days = [int(d.strip()) for d in settings.WEEKOFF_DAYS.split(",") if d.strip()]

    if dt.weekday() in weekoff_days:
        return f"⚠️ {dt.strftime('%A, %d %b %Y')} is a weekoff day."

    holidays = _fetch_holidays(dt.year)
    date_key = dt.strftime("%Y-%m-%d")
    for h in holidays:
        if h.get("date") == date_key:
            name = h.get("localName") or h.get("name") or "Public Holiday"
            return f"⚠️ {dt.strftime('%A, %d %b %Y')} is a public holiday ({name})."

    return None
