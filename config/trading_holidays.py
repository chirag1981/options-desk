"""
config/trading_holidays.py — Dated Exchange Trading Holidays and Special Sessions Configuration
Sources:
- NSE Master Circular Ref: NSE/CMTR/65500 & NSE Trading Holidays Calendar 2026
- BSE Notice No: 20251215-18 (Trading Holidays for Calendar Year 2026)
Effective Calendar Year: 2026
"""

import logging
from datetime import datetime, date, time as dtime
from zoneinfo import ZoneInfo

log = logging.getLogger("trading_holidays")
IST = ZoneInfo("Asia/Kolkata")

# Standard NSE/BSE Trading Holidays (YYYY-MM-DD -> Description)
# Verified against NSE/BSE 2026 official publications
NSE_HOLIDAYS_MAP = {
    "2026-01-26": "Republic Day",
    "2026-02-18": "Mahashivratri",
    "2026-03-03": "Holi",
    "2026-03-26": "Id-Ul-Fitr (Ramzan Id)",
    "2026-04-03": "Good Friday",
    "2026-04-14": "Dr. Baba Saheb Ambedkar Jayanti",
    "2026-05-01": "Maharashtra Day",
    "2026-05-28": "Bakri Id",
    "2026-06-26": "Muharram",
    "2026-08-15": "Independence Day",
    "2026-09-04": "Janmashtami",
    "2026-10-02": "Mahatma Gandhi Jayanti",
    "2026-10-20": "Dussehra",
    "2026-11-08": "Diwali Laxmi Pujan (Regular session closed; Muhurat trading only)",
    "2026-11-10": "Diwali Balipratipada",
    "2026-11-24": "Gurunanak Jayanti",
    "2026-12-25": "Christmas",
}

NSE_HOLIDAYS = set(NSE_HOLIDAYS_MAP.keys())

# Special Trading Sessions (e.g. Annual Diwali Muhurat Trading, Mock Trading)
# Format: YYYY-MM-DD: {"name": str, "start_time": "HH:MM", "end_time": "HH:MM"}
SPECIAL_TRADING_SESSIONS = {
    "2026-11-08": {
        "name": "Diwali Laxmi Pujan (Muhurat Trading)",
        "start_time": dtime(18, 15),
        "end_time": dtime(19, 15),
    }
}

# Early Closes / Shortened Sessions
EARLY_CLOSE_SESSIONS = {}


def is_market_holiday(target_date: date | datetime | str) -> bool:
    """
    Checks if given date is an official exchange trading holiday.
    Logs a warning if the list is missing definitions for the target year.
    Returns True if holiday, False otherwise.
    """
    if isinstance(target_date, datetime):
        d_obj = target_date.date()
    elif isinstance(target_date, date):
        d_obj = target_date
    else:
        try:
            d_obj = datetime.strptime(str(target_date)[:10], "%Y-%m-%d").date()
        except Exception:
            return False

    year_str = str(d_obj.year)
    known_years = {k[:4] for k in NSE_HOLIDAYS}
    if year_str not in known_years:
        log.warning(
            f"[HOLIDAY_CONFIG_OUTDATED] NSE/BSE holidays configuration is missing calendar definitions for year {year_str}. "
            f"Please update config/trading_holidays.py with current exchange circulars."
        )

    d_str = d_obj.strftime("%Y-%m-%d")
    return d_str in NSE_HOLIDAYS


def get_holiday_reason(target_date: date | datetime | str) -> str:
    """Returns description of the holiday if target date is an exchange holiday."""
    if isinstance(target_date, (datetime, date)):
        d_str = target_date.strftime("%Y-%m-%d")
    else:
        d_str = str(target_date)[:10]
    return NSE_HOLIDAYS_MAP.get(d_str, "")


def get_special_session_window(target_date: date | datetime | str) -> dict | None:
    """Returns special session timings if target date is configured for non-standard trading hours."""
    if isinstance(target_date, (datetime, date)):
        d_str = target_date.strftime("%Y-%m-%d")
    else:
        d_str = str(target_date)[:10]

    return SPECIAL_TRADING_SESSIONS.get(d_str)


def is_special_session(target_date: date | datetime | str) -> tuple[bool, str, tuple[dtime, dtime] | None]:
    """
    Checks if given date has a special trading session (e.g. Diwali Muhurat Trading).
    Returns (is_special, session_name, (start_time, end_time) or None).
    """
    spec = get_special_session_window(target_date)
    if spec:
        return True, spec["name"], (spec["start_time"], spec["end_time"])
    return False, "", None
