"""Type-specific extractors.

Deterministic parsers run first on every page (cheap, reproducible, auditable).
Each value becomes a Fact with file/page/method/confidence. Built-in validators
(totals that must add up, VIN check digits, date sanity) raise or lower the
confidence of what was read. Pages whose deterministic parse is weak can be
re-read by the LLM (see ai.py) - the result goes through the same validators.
"""
from __future__ import annotations

import re
from collections import defaultdict

from .knowledge import fuzzy_state, vin_check
from .model import fact, money, parse_date, norm_name

DATE = r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"


class Bag:
    """Everything extracted from one submission, before reconciliation."""

    def __init__(self):
        self.business = defaultdict(list)   # field -> [Fact]
        self.drivers = []                   # [{field: Fact}]
        self.mvrs = []
        self.licenses = []
        self.vehicles = []
        self.prior_policies = []
        self.loss_runs = []
        self.ifta = []
        self.notes = []                     # extraction warnings

    def add(self, field, f):
        if f is not None and f.value not in (None, "", []):
            self.business[field].append(f)


def _m(pattern, text, flags=re.I, group=1):
    m = re.search(pattern, text, flags)
    return m.group(group).strip() if m else None


def _checkbox(segment: str) -> bool | None:
    """pdf check marks render as 'n'/'■' next to the chosen option."""
    s = segment.replace("■", "n")
    if re.search(r"\bn\s*Yes\b|\bYesn\b", s):
        return True
    if re.search(r"\bNon\b|\bn\s*No\b(?!\w)", s):
        return False
    return None


# ---------------------------------------------------------------- quote form
def quote_form(text, file, page, bag: Bag):
    F = lambda v, snip="", c=None: fact(v, file, page, "text-layer", snip, conf=c)
    bag.add("agency_name", F(_m(r"Agency Name:\s*(.+?)\s{1,}Contact Name", text), "Agency Name"))
    bag.add("agency_phone", F(_m(r"Contact Name:.*?Ph:\s*(\(\d{3}\)\s*\d{3}-\d{4})", text), "Ph:"))
    contact = _m(r"Contact Name:\s*(.*?)\s*Ph:", text)
    bag.add("agency_contact", F(contact or "(blank)", "Contact Name:", 0.9))
    bag.add("insured_name", F(_m(r"Applicant Name:\s*(.+?)\s{2,}DBA", text), "Applicant Name"))
    addr = _m(r"Mailing Address:\s*(.+?)\s+City:", text)
    city = _m(r"City:\s*([A-Z .]+?)\s+State:", text)
    st = _m(r"State:\s*([A-Z]{2})\s+Zip", text)
    z = _m(r"Zip:\s*(\d{5})", text)
    if addr:
        bag.add("mailing_address", F(f"{addr}, {city}, {st} {z}", "Mailing Address"))
        bag.add("domicile_state", F(st, "Mailing Address State"))
    bag.add("years_in_business", F(_m(r"Years in Business:\s*(\d+)", text), "Years in Business"))
    bag.add("years_prior_insurance", F(_m(r"Years of Prior Insurance:\s*([\w ]+?)\s*$", text, re.I | re.M), "Years of Prior Insurance"))
    bag.add("dot_number", F(_m(r"DOT\s*#\s*(\d{5,8})", text), "DOT #"))
    bag.add("mc_number", F(_m(r"MC\s*#\s*(\d{5,8})", text), "MC #"))
    rad = _m(r"RADIUS INFO:(.*?)COMMODITIES", text, re.I | re.S)
    if rad:
        nums = [int(re.sub(r"\D", "", x)) for x in re.findall(r"_[\d_]+_*%", rad)]
        if len(nums) == 3:
            bag.add("radius", F({"0-500 mi (CA only)": nums[0], "501-750 mi (regional)": nums[1], "48 states": nums[2]},
                               "RADIUS INFO", 0.9))
    comm = []
    for name, pct, val in re.findall(r"([A-Z][A-Z /&]+?)\s+(\d{1,3})\s*%\s*(\d{4,7})", text):
        comm.append({"commodity": name.strip(), "pct": int(pct), "value": int(val)})
    if comm:
        bag.add("commodities", F(comm, "COMMODITIES TRANSPORTED", 0.9))
    bag.add("al_limit", F(money(_m(r"Auto Liability Limit:\s*\$?\s*([\d,]+)", text)), "Auto Liability Limit"))
    bag.add("um_limit", F(_m(r"U\.?M\.? Limit:\s*\$?\s*([\d, /]+)", text), "U.M. Limit"))
    pd = _m(r"Physical Damage\?(.*?)$", text, re.I | re.M)
    if pd is not None:
        bag.add("pd_requested", F(_checkbox(pd), "Physical Damage? Yes/No"))
    cg = _m(r"Cargo Coverage\?(.*?)If Yes", text, re.I | re.S)
    if cg is not None:
        bag.add("cargo_requested", F(_checkbox(cg), "Cargo Coverage? Yes/No"))
    bag.add("cargo_limit", F(money(_m(r"what limits\?\s*\$?\s*([\d,]+)", text)), "Cargo limit"))
    rf = _m(r"Reefer Breakdown:(.*?)$", text, re.I | re.M)
    if rf is not None:
        bag.add("reefer", F(_checkbox(rf), "Reefer Breakdown: Yes/No"))
    # numbered operations questions
    qmap = {2: "unlisted_vehicles", 3: "leases_to_others", 4: "hires_subhaulers", 6: "team_driving",
            9: "freight_broker", 10: "ownership_change", 12: "workers_comp", 14: "doubles_oversize",
            15: "flatbed", 16: "hazmat"}
    for line in text.splitlines():
        mq = re.match(r"\s*(\d{1,2})\s+(.+)$", line)
        if mq and int(mq.group(1)) in qmap and "?" in line:
            bag.add(qmap[int(mq.group(1))], F(_checkbox(line.split("?")[-1]), line.strip()[:80]))
    # inline owner driver row
    for no, name, lic, st, dob, exp, hired in re.findall(
            rf"^\s*(\d)\s+([A-Z][A-Z ]+?)\s{{2,}}([A-Z]?\d{{6,15}})\s+([A-Z]{{2}})\s+({DATE})\s+(.+?)\s{{2,}}(\S.*?)\s*$",
            text, re.M):
        bag.drivers.append({"name": F(name.strip()), "license_no": F(lic), "license_state": F(st),
                            "dob": F(parse_date(dob)), "experience": F(exp.strip()), "hire_date": F(hired.strip()),
                            "_src": "quote form driver table"})
    # prior insurance table
    for d1, d2, carrier, cov, units in re.findall(
            rf"({DATE})\s+To\s+({DATE})\s+(.+?)\s+\((LIAB[^)]*)\)\s+(\d+)", text):
        bag.prior_policies.append({"from": F(parse_date(d1)), "to": F(parse_date(d2)), "carrier": F(carrier.strip()),
                                   "coverages": F(cov), "units": F(int(units))})
    if re.search(r"only for 1-4 Power Units", text, re.I):
        bag.add("quote_form_unit_limit", F(4, "Quick Quote Form is only for 1-4 Power Units"))


# ---------------------------------------------------------------- application (xlsx)
APP_LABELS = {
    "Date of Application": "application_date", "Effective Date Requested": "effective_date",
    "Agency Name": "agency_name", "Agency Contact": "agency_contact", "Agency Phone": "agency_phone",
    "Business Name": "insured_name", "Primary Contact Name": "owner_name",
    "Physical Garaged Address": "garaging_address", "Mailing Address": "mailing_street",
    "FEIN Tax ID #": "fein", "DOT #": "dot_number", "MC #": "mc_number", "Date of Authority": "date_of_authority",
    "Description of Operation:": "operation", "First date in business?": "first_date_in_business",
    "Farthest Territory Traveled": "farthest_territory", "New Venture?": "new_venture",
    "Is Owner a driver?": "owner_is_driver", "Reefer?*": "reefer",
    "Fast Growth Risk (>5 units per year)?": "fast_growth",
    "How many years of experience do you have related to operating a transportation company?": "years_in_business",
    "How many years of prior insurance under the applicants name?": "years_prior_insurance",
    "Is the company a FREIGHT BROKER?": "freight_broker",
    "What percentage of your vehicles utilize Telematic Devices?": "telematics_pct",
    "Do you pull Double or Triple Trailers?": "doubles_oversize",
    "Do you operate cross-border transportation services?": "cross_border",
    "Do you haul Oversize / Overweight loads?": "oversize",
    "Signed this": "signature_date",
}
INTERMODAL_KEY = "Do you haul any Intermodal Containers?"
ELP_KEY = "English Language Proficiency"


def application_sheet(rows, file, page, bag: Bag):
    F = lambda v, snip, c=None: fact(v, file, page, "xlsx", snip, conf=c)
    entity_seen = False
    radius = {}
    commodities = []
    in_comm = False
    for r in rows:
        cells = [c for c in r]
        for i, c in enumerate(cells):
            if not isinstance(c, str):
                continue
            label = c.strip()
            nxt = next((x for x in cells[i + 1:] if x not in (None, "")), None)
            key = APP_LABELS.get(label) or APP_LABELS.get(c)
            if label.startswith(INTERMODAL_KEY):
                key = "intermodal"
            if key:
                val = nxt
                # a following label means the answer was left blank
                if isinstance(val, str) and (val.strip() in APP_LABELS or val.strip().endswith(":") or val.strip() in
                                             ("County", "Title", "State", "Zip", "City", "Email", "Deductible", "State ID#")):
                    val = None
                if key == "signature_date":
                    val = val if val not in ("By:  ", "By:") else None
                bag.add(key, F(val if val is not None else "(blank)", label, 0.97 if val is not None else 0.95))
            if label == "City" and nxt and any(isinstance(x, str) and x == "LATHROP" for x in cells):
                st = cells[cells.index("State") + 1] if "State" in cells else None
                zp = cells[cells.index("Zip") + 1] if "Zip" in cells else None
                bag.add("mailing_city_state_zip", F(f"{nxt}, {st} {zp}", "City/State/Zip"))
                bag.add("domicile_state", F(st, "Mailing State"))
            if ELP_KEY in label:
                ans = nxt if nxt not in (None, "") else "(blank)"
                bag.add("elp_compliance", F(ans, "English Language Proficiency question"))
            if label in ("Corporation", "LLC", "Sole Proprietorship", "Partnership") and not entity_seen:
                # the X sits right after the selected entity box
                for j, x in enumerate(cells):
                    if isinstance(x, str) and x.strip().upper() == "X" and j > 0:
                        bag.add("entity_type", F(str(cells[j - 1]).upper(), "Entity type checkbox"))
                        entity_seen = True
            if re.match(r"^(<\s*50|51\s*-\s*200|201\s*-\s*500|>\s*500)\s*Miles", label, re.I):
                if isinstance(nxt, (int, float)):
                    radius[label] = round(nxt * 100)
            if label == "Commodities Hauled":
                in_comm = True
                continue
            if label in ("General Liability",):
                in_comm = False
        if in_comm and isinstance(cells[0], str) and isinstance(cells[1] if len(cells) > 1 else None, (int, float)):
            pass
        # commodity rows: name, pct(float), avg, max
        vals = [x for x in cells if x not in (None, "")]
        if in_comm and len(vals) >= 4 and isinstance(vals[0], str) and isinstance(vals[1], float) and vals[1] <= 1:
            commodities.append({"commodity": vals[0].strip(), "pct": round(vals[1] * 100), "value": vals[2], "max_value": vals[3]})
    if radius:
        bag.add("radius", F(radius, "Operational Range radius %", 0.95))
    if commodities:
        bag.add("commodities", F(commodities, "Commodities Hauled", 0.95))


# ---------------------------------------------------------------- schedules (any sheet, header-driven)
HEADER_SYNONYMS = {
    "first": ["first name"], "last": ["last name"], "mi": ["middle initial"],
    "name": ["driver name", "name"], "dob": ["date of birth", "dob"],
    "license_no": ["driver license #", "cdl number", "license #", "license number", "dl #"],
    "license_state": ["state licensed", "cdl state", "license state"],
    "experience": ["exp. in yrs", "years of experience", "exp in yrs"],
    "hire_date": ["d.o.h", "hire date", "date hired"], "driver_type": ["driver type"],
    "year": ["year"], "make": ["make"], "model": ["model"], "vin": ["vin"],
    "cost_new": ["cost new", "original new veh. cost"], "stated_value": ["stated value"],
    "vehicle_type": ["vehicle type"], "gvw": ["gross vehicle weight"], "reg_state": ["registered state"],
}


def _header_map(row):
    m = {}
    for i, c in enumerate(row):
        if not isinstance(c, str):
            continue
        h = c.strip().lower().rstrip(":").strip()
        for key, syns in HEADER_SYNONYMS.items():
            if h in syns and key not in m:
                m[key] = i
    return m


def schedule_sheet(rows, file, page, bag: Bag, sheet_name=""):
    """Find header rows by meaning; a 'Drivers.xlsx' full of VINs still parses as vehicles."""
    F = lambda v, snip, c=None: fact(v, file, page, "xlsx", snip, conf=c)
    for hi, row in enumerate(rows):
        hm = _header_map(row)
        is_drv = ("dob" in hm) and ("license_no" in hm or "experience" in hm)
        is_veh = "vin" in hm and ("year" in hm or "make" in hm)
        if not (is_drv or is_veh):
            continue
        for r in rows[hi + 1:]:
            if is_drv:
                g = lambda k: r[hm[k]] if k in hm and hm[k] < len(r) else None
                first, last = g("first"), g("last")
                name = " ".join(str(x) for x in (first, last) if x) if (first or last) else g("name")
                if name and g("dob") and not str(name).upper().startswith(("DRIVER", "LIST")):
                    snip = f"{sheet_name} row: {name}"
                    lic = g("license_no")
                    exp = g("experience")
                    # application sheet puts CDL # in a shifted column for the owner row - detect class text
                    rec = {"name": F(norm_name(str(name)), snip), "dob": F(parse_date(g("dob")), snip),
                           "license_no": F(str(lic) if lic else None, snip), "license_state": F(g("license_state"), snip),
                           "experience": F(str(exp) if exp is not None else None, snip),
                           "hire_date": F(g("hire_date"), snip), "_src": sheet_name}
                    # owner row on application sheet: values are shifted one column left
                    if isinstance(g("hire_date"), str) and re.match(r"^[A-Z]?\d{6,}$", str(g("hire_date"))):
                        rec["license_no"] = F(g("hire_date"), snip, 0.8)
                        rec["license_state"] = F(r[hm["license_no"]] if "license_no" in hm else None, snip, 0.8)
                        rec["hire_date"] = F(None, snip)
                        rec["experience"] = F(r[hm["experience"]] if "experience" in hm else None, snip, 0.8)
                        dt = r[hm["license_state"]] if "license_state" in hm else None
                        rec["driver_type"] = F(dt, snip, 0.8)
                        rec["notes"] = ["Columns shifted on source sheet; mapped by value pattern"]
                    bag.drivers.append(rec)
            if is_veh:
                g = lambda k: r[hm[k]] if k in hm and hm[k] < len(r) else None
                vin = g("vin")
                if vin and re.match(r"^[A-Z0-9]{11,17}$", str(vin).strip().upper()):
                    snip = f"{sheet_name} row: {vin}"
                    v = str(vin).strip().upper()
                    chk = vin_check(v)
                    conf = 0.97 if chk["valid_length"] and chk["check_digit_ok"] else 0.6
                    bag.vehicles.append({"vin": F(v, snip, conf), "year": F(g("year"), snip), "make": F(g("make"), snip),
                                         "model": F(g("model"), snip), "cost_new": F(g("cost_new"), snip),
                                         "stated_value": F(g("stated_value"), snip), "vehicle_type": F(g("vehicle_type"), snip),
                                         "_src": sheet_name})
                elif vin and str(vin).upper().startswith("VIN"):
                    # e.g. "VIN & VEHICLE LIST ATTACHED" on the application fleet tab
                    pass
        break


# ---------------------------------------------------------------- MVR
def mvr_pages(pages, file, bag: Bag):
    """MVRs can span pages; split on report headers."""
    current = None
    for p in pages:
        for block in re.split(r"(?=\b[A-Z ]+ Driver Record - )", p.text):
            if re.search(r"Driver Record - ", block):
                if current:
                    bag.mvrs.append(current)
                current = {"_pages": [p.n], "_text": block, "_old": bool(re.search(r"OLD MVR", p.text))}
            elif current:
                current["_text"] += "\n" + block
                substantive = len(re.sub(r"\s|MVR Report|OLD MVR|\d+/\d+/\d+,?|\d+:\d+ [AP]M", "", block)) > 120
                if substantive and p.n not in current["_pages"]:
                    current["_pages"].append(p.n)
    if current:
        bag.mvrs.append(current)
    # parse each
    for rec in bag.mvrs:
        if "name" in rec:
            continue
        t = rec["_text"]
        pg = rec["_pages"][0]
        F = lambda v, snip="", c=None: fact(v, file, pg, "text-layer", snip, conf=c)
        rec["state"] = F(_m(r"(?:^|\s{2,})([A-Z]+(?: [A-Z]+)?) Driver Record", t, re.M), "Driver Record")
        rec["order_date"] = F(parse_date(_m(rf"Order Date:\s*({DATE})", t)), "Order Date")
        rec["license_no"] = F(_m(r"License:\s+([A-Z0-9]{6,20})\s*$", t, re.I | re.M), "License:")
        nm = _m(r"Name:\s+([A-Z ,.'-]+?)\s*$", t, re.M)
        if nm and "," in nm:
            last, first = [x.strip() for x in nm.split(",", 1)]
            nm = f"{first} {last}"
        rec["name"] = F(norm_name(nm or ""), "Name:")
        rec["dob"] = F(parse_date(_m(rf"DOB:\s*({DATE})", t)), "DOB:")
        rec["address"] = F(_m(r"Address:\s+(\d.+?)\s*$", t, re.M), "Address:")
        lic = re.findall(r"License:\s*(COMMERCIAL|PERSONAL|IDENTIFICATION)\s+Issue:\s*(" + DATE + r")?\s*Expire:\s*(" + DATE + r")\s+Status:\s*(\w+)\s*\n\s*Class:\s*(\w+)", t)
        primary = next((l for l in lic if l[0] == "COMMERCIAL"), lic[0] if lic else None)
        if primary:
            kind, iss, exp, status, cls = primary
            rec["license_type"] = F(kind, f"License: {kind} Class {cls}")
            rec["license_class"] = F(cls, f"Class: {cls}")
            rec["cdl"] = F(kind == "COMMERCIAL", f"License: {kind}")
            rec["license_issue"] = F(parse_date(iss), "Issue:")
            rec["license_exp"] = F(parse_date(exp), "Expire:")
            rec["license_status"] = F(status, "Status:")
        fy = _m(r"Year Lic\. First Issued:\s*(\d{4})", t)
        rec["first_issued_year"] = F(int(fy) if fy else None, "Approx. Year Lic. First Issued")
        viol = []
        for line in t.splitlines():
            mv = re.match(rf"\s*(VIOL|CONV|ACCD|FTA|SUSP)\s+({DATE})\s+(?:({DATE})\s+)?(.*)$", line)
            if mv:
                typ, d1, d2, rest = mv.groups()
                pts = re.findall(r"\s(\d{1,2})\s*$", rest)
                toks = rest.split()
                while toks and (re.search(r"\d", toks[0]) or toks[0] == "-" or (len(toks[0]) <= 4 and toks[0].isalpha() and toks[0] != "**")):
                    toks.pop(0)
                words = []
                for tk in toks:
                    if tk in ("Y", "N"):
                        break
                    words.append(tk)
                desc = " ".join(words).replace("** ", "").replace(" **", "").strip() or rest.strip()
                viol.append({"type": typ, "date": parse_date(d1), "conv_date": parse_date(d2), "raw": line.strip(),
                             "description": desc, "points": int(pts[0]) if pts else 0,
                             "at_fault": None if typ != "ACCD" else ("N" if " - " in rest or "*ACC*" in rest else None)})
        # attach following description continuation lines (e.g. 'OVER LIMIT')
        rec["violations"] = F(viol, "Violations/Convictions")
        events = re.findall(rf"^\s*({DATE})\s+(.+?)\s+AC(?:\s+-\s+\d+)?\s*$", t, re.M)
        rec["events"] = F([{"date": parse_date(d), "event": e.strip()} for d, e in events], "Miscellaneous State Data")
        rec["is_old"] = rec.pop("_old")
        rec["pages"] = rec.pop("_pages")
        rec.pop("_text")


# ---------------------------------------------------------------- driver license images
def license_page(text, file, page, ocr_conf, bag: Bag):
    F = lambda v, snip="", c=None: fact(v, file, page, "ocr", snip, ocr_conf=ocr_conf, conf=c)
    t = text.upper()
    state = None
    for s in ("NEW JERSEY", "CALIFORNIA", "TEXAS", "ARIZONA", "NEVADA", "NEW YORK", "PENNSYLVANIA"):
        if s in t:
            state = s
    dl = None
    for cand in re.findall(r"\b([A-Z0O]\s?\d[\d ]{5,18}\d)\b", t):
        c = re.sub(r"\s", "", cand)
        c = ("O" if c[0] in "0O" else c[0]) + c[1:]  # leading O vs 0 confusion on CA licences
        if 7 <= len(c) <= 16:
            dl = c
            break
    dates = [parse_date(d) for d in re.findall(r"\d{2}[/-]\d{2}[/-]\d{4}", t)]
    dates = [d for d in dates if d]
    labelled = {}
    for lab, pat in (("dob", r"(?:DOB|D0B|008|P08|BOB)\W{0,3}(\d{2}[/-]\d{2}[/-]\d{4})"),
                     ("iss", r"(?:ISS|SS)\W{0,3}(\d{2}[/-]\d{2}[/-]\d{4})"),
                     ("exp", r"(?:EXP|EXE|EXF)\W{0,3}(\d{2}[/-]\d{2}[/-]\d{4})")):
        mm = re.search(pat, t)
        if mm:
            labelled[lab] = parse_date(mm.group(1))
    rest = [d for d in dates if d not in labelled.values()]
    if "dob" not in labelled and rest:
        old = [d for d in rest if d.year < 2006]
        if old:
            labelled["dob"] = old[0]
            rest.remove(old[0])
    rest = sorted(rest)
    if "iss" not in labelled and len(rest) >= 2:
        labelled["iss"] = rest[0]
    if "exp" not in labelled and rest:
        labelled["exp"] = rest[-1]
    # name: line after the licence class block that is all letters
    last = _m(r"\b(?:LN\s+)?([A-Z]{3,})\s*\n\s*(?:FN\s+|EY\s+|_\s+)?([A-Z]{3,}(?:\s[A-Z]{3,})?)\s*\n", t, 0)
    names = re.findall(r"^\W*(?:LN|FN|EY)?\s*([A-Z]{3,}(?: [A-Z]{3,})?)\s*$", t, re.M)
    names = [n for n in names if n not in ("COMMERCIAL", "CALIFORNIA", "NEW JERSEY", "LIMITED", "CLASS A", "NONE")
             and not n.startswith(("END", "RSTR", "SEX", "HGT", "DRIVER"))]
    cls = _m(r"CLASS\s*([ABC])\b", t)
    rec = {"state": F(state, "state header"), "license_no": F(dl, "DL number"),
           "dob": F(labelled.get("dob"), "DOB"), "issue": F(labelled.get("iss"), "ISS"),
           "exp": F(labelled.get("exp"), "EXP"), "class": F(cls, "CLASS"),
           "cdl": F("COMMERCIAL" in t, "COMMERCIAL DRIVER LICENSE"),
           "limited_term": F("LIMITED" in t, "LIMITED-TERM" if "LIMITED" in t else ""),
           "name_tokens": F(names[:3], "name lines"), "page": page, "ocr_conf": ocr_conf}
    bag.licenses.append(rec)


# ---------------------------------------------------------------- loss runs
def loss_run(text, file, page, bag: Bag):
    F = lambda v, snip="", c=None: fact(v, file, page, "text-layer", snip, conf=c)
    per = re.search(rf"Policy Period:\s*({DATE})\s*to\s*({DATE})", text)
    cols = ["AL", "APD", "MTC"]
    # which coverage column holds money tells us the line of business
    paid_line = _m(r"TOTAL LOSSES PAID\s+(.+)$", text, re.I | re.M) or ""
    tokens = re.findall(r"No Coverage|\$[\d,]+\.\d{2}", paid_line)
    line = None
    for i, tok in enumerate(tokens[:3]):
        if tok != "No Coverage":
            line = cols[i]

    def total(label):
        row = _m(label + r"\s+(.+)$", text, re.I | re.M) or ""
        toks = re.findall(r"No Coverage|\$[\d,]+\.\d{2}", row)
        return sum(money(x) for x in toks if x != "No Coverage")

    lr = {"kind": "loss_run", "file": file,
          "carrier": F(_m(r"Insurance Co:\s*(.+?)\s{2,}", text), "Insurance Co"),
          "policy_no": F(_m(r"Policy Number:\s*(\S+)", text), "Policy Number"),
          "insured": F(_m(r"Name Of Insured:\s*(.+?)\s{2,}", text), "Name Of Insured"),
          "from": F(parse_date(per.group(1)) if per else None, "Policy Period"),
          "to": F(parse_date(per.group(2)) if per else None, "Policy Period"),
          "run_date": F(parse_date(_m(rf"Run Date:\s*({DATE})", text)), "Run Date"),
          "deductible": F(money(_m(r"Deductible Amount:\s*(\$?[\d,]+)", text)), "Deductible Amount"),
          "line": F(line, "coverage column with values"),
          "paid": F(total("TOTAL LOSSES PAID"), "TOTAL LOSSES PAID"),
          "reserved": F(total("TOTAL RESERVED"), "TOTAL RESERVED"),
          "incurred": F(total("TOTAL INCURRED"), "TOTAL INCURRED"),
          "carries_apd": F(len(tokens) > 1 and tokens[1] != "No Coverage", "PHYSICAL DAMAGE column"),
          "claims": []}
    blocks = re.split(r"(?=Claim Number:)", text)[1:]
    claim_sum = 0.0
    for b in blocks:
        c = {"claim_no": _m(r"Claim Number:\s*(\S+)", b), "status": _m(r"Claim Status:\s*(\w+)", b),
             "driver": norm_name(_m(r"Name of Driver:\s*(.+?)\s+Date of Birth", b) or ""),
             "driver_dob": parse_date(_m(rf"Date of Birth:\s*({DATE})", b)),
             "driver_status": _m(r"Driver Status:\s*(\w+)", b),
             "hire_date": _m(r"Date of Hire:\s*(\S+)", b),
             "vehicle": (_m(r"Vehicle Info\s*:\s*(\d{4}\s*-\s*[A-Z ]+?)\s*-", b) or "").replace(" ", ""),
             "vin": _m(r"\b([A-HJ-NPR-Z0-9]{17})\b", b),
             "loss_date": parse_date(_m(rf"Date of Loss:\s*({DATE})", b)),
             "reported": parse_date(_m(rf"Date Reported:\s*({DATE})", b)),
             "late": _m(r"Late Reported Claim:\s*([YN])", b) == "Y",
             "unreported_unit": _m(r"Unreported Unit:\s*([YN])", b) == "Y",
             "location": " ".join(x for x in [_m(r"Accident Location:\s*(.+?)\s*$", b, re.M),
                                              _m(r"Unreported Unit:\s*[YN][ \t]+(\S.*?)[ \t]*$", b, re.M)] if x),
             "description": _m(r"Description:\s*(.+?)\s*$", b, re.M),
             "bi": bool(re.search(r"\bBI\b", b)),
             "legal": "LEGAL EXPENSES" in b}
        tot = re.search(r"CLAIM TOTAL:\s+(\$[\d,.]+)\s+(\$[\d,.]+)\s+(\$[\d,.]+)\s+(\$[\d,.]+)", b)
        if tot:
            c["paid"], c["reserve"], c["recovered"], c["incurred"] = [money(x) for x in tot.groups()]
            claim_sum += c["incurred"]
        lr["claims"].append(fact(c, file, page, "text-layer", f"Claim {c['claim_no']}"))
    # validator: claim totals must equal the policy totals
    if blocks:
        ok = abs(claim_sum - lr["incurred"].value) < 0.01
        for k in ("paid", "reserved", "incurred"):
            lr[k].confidence = 0.99 if ok else 0.6
            lr[k].notes.append("claim-level totals reconcile to policy totals" if ok else
                               f"claim totals {claim_sum:,.2f} do not match policy total")
    lr["claim_count"] = F(len(blocks), "TOTAL CLAIMS")
    bag.loss_runs.append(lr)


def no_loss_letter(text, file, page, bag: Bag):
    F = lambda v, snip="", c=None: fact(v, file, page, "text-layer", snip, conf=c)
    first_line = next((l.strip() for l in text.splitlines() if l.strip()), "")
    per = re.search(r"period of\s*(\d{1,2}/\d{1,2}/\d{2,4})\s*-\s*(\d{1,2}/\d{1,2}/\d{2,4})", text)
    pol = _m(r"Policy Number:\s*(.+?)\s*$", text, re.M) or ""
    line = "MTC" if re.search(r"\bMTC\b|CARGO", pol + text) else ("AL" if re.search(r"\bAL\b|LIAB", pol) else None)
    letter_date = parse_date(_m(r"^\s*([A-Z][a-z]+ \d{1,2}, \d{4})\s*$", text, re.M))
    bag.loss_runs.append({
        "kind": "no_loss_letter", "file": file,
        "carrier": F(first_line, "letterhead"), "policy_no": F(pol, "Policy Number"),
        "insured": F(_m(r"Insured Name:\s*(.+?)\s*$", text, re.M), "Insured Name"),
        "from": F(parse_date(per.group(1)) if per else None, "period of"),
        "to": F(parse_date(per.group(2)) if per else None, "period of"),
        "run_date": F(letter_date, "letter date"), "line": F(line, "policy suffix"),
        "statement": F(_m(r"(There have been .+?)\s*$", text, re.M), "letter body"),
        "paid": F(0.0, "no paid losses"), "reserved": F(None, "not stated"), "incurred": F(None, "not stated"),
        "claims": [], "claim_count": F(None, "not stated")})


# ---------------------------------------------------------------- IFTA
def ifta_pages(pages, file, bag: Bag):
    text = "\n".join(p.text for p in pages)
    conf = min((p.ocr_conf or 95) for p in pages)
    method = "ocr" if any(p.method == "ocr" for p in pages) else "text-layer"
    pg = pages[0].n
    F = lambda v, snip="", c=None, page=pg: fact(v, file, page, method, snip, ocr_conf=conf, conf=c)
    begin = parse_date(_m(r"Period Begin:\s*([A-Z][a-z]+ \d{1,2},? \d{4})", text))
    end = parse_date(_m(r"Period End:\s*([A-Z][a-z]+ \d{1,2},? \d{4})", text))
    rows = []
    for line in text.splitlines():
        mm = re.match(r"\s*[^\w\s]*\s*([A-Za-z|][A-Za-z|!1 .:]+?)\s*:?\s+(Surcharge\s+)?2\.?\s*D[il1]es\S*\s+([\d.,]+)\s+([\d.,]+)", line)
        if mm:
            st = fuzzy_state(mm.group(1))
            miles = int(re.sub(r"[.,]", "", mm.group(3)))   # OCR reads 2,659 as 2.659
            # first read wins: weak pages carry extra OCR passes appended below the primary one
            if st and not mm.group(2) and st not in {r["state"] for r in rows}:
                rows.append({"state": st, "miles": miles, "raw": line.strip()[:90]})
    total = None
    for mm in re.finditer(r"^\W*([\d]{2,3}[,.]\d{3})\s+([\d]{2,3}[,.]\d{3})\s+[\d,.]+", text, re.M):
        if mm.group(1) == mm.group(2):  # total miles == taxable miles on the totals line
            total = int(re.sub(r"[.,]", "", mm.group(1)))
            break
    s = sum(r["miles"] for r in rows)
    reconciles = total is not None and s == total
    penalty = money(_m(r"Penalty\s+(\$[\d,.]+)", text))
    interest = money(_m(r"Interest\s+(\$[\d,.]+)", text))
    name = _m(r"Taxpayer Name:\s*(.+?)\s+(?:Account Number|$)", text, re.I | re.M)
    q = {"file": file, "pages": [p.n for p in pages],
         "taxpayer_name": F(name, "Taxpayer Name"), "account": F(_m(r"Account Number:\s*([\d-]+)", text), "Account Number"),
         "begin": F(begin, "Period Begin"), "end": F(end, "Period End"),
         "total_miles": F(total, "Jurisdiction Summary total", 0.97 if reconciles else 0.6, page=pages[-1].n),
         "rows": F(rows, "Jurisdiction rows", 0.97 if reconciles else 0.65, page=pages[1].n if len(pages) > 1 else pg),
         "sum_of_rows": s, "reconciles": reconciles,
         "penalty": F(penalty, "Penalty"), "interest": F(interest, "Interest"),
         "ocr_conf": conf, "method": method}
    if reconciles:
        q["rows"].notes.append(f"{len(rows)} jurisdiction rows sum to reported total {total:,} mi")
    else:
        q["rows"].notes.append(f"Row sum {s:,} vs total {total}: OCR re-read recommended")
    bag.ifta.append(q)
