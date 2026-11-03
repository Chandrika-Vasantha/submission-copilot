"""Entity resolution, duplicate detection and conflict detection.

Principles
* Never silently pick a value. Every field keeps all candidate Facts.
* When candidates disagree we raise a Conflict, propose a value using an
  explicit source-authority policy (below) and say why. Material conflicts stay
  'open' for a human; immaterial ones are 'auto-resolved' but still shown.
* Duplicates (same fact from several documents) raise confidence and are
  listed so the underwriter knows what was merged.
"""
from __future__ import annotations

import difflib
import itertools
import re
from datetime import date

from .knowledge import vin_check, age_on, US_MEXICO_BORDER_STATES
from .model import Fact, fact, norm_name, norm_entity, parse_date

# Which document wins when two disagree (higher = more authoritative), per field family.
SOURCE_AUTHORITY = {
    "identity": ["mvr", "drivers_license", "driver_schedule", "application", "quote_form", "loss_run"],
    "license": ["mvr", "drivers_license", "driver_schedule", "application", "quote_form"],
    "vehicle_make": ["vin_decode", "vehicle_schedule", "application", "loss_run"],
    "territory": ["ifta_return", "loss_run", "application", "quote_form"],
    "entity": ["fein_record", "application", "loss_run", "ifta_return", "quote_form"],
}


class Ctx:
    def __init__(self, bag, docs):
        self.bag = bag
        self.docs = docs
        self.conflicts = []
        self.duplicates = []
        self.gaps = []
        self._n = 0

    def conflict(self, field, entity, values, proposed, rationale, severity="medium", auto=False):
        self._n += 1
        grouped = {}
        for f in values:  # one row per distinct value, listing every document that states it
            k = str(_show(f.value))
            if k in grouped:
                grouped[k]["also_in"].append(f.source.label())
            else:
                grouped[k] = {"value": _show(f.value), "source": f.source.label(), "method": f.source.method,
                              "snippet": f.source.snippet, "confidence": f.confidence, "also_in": []}
        c = {"id": f"C{self._n:02d}", "field": field, "entity": entity, "severity": severity,
             "values": list(grouped.values()),
             "proposed": _show(proposed), "rationale": rationale,
             "status": "auto-resolved" if auto else "open", "resolution": None}
        self.conflicts.append(c)
        return c["id"]


def _show(v):
    if isinstance(v, date):
        return v.strftime("%m/%d/%Y")
    if isinstance(v, bool):
        return "Yes" if v else "No"
    return v


def _first(facts):
    return next((f for f in facts if f.value not in (None, "", "(blank)")), None)


def resolve_field(ctx, field, facts, entity="Insured", normaliser=lambda v: v, severity="medium",
                  prefer=None, rationale=None):
    """Return a resolved record for a scalar business field."""
    real = [f for f in facts if f.value not in (None, "", "(blank)")]
    blanks = [f for f in facts if f.value == "(blank)"]
    if not real:
        if blanks:
            ctx.gaps.append({"field": field, "entity": entity, "sources": [b.source.label() for b in blanks]})
        return {"value": None, "status": "missing", "confidence": 0.0, "sources": [f.to_dict() for f in facts]}
    groups = {}
    for f in real:
        groups.setdefault(str(normaliser(f.value)), []).append(f)
    if len(groups) == 1:
        conf = min(0.995, max(f.confidence for f in real) + 0.02 * (len(real) - 1))
        status = "agreed" if len(real) > 1 else "single"
        return {"value": real[0].value, "status": status, "confidence": round(conf, 3),
                "sources": [f.to_dict() for f in real]}
    chosen = prefer(real) if prefer else max(real, key=lambda f: f.confidence)
    cid = ctx.conflict(field, entity, real, chosen.value,
                       rationale or "values differ across documents; highest-confidence source proposed",
                       severity)
    return {"value": chosen.value, "status": "conflict", "confidence": round(chosen.confidence * 0.6, 3),
            "conflict": cid, "sources": [f.to_dict() for f in real]}


# --------------------------------------------------------------------------- business
def reconcile_business(ctx):
    b = ctx.bag.business
    out = {}
    simple = ["application_date", "effective_date", "owner_name", "mailing_address", "domicile_state", "dot_number",
              "mc_number", "fein", "garaging_address", "entity_type", "operation", "years_in_business",
              "years_prior_insurance", "al_limit", "um_limit", "pd_requested", "cargo_requested", "cargo_limit",
              "hazmat", "intermodal", "cross_border", "freight_broker", "doubles_oversize", "telematics_pct",
              "new_venture", "date_of_authority", "first_date_in_business", "elp_compliance", "signature_date",
              "unlisted_vehicles", "leases_to_others", "hires_subhaulers", "team_driving", "flatbed",
              "workers_comp", "fast_growth", "farthest_territory"]
    for k in simple:
        out[k] = resolve_field(ctx, k, b.get(k, []), normaliser=lambda v: str(v).strip().upper().rstrip("."))
    for k in ("application_date", "effective_date", "signature_date"):
        if out[k]["value"] is not None:
            out[k]["value"] = parse_date(out[k]["value"]) or out[k]["value"]
    out["agency_name"] = resolve_field(ctx, "agency_name", b.get("agency_name", []), "Agency",
                                       normaliser=lambda v: re.sub(r"\b(INSURANCE|SERVICES|AGENCY|INC)\b|\W", "", norm_name(v)) + "|" + norm_name(v),
                                       severity="low", rationale="Agency legal name differs between application and quote form; confirm the producing agency of record (licensing / commission).")
    out["agency_phone"] = resolve_field(ctx, "agency_phone", b.get("agency_phone", []), "Agency", severity="low",
                                        rationale="Two agency phone numbers; low impact, confirm contact.")
    out["agency_contact"] = resolve_field(ctx, "agency_contact", b.get("agency_contact", []), "Agency")

    # insured name across EVERY document that states it (app, quote, loss runs, IFTA)
    names = list(b.get("insured_name", []))
    for lr in ctx.bag.loss_runs:
        if lr["insured"].value:
            names.append(lr["insured"])
    for q in ctx.bag.ifta:
        if q["taxpayer_name"].value:
            names.append(q["taxpayer_name"])
    out["insured_name"] = resolve_field(
        ctx, "insured_name", names, normaliser=norm_entity, severity="high",
        prefer=lambda fs: max(fs, key=lambda f: sum(norm_entity(g.value) == norm_entity(f.value) for g in fs)),
        rationale="Legal entity differs (INC vs LLC). Most documents and the application's 'Corporation' box say INC; "
                  "an LLC on a state tax filing can mean a separate or predecessor entity. Confirm FEIN and legal entity "
                  "before quoting - the named insured must match the MC authority holder.")
    # reefer: explicit answers plus commodity inference
    reefer = list(b.get("reefer", []))
    comm = [c for f in b.get("commodities", []) for c in f.value]
    if any(re.search(r"FROZEN|REFRIG|PRODUCE|REEFER", c["commodity"], re.I) for c in comm):
        src = next(f for f in b["commodities"] if any(re.search(r"FROZEN|PRODUCE", c["commodity"], re.I) for c in f.value))
        reefer.append(fact(True, src.source.file, src.source.page, "derived",
                           "inferred: fresh produce / frozen food is temperature-controlled freight", conf=0.75))
    out["reefer"] = resolve_field(ctx, "reefer", reefer, "Operations", severity="medium",
                                  prefer=lambda fs: next(f for f in fs if f.value is True),
                                  rationale="Application says no reefer; quote form requests reefer breakdown and lists fresh "
                                            "produce/frozen food. Temperature-controlled freight is the more likely truth and "
                                            "drives cargo/reefer-breakdown exposure.")
    out["radius"] = {"value": [f.value for f in b.get("radius", [])], "sources": [f.to_dict() for f in b.get("radius", [])]}
    out["commodities"] = {"value": [f.value for f in b.get("commodities", [])],
                          "sources": [f.to_dict() for f in b.get("commodities", [])]}
    if len(b.get("radius", [])) > 1:
        r1, r2 = b["radius"][0], b["radius"][1]
        ctx.conflict("radius", "Operations", [r1, r2], "Use IFTA mileage (see territory analysis)",
                     "Radius splits use different buckets and disagree (application 40% >500 mi vs quote form 50% "
                     "48-state). IFTA filings are the objective record of where the trucks ran.", "medium")
    if len(b.get("commodities", [])) > 1:
        c1, c2 = b["commodities"][0], b["commodities"][1]
        maxv = max((c.get("max_value") or c.get("value") or 0) for c in comm)
        ctx.conflict("commodities", "Operations", [c1, c2], "Clarify commodity mix",
                     f"Commodity percentages differ between application and quote form. Highest stated load value "
                     f"${maxv:,.0f} vs cargo limit requested ${(out['cargo_limit']['value'] or 0):,.0f}.", "low", auto=True)
    return out


# --------------------------------------------------------------------------- drivers
def _lic_key(s):
    return re.sub(r"[^A-Z0-9]", "", str(s or "").upper()).replace("O", "0")


def _name_sim(a, b):
    a, b = norm_name(a), norm_name(b)
    ta, tb = set(a.split()), set(b.split())
    if not ta or not tb:
        return 0
    return max(difflib.SequenceMatcher(None, a, b).ratio(), len(ta & tb) / min(len(ta), len(tb)))


def reconcile_drivers(ctx, app_date: date):
    bag = ctx.bag
    drivers = []
    for rec in bag.drivers:
        key = _lic_key(rec["license_no"].value) or norm_name(rec["name"].value)
        d = next((x for x in drivers if x["key"] == key or
                  (_name_sim(x["name"], rec["name"].value) > 0.85 and x.get("_dob_any") == rec["dob"].value)), None)
        if d is None:
            d = {"key": key, "name": rec["name"].value, "facts": {}, "sources": [], "flags": [], "_dob_any": rec["dob"].value}
            drivers.append(d)
        d["sources"].append(f"{rec['name'].source.label()} ({rec.get('_src')})")
        for k, f in rec.items():
            if isinstance(f, Fact):
                d["facts"].setdefault(k, []).append(f)
    # attach MVRs (current + stale) by licence number, fall back to name
    for m in bag.mvrs:
        d = next((x for x in drivers if x["key"] == _lic_key(m["license_no"].value)), None) or \
            next((x for x in drivers if _name_sim(x["name"], m["name"].value) > 0.8), None)
        if d is None:
            d = {"key": _lic_key(m["license_no"].value), "name": m["name"].value, "facts": {}, "sources": [], "flags": []}
            drivers.append(d)
            d["flags"].append("MVR provided for a driver not on the schedule")
        d.setdefault("mvrs", []).append(m)
    for lic in bag.licenses:
        d = next((x for x in drivers if lic["license_no"].value and x["key"] == _lic_key(lic["license_no"].value)), None)
        if d is None and lic["dob"].value:
            d = next((x for x in drivers if any(f.value == lic["dob"].value for f in x["facts"].get("dob", []))
                      and any(_name_sim(x["name"], t) > 0.5 for t in lic["name_tokens"].value)), None)
        if d is None:
            bag.notes.append("License image could not be matched to a scheduled driver")
            continue
        d.setdefault("license_images", []).append(lic)

    out = []
    for d in drivers:
        mvrs = sorted(d.get("mvrs", []), key=lambda m: m["order_date"].value or date.min, reverse=True)
        cur = mvrs[0] if mvrs else None
        stale = mvrs[1:]
        for s in stale:
            ctx.duplicates.append({"entity": f"Driver {d['name']}", "kind": "superseded document",
                                   "detail": f"Older MVR ordered {_show(s['order_date'].value)} (marked 'OLD MVR') superseded by MVR "
                                             f"ordered {_show(cur['order_date'].value)}; only the current one is used.",
                                   "sources": [f"{s['order_date'].source.file} p.{s['pages'][0]}"]})
        if len(d["sources"]) > 1:
            ctx.duplicates.append({"entity": f"Driver {d['name']}", "kind": "same driver on several documents",
                                   "detail": f"Merged {len(d['sources'])} schedule rows into one driver record.",
                                   "sources": d["sources"]})
        imgs = d.get("license_images", [])
        if imgs and cur:
            ctx.duplicates.append({"entity": f"Driver {d['name']}", "kind": "license image + MVR",
                                   "detail": "Photographed license matched to MVR by license number/DOB.",
                                   "sources": [f"{i['license_no'].source.file} p.{i['page']}" for i in imgs]})
        # DOB: schedule vs MVR vs license image
        dob_facts = list(d["facts"].get("dob", []))
        if cur and cur["dob"].value:
            dob_facts.append(cur["dob"])
        dob_facts += [i["dob"] for i in imgs if i["dob"].value]
        dob = resolve_field(ctx, "date_of_birth", dob_facts, f"Driver {d['name']}", severity="medium",
                            prefer=lambda fs: next((f for f in fs if "MVR" in f.source.file.upper() and f.source.method == "text-layer"), fs[0]),
                            rationale="DOB on the driver schedule differs from the state MVR. The MVR is the state record of "
                                      "truth; a mismatch can also mean the MVR was pulled for the wrong person - confirm.")
        # license expiry: image vs MVR (the MVR is newer and state-sourced)
        lic_exp = cur["license_exp"].value if cur and "license_exp" in cur else None
        for i in imgs:
            if i["exp"].value and lic_exp and i["exp"].value != lic_exp:
                ctx.conflict("license_expiration", f"Driver {d['name']}", [i["exp"], cur["license_exp"]], lic_exp,
                             f"License photo shows expiration {_show(i['exp'].value)}; MVR ordered "
                             f"{_show(cur['order_date'].value)} shows a renewed license valid to {_show(lic_exp)}. "
                             f"The photo is an outdated copy - MVR value used.", "low", auto=True)
        cdl = bool(cur and cur.get("cdl") and cur["cdl"].value) or any(i["cdl"].value for i in imgs)
        limited = any(i["limited_term"].value for i in imgs)
        # earliest evidence of CDL privileges
        cdl_dates = []
        for i in imgs:
            if i["cdl"].value and i["issue"].value:
                cdl_dates.append((i["issue"].value, f"license photo ISS date (p.{i['page']})"))
        if cur:
            for e in (cur.get("events") and cur["events"].value) or []:
                if re.search(r"CDL (ISSUED|RENEWAL|CLEARANCE|TRANSFER)", e["event"]) and e["date"]:
                    cdl_dates.append((e["date"], f"MVR event '{e['event']}'"))
            if cur.get("cdl") and cur["cdl"].value and cur.get("license_issue") and cur["license_issue"].value:
                cdl_dates.append((cur["license_issue"].value, "MVR commercial issue date (may be a renewal)"))
        cdl_since = min(cdl_dates) if cdl_dates else None
        yrs_cdl = round((app_date - cdl_since[0]).days / 365.25, 1) if cdl_since and app_date else None
        self_exp = _first(d["facts"].get("experience", []))
        hire = _first(d["facts"].get("hire_date", []))
        rec = {
            "key": d["key"], "name": d["name"], "sources": d["sources"],
            "dob": dob, "age": age_on(dob["value"], app_date) if isinstance(dob["value"], date) else None,
            "license_no": _first(d["facts"].get("license_no", [])).value if _first(d["facts"].get("license_no", [])) else (cur["license_no"].value if cur else None),
            "license_state": (_first(d["facts"].get("license_state", [])) or Fact(None, None)).value,
            "cdl": cdl, "license_class": cur["license_class"].value if cur and "license_class" in cur else None,
            "license_type": cur["license_type"].value if cur and "license_type" in cur else None,
            "license_status": cur["license_status"].value if cur and "license_status" in cur else None,
            "license_exp": lic_exp, "limited_term": limited,
            "cdl_since": cdl_since[0] if cdl_since else None, "cdl_since_basis": cdl_since[1] if cdl_since else None,
            "years_cdl": yrs_cdl, "self_reported_experience": self_exp.value if self_exp else None,
            "hire_date": (hire.value.date() if hasattr(hire.value, "date") and callable(hire.value.date) else hire.value) if hire else None,
            "mvr": None, "stale_mvrs": len(stale), "license_images": len(imgs),
            "license_image_sources": [f"{i['license_no'].source.file} p.{i['page']}" for i in imgs], "flags": list(d["flags"]),
        }
        if cur:
            rec["mvr"] = {"order_date": cur["order_date"].value, "state": cur["state"].value,
                          "violations": cur["violations"].value, "events": cur["events"].value,
                          "source": f"{cur['order_date'].source.file} p.{cur['pages'][0]}"}
            for e in cur["events"].value:
                if "DOWNGRADE" in e["event"]:
                    rec["flags"].append(f"CDL downgrade notice {_show(e['date'])}")
        out.append(rec)

    # owner listed as a driver vs application answer
    oid = ctx.bag.business.get("owner_is_driver", [])
    owner = next((r for r in out if r["name"] and any(f.value and norm_name(f.value) == r["name"] for f in ctx.bag.business.get("owner_name", []))), None)
    if owner and oid and oid[0].value is False:
        sched = [f for f in ctx.bag.drivers if f["name"].value == owner["name"]]
        ctx.conflict("owner_is_driver", "Operations", [oid[0]] + [s["name"] for s in sched[:2]], "Owner appears on driver schedule",
                     "Application answers 'Is Owner a driver? No', yet the owner is listed as driver #1 on the driver "
                     "schedule and quote form (with a Class C, non-commercial license). Either exclude the owner by "
                     "endorsement or he does not meet the CDL baseline.", "high")
    return out


# --------------------------------------------------------------------------- vehicles
def reconcile_vehicles(ctx):
    vehicles = []
    for v in ctx.bag.vehicles:
        vin = v["vin"].value
        chk = vin_check(vin)
        rec = {"vin": vin, "year": v["year"].value, "make": v["make"].value, "model": v["model"].value,
               "cost_new": v["cost_new"].value, "stated_value": v["stated_value"].value,
               "vehicle_type": v["vehicle_type"].value or v["model"].value, "vin_check": chk,
               "source": v["vin"].source.label(), "flags": [], "loss_refs": []}
        if not chk["valid_length"]:
            rec["flags"].append(f"VIN has {chk['length']} characters (must be 17)")
        elif chk["check_digit_ok"] is False:
            rec["flags"].append("VIN check digit fails - likely typo")
        if chk["make"] and rec["make"] and chk["make"] not in str(rec["make"]).upper():
            rec["flags"].append(f"VIN decodes to {chk['make']}, schedule says {rec['make']}")
        if chk["model_year"] and rec["year"] and int(rec["year"]) != chk["model_year"]:
            rec["flags"].append(f"VIN model-year code {chk['model_year']} vs schedule {rec['year']}")
        if rec["stated_value"] in (None, ""):
            rec["flags"].append("Stated value missing (only cost new provided)")
        vehicles.append(rec)
    # vehicles referenced in loss runs
    unscheduled = []
    for lr in ctx.bag.loss_runs:
        for cf in lr["claims"]:
            c = cf.value
            if not c.get("vin"):
                continue
            match = next((v for v in vehicles if v["vin"] == c["vin"]), None)
            yr_make = c.get("vehicle", "")
            loss_make = yr_make.split("-")[-1] if "-" in yr_make else yr_make
            if match:
                match["loss_refs"].append(c["claim_no"])
                ctx.duplicates.append({"entity": f"Vehicle {c['vin']}", "kind": "vehicle in schedule and loss run",
                                       "detail": f"Claim {c['claim_no']} matched to scheduled unit by VIN.",
                                       "sources": [match["source"], cf.source.label()]})
                if loss_make and match["make"] and loss_make.upper() not in str(match["make"]).upper():
                    dec = vin_check(c["vin"])["make"]
                    fs = [fact(match["make"], match["source"].split(" p.")[0], None, "xlsx", "Vehicle schedule"),
                          fact(loss_make, cf.source.file, cf.source.page, "text-layer", f"Claim {c['claim_no']} Vehicle Info"),
                          fact(dec, "VIN decode (WMI)", None, "derived", f"WMI {c['vin'][:3]} = {dec}", conf=0.98)]
                    ctx.conflict("vehicle_make", f"Vehicle {c['vin']}", fs, dec,
                                 f"Loss run lists the unit as {loss_make}; schedule says {match['make']}. The VIN's "
                                 f"manufacturer code ({c['vin'][:3]}) decodes to {dec}, so the loss run description is "
                                 f"wrong - same truck, carrier data-entry error.", "low", auto=True)
            else:
                chk = vin_check(c["vin"])
                unscheduled.append({"vin": c["vin"], "desc": yr_make, "claim": c["claim_no"], "loss_date": c["loss_date"],
                                    "vin_make": chk["make"], "vin_year": chk["model_year"], "source": cf.source.label(),
                                    "carrier_flag": c.get("unreported_unit")})
    return vehicles, unscheduled


def reconcile_unit_count(ctx, vehicles, unscheduled, prior_terms):
    scheduled = len(vehicles)
    facts_ = [fact(scheduled, vehicles[0]["source"].split(" p.")[0] if vehicles else "vehicle schedule", None, "derived",
                   f"{scheduled} power units on vehicle schedule")]
    cur = max(prior_terms, key=lambda p: p["from"].value) if prior_terms else None
    if cur:
        facts_.append(cur["units"])
    lim = ctx.bag.business.get("quote_form_unit_limit", [])
    claimed = {f.value for f in facts_}
    if len(claimed) > 1:
        extra = f" A 2021 Peterbilt (VIN {unscheduled[0]['vin']}) appears in a {unscheduled[0]['loss_date'].year} claim but is not scheduled." if unscheduled else ""
        ctx.conflict("power_unit_count", "Fleet", facts_, f"{scheduled} scheduled (5th unit unexplained)",
                     f"Vehicle schedule lists {scheduled} tractors; the prior-insurance table shows "
                     f"{cur['units'].value} units insured on the current term.{extra} The quote form is only valid for "
                     f"1-{lim[0].value if lim else 4} units - 5+ requires a Fleet Supplemental.", "high")
    return scheduled


# --------------------------------------------------------------------------- loss history
def reconcile_loss_history(ctx):
    terms = []
    for lr in ctx.bag.loss_runs:
        terms.append({"line": lr["line"].value or "?", "kind": lr["kind"], "carrier": lr["carrier"].value,
                      "policy_no": lr["policy_no"].value, "from": lr["from"].value, "to": lr["to"].value,
                      "run_date": lr["run_date"].value, "paid": lr["paid"].value, "reserved": lr["reserved"].value,
                      "incurred": lr["incurred"].value, "claims": [c.value for c in lr["claims"]],
                      "carries_apd": lr.get("carries_apd").value if lr.get("carries_apd") else None,
                      "deductible": lr.get("deductible").value if lr.get("deductible") else None,
                      "source": lr["carrier"].source.label(), "statement": lr.get("statement").value if lr.get("statement") else None})
    terms.sort(key=lambda t: (t["from"] or date.min, t["line"]))
    prior = sorted(ctx.bag.prior_policies, key=lambda p: p["from"].value, reverse=True)
    # compare each application term with the loss runs actually supplied
    for p in prior:
        pf, pt, carrier = p["from"].value, p["to"].value, p["carrier"].value
        overlapping = [t for t in terms if t["from"] and abs((t["from"] - pf).days) <= 31]
        for t in overlapping:
            if t["carrier"] and carrier and not t["carrier"].upper().startswith(carrier.split()[0].upper()):
                ctx.conflict("prior_carrier", f"Term {_show(pf)}-{_show(pt)} ({t['line']})",
                             [p["carrier"], fact(t["carrier"], t["source"].split(" p.")[0], 1, "text-layer", "letterhead")],
                             t["carrier"],
                             f"Application lists {carrier} for all lines this term; the {t['line']} history was supplied by "
                             f"{t['carrier']}. Confirm the cargo carrier and request its currently valued loss run.", "medium")
            if t["from"] != pf:
                ctx.conflict("policy_dates", f"{t['line']} {t['policy_no']}",
                             [p["from"], fact(t["from"], t["source"].split(" p.")[0], 1, "text-layer", "Policy Period")],
                             t["from"], f"Policy period starts {_show(t['from'])} on the loss run vs {_show(pf)} on the "
                                        f"application (1-day offset between AL and cargo terms).", "low", auto=True)
    no_pd = []
    for p in prior:
        pf = p["from"].value
        if "PHD" in (p["coverages"].value or ""):
            al = [t for t in terms if t["from"] and abs((t["from"] - pf).days) <= 31 and t["line"] == "AL" and t["carries_apd"] is False]
            if al:
                no_pd.append((p, al[0]))
    if no_pd:
        ctx.conflict("prior_coverages", f"{len(no_pd)} prior term(s)",
                     [no_pd[0][0]["coverages"]] + [fact("AL only - Physical Damage: No Coverage", t["source"].split(" p.")[0], 1,
                                                        "text-layer", "PHYSICAL DAMAGE No Coverage") for _, t in no_pd],
                     "No physical damage carried",
                     f"Application lists LIAB/PHD/MTC for each term, but the carrier AL loss runs for "
                     f"{', '.join(_show(p['from'].value)[-4:] for p, _ in no_pd)} terms show Physical Damage 'No Coverage'.",
                     "low", auto=True)
    return terms, prior


# --------------------------------------------------------------------------- IFTA / territory
def reconcile_ifta(ctx, available_states):
    qs = sorted(ctx.bag.ifta, key=lambda q: q["begin"].value or date.min)
    by_state = {}
    for q in qs:
        for r in q["rows"].value:
            by_state[r["state"]] = by_state.get(r["state"], 0) + r["miles"]
    total = sum(by_state.values())
    non_listed = {s: m for s, m in by_state.items() if s not in available_states}
    border = {s: m for s, m in by_state.items() if s in US_MEXICO_BORDER_STATES}
    accounts = {q["account"].value for q in qs}
    out = {"quarters": [{"period": f"{_show(q['begin'].value)} - {_show(q['end'].value)}", "begin": q["begin"].value,
                         "end": q["end"].value, "total_miles": q["total_miles"].value, "rows_sum": q["sum_of_rows"],
                         "reconciles": q["reconciles"], "jurisdictions": len(q["rows"].value),
                         "penalty": q["penalty"].value, "taxpayer": q["taxpayer_name"].value, "file": q["file"],
                         "ocr_conf": q["ocr_conf"], "method": q["method"], "confidence": q["rows"].confidence} for q in qs],
           "by_state": dict(sorted(by_state.items(), key=lambda kv: -kv[1])), "total_miles": total,
           "non_listed_miles": sum(non_listed.values()), "non_listed_share": round(sum(non_listed.values()) / total, 3) if total else None,
           "non_listed_states": sorted(non_listed, key=lambda s: -non_listed[s]),
           "border_state_miles": border, "accounts": sorted(accounts)}
    # territory claimed on application vs IFTA reality
    ft = ctx.bag.business.get("farthest_territory", [])
    if ft and total:
        east = [s for s in ("NJ", "PA", "MD", "DE", "VA", "WV", "NC", "SC", "GA", "OH") if s in by_state]
        if east:
            ctx.conflict("farthest_territory", "Operations",
                         [ft[0], fact(f"{', '.join(east[:6])} ... ({len(by_state)} jurisdictions)", qs[0]["file"],
                                      qs[0]["pages"][1] if len(qs[0]["pages"]) > 1 else 1, "ocr",
                                      "IFTA jurisdiction summary", conf=0.95)],
                         "Nationwide (48-state) - per IFTA", f"Application says farthest territory is TX, but four quarters of IFTA "
                         f"filings show {total:,} miles across {len(by_state)} jurisdictions including "
                         f"{', '.join(east[:6])}. Government filings outweigh the self-reported answer; this is a "
                         f"material misstatement of territory.", "high")
    return out
