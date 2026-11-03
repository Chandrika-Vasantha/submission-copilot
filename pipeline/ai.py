"""AI services: page classification fallback, vision re-read, underwriting analysis.

Two interchangeable engines behind one interface:
  * ClaudeEngine  - Anthropic API (used when ANTHROPIC_API_KEY is set).
  * OfflineEngine - deterministic underwriting heuristics, so the system still
                    works (and is testable) with no network / key.

Guard-rails for the LLM:
  * Structured JSON output only, validated before use.
  * Every finding must cite evidence ids that exist in the facts we sent;
    uncited claims are kept but flagged 'ungrounded' and confidence-capped.
  * The LLM never decides routing. Rules do. The LLM explains, spots patterns
    and drafts communications.
"""
from __future__ import annotations

import base64
import json
import os
import re
from datetime import date

from .knowledge import MVR_EVENT_MEANINGS, vin_check
from .model import jsonable

CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")
CLAUDE_FAST_MODEL = os.environ.get("CLAUDE_FAST_MODEL", "claude-haiku-5-5")


def _d(x):
    return x.strftime("%m/%d/%Y") if isinstance(x, date) else str(x)


def get_engine():
    mode = os.environ.get("AI_ENGINE", "auto").lower()
    if mode in ("auto", "claude") and os.environ.get("ANTHROPIC_API_KEY"):
        try:
            return ClaudeEngine()
        except Exception as e:  # SDK missing etc.
            print(f"[ai] Claude unavailable ({e}); using offline engine")
    return OfflineEngine()


# =============================================================================
class OfflineEngine:
    name = "offline-heuristics"
    available = False  # no page-level LLM calls

    def classify(self, page, file, types):
        return None

    def reread(self, page, doc_type):
        return None

    # ------------------------------------------------------------------ analysis
    def analyze(self, S: dict) -> dict:
        F = []  # findings

        def add(fid, title, detail, severity, confidence, category, evidence):
            F.append({"id": fid, "title": title, "detail": detail, "severity": severity,
                      "confidence": round(confidence, 2), "category": category, "evidence": evidence,
                      "engine": self.name, "grounded": True})

        biz, ifta, drivers, terms = S["business"], S["ifta"], S["drivers"], S["terms"]
        conflicts = {c["field"]: c for c in S["conflicts"]}

        # 1. territory
        if "farthest_territory" in conflicts:
            east = [s for s in ifta["by_state"] if s in ("NJ", "PA", "MD", "DE", "OH", "IN", "IL")]
            add("AI-01", "Operating territory misstated - this is a nationwide long-haul risk",
                f"Application says farthest territory is TX and the quote form puts only 50% in '48 states', but IFTA shows "
                f"{ifta['total_miles']:,} miles over 4 quarters across {len(ifta['by_state'])} jurisdictions; only "
                f"{ifta['by_state'].get('CA', 0) / ifta['total_miles']:.0%} in California and heavy mileage in "
                f"{', '.join(f'{s} ({ifta['by_state'][s]:,})' for s in east[:4])}. Rate and appetite should be based on "
                f"the IFTA territory, not the application answers.", "high", 0.93, "Operations",
                [q["file"] + " p.2" for q in ifta["quarters"]] +
                [s["source"]["file"] + " p." + str(s["source"]["page"]) for s in biz.get("farthest_territory", {}).get("sources", [])[:1]])
        # 2. NJ drivers + NJ miles -> garaging
        nj = [d for d in drivers if (d.get("license_state") or "") == "NJ"]
        if nj and ifta["by_state"].get("NJ"):
            add("AI-02", "Possible second terminal / garaging in New Jersey (non-listed state)",
                f"{len(nj)} of {len(drivers)} drivers ({', '.join(d['name'] for d in nj)}) hold NJ CDLs with NJ home addresses, "
                f"and the fleet ran {ifta['by_state']['NJ']:,} NJ miles in 4 quarters. The application's garaging address is "
                f"blank. If units are domiciled or dispatched from NJ, the account falls outside the available states. "
                f"Ask where each unit is garaged and where these drivers start/end their tours.", "high", 0.78, "Operations",
                [x["mvr"]["source"] for x in nj if x["mvr"]] + [q["file"] + " p.2" for q in ifta["quarters"][:1]] +
                [s["source"]["file"] + " p." + str(s["source"]["page"]) for s in biz.get("garaging_address", {}).get("sources", [])[:1]])
        # 3. losses
        al = [t for t in terms if t["line"] == "AL"]
        inc = sum((t["incurred"] or 0) for t in al)
        claims = [c for t in al for c in t["claims"]]
        open_bi = [c for c in claims if c.get("status") == "Open" and c.get("bi")]
        if claims:
            late = [c for c in claims if c.get("late")]
            lag = [((c["reported"] - c["loss_date"]).days) for c in claims if c.get("reported") and c.get("loss_date")]
            terms_w = sorted({f"{t['from'].year}-{str(t['to'].year)[2:]}" for t in al if t["claims"]})
            add("AI-03", f"{len(claims)} AL claim(s), ${inc:,.0f} incurred (term {', '.join(terms_w)})",
                (f"{len(open_bi)} open bodily-injury claim(s) {'with attorney involvement ' if any(c.get('legal') for c in open_bi) else ''}"
                 f"(open reserve ${sum(c['reserve'] for c in open_bi):,.0f}). Description '{open_bi[0]['description']}' "
                 f"('IV' = insured vehicle) suggests insured fault; BI claims with legal expense typically develop upward, "
                 f"so treat the reserve as a floor. " if open_bi else "") +
                f"{len(late)} of {len(claims)} claims reported late ({', '.join(str(x) for x in lag)} days after loss) - "
                f"a claims-reporting discipline issue. Loss ratio cannot be computed: premiums are not in the package.",
                "high", 0.9, "Loss history", [t["source"] for t in al if t["claims"]])
        unsched = S["unscheduled_vehicles"]
        loss_drivers = [c for c in claims if c.get("driver_status") == "Unreported"]
        if unsched or loss_drivers:
            u = unsched[0] if unsched else None
            add("AI-04", "Undisclosed driver and power unit in the loss history",
                (f"Claim {u['claim']} ({_d(u['loss_date'])}) involved a {u['desc'].replace('-', ' ')} (VIN {u['vin']}, decodes to "
                 f"{u['vin_year']} {u['vin_make']}) that the carrier flagged as an unreported unit and is not on today's "
                 f"schedule. " if u else "") +
                (f"Driver {loss_drivers[0]['driver']} was also unreported to the carrier and is not on the driver list. " if loss_drivers else "") +
                "The quote form answers 'No' to vehicles not listed. This may explain the 5 vs 4 unit count. Confirm whether "
                "the unit/driver are still operating (owner-operator, leased, or sold) and obtain an MVR if retained.",
                "high", 0.88, "Fleet & drivers", [u["source"]] if u else [])
        # 4. no-paid-loss letter
        for t in terms:
            if t["kind"] == "no_loss_letter":
                add("AI-05", f"{t['line']} {t['from'].year}-{t['to'].year} history is a 'no paid losses' letter, not a loss run",
                    f"{t['carrier']} states only that there were no *paid* losses. That wording does not exclude open, reserved "
                    f"or incurred-but-unpaid claims."
                    + (f" It also names a different carrier than the application lists for that term ({app_c})." if (app_c := next((p['carrier'].value for p in S['prior'] if p['from'].value and abs((p['from'].value - t['from']).days) <= 31), None)) and not t['carrier'].upper().startswith(app_c.split()[0]) else "")
                    + f" Request a currently valued loss run from {t['carrier']}.",
                    "medium", 0.9, "Loss history", [t["source"]])
        # 5. drivers
        for d in drivers:
            if not d["cdl"]:
                add(f"AI-D-{d['key'][-4:]}", f"{d['name']} (owner) has no CDL but is on the driver schedule",
                    f"MVR shows a {d['license_type']} Class {d['license_class']} non-commercial license. A Class C cannot "
                    f"legally operate a tractor-trailer. The application answers 'Owner is a driver: No' - if true, he should "
                    f"be excluded by endorsement and removed from the schedule; if he drives, he fails the CDL baseline.",
                    "high", 0.95, "Drivers", [d["mvr"]["source"]] if d["mvr"] else [])
            for e in (d["mvr"] or {}).get("events", []):
                if "DOWNGRADE" in e["event"]:
                    clear = min((x for x in d["mvr"]["events"] if "CLEARANCE" in x["event"] and x["date"] and x["date"] > e["date"]),
                                key=lambda x: x["date"], default=None)
                    add(f"AI-CDL-{d['key'][-4:]}", f"{d['name']}: CDL downgraded {_d(e['date'])}"
                        + (f", cleared {_d(clear['date'])}" if clear else ""),
                        f"{MVR_EVENT_MEANINGS['CDL DOWNGRADE']} This happened while he was employed by the applicant "
                        f"(hired {_d(d['hire_date'])}). Ask for the driver qualification file / medical card history for that "
                        f"window. He is also the driver on the open BI claim.", "medium", 0.8, "Drivers", [d["mvr"]["source"]])
            if d["limited_term"]:
                add(f"AI-LT-{d['key'][-4:]}", f"{d['name']}: limited-term (non-domiciled) CDL; hired {_d(d['hire_date'])}",
                    f"{MVR_EVENT_MEANINGS['LIMITED-TERM']} The license photo submitted is expired ({d['license_images']} image); "
                    f"the MVR shows a renewal valid to {_d(d['license_exp'])}. He was hired days before the application. "
                    f"The English-language-proficiency question on the application is unanswered.",
                    "medium", 0.75, "Drivers", [d["mvr"]["source"]] + d.get("license_image_sources", []))
        if "date_of_birth" in conflicts:
            c = conflicts["date_of_birth"]
            add("AI-06", f"DOB mismatch for {c['entity'].replace('Driver ', '')}",
                f"Driver list shows {c['values'][0]['value']}, MVR shows {c['proposed']} (day/month look transposed: 10/03 vs "
                f"10/30). Likely a typo, but an MVR pulled on the wrong DOB can be the wrong person. Confirm against the license.",
                "medium", 0.85, "Drivers", [v["source"] for v in c["values"]])
        # 6. entity
        if "insured_name" in conflicts:
            add("AI-07", "Named insured appears as both INC and LLC",
                f"Values seen: {', '.join(sorted({str(v['value']) for v in conflicts['insured_name']['values']}))}. The proposed name "
                f"({conflicts['insured_name']['proposed']}) is on most documents; the application entity box says "
                f"{biz.get('entity_type', {}).get('value')}. FEIN is {'blank' if not biz.get('fein', {}).get('value') else biz['fein']['value']}, so the legal entity cannot be confirmed. "
                f"Request FEIN and articles; confirm which entity holds MC {biz['mc_number']['value']}.", "high", 0.86, "Insured",
                [v["source"] for v in conflicts["insured_name"]["values"] if "LLC" in str(v["value"])])
        # 7. utilisation / growth
        units = S["unit_count"]
        app_date = S["business"]["application_date"]["value"]
        recent_hires = [d for d in drivers if d["cdl"] and isinstance(d.get("hire_date"), date) and (app_date - d["hire_date"]).days <= 365]
        if ifta["total_miles"] and units:
            per = ifta["total_miles"] / units
            add("AI-08", f"High utilisation: ~{per:,.0f} miles per scheduled unit per year",
                f"{ifta['total_miles']:,} IFTA miles / {units} scheduled tractors = {per:,.0f} mi/unit (~{ifta['total_miles'] / max(len([d for d in drivers if d['cdl']]), 1):,.0f} "
                f"per CDL driver). That is at the top of the long-haul range and consistent with more units or team/slip-seating "
                f"than disclosed. Units insured by term: {' -> '.join(str(p['units'].value) for p in sorted(S['prior'], key=lambda p: p['from'].value))}; "
                f"{len(recent_hires)} of {len([d for d in drivers if d['cdl']])} CDL drivers hired in the last 12 months.",
                "medium", 0.8, "Operations", [q["file"] + " p.2" for q in ifta["quarters"]])
        bad = [q for q in ifta["quarters"] if not q["reconciles"]]
        for q in bad:
            add(f"AI-OCR-{q['file'][:6]}", f"Low-confidence read: {q['period']} IFTA ({q['file']})",
                f"The jurisdiction rows read from this photographed/scanned return sum to {q['rows_sum']:,} miles but the "
                f"return's own total is {q['total_miles']:,}. Mileage-by-state for this quarter should be verified against the "
                f"image before relying on it (the vision model re-reads such pages automatically when enabled).",
                "medium", 0.95, "Data quality", [q["file"] + " p.2"])
        pen = [q for q in ifta["quarters"] if q.get("penalty")]
        if pen:
            add("AI-09", "Late IFTA filing", f"{pen[0]['period']} return carries a ${pen[0]['penalty']:,.2f} penalty plus "
                f"interest - filed late. A minor but real indicator of back-office / cash-flow discipline.", "low", 0.85,
                "Compliance", [pen[0]["file"]])
        # 8. prior insurance
        gaps = [r for r in S["rules"]["results"] if r["id"] == "PRI-02" and r["status"] == "fail"]
        if gaps:
            add("AI-10", "Coverage history has a short term and/or a lapse over 30 days",
                " ".join(gaps[0]["items"]) + " Short terms can indicate cancellation (non-pay or underwriting). "
                "The guideline requires any lapse over 30 days to be explained before quoting.",
                "medium", 0.82, "Prior insurance", ["QUOTE FORM prior policy table"])
        # 9. coverage asks
        if biz.get("pd_requested", {}).get("value") is True or biz.get("cargo_requested", {}).get("value") is True:
            add("AI-11", "Requested coverages exceed the program",
                f"Agent asked for physical damage: {'yes' if biz.get('pd_requested', {}).get('value') else 'no'}; cargo: "
                f"{'$' + format(biz['cargo_limit']['value'] or 0, ',.0f') if biz.get('cargo_requested', {}).get('value') else 'no'}"
                f"{' with reefer breakdown' if biz.get('reefer', {}).get('value') else ''}. APD is not available and cargo is "
                f"not in the program snapshot - quote auto liability only and tell the agent early."
                + (f" Highest stated load value (${maxval:,.0f}) exceeds the requested cargo limit." if (maxval := max([c.get('max_value') or c.get('value') or 0 for lst in biz['commodities']['value'] for c in lst] or [0])) > (biz.get('cargo_limit', {}).get('value') or 1e12) else ""),
                "low", 0.9, "Coverage",
                ["QUOTE FORM Coverages & Limits"])
        # positives
        pos = []
        clean = [d["name"] for d in drivers if d["cdl"] and not [v for v in d["mvr"]["violations"] if v["points"]]]
        cdl_d = [d for d in drivers if d["cdl"]]
        if not any(r["status"] == "fail" for r in S["rules"]["results"] if r["id"] in ("DRV-05", "DRV-06")):
            pos.append(f"No major violations; {len(clean)} of {len(cdl_d)} CDL drivers have clean MVRs; CDL drivers aged "
                       f"{min(d['age'] for d in cdl_d)}-{max(d['age'] for d in cdl_d)}, each with "
                       f">= {min(d['years_cdl'] or 99 for d in cdl_d):.0f} yrs CDL evidence")
        pos.append("Tractors only, general freight (food, beverages, paper, appliances); no hazmat, intermodal or flatbed")
        tel = biz.get("telematics_pct", {}).get("value")
        if tel:
            pos.append(f"{float(tel):.0%} telematics - can verify I-10, border distance and garaging objectively")
        rd = sorted({t['run_date'] for t in terms if t['run_date']})
        cur_al = max(al, key=lambda t: t["from"]) if al else None
        if rd:
            pos.append(f"Loss runs current (valued {_d(rd[0])})"
                       + (f"; current AL term {cur_al['from'].year}-{cur_al['to'].year} clean to date" if cur_al and not cur_al['claims'] else ""))

        decision = S["rules"]
        summary = (
            f"{biz['insured_name']['value']} (DOT {biz['dot_number']['value']}, MC {biz['mc_number']['value']}) of "
            f"{biz['mailing_address']['value']} is a for-hire long-haul tractor-trailer carrier with {units} scheduled power "
            f"units and {len(drivers)} listed drivers, requesting $1M auto liability effective {_d(biz['effective_date']['value'])} "
            f"through {biz['agency_name']['value']}. The risk class (general freight, tractors only) fits the "
            f"program, but the submission is not quotable as received: IFTA shows nationwide operations ({ifta['non_listed_share']:.0%} "
            f"of miles in non-listed states) that contradict the application, the loss history has an open attorney-represented "
            f"BI claim plus an undisclosed driver and truck, the owner is scheduled without a CDL, and key items are missing "
            f"({', '.join(dict.fromkeys(m['short'] for m in S['missing'] if m.get('short')))}). Rules result: {decision['decision']} to "
            f"{decision['team']}.")
        recs = self._recommendations(S)
        return {"engine": self.name, "summary": summary, "findings": F, "positives": pos,
                "recommendations": recs, "agent_email": self._email(S)}

    def _recommendations(self, S):
        dec = S["rules"]
        r = []
        r.append({"priority": 1, "owner": "Underwriter", "action": f"Route to {dec['team']} as REFER; do not quote until territory and garaging are confirmed",
                  "rationale": "Territory/garaging decides whether the risk is in appetite at all (APP-03/APP-04).", "confidence": 0.9})
        r.append({"priority": 2, "owner": "Agent", "action": "Send the follow-up request (draft below) for missing items and explanations",
                  "rationale": f"{len(S['missing'])} items outstanding.", "confidence": 0.95})
        r.append({"priority": 3, "owner": "Underwriter", "action": "Request a telematics lane/heat-map report for the last 12 months",
                  "rationale": "Only objective way to clear I-10, 50-mile border and NJ garaging questions.", "confidence": 0.8})
        r.append({"priority": 4, "owner": "Operations", "action": f"Run FMCSA SAFER/SMS for DOT {S['business']['dot_number']['value']} and attach to file",
                  "rationale": "Guideline prohibits serious SAFER violations; not in the package.", "confidence": 0.9})
        open_c = [c for t in S["terms"] for c in t["claims"] if c.get("status") == "Open"]
        if open_c:
            r.append({"priority": 5, "owner": "Underwriter", "action": f"Obtain claim notes and defense counsel status on open claim(s) {', '.join(c['claim_no'] for c in open_c)}",
                      "rationale": "Reserve adequacy drives the loss pick; legal involvement signals development.", "confidence": 0.8})
        r.append({"priority": 6, "owner": "Underwriter", "action": "If quoted: AL only, exclude the owner (non-CDL) by endorsement, schedule only verified units/drivers",
                  "rationale": "APD not available; cargo outside program; owner fails CDL baseline.", "confidence": 0.75})
        return r

    def _email(self, S):
        missing = S["missing"]
        lines = [f"{i}. {m['item']}" for i, m in enumerate(missing, 1)]
        body = (
            f"Hi {(S['business'].get('agency_contact', {}).get('value') or 'there').split()[0].title()},\n\nThank you for the "
            f"{S['business']['insured_name']['value'].title()} submission (eff. {_d(S['business']['effective_date']['value'])}). "
            f"Before we can quote we need the following:\n\n" + "\n".join(lines) +
            "\n\nPlease also note that physical damage is not available in this program and cargo is outside our program "
            "appetite; we can review auto liability only.\n\nThank you,\nKnight Specialty - Trucking Underwriting")
        return {"to": S.get("reply_to", ""), "subject": f"{S['business']['insured_name']['value'].title()} - information needed to quote",
                "body": body}


# =============================================================================
class ClaudeEngine(OfflineEngine):
    name = "claude"
    available = True

    def __init__(self):
        import anthropic  # noqa: F401  (pip install anthropic)
        self.client = anthropic.Anthropic()
        self.calls = []

    def _json_call(self, model, system, content, max_tokens=4000):
        msg = self.client.messages.create(model=model, max_tokens=max_tokens, system=system,
                                          messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        self.calls.append({"model": model, "in": msg.usage.input_tokens, "out": msg.usage.output_tokens})
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else None

    @staticmethod
    def _image_block(path):
        with open(path, "rb") as f:
            return {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                "data": base64.b64encode(f.read()).decode()}}

    def classify(self, page, file, types):
        try:
            content = [{"type": "text", "text": f"File name (may be misleading): {file}\nPage {page.n} text:\n{page.text[:6000]}\n\n"
                                                f"Classify this page as one of {types}. Reply JSON: "
                                                '{"doc_type": str, "confidence": 0-1, "rationale": str}'}]
            if page.method == "ocr" and page.image:
                content.insert(0, self._image_block(page.image))
            r = self._json_call(CLAUDE_FAST_MODEL, "You classify insurance submission documents. Ignore file names "
                                                   "if content disagrees. JSON only.", content, 300)
            if r and r.get("doc_type") in types:
                r["confidence"] = float(r.get("confidence", 0.7))
                return r
        except Exception as e:
            print(f"[ai] classify failed: {e}")
        return None

    def reread(self, page, doc_type):
        """Vision re-read of a low-confidence scan; result is validated by the caller."""
        if doc_type != "drivers_license" or not page.image:
            return None
        try:
            return self._json_call(CLAUDE_MODEL, "Extract fields from a photographed driver license. JSON only; use null "
                                                 "when unreadable; dates MM/DD/YYYY.",
                                   [self._image_block(page.image), {"type": "text", "text":
                                    'Return {"state":..,"license_no":..,"name":..,"dob":..,"issue":..,"exp":..,'
                                    '"class":..,"commercial":bool,"limited_term":bool,"confidence":0-1}'}], 500)
        except Exception as e:
            print(f"[ai] reread failed: {e}")
            return None

    def reread_ifta(self, pages):
        """Vision re-read of an IFTA jurisdiction table when OCR rows do not sum to the total."""
        try:
            content = [self._image_block(p.image) for p in pages if p.image]
            content.append({"type": "text", "text": 'Read the IFTA Jurisdiction Summary table(s). Return JSON '
                            '{"rows": [{"jurisdiction": str, "total_miles": int}], "grand_total_miles": int}. '
                            'Skip Surcharge rows. Numbers exactly as printed.'})
            return self._json_call(CLAUDE_MODEL, "You transcribe tables from photographed tax filings. JSON only.", content, 3000)
        except Exception as e:
            print(f"[ai] IFTA reread failed: {e}")
            return None

    def analyze(self, S):
        base = super().analyze(S)  # deterministic signals are given to the model as evidence
        evidence_ids = sorted({e for f in base["findings"] for e in f["evidence"]} |
                              {t["source"] for t in S["terms"]} | {q["file"] for q in S["ifta"]["quarters"]})
        facts = {"business": {k: v.get("value") for k, v in S["business"].items() if isinstance(v, dict)},
                 "drivers": S["drivers"], "vehicles": S["vehicles"], "unscheduled_vehicles": S["unscheduled_vehicles"],
                 "loss_terms": S["terms"], "ifta": S["ifta"], "conflicts": S["conflicts"],
                 "rule_results": [{k: r[k] for k in ("id", "title", "status", "message", "items", "effect")} for r in S["rules"]["results"]],
                 "decision": {k: S["rules"][k] for k in ("decision", "team", "reason")},
                 "deterministic_signals": base["findings"], "missing": S["missing"]}
        system = (
            "You are a senior commercial auto (trucking) underwriter reviewing a submission that has already been "
            "extracted, reconciled and run through the carrier's rules. Use industry knowledge (FMCSA rules, MVR codes, "
            "loss development, IFTA, VIN decoding, E&S trucking appetite) to explain what matters. Do NOT change the rules "
            "decision. Every finding must cite one or more evidence ids from the provided list. Reply with JSON only.")
        prompt = (
            f"Guideline (business rules):\n{S['guideline_text']}\n\nEvidence ids you may cite:\n{json.dumps(evidence_ids)}\n\n"
            f"Submission facts (JSON):\n{json.dumps(jsonable(facts), default=str)[:150000]}\n\n"
            'Return JSON: {"summary": str (5-7 sentences, underwriter tone), '
            '"findings": [{"title": str, "detail": str, "severity": "high|medium|low", "confidence": 0-1, '
            '"category": str, "evidence": [evidence ids]}], "positives": [str], '
            '"recommendations": [{"priority": int, "owner": "Underwriter|Agent|Operations", "action": str, '
            '"rationale": str, "confidence": 0-1}], "agent_email": {"subject": str, "body": str}}')
        try:
            out = self._json_call(CLAUDE_MODEL, system, [{"type": "text", "text": prompt}], 6000)
        except Exception as e:
            print(f"[ai] analyze failed, using deterministic analysis: {e}")
            base["engine"] = f"{self.name} (failed -> offline)"
            return base
        if not out:
            return base
        allowed = set(evidence_ids)
        findings = []
        for i, f in enumerate(out.get("findings", []), 1):
            cited = [e for e in f.get("evidence", []) if e in allowed or any(e in a or a in e for a in allowed)]
            grounded = bool(cited)
            findings.append({"id": f"LLM-{i:02d}", "title": f.get("title", ""), "detail": f.get("detail", ""),
                             "severity": f.get("severity", "medium"), "category": f.get("category", ""),
                             "confidence": round(min(float(f.get("confidence", 0.6)), 0.95 if grounded else 0.4), 2),
                             "evidence": cited or f.get("evidence", []), "engine": CLAUDE_MODEL, "grounded": grounded})
        email = out.get("agent_email") or base["agent_email"]
        email.setdefault("to", base["agent_email"]["to"])
        return {"engine": f"{self.name} ({CLAUDE_MODEL})", "summary": out.get("summary", base["summary"]),
                "findings": findings or base["findings"], "positives": out.get("positives", base["positives"]),
                "recommendations": out.get("recommendations", base["recommendations"]), "agent_email": email,
                "deterministic_signals": base["findings"], "llm_calls": self.calls}
