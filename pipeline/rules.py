"""Deterministic rules engine over the reconciled submission.

The YAML file (rules/knight_trucking.yaml) is the business-owned source of
truth: ids, guideline citations, thresholds and actions. This module only
contains the check implementations, keyed by name. AI output never changes a
rule result directly: the LLM may *add* findings and questions, but routing is
decided here, reproducibly, and every result cites evidence.
"""
from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

import yaml

from .knowledge import MAJOR_VIOLATION_PATTERNS, US_MEXICO_BORDER_STATES

CHECKS = {}


def check(name):
    def deco(fn):
        CHECKS[name] = fn
        return fn
    return deco


def R(status, message, items=None, evidence=None):
    return {"status": status, "message": message, "items": items or [], "evidence": evidence or []}


def _d(x):
    return x.strftime("%m/%d/%Y") if isinstance(x, date) else str(x)


def _val(c, k):
    v = c["business"].get(k, {})
    return v.get("value") if isinstance(v, dict) else None


def _blank(v):
    return v in (None, "", "(blank)")


# ------------------------------------------------------------ submission requirements
@check("application_fein")
def _(c, p):
    if "application" not in c["doc_types"]:
        return R("fail", "No application found in the package")
    fein = _val(c, "fein")
    if _blank(fein):
        return R("fail", "Application received but the FEIN / Tax ID field is blank",
                 evidence=[s["source"]["file"] for s in c["business"]["fein"].get("sources", [])[:1]] or ["application"])
    return R("pass", f"FEIN {fein}")


@check("mc_number")
def _(c, p):
    mc = _val(c, "mc_number")
    return R("pass", f"MC# {mc}, DOT# {_val(c, 'dot_number')}") if not _blank(mc) else R("fail", "MC / docket number missing")


@check("loss_runs_current")
def _(c, p):
    asof = c["app_date"]
    stale = [t for t in c["terms"] if t["run_date"] and (asof - t["run_date"]).days > p["max_age_days"]]
    if not c["terms"]:
        return R("fail", "No loss runs supplied")
    if stale:
        return R("fail", f"{len(stale)} loss run(s) valued more than {p['max_age_days']} days before the application",
                 [f"{t['carrier']} {t['policy_no']} run {_d(t['run_date'])}" for t in stale])
    dates = sorted({t["run_date"] for t in c["terms"] if t["run_date"]})
    return R("pass", f"All loss runs valued {_d(dates[0])} ({(asof - dates[0]).days} days before application)")


def _term_years(c):
    """AL policy terms the account must evidence: expiring term + N prior, aligned to the expiring term."""
    al = [t for t in c["terms"] if t["line"] == "AL"]
    return al


@check("loss_run_years")
def _(c, p):
    eff = c["eff_date"]
    al = _term_years(c)
    have = sorted({t["from"] for t in al if t["from"]})
    need_start = date(eff.year - 1 - p["prior_years"], eff.month, eff.day) if eff else None
    # expiring term starts one year before requested effective date
    missing = []
    for k in range(p["prior_years"] + 1):
        start = date(eff.year - 1 - k, eff.month, eff.day)
        covered = any(abs((h - start).days) <= 45 for h in have)
        if not covered:
            app_term = next((pp for pp in c["prior"] if pp["from"].value and pp["from"].value <= start + timedelta(days=60)
                             and pp["to"].value and pp["to"].value >= start - timedelta(days=300)), None)
            label = f"{start.year}-{start.year + 1} term"
            if app_term:
                label += (f" - application lists {app_term['carrier'].value} {_d(app_term['from'].value)}-"
                          f"{_d(app_term['to'].value)}; no loss run supplied")
            else:
                label += " - no policy listed and no loss run (possible lapse)"
            missing.append(label)
    if missing:
        return R("fail", f"Auto liability loss history incomplete: have {len(have)} of {p['prior_years'] + 1} required years",
                 missing)
    return R("pass", "Expiring + 3 prior years of auto liability loss runs supplied")


@check("ifta_quarters")
def _(c, p):
    asof = c["app_date"]
    qs = c["ifta"]["quarters"]
    # most recent quarter whose return was due (quarter end + grace) on or before the application date
    y, m = asof.year, ((asof.month - 1) // 3) * 3 + 1
    q_start = date(y, m, 1)
    expected = []
    cur = q_start
    while len(expected) < p["quarters"]:
        prev_end = cur - timedelta(days=1)
        prev_start = date(prev_end.year, ((prev_end.month - 1) // 3) * 3 + 1, 1)
        due = prev_end + timedelta(days=p["filing_grace_days"])
        if due <= asof:
            expected.append((prev_start, prev_end))
        cur = prev_start
    have = {q["begin"] for q in qs}
    missing = [f"{_d(s)} - {_d(e)}" for s, e in expected if s not in have]
    note = ""
    last_end = (q_start - timedelta(days=1))
    if last_end not in {q["end"] for q in qs} and (last_end + timedelta(days=p["filing_grace_days"])) > asof:
        note = (f" Quarter ending {_d(last_end)} was not yet due when the application was signed "
                f"(due {_d(last_end + timedelta(days=p['filing_grace_days']))}) - request it once filed.")
    if missing:
        return R("fail", f"Missing IFTA quarter(s): {', '.join(missing)}.{note}")
    return R("pass", f"{len(qs)} consecutive quarters supplied ({qs[0]['period']} to {qs[-1]['period'].split(' - ')[1]}).{note}")


@check("mvrs_current")
def _(c, p):
    asof = c["app_date"]
    items = []
    for d in c["drivers"]:
        if not d["mvr"]:
            items.append(f"{d['name']}: no MVR")
        elif (asof - d["mvr"]["order_date"]).days > p["max_age_days"]:
            items.append(f"{d['name']}: MVR ordered {_d(d['mvr']['order_date'])} (>{p['max_age_days']} days)")
    if items:
        return R("fail", "MVR missing or stale for scheduled driver(s)", items)
    oldest = min(d["mvr"]["order_date"] for d in c["drivers"])
    return R("pass", f"{len(c['drivers'])} drivers, all MVRs current (oldest {_d(oldest)}); "
                     f"{sum(d['stale_mvrs'] for d in c['drivers'])} superseded MVR ignored")


@check("vehicle_schedule_complete")
def _(c, p):
    if not c["vehicles"]:
        return R("fail", "No vehicle schedule found")
    items = []
    for v in c["vehicles"]:
        probs = []
        if not v["vin_check"]["valid_length"]:
            probs.append(f"VIN {v['vin']} is {v['vin_check']['length']} characters")
        if v["vin_check"]["check_digit_ok"] is False:
            probs.append(f"VIN {v['vin']} check digit invalid")
        if not v["year"] or not v["make"]:
            probs.append("year/make missing")
        if v["stated_value"] in (None, ""):
            probs.append("stated value missing")
        if probs:
            items.append(f"Unit {v['year']} {v['make']} {v['vin']}: " + "; ".join(probs))
    if items:
        return R("fail", f"{len(items)} of {len(c['vehicles'])} units incomplete", items)
    return R("pass", "All units have year, make, valid VIN and stated value")


@check("application_signed")
def _(c, p):
    sig = _val(c, "signature_date")
    return R("pass", f"Signed {sig}") if not _blank(sig) else R("fail", "Application signature / date block is blank")


# ------------------------------------------------------------ appetite
@check("eligible_vehicle_types")
def _(c, p):
    bad = [v for v in c["vehicles"] if any(k in str(v.get("vehicle_type") or "").upper() for k in p["ineligible"])]
    if bad:
        return R("fail", "Ineligible vehicle type(s)", [f"{v['year']} {v['make']} {v['vehicle_type']}" for v in bad])
    types = sorted({str(v.get("vehicle_type")).upper() for v in c["vehicles"]})
    return R("pass", f"All scheduled units are {', '.join(types)}; VIN manufacturer codes are heavy-truck makers")


@check("new_venture")
def _(c, p):
    yrs = _val(c, "years_in_business")
    try:
        yrs = float(yrs)
    except (TypeError, ValueError):
        return R("unknown", "Years in business not stated")
    if yrs < p["min_years_in_business"]:
        return R("fail", f"New venture ({yrs:g} yrs): needs CDL experience + letter of experience; corporations UW review")
    return R("pass", f"{yrs:g} years in business; prior insurance documented since "
                     f"{_d(min(pp['from'].value for pp in c['prior']))}" if c["prior"] else f"{yrs:g} years in business")


@check("domicile_state")
def _(c, p):
    st = _val(c, "domicile_state")
    if not st:
        return R("unknown", "Domicile state not determined")
    if st in c["cfg"]["available_states"]:
        return R("pass", f"Domiciled in {st} (available state); garaging address not provided")
    if st in c["cfg"].get("selective_states", []):
        return R("fail", f"Domiciled in {st} - selective basis only")
    return R("fail", f"Domiciled in {st}, not an available state")


@check("operations_non_listed_states")
def _(c, p):
    t = c["ifta"]
    if not t["total_miles"]:
        return R("unknown", "No IFTA mileage to evaluate territory")
    share = t["non_listed_share"]
    if share > p["refer_share_over"]:
        top = ", ".join(f"{s} {t['by_state'][s]:,}" for s in t["non_listed_states"][:6])
        return R("fail", f"{share:.0%} of {t['total_miles']:,} IFTA miles ({t['non_listed_miles']:,}) were run in "
                         f"{len(t['non_listed_states'])} non-listed states", [f"Top non-listed: {top}"])
    return R("pass", "All IFTA mileage in available states")


@check("texas_i10")
def _(c, p):
    tx = c["ifta"]["by_state"].get("TX", 0)
    if not tx:
        return R("pass", "No Texas mileage")
    return R("unknown", f"{tx:,} Texas miles in the last 4 quarters; documents cannot show whether they are north of I-10 "
                        f"(request telematics lanes / load history)")


@check("no_al_deductible")
def _(c, p):
    return R("pass", "No auto liability deductible requested (application: 'Do not offer')")


@check("apd_requested")
def _(c, p):
    if _val(c, "pd_requested") is True:
        return R("fail", "Agent requested a physical damage quote - APD is not available in this program; advise agent")
    return R("pass", "No physical damage requested")


@check("cargo_requested")
def _(c, p):
    if _val(c, "cargo_requested") is True:
        lim = _val(c, "cargo_limit")
        return R("fail", f"Motor truck cargo requested (limit ${lim:,.0f}" + (", reefer breakdown" if _val(c, "reefer") else "")
                 + ") - not in program snapshot; quote AL only or place cargo elsewhere")
    return R("pass", "No cargo requested")


# ------------------------------------------------------------ drivers
@check("driver_cdl")
def _(c, p):
    items = []
    for d in c["drivers"]:
        if not d["cdl"]:
            items.append(f"{d['name']}: {d['license_type'] or 'non-commercial'} Class {d['license_class'] or '?'} - no CDL")
        elif d["license_status"] and d["license_status"] != "VALID":
            items.append(f"{d['name']}: license status {d['license_status']}")
        if d["limited_term"]:
            items.append(f"{d['name']}: limited-term (non-domiciled) CDL - verify current lawful status / eligibility")
    if items:
        return R("fail", "Driver(s) without a valid CDL or needing license verification", items)
    return R("pass", "All drivers hold valid CDLs")


@check("driver_experience")
def _(c, p):
    items, unknown = [], []
    for d in c["drivers"]:
        if not d["cdl"]:
            continue
        if d["years_cdl"] is None:
            unknown.append(f"{d['name']}: CDL issue date not on MVR; self-reported '{d['self_reported_experience']}'")
        elif d["years_cdl"] < p["min_years"]:
            items.append(f"{d['name']}: {d['years_cdl']} yrs (since {_d(d['cdl_since'])}, {d['cdl_since_basis']})")
    if items:
        return R("fail", f"Driver(s) under {p['min_years']} years CDL experience", items)
    ok = [f"{d['name']}: >= {d['years_cdl']} yrs ({d['cdl_since_basis']})" for d in c["drivers"] if d["years_cdl"]]
    return R("pass", f"All CDL drivers evidence >= {p['min_years']} years", ok + unknown)


@check("driver_min_age")
def _(c, p):
    young = [f"{d['name']}: {d['age']}" for d in c["drivers"] if d["age"] is not None and d["age"] < p["min_age"]]
    ages = ", ".join(f"{d['name'].split()[-1]} {d['age']}" for d in c["drivers"])
    return R("fail", "Under-age driver(s)", young) if young else R("pass", f"Ages: {ages}")


@check("driver_senior")
def _(c, p):
    old = [f"{d['name']}: {d['age']}" for d in c["drivers"] if d["age"] and d["age"] >= p["age"]]
    return R("fail", f"Driver(s) {p['age']}+ need DOT medical report", old) if old else R("pass", f"No drivers {p['age']}+")


def _points(d, start):
    return sum(v["points"] for v in d["mvr"]["violations"] if v["date"] and v["date"] >= start) if d["mvr"] else 0


@check("driver_points")
def _(c, p):
    asof = c["app_date"]
    items, ok = [], []
    for d in c["drivers"]:
        p3 = _points(d, date(asof.year - 3, asof.month, asof.day))
        p1 = _points(d, date(asof.year - 1, asof.month, asof.day))
        allpts = _points(d, date(1900, 1, 1))
        if p3 > p["max_3yr"] or p1 > p["max_12mo"]:
            items.append(f"{d['name']}: {p3} pts / 3 yrs, {p1} pts / 12 mo")
        elif allpts:
            ok.append(f"{d['name']}: {p3} pts in 3 yrs ({allpts} on full record)")
    if items:
        return R("fail", "Driver(s) over point thresholds", items)
    return R("pass", "All drivers within point thresholds", ok)


@check("driver_major_violations")
def _(c, p):
    asof = c["app_date"]
    start = date(asof.year - p["lookback_years"], asof.month, asof.day)
    items = []
    for d in c["drivers"]:
        for v in (d["mvr"] or {}).get("violations", []):
            for pat, label in MAJOR_VIOLATION_PATTERNS:
                if re.search(pat, v["raw"], re.I) and v["date"] and v["date"] >= start:
                    items.append(f"{d['name']}: {label} on {_d(v['date'])}")
    return R("fail", "Unacceptable driver history", items) if items else R("pass", "No major violations in 5 years")


@check("loss_drivers_scheduled")
def _(c, p):
    names = {d["name"] for d in c["drivers"]}
    items = []
    for t in c["terms"]:
        for cl in t["claims"]:
            if cl["driver"] and not any(cl["driver"] == n or set(cl["driver"].split()) <= set(n.split()) for n in names):
                items.append(f"{cl['driver']} (DOB {_d(cl['driver_dob'])}) - claim {cl['claim_no']} {_d(cl['loss_date'])}, "
                             f"carrier marked driver '{cl['driver_status']}'")
    return R("fail", "Driver(s) involved in losses are not on the schedule", items) if items else R("pass", "All loss drivers scheduled")


# ------------------------------------------------------------ prohibited / selective
@check("hazmat")
def _(c, p):
    hz = _val(c, "hazmat")
    comm = " ".join(x["commodity"] for lst in c["business"]["commodities"]["value"] for x in lst).upper()
    if hz is True or re.search(r"HAZ|LITHIUM|BATTER|EXPLOSIVE|FLAMMABLE", comm):
        return R("fail", "Hazardous materials / lithium battery exposure")
    note = " (household appliances can include lithium-battery products - confirm)" if "APPLIANCE" in comm else ""
    return R("pass", f"Hazmat answered 'No'; commodities: no hazmat{note}")


@check("prohibited_operations")
def _(c, p):
    comm = " ".join(x["commodity"] for lst in c["business"]["commodities"]["value"] for x in lst).upper()
    hits = [k for k in p["keywords"] if re.search(rf"\b{k}", comm)]
    if _val(c, "intermodal") is True:
        hits.append("intermodal (application answer)")
    return R("fail", "Prohibited operation", hits) if hits else R("pass", "Intermodal 'No'; no towing / waste commodities")


@check("border_operations")
def _(c, p):
    b = c["ifta"]["border_state_miles"]
    if _val(c, "cross_border") is True:
        return R("fail", "Applicant operates cross-border")
    if b:
        tot = sum(b.values())
        return R("unknown", f"{tot:,} miles in border states ({', '.join(f'{s} {m:,}' for s, m in b.items())}); "
                            f"cross-border answered 'No' but IFTA cannot show distance from the border - verify with telematics")
    return R("pass", "No border-state mileage")


@check("safer_lookup")
def _(c, p):
    return R("unknown", f"FMCSA SAFER / SMS lookup for DOT# {_val(c, 'dot_number')} not run in prototype "
                        f"(production: FMCSA QCMobile API call at intake)")


@check("selective_exposures")
def _(c, p):
    comm = " ".join(x["commodity"] for lst in c["business"]["commodities"]["value"] for x in lst).upper()
    items = []
    if re.search(r"AUTO(MOBILE)? HAUL|CAR HAUL|VEHICLES", comm):
        items.append("Automobile hauler")
    if any("BOX" in str(v.get("vehicle_type") or "").upper() for v in c["vehicles"]):
        items.append("Box trucks / transit vans (min premium $250,000)")
    if c["unit_count"] < p["min_units_for_low_rate"]:
        items.append(f"Only {c['unit_count']} power units: target premium must be >= ${p['min_rate_per_unit']:,}/unit "
                     f"(premium sought not stated)")
    if items and any("Automobile" in i or "Box" in i for i in items):
        return R("fail", "Selective exposure", items)
    return R("unknown" if items else "pass", "Rate-per-unit test cannot be evaluated until target premium is known", items)


# ------------------------------------------------------------ prior insurance
def _coverage_spans(c):
    spans = [(pp["from"].value, pp["to"].value, pp["carrier"].value) for pp in c["prior"] if pp["from"].value and pp["to"].value]
    return sorted(spans)


@check("continuous_coverage")
def _(c, p):
    eff = c["eff_date"]
    need_from = date(eff.year - p["years"], eff.month, eff.day)
    spans = _coverage_spans(c)
    covered_from = None
    cur_end = eff
    for s, e, _carrier in sorted(spans, key=lambda x: x[1], reverse=True):
        if (cur_end - e).days <= 30:
            covered_from = s
            cur_end = s
    if covered_from and covered_from <= need_from:
        return R("pass", f"Continuous AL coverage from {_d(covered_from)} to expiration {_d(eff)} "
                         f"(3-year look-back starts {_d(need_from)})")
    return R("fail", f"Coverage not continuous for 3 years before {_d(eff)} (continuous only from {_d(covered_from)})")


@check("coverage_lapses")
def _(c, p):
    spans = _coverage_spans(c)
    gaps = []
    for (s1, e1, c1), (s2, e2, c2) in zip(spans, spans[1:]):
        g = (s2 - e1).days
        if g > p["max_gap_days"]:
            gaps.append(f"{_d(e1)} -> {_d(s2)}: {g}-day lapse between {c1} and {c2}")
    for s, e, carrier in spans:
        if (e - s).days < 350:
            gaps.append(f"{carrier} {_d(s)}-{_d(e)} ran only {(e - s).days} days - ask whether cancelled mid-term and why")
    return R("fail", "Coverage lapse(s) over 30 days need an explanation", gaps) if gaps else R("pass", "No lapses over 30 days")


@check("losses_every_term")
def _(c, p):
    items = []
    for pp in c["prior"]:
        f, t = pp["from"].value, pp["to"].value
        lines = {x["line"] for x in c["terms"] if x["from"] and abs((x["from"] - f).days) <= 31}
        letters = [x for x in c["terms"] if x["from"] and abs((x["from"] - f).days) <= 31 and x["kind"] == "no_loss_letter"]
        if not lines:
            items.append(f"{pp['carrier'].value} {_d(f)}-{_d(t)}: no loss run")
        else:
            if "AL" not in lines:
                items.append(f"{_d(f)}-{_d(t)}: no auto liability loss run")
            for l in letters:
                items.append(f"{l['line']} {_d(l['from'])}-{_d(l['to'])}: carrier letter states 'no paid losses' only - "
                             f"not a loss run (open/reserved claims not shown); request currently valued loss run")
    return R("fail", "Loss history missing for listed term(s)", items) if items else R("pass", "Losses reported for every term")


# ------------------------------------------------------------ data integrity
@check("open_conflicts")
def _(c, p):
    open_ = [x for x in c["conflicts"] if x["status"] == "open" and x["severity"] in p["severities"]]
    if open_:
        return R("fail", f"{len(open_)} unresolved material conflict(s)",
                 [f"{x['id']} {x['field']} ({x['entity']}, {x['severity']})" for x in open_])
    return R("pass", "No unresolved material conflicts")


# ------------------------------------------------------------ engine
def load_rules(path):
    raw = open(path, "rb").read()
    cfg = yaml.safe_load(raw)
    cfg["_sha256"] = hashlib.sha256(raw).hexdigest()[:12]
    cfg["_path"] = path
    return cfg


def evaluate(cfg, canonical):
    canonical["cfg"] = cfg
    results = []
    for rule in cfg["rules"]:
        if rule.get("enabled", True) is False:
            continue
        fn = CHECKS[rule["check"]]
        try:
            r = fn(canonical, rule.get("params", {}))
        except Exception as e:  # a broken rule must surface, never silently pass
            r = R("unknown", f"Rule error: {type(e).__name__}: {e}")
        r.update({k: rule[k] for k in ("id", "title", "category", "guideline", "action")})
        r["effect"] = ("decline" if r["status"] == "fail" and rule["action"] == "decline" else
                       "refer" if r["status"] == "fail" and rule["action"] in ("refer", "missing") else
                       "refer" if r["status"] == "unknown" and rule["action"] != "info" else
                       "info" if r["status"] == "fail" else "none")
        results.append(r)
    units = canonical["unit_count"]
    team = next(rt["team"] for rt in cfg["routing"] if units <= rt["max_units"])
    declines = [r for r in results if r["effect"] == "decline"]
    refers = [r for r in results if r["effect"] == "refer"]
    if declines:
        decision = "DECLINE"
        why = "Prohibited exposure / unacceptable driver: " + "; ".join(r["id"] + " " + r["title"] for r in declines)
    elif refers:
        decision = "REFER"
        why = f"{len(refers)} rule(s) need underwriter review or missing items"
    else:
        decision = "PROCEED"
        why = "All rules pass"
    return {"decision": decision, "reason": why, "team": team, "units": units, "results": results,
            "rules_version": cfg["version"], "rules_hash": cfg["_sha256"],
            "counts": {k: sum(1 for r in results if r["effect"] == k) for k in ("decline", "refer", "info", "none")}}
