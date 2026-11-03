"""Workflow orchestration: intake -> load/OCR -> classify -> extract -> reconcile
-> rules -> AI analysis -> persist. Each step is timed and audit-logged.

Production: the same steps become idempotent tasks on a queue (SQS / Service
Bus + Step Functions / Durable Functions / Temporal), one message per
submission, with per-document fan-out for OCR.
"""
from __future__ import annotations

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date

from . import ai, classify, dispatch, extract, loaders, reconcile, rules
from .classify import DOC_TYPES
from .model import jsonable

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RULES_PATH = os.environ.get("RULES_PATH", os.path.join(ROOT, "rules", "knight_trucking.yaml"))
GUIDELINE_TXT = os.path.join(ROOT, "rules", "knight_guideline.txt")

REQUIRED_DOCS = {"application": "Signed application", "loss_run": "Loss runs", "ifta_return": "IFTA returns",
                 "driver_schedule": "Driver schedule", "mvr": "MVRs", "vehicle_schedule": "Vehicle schedule"}


def _timed(store, sid, step, fn, *a, **kw):
    t = time.time()
    out = fn(*a, **kw)
    store.audit(sid, "system", f"step:{step}", {"seconds": round(time.time() - t, 2)})
    return out


def build_missing(ctx, rule_out, biz):
    """Consolidated list of what to request from the agent (deduplicated, with reason)."""
    items = []
    seen = set()

    SHORT = {"SUB-01": "FEIN", "SUB-02": "MC number", "SUB-03": "current loss runs", "SUB-04": "missing loss-run year(s)",
             "SUB-05": "IFTA quarters", "SUB-06": "MVRs", "SUB-08": "signature", "PRI-02": "lapse explanation",
             "PRI-03": "loss history for every term"}

    def add(item, why, src, short=None):
        k = item.lower()[:60]
        if k not in seen:
            seen.add(k)
            items.append({"item": item, "why": why, "source": src, "short": short})

    for r in rule_out["results"]:
        if r["action"] == "missing" and r["status"] == "fail":
            if r["items"]:
                for it in r["items"]:
                    short = SHORT.get(r["id"])
                    if r["id"] == "SUB-07":
                        short = "stated values" if "stated value" in it else None
                        if "characters" in it or "check digit" in it:
                            add(it, r["title"], r["id"], "corrected VIN")
                            continue
                    add(it, r["title"], r["id"], short)
            else:
                add(r["message"], r["title"], r["id"], SHORT.get(r["id"]))
    labels = {"garaging_address": "Physical garaging address for each unit", "date_of_authority": "MC date of authority",
              "first_date_in_business": "First date in business", "elp_compliance":
              "Answer to English Language Proficiency (49 CFR 391.11(b)(2)) question",
              "agency_contact": "Agency contact name on quote form"}
    for g in ctx.gaps:
        if g["field"] in labels:
            add(labels[g["field"]], "Blank on application", "extraction", "garaging address" if g["field"] == "garaging_address" else None)
    for c in ctx.conflicts:
        if c["status"] == "open" and c["severity"] == "high":
            add(f"Clarify {c['field'].replace('_', ' ')} ({c['entity']}): {c['rationale'].split('.')[0]}.", "Conflicting information", c["id"])
    for r in rule_out["results"]:
        if r["id"] == "DRV-07" and r["status"] == "fail":
            for it in r["items"]:
                add(f"Status of driver {it.split(' (')[0]} (still employed? provide MVR or confirm terminated)", r["title"], r["id"])
        if r["id"] == "APP-05" and r["status"] == "unknown":
            add("Confirmation that Texas operations stay north of I-10 (telematics lanes)", r["title"], r["id"])
        if r["id"] == "PRO-03" and r["status"] == "unknown":
            add("Confirmation of no operations within 50 miles of the Mexico border", r["title"], r["id"])
    return items


def process(sid, sub_dir, store, ai_engine=None):
    t0 = time.time()
    meta = json.load(open(os.path.join(sub_dir, "intake.json")))
    engine = ai_engine or ai.get_engine()
    store.upsert(id=sid, received_at=meta["received_at"], sender=meta["from"], subject=meta["subject"],
                 status="processing", ai_engine=engine.name)
    store.audit(sid, "system", "intake", {"from": meta["from"], "subject": meta["subject"], "raw_sha256": meta["raw_sha256"],
                                          "attachments": [(a["file"], a["sha256"][:12], a["bytes"]) for a in meta["attachments"]]})
    img_dir = os.path.join(sub_dir, "pages")

    # 1-2. load + OCR (parallel per document) and classify
    def work(a):
        d = loaders.load(a["path"], a["file"], a["sha256"], img_dir)
        info = classify.classify_document(d, engine)
        store.audit(sid, "system", "document-read", {
            "file": d.file, "types": info["content_types"], "pages": len(d.pages),
            "ocr_pages": sum(1 for p in d.pages if p.method == "ocr"), "mismatch": info["filename_mismatch"]})
        return d, info
    with ThreadPoolExecutor(max_workers=min(8, os.cpu_count() or 4)) as ex:
        loaded = list(ex.map(work, [a for a in meta["attachments"] if not a.get("duplicate_of")]))
    store.audit(sid, "system", "step:load+classify", {"seconds": round(time.time() - t0, 2), "documents": len(loaded)})

    # optional LLM vision re-read of low-confidence scans
    for d, _ in loaded:
        for p in d.pages:
            if p.method == "ocr" and (p.ocr_conf or 0) < 85 and engine.available:
                rr = engine.reread(p, p.doc_type)
                if rr:
                    p.quality["llm_reread"] = rr
                    store.audit(sid, engine.name, "vision-reread", {"file": d.file, "page": p.n})

    # 3. extract
    bag = extract.Bag()
    for d, _ in loaded:
        dispatch.extract_document(d, bag)
    _apply_rereads(loaded, bag)
    if engine.available:  # IFTA quarters whose rows do not sum to the printed total get a vision re-read
        from .knowledge import fuzzy_state
        for q in bag.ifta:
            if q["reconciles"]:
                continue
            d = next(d for d, _ in loaded if d.file == q["file"])
            rr = engine.reread_ifta([p for p in d.pages if p.n in q["pages"]])
            rows = [{"state": fuzzy_state(x.get("jurisdiction", "")), "miles": int(x.get("total_miles") or 0),
                     "raw": "vision re-read"} for x in (rr or {}).get("rows", [])]
            rows = [x for x in rows if x["state"]]
            if rows and sum(x["miles"] for x in rows) == q["total_miles"].value:
                q["rows"].value, q["sum_of_rows"], q["reconciles"] = rows, q["total_miles"].value, True
                q["rows"].confidence = 0.95
                q["rows"].notes.append("OCR rows failed the sum check; vision re-read reconciles to the printed total")
                q["method"] = "ocr + llm-vision"
                store.audit(sid, engine.name, "vision-reread-ifta", {"file": q["file"], "reconciled": True})
    store.audit(sid, "system", "step:extract", {"drivers": len(bag.drivers), "vehicles": len(bag.vehicles),
                                                "mvrs": len(bag.mvrs), "loss_runs": len(bag.loss_runs), "ifta": len(bag.ifta)})

    # 4. reconcile
    cfg = rules.load_rules(RULES_PATH)
    ctx = reconcile.Ctx(bag, loaded)
    biz = reconcile.reconcile_business(ctx)
    app_date = biz["application_date"]["value"] if isinstance(biz["application_date"]["value"], date) else date.today()
    eff = biz["effective_date"]["value"] if isinstance(biz["effective_date"]["value"], date) else app_date
    drivers = reconcile.reconcile_drivers(ctx, app_date)
    vehicles, unscheduled = reconcile.reconcile_vehicles(ctx)
    terms, prior = reconcile.reconcile_loss_history(ctx)
    units = reconcile.reconcile_unit_count(ctx, vehicles, unscheduled, prior)
    ifta = reconcile.reconcile_ifta(ctx, set(cfg["available_states"]))
    store.audit(sid, "system", "step:reconcile", {"conflicts": len(ctx.conflicts), "duplicates": len(ctx.duplicates)})

    doc_types = {p.doc_type for d, _ in loaded for p in d.pages}
    canonical = {"business": biz, "drivers": drivers, "vehicles": vehicles, "unscheduled_vehicles": unscheduled,
                 "terms": terms, "prior": prior, "ifta": ifta, "unit_count": units, "conflicts": ctx.conflicts,
                 "doc_types": doc_types, "app_date": app_date, "eff_date": eff}

    # 5. rules
    rule_out = rules.evaluate(cfg, canonical)
    store.audit(sid, "rules-engine", "rules-evaluated", {"version": cfg["version"], "hash": cfg["_sha256"],
                                                         "decision": rule_out["decision"], "team": rule_out["team"],
                                                         "counts": rule_out["counts"]})
    missing = build_missing(ctx, rule_out, biz)

    # 6. AI analysis
    S = dict(canonical, rules=rule_out, missing=missing,
             guideline_text=open(GUIDELINE_TXT).read() if os.path.exists(GUIDELINE_TXT) else "",
             reply_to=meta["from"])
    analysis = engine.analyze(S)
    store.audit(sid, engine.name, "ai-analysis", {"engine": analysis["engine"], "findings": len(analysis["findings"])})

    # 7. persist
    inventory = []
    for d, info in loaded:
        inventory.append({"file": d.file, "kind": d.kind, "sha256": d.sha256[:12], "errors": d.errors, **info,
                          "pages": [{"n": p.n, "doc_type": p.doc_type, "label": DOC_TYPES.get(p.doc_type, p.doc_type),
                                     "confidence": p.type_conf, "reason": p.type_reason, "engine": p.type_engine,
                                     "method": p.method, "ocr_conf": p.ocr_conf, "quality": p.quality,
                                     "image": os.path.relpath(p.image, sub_dir) if p.image else None,
                                     "text": p.text[:20000]} for p in d.pages]})
    result = {"submission_id": sid, "email": {k: meta[k] for k in ("from", "to", "subject", "date", "body", "received_at")},
              "documents": inventory, "business": biz, "drivers": drivers, "vehicles": vehicles,
              "unscheduled_vehicles": unscheduled, "loss_terms": terms,
              "prior_policies": [{k: v.value for k, v in p.items()} for p in prior], "ifta": ifta,
              "unit_count": units, "conflicts": ctx.conflicts, "duplicates": ctx.duplicates, "gaps": ctx.gaps,
              "missing": missing, "rules": {k: v for k, v in rule_out.items()}, "analysis": analysis,
              "extraction_notes": bag.notes, "as_of": {"application_date": app_date, "effective_date": eff},
              "processing_seconds": round(time.time() - t0, 1)}
    result = jsonable(result)
    path = os.path.join(sub_dir, "result.json")
    with open(path, "w") as f:
        json.dump(result, f, indent=1, default=str)
    store.upsert(id=sid, insured=biz["insured_name"]["value"], status="ready for review", decision=rule_out["decision"],
                 team=rule_out["team"], rules_version=f"{cfg['version']} ({cfg['_sha256']})", ai_engine=analysis["engine"],
                 result_path=path)
    store.audit(sid, "system", "completed", {"seconds": result["processing_seconds"], "decision": rule_out["decision"]})
    return result


def _apply_rereads(loaded, bag):
    """Merge vision re-reads into license records when they validate (dates parse, DL number shape)."""
    from .model import fact, parse_date
    for d, _ in loaded:
        for p in d.pages:
            rr = p.quality.get("llm_reread")
            if not rr:
                continue
            rec = next((l for l in bag.licenses if l["page"] == p.n and l["license_no"].source.file == d.file), None)
            if not rec:
                continue
            conf = float(rr.get("confidence") or 0.8)
            for k_rr, k_rec, conv in (("license_no", "license_no", str), ("dob", "dob", parse_date), ("issue", "issue", parse_date),
                                      ("exp", "exp", parse_date), ("class", "class", str), ("commercial", "cdl", bool),
                                      ("limited_term", "limited_term", bool)):
                v = rr.get(k_rr)
                if v in (None, ""):
                    continue
                v = conv(v)
                old = rec[k_rec]
                if v is not None and (old.value in (None, "") or old.value != v):
                    nf = fact(v, d.file, p.n, "llm-vision", f"vision re-read (OCR said {old.value})", conf=0.88 * conf)
                    if old.value not in (None, "") and old.value != v:
                        nf.notes.append(f"OCR read '{old.value}'")
                    rec[k_rec] = nf
