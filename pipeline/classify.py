"""Page-level document classification.

Classification is done per page (or per sheet) from content, because one
attachment often holds several document types: the "MVR REPORT" PDF in the
sample also contains two photographed driver licenses and a stale MVR.

Stage 1 - deterministic content signatures (fast, free, explainable).
Stage 2 - if the margin between the top two types is weak, the page goes to the
          LLM (text, plus the page image when the text came from OCR).
The file name is only a weak prior; disagreement between the file name and the
content is reported, never trusted.
"""
from __future__ import annotations

import math
import re

DOC_TYPES = {
    "application": "Commercial auto application",
    "quote_form": "Agency quick-quote form",
    "driver_schedule": "Driver schedule",
    "vehicle_schedule": "Vehicle / fleet schedule",
    "mvr": "Motor vehicle record (MVR)",
    "drivers_license": "Driver license image",
    "loss_run": "Carrier loss run",
    "no_loss_letter": "No-loss / no-paid-loss letter",
    "ifta_return": "IFTA quarterly fuel tax return",
    "reference": "Reference / lookup data (no business content)",
    "unknown": "Unclassified",
}

SIGNATURES: dict[str, list[tuple[str, float]]] = {
    "application": [(r"insurance application", 3), (r"date of application", 3), (r"effective date requested", 2),
                    (r"fein|tax id", 1.5), (r"fraud statement", 1.5), (r"operational questions", 2),
                    (r"signature of insured", 1.5), (r"business name", 1)],
    "quote_form": [(r"quick quote form", 4), (r"operation type", 2), (r"radius info", 2),
                   (r"commodities transported", 2), (r"prior policy dates", 2), (r"agency name", 0.5)],
    "driver_schedule": [(r"\bD\.?O\.?B\b|date of birth", 1.5), (r"license\s*#|licen[cs]e no|cdl number|driver license", 1.5),
                        (r"d\.o\.h|hire date|date hired", 1.5), (r"exp\.? in yrs|years of experience", 1.5),
                        (r"first name.*last name|driver #", 1.5)],
    "vehicle_schedule": [(r"\bvin\b", 2), (r"\bmake\b", 1), (r"\byear\b", 0.5), (r"cost new|stated value|tiv", 1.5),
                         (r"vehicle #|unit #", 1.5), (r"\b[0-9A-HJ-NPR-Z]{16,17}\b", 0.6)],
    "mvr": [(r"driver record", 3), (r"violations/convictions", 3), (r"suspensions/revocations", 2),
            (r"license and permit information", 2), (r"end of report for", 2), (r"order date", 1)],
    "drivers_license": [(r"driver\s*licen[cs]e|driverlicense|oriveruicense", 2), (r"\bclass\s*[abc]\b", 1.5),
                        (r"\brstr\b|\brestr\b", 2), (r"\bend\s+none\b", 1.5), (r"\bhgt\b|eyes|hair", 1.5),
                        (r"\bsex\s*[mf]\b|\bsexm\b", 1), (r"\biss\b|\bexp\b", 0.5)],
    "loss_run": [(r"run date", 2.5), (r"total incurred", 2.5), (r"policy period", 1.5), (r"losses paid", 2),
                 (r"claim number|claims?:", 1), (r"total reserved", 2)],
    "no_loss_letter": [(r"to whom it may concern", 2.5), (r"no (paid )?losses", 3), (r"\bRE:\s", 1),
                       (r"have not been any claims", 1)],
    "ifta_return": [(r"\bIFTA\b", 3), (r"jurisdiction", 1.5), (r"taxable (miles|gal)", 2.5), (r"tax paid gal", 2),
                    (r"\bMPG\b", 1), (r"filing period|period begin", 1.5), (r"diesel", 1)],
    "reference": [(r"^\[sheet: lookups?\]", 4)],
}

FILENAME_HINTS = [
    (r"\bmvr\b", "mvr"), (r"driver", "driver_schedule"), (r"vehicle|fleet|equip", "vehicle_schedule"),
    (r"loss|\bal\b|\bmtc\b|\bapd\b", "loss_run"), (r"ifta|\bq[1-4]\b", "ifta_return"),
    (r"quote", "quote_form"), (r"\bapp\b|application", "application"), (r"\bdl\b|licen", "drivers_license"),
]


def filename_hint(name: str) -> str | None:
    n = re.sub(r"[_\-.]", " ", name.lower())
    for pat, t in FILENAME_HINTS:
        if re.search(pat, n):
            return t
    return None


def score_page(text: str) -> dict[str, float]:
    t = text or ""
    scores = {}
    for dtype, sigs in SIGNATURES.items():
        s = 0.0
        for pat, w in sigs:
            hits = len(re.findall(pat, t, flags=re.I | re.M))
            if hits:
                s += w * min(1 + math.log(hits), 2.0)
        scores[dtype] = s
    # loss runs mention "No Coverage"/"claims" but a letter is not a loss run
    if scores["no_loss_letter"] >= 4 and scores["loss_run"] < 4:
        scores["loss_run"] *= 0.3
    return scores


def classify_page(text: str, file_name: str) -> tuple[str, float, str, dict]:
    scores = score_page(text)
    hint = filename_hint(file_name)
    if hint and hint in scores:
        scores[hint] += 0.75  # weak prior only
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    (top, s1), (second, s2) = ranked[0], ranked[1]
    if s1 < 2.0:
        return "unknown", 0.2, "no strong content signature", scores
    # confidence from absolute evidence and margin over the runner-up
    margin = (s1 - s2) / (s1 + 1e-6)
    conf = round(min(0.99, 0.45 + 0.35 * min(s1 / 8, 1) + 0.25 * margin), 2)
    reason = f"content signature '{top}' scored {s1:.1f} vs '{second}' {s2:.1f}"
    return top, conf, reason, scores


def classify_document(doc, ai=None, threshold: float = 0.70):
    """Classify every page; low-confidence pages are escalated to the LLM."""
    hint = filename_hint(doc.file)
    prev = None
    for p in doc.pages:
        dtype, conf, reason, _ = classify_page(p.text, doc.file)
        engine = "rules"
        # continuation pages (e.g. the trailing 'END OF REPORT' page of an MVR)
        if prev and dtype in ("unknown",) and re.search(r"end of report|filing period|account number", p.text, re.I):
            dtype, conf, reason = prev.doc_type, max(0.75, (prev.type_conf or 0.8) - 0.1), f"continuation of p.{prev.n}"
        if conf < threshold and ai is not None and ai.available:
            res = ai.classify(p, doc.file, list(DOC_TYPES))
            if res:
                dtype, conf, reason, engine = res["doc_type"], res["confidence"], res.get("rationale", ""), ai.name
        p.doc_type, p.type_conf, p.type_reason, p.type_engine = dtype, conf, reason, engine
        prev = p
    types = sorted({p.doc_type for p in doc.pages if p.doc_type not in ("reference",)})
    mismatch = bool(hint and types and hint not in types)
    return {"filename_hint": hint, "content_types": types, "filename_mismatch": mismatch,
            "multi_type": len(types) > 1}
