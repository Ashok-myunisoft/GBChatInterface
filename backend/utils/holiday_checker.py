import holidays
from datetime import date, datetime
from typing import Optional
from config import settings

_HOLIDAY_CACHE: dict = {}

# Fixed Indian national holidays not covered by the `holidays` library
_FIXED_IN_HOLIDAYS = {
    (4, 14): "Dr. B.R. Ambedkar Jayanti",
}


def _fetch_holidays(year: int) -> dict:
    """Returns a dict of {date: holiday_name} for the configured country and year."""
    if year in _HOLIDAY_CACHE:
        return _HOLIDAY_CACHE[year]
    try:
        country_code = settings.HOLIDAY_COUNTRY_CODE
        h = dict(holidays.country_holidays(country_code, years=year))

        # Supplement with fixed holidays missing from the library (India-specific)
        if country_code == "IN":
            for (month, day), name in _FIXED_IN_HOLIDAYS.items():
                key = date(year, month, day)
                if key not in h:
                    h[key] = name

        _HOLIDAY_CACHE[year] = h
    except Exception:
        _HOLIDAY_CACHE[year] = {}
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

    holidays_map = _fetch_holidays(dt.year)
    holiday_name = holidays_map.get(dt.date())
    if holiday_name:
        return f"⚠️ {dt.strftime('%A, %d %b %Y')} is a public holiday ({holiday_name})."

    return None
