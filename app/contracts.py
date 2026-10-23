"""
app/contracts.py — Authoritative Derivative Contract Specification Registry & Validator
Authoritative Source: National Stock Exchange of India (NSE) Circulars:
1. Circular No. NSE/FAOP/64625 (Dated Oct 18, 2024; Effective Nov 20, 2024): NIFTY lot size revised from 25 to 75.
2. Circular No. NSE/FAOP/70616 (Dated Oct 3, 2025; Effective Oct 28, 2025 EOD): NIFTY lot size revised from 75 to 65.
"""

import re
from datetime import datetime, date
from typing import Tuple, Optional, Dict, Any
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")

# Versioned Contract Specifications by Date
# Format: list of (effective_from_date, effective_to_date, lot_size, strike_step)
NIFTY_SPEC_SCHEDULE = [
    {
        "effective_from": date(2020, 1, 1),
        "effective_to": date(2024, 11, 19),
        "lot_size": 25,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/PRE_2024",
    },
    {
        "effective_from": date(2024, 11, 20),
        "effective_to": date(2025, 10, 27),
        "lot_size": 75,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/64625",
    },
    {
        "effective_from": date(2025, 10, 28),
        "effective_to": date(2099, 12, 31),
        "lot_size": 65,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/70616",
    },
]

# Regex for standard NSE options symbol format: e.g. NSE:NIFTY26OCT24500CE or NIFTY26OCT24500PE
SYMBOL_PATTERN = re.compile(
    r"^(NSE:)?(NIFTY)(\d{2}[A-Z]{3})(\d+(?:\.\d+)?)(CE|PE)$",
    re.IGNORECASE,
)


class ContractRegistry:
    """Authoritative validator for index derivative contract specifications."""

    @classmethod
    def get_authoritative_lot_size(cls, symbol: str = "NIFTY", as_of_date: Optional[date] = None) -> int:
        """Returns the authoritative exchange lot size active on a given calendar date."""
        if as_of_date is None:
            as_of_date = datetime.now(IST).date()

        sym_upper = symbol.upper().replace("NSE:", "").strip()
        if sym_upper.startswith("NIFTY"):
            for spec in NIFTY_SPEC_SCHEDULE:
                if spec["effective_from"] <= as_of_date <= spec["effective_to"]:
                    return spec["lot_size"]
            return 65

        raise ValueError(f"Unsupported contract symbol for lot size lookup: {symbol}")

    @classmethod
    def validate_contract(
        cls,
        symbol: str,
        strike: float,
        expiry: str,
        provided_lot_size: Optional[int] = None,
        as_of_date: Optional[date] = None,
    ) -> Tuple[bool, str, int]:
        """
        Validates contract metadata against official NSE specifications.
        Returns: (is_valid, rejection_reason, validated_lot_size)
        """
        if as_of_date is None:
            as_of_date = datetime.now(IST).date()

        # 1. Validate Strike Step (NIFTY standard is 50.0 pts)
        if strike <= 0 or (strike % 50.0 != 0.0):
            return False, f"Invalid strike {strike}: NIFTY options must be exact multiples of 50.0.", 0

        # 2. Check Expiry String
        if not expiry or not expiry.strip():
            return False, "Missing contract expiry.", 0

        # 3. Determine Authoritative Lot Size
        expected_lot_size = cls.get_authoritative_lot_size("NIFTY", as_of_date)

        # 4. If provider supplied a lot size, verify it matches exchange specification
        if provided_lot_size is not None and provided_lot_size > 0:
            if provided_lot_size != expected_lot_size:
                return (
                    False,
                    f"Provider lot size ({provided_lot_size}) does not match authoritative NSE lot size ({expected_lot_size}) for date {as_of_date}.",
                    0,
                )

        return True, "Contract validated against authoritative NSE specification.", expected_lot_size
