import re
from datetime import datetime, timedelta
import calendar

def parse_date(text: str) -> str | None:
    """
    Parses natural language date strings into DD-MM-YYYY format.
    Returns None if no date found.
    
    Supported formats:
    - "today", "tomorrow", "yesterday"
    - "day after tomorrow"
    - "next monday", "next friday"
    - "last monday"
    - "5th jan", "jan 5", "5 january"
    - "05-01-2026", "05/01/2026", "2026-01-05"
    """
    if not text:
        return None
        
    t = text.lower().strip()
    today = datetime.now()
    
    # ---------------- RELATIVE DATES ----------------
    if t == "today":
        return today.strftime("%d-%m-%Y")
        
    if t == "tomorrow" or t == "tmrw":
        return (today + timedelta(days=1)).strftime("%d-%m-%Y")
        
    if t == "day after tomorrow":
        return (today + timedelta(days=2)).strftime("%d-%m-%Y")
        
    if t == "yesterday":
        return (today - timedelta(days=1)).strftime("%d-%m-%Y")
        
    # ---------------- NEXT / LAST / THIS / BARE WEEKDAY ----------------
    weekdays = list(calendar.day_name)  # ['Monday', 'Tuesday', ...]
    weekdays_lower = [d.lower() for d in weekdays]

    m = re.search(r"(next|last|this)\s+([a-z]+)", t)
    if m:
        direction, day_name = m.groups()
        if day_name in weekdays_lower:
            target_idx = weekdays_lower.index(day_name)
            current_idx = today.weekday()

            if direction == "next":
                days_ahead = target_idx - current_idx
                if days_ahead <= 0:
                    days_ahead += 7
                return (today + timedelta(days=days_ahead)).strftime("%d-%m-%Y")

            elif direction == "last":
                days_behind = current_idx - target_idx
                if days_behind <= 0:
                    days_behind += 7
                return (today - timedelta(days=days_behind)).strftime("%d-%m-%Y")

            elif direction == "this":
                days_ahead = target_idx - current_idx
                if days_ahead < 0:
                    days_ahead += 7
                return (today + timedelta(days=days_ahead)).strftime("%d-%m-%Y")

    # bare weekday name: "saturday", "monday" → next upcoming occurrence
    for idx, day_name in enumerate(weekdays_lower):
        if re.search(rf"\b{day_name}\b", t):
            days_ahead = idx - today.weekday()
            if days_ahead <= 0:
                days_ahead += 7
            return (today + timedelta(days=days_ahead)).strftime("%d-%m-%Y")

    # ---------------- ABSOLUTE DATES ----------------
    # YYYY-MM-DD (ISO format — must check BEFORE DD-MM-YY to avoid substring misparse)
    # e.g. "2026-03-06" must not be parsed as "26-03-06" → year 2006
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", t)
    if m:
        y, mo, d = m.groups()
        return f"{int(d):02d}-{int(mo):02d}-{y}"

    # DD-MM-YYYY / DD/MM/YYYY
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", t)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}-{int(mo):02d}-{y}"

    # DD-MM-YY
    m = re.search(r"(\d{1,2})[-/](\d{1,2})[-/](\d{2})", t)
    if m:
        d, mo, y = m.groups()
        return f"{int(d):02d}-{int(mo):02d}-20{y}"

    # ---------------- TEXT DATES (5th Jan, Jan 5) ----------------
    months = {
        "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
        "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
        "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
        "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12
    }
    
    # "5th Jan" or "5 Jan"
    m = re.search(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,})", t)
    if m:
        d, month_str = m.groups()
        month_key = month_str.lower()
        # check full name or 3-char
        val = months.get(month_key) or months.get(month_key[:3])
        if val:
             return f"{int(d):02d}-{val:02d}-{today.year}"

    # "Jan 5" or "Jan 5th"
    m = re.search(r"([a-z]{3,})\s+(\d{1,2})(?:st|nd|rd|th)?", t)
    if m:
        month_str, d = m.groups()
        month_key = month_str.lower()
        val = months.get(month_key) or months.get(month_key[:3])
        if val:
             return f"{int(d):02d}-{val:02d}-{today.year}"

    return None
