"""Industry reference knowledge used by validators and the offline analyst.

Everything here is deterministic and explainable: VIN decoding, state lists,
MVR code meanings, etc. The LLM analyst gets the same facts in its prompt so
both engines reason from identical ground truth.
"""
from __future__ import annotations

import re
from datetime import date

# --- VIN -------------------------------------------------------------------
_VIN_VALUES = {**{str(i): i for i in range(10)},
               "A": 1, "B": 2, "C": 3, "D": 4, "E": 5, "F": 6, "G": 7, "H": 8,
               "J": 1, "K": 2, "L": 3, "M": 4, "N": 5, "P": 7, "R": 9,
               "S": 2, "T": 3, "U": 4, "V": 5, "W": 6, "X": 7, "Y": 8, "Z": 9}
_VIN_WEIGHTS = [8, 7, 6, 5, 4, 3, 2, 10, 0, 9, 8, 7, 6, 5, 4, 3, 2]

# World Manufacturer Identifier -> make (heavy truck makers most common in trucking)
WMI_MAKES = {
    "1XP": "PETERBILT", "1NP": "PETERBILT", "2NP": "PETERBILT",
    "1XK": "KENWORTH", "1NK": "KENWORTH", "2XK": "KENWORTH", "3WK": "KENWORTH",
    "4V4": "VOLVO", "4V1": "VOLVO", "4V5": "VOLVO", "4VG": "VOLVO",
    "1FU": "FREIGHTLINER", "1FV": "FREIGHTLINER", "3AK": "FREIGHTLINER", "3AL": "FREIGHTLINER",
    "1HS": "INTERNATIONAL", "3HS": "INTERNATIONAL", "1HT": "INTERNATIONAL", "3HA": "INTERNATIONAL",
    "1M1": "MACK", "1M2": "MACK", "5KJ": "WESTERN STAR", "5KK": "WESTERN STAR",
    "1GR": "GREAT DANE (TRAILER)", "1UY": "UTILITY (TRAILER)", "1JJ": "WABASH (TRAILER)",
}
_YEAR_CODES = "ABCDEFGHJKLMNPRSTVWXY123456789"  # 2010..2039 cycle for 10th char


def vin_check(vin: str) -> dict:
    """Validate a VIN and decode what we can. Never raises."""
    v = re.sub(r"[^A-Z0-9]", "", (vin or "").upper())
    out = {"vin": v, "length": len(v), "valid_length": len(v) == 17,
           "illegal_chars": bool(set(v) & set("IOQ")), "check_digit_ok": None,
           "make": None, "model_year": None}
    if len(v) >= 3:
        out["make"] = WMI_MAKES.get(v[:3])
    if len(v) == 17 and not out["illegal_chars"]:
        s = sum(_VIN_VALUES.get(c, 0) * w for c, w in zip(v, _VIN_WEIGHTS)) % 11
        out["check_digit_ok"] = (("X" if s == 10 else str(s)) == v[8])
        yc = v[9]
        if yc in _YEAR_CODES:
            idx = _YEAR_CODES.index(yc)
            out["model_year"] = 2010 + idx  # trucks in scope are 2010+; good enough for triage
    return out


# --- Geography -------------------------------------------------------------
STATE_ABBR = {
    "ALABAMA": "AL", "ALASKA": "AK", "ARIZONA": "AZ", "ARKANSAS": "AR", "CALIFORNIA": "CA",
    "COLORADO": "CO", "CONNECTICUT": "CT", "DELAWARE": "DE", "FLORIDA": "FL", "GEORGIA": "GA",
    "IDAHO": "ID", "ILLINOIS": "IL", "INDIANA": "IN", "IOWA": "IA", "KANSAS": "KS",
    "KENTUCKY": "KY", "LOUISIANA": "LA", "MAINE": "ME", "MARYLAND": "MD", "MASSACHUSETTS": "MA",
    "MICHIGAN": "MI", "MINNESOTA": "MN", "MISSISSIPPI": "MS", "MISSOURI": "MO", "MONTANA": "MT",
    "NEBRASKA": "NE", "NEVADA": "NV", "NEW HAMPSHIRE": "NH", "NEW JERSEY": "NJ", "NEW MEXICO": "NM",
    "NEW YORK": "NY", "NORTH CAROLINA": "NC", "NORTH DAKOTA": "ND", "OHIO": "OH", "OKLAHOMA": "OK",
    "OREGON": "OR", "PENNSYLVANIA": "PA", "RHODE ISLAND": "RI", "SOUTH CAROLINA": "SC",
    "SOUTH DAKOTA": "SD", "TENNESSEE": "TN", "TEXAS": "TX", "UTAH": "UT", "VERMONT": "VT",
    "VIRGINIA": "VA", "WASHINGTON": "WA", "WEST VIRGINIA": "WV", "WISCONSIN": "WI", "WYOMING": "WY",
    "DISTRICT OF COLUMBIA": "DC",
}
US_MEXICO_BORDER_STATES = {"CA", "AZ", "NM", "TX"}


_CONFUSABLE = str.maketrans({"I": "L", "1": "L", "|": "L", "!": "L", "0": "O", "Q": "O", "5": "S", "8": "B", "H": "LL", "N": "N"})


def fuzzy_state(token: str) -> str | None:
    """Map an OCR'd jurisdiction name (e.g. 'lowa', 'Minois', 'IIhnors') to a postal code.

    Compares both the raw text and an OCR-confusable-folded form (I/l/1/|, 0/O ...),
    and only accepts a match that clearly beats the runner-up.
    """
    import difflib
    t = re.sub(r"[^A-Za-z0-9|! ]", "", token or "").strip().upper()
    if not t:
        return None
    if t in STATE_ABBR:
        return STATE_ABBR[t]
    tf = t.translate(_CONFUSABLE)
    scored = []
    for name, ab in STATE_ABBR.items():
        r1 = difflib.SequenceMatcher(None, t, name).ratio()
        r2 = difflib.SequenceMatcher(None, tf, name.translate(_CONFUSABLE)).ratio()
        scored.append((max(r1, r2), ab))
    scored.sort(reverse=True)
    (best, ab), (second, _) = scored[0], scored[1]
    return ab if best >= 0.66 and best - second >= 0.08 else None


# --- Driver records --------------------------------------------------------
MVR_EVENT_MEANINGS = {
    "CDL DOWNGRADE": "CDL privileges were downgraded - most often because the driver's DOT "
                     "medical certificate lapsed or was not on file with the state. The driver "
                     "could not legally operate a CMV until it was cleared.",
    "LIMITED-TERM": "Limited-term (non-domiciled) CDL: issued to drivers with temporary lawful "
                    "status; validity is tied to immigration documents, and federal rules for "
                    "non-domiciled CDLs tightened in 2025. Verify current status.",
    "SURCHARGE": "State insurance surcharge assessed - indicates a point-bearing conviction.",
    "POINT CREDIT": "Annual safe-driving point credit (points reduced on the state record; "
                    "underwriting still counts the original violation).",
}

MAJOR_VIOLATION_PATTERNS = [
    (r"\bDUI\b|\bDWI\b|UNDER THE INFLUENCE|IMPAIRED", "DUI/DWI"),
    (r"HOMICIDE|VEHICULAR ASSAULT|MANSLAUGHTER", "Vehicular homicide/assault"),
    (r"RECKLESS|NEGLIGENT DRIV|CARELESS", "Reckless/negligent driving"),
    (r"(SPEED\w*).*?(3[0-9]|[4-9][0-9])\s*\+?\s*MPH OVER|OVER LIMIT\s*(3[0-9]|[4-9][0-9])", "Speeding 30+ over"),
    (r"HIT.AND.RUN|LEAVING THE SCENE|LEAVE SCENE", "Hit-and-run"),
    (r"FLEE|ELUD", "Fleeing/eluding"),
    (r"FELONY", "Felony involving a motor vehicle"),
    (r"SCHOOL BUS", "Passing a stopped school bus"),
]


def age_on(dob: date | None, on: date) -> int | None:
    if not dob:
        return None
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))
