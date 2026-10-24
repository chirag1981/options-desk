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

# Versioned Contract Specifications by Date & Expiry Series
# Authoritative Source: National Stock Exchange of India (NSE) Circulars:
# 1. Circular No. NSE/FAOP/61328 (Dated Mar 28, 2024; Effective Apr 26, 2024): NIFTY lot size 25.
# 2. Circular No. NSE/FAOP/64625 (Dated Oct 18, 2024; Effective Nov 20, 2024): NIFTY lot size revised from 25 to 75.
# 3. Circular No. NSE/FAOP/70616 (Dated Oct 3, 2025; Effective EOD Oct 28, 2025):
#    - NIFTY lot size revised from 75 to 65.
#    - TRANSITIONAL TREATMENT (Contract-Series Specific):
#      * Last old-lot (75) weekly expiry: 23-Dec-2025.
#      * Last old-lot (75) monthly expiry: 30-Dec-2025.
#      * Transition of existing quarterly & half-yearly contracts: 30-Dec-2025 EOD.
#      * First revised-lot (65) weekly expiry: 06-Jan-2026.
#      * First revised-lot (65) monthly expiry: 27-Jan-2026.
NIFTY_SPEC_SCHEDULE = [
    {
        "effective_from": date(2020, 1, 1),
        "effective_to": date(2024, 4, 25),
        "lot_size": 50,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/61328 (Prior standard lot)",
    },
    {
        "effective_from": date(2024, 4, 26),
        "effective_to": date(2024, 11, 19),
        "lot_size": 25,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/61328 (Revision 50 -> 25)",
    },
    {
        "effective_from": date(2024, 11, 20),
        "effective_to": date(2025, 12, 30),
        "lot_size": 75,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/64625 & NSE/FAOP/70616 Transition (Last weekly: 23-Dec-2025; Last monthly: 30-Dec-2025)",
    },
    {
        "effective_from": date(2025, 12, 31),
        "effective_to": date(2099, 12, 31),
        "lot_size": 65,
        "strike_step": 50.0,
        "circular": "NSE/FAOP/70616 (Quarterly/Half-yearly transitioned at 30-Dec-2025 EOD; First weekly: 06-Jan-2026; First monthly: 27-Jan-2026)",
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
    def get_contract_lot_size(cls, symbol: str = "NIFTY", expiry: Optional[str] = None) -> int:
        """Determines authoritative lot size from contract expiry series."""
        effective_date = None
        if expiry and expiry.strip():
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y", "%d%b%Y"):
                try:
                    effective_date = datetime.strptime(expiry.strip().upper(), fmt).date()
                    break
                except ValueError:
                    pass
        return cls.get_authoritative_lot_size(symbol, effective_date)

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

        # 3. Determine Authoritative Lot Size from Contract Expiry Series
        effective_date = as_of_date
        if expiry and expiry.strip():
            for fmt in ("%d-%b-%Y", "%Y-%m-%d", "%d-%B-%Y", "%d%b%Y"):
                try:
                    effective_date = datetime.strptime(expiry.strip().upper(), fmt).date()
                    break
                except ValueError:
                    pass
        if effective_date is None:
            effective_date = datetime.now(IST).date()

        expected_lot_size = cls.get_authoritative_lot_size("NIFTY", effective_date)

        # 4. If provider supplied a lot size, verify it matches exchange specification
        if provided_lot_size is not None and provided_lot_size > 0:
            if provided_lot_size != expected_lot_size:
                return (
                    False,
                    f"Provider lot size ({provided_lot_size}) does not match authoritative NSE lot size ({expected_lot_size}) for contract series {expiry} (effective date {effective_date}).",
                    0,
                )

        return True, "Contract validated against authoritative NSE specification.", expected_lot_size
