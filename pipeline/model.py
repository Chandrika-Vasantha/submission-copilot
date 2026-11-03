"""Core data structures. Every extracted value carries its provenance."""
from __future__ import annotations

import re
from dataclasses import dataclass, field, asdict
from datetime import date, datetime
from typing import Any

# Base confidence by how the value was obtained, before validation adjustments.
METHOD_BASE = {
    "xlsx": 0.97,          # structured cell value
    "text-layer": 0.95,    # digital PDF text
    "ocr": 0.85,           # tesseract on scan/photo (scaled by OCR confidence)
    "llm-vision": 0.88,    # Claude reading the page image
    "llm-text": 0.90,      # Claude reading extracted text
    "derived": 0.90,       # computed from other facts
    "human": 1.00,         # underwriter-confirmed
}


@dataclass
class Source:
    file: str
    page: int | None = None      # 1-based
    method: str = "text-layer"
    snippet: str = ""

    def label(self) -> str:
        return f"{self.file}" + (f" p.{self.page}" if self.page else "")


@dataclass
class Fact:
    value: Any
    source: Source
    confidence: float = 0.9
    notes: list[str] = field(default_factory=list)

    def to_dict(self):
        d = asdict(self)
        d["value"] = jsonable(self.value)
        return d


def fact(value, file, page=None, method="text-layer", snippet="", conf=None, ocr_conf=None, notes=None):
    base = METHOD_BASE.get(method, 0.8) if conf is None else conf
    if ocr_conf is not None and method == "ocr":
        # tesseract mean word confidence (0-100) scales the base
        base = base * (0.6 + 0.4 * max(0.0, min(ocr_conf, 100)) / 100)
    return Fact(value, Source(file, page, method, (snippet or "")[:160]), round(base, 3), list(notes or []))


def jsonable(v):
    if isinstance(v, (date, datetime)):
        return v.isoformat()[:10]
    if isinstance(v, dict):
        return {k: jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple)):
        return [jsonable(x) for x in v]
    if isinstance(v, Fact):
        return v.to_dict()
    return v


_DATE_PATTERNS = ["%m/%d/%Y", "%m/%d/%y", "%m-%d-%Y", "%Y-%m-%d", "%B %d, %Y", "%b %d, %Y"]


def parse_date(s) -> date | None:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s.date()
    if isinstance(s, date):
        return s
    s = str(s).strip().rstrip(".")
    for p in _DATE_PATTERNS:
        try:
            return datetime.strptime(s, p).date()
        except ValueError:
            pass
    m = re.search(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
    if m:
        mm, dd, yy = m.groups()
        yy = int(yy) + (2000 if len(yy) == 2 and int(yy) < 50 else 1900 if len(yy) == 2 else 0)
        try:
            return date(yy, int(mm), int(dd))
        except ValueError:
            return None
    return None


def money(s) -> float | None:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    t = re.sub(r"[^0-9.\-]", "", str(s))
    try:
        return float(t) if t not in ("", "-", ".") else None
    except ValueError:
        return None


def norm_name(s: str) -> str:
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def norm_entity(s: str) -> str:
    """Normalise a business name for comparison but keep the legal-form suffix."""
    return norm_name(s).replace("INCORPORATED", "INC").replace("COMPANY", "CO")
