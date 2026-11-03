"""Underwriter workbench (Flask).

Run:  python app.py   ->  http://localhost:8000
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, url_for

from pipeline import intake, run
from pipeline.store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
SUBS = os.path.join(DATA, "submissions")
SAMPLE = os.path.join(ROOT, "samples", "cobalt_mesa_submission.eml")
STRESS = os.path.join(ROOT, "samples", "stress_test_submission.eml")

app = Flask(__name__)
store = Store(os.path.join(DATA, "submissions.db"))
USER = os.environ.get("UW_USER", "underwriter@knight")


def _process_async(sid):
    def go():
        try:
            run.process(sid, os.path.join(SUBS, sid), store)
        except Exception as e:  # surface failures in the inbox, never lose the email
            store.upsert(id=sid, status=f"error: {type(e).__name__}: {e}"[:200])
            store.audit(sid, "system", "error", {"error": repr(e)})
    threading.Thread(target=go, daemon=True).start()


def _ingest(raw: bytes):
    sid, meta = intake.ingest_eml(raw, SUBS)
    store.upsert(id=sid, received_at=meta["received_at"], sender=meta["from"], subject=meta["subject"], status="queued")
    _process_async(sid)
    return sid


def _load(sid):
    p = os.path.join(SUBS, sid, "result.json")
    if not os.path.exists(p):
        return None
    with open(p) as f:
        return json.load(f)


def _save(sid, res):
    with open(os.path.join(SUBS, sid, "result.json"), "w") as f:
        json.dump(res, f, indent=1, default=str)


def _recompute(res):
    """After a human resolves conflicts or waives a rule, re-derive the routing decision."""
    rules = res["rules"]
    open_ = [c for c in res["conflicts"] if c["status"] == "open" and c["severity"] in ("high", "medium")]
    for r in rules["results"]:
        if r["id"] == "DQ-01" and not r.get("waived"):
            r["status"] = "fail" if open_ else "pass"
            r["message"] = f"{len(open_)} unresolved material conflict(s)" if open_ else "No unresolved material conflicts"
            r["items"] = [f"{c['id']} {c['field']} ({c['entity']}, {c['severity']})" for c in open_]
            r["effect"] = "refer" if open_ else "none"
    eff = [r["effect"] for r in rules["results"]]
    rules["decision"] = "DECLINE" if "decline" in eff else "REFER" if "refer" in eff else "PROCEED"
    rules["counts"] = {k: eff.count(k) for k in ("decline", "refer", "info", "none")}
    n = rules["counts"]["refer"]
    rules["reason"] = (rules["reason"] if rules["decision"] == "DECLINE" else
                       f"{n} rule(s) need underwriter review or missing items" if n else "All rules pass or waived")
    return res


# ------------------------------------------------------------------ pages
@app.route("/")
def inbox():
    return render_template("inbox.html", subs=store.list(), has_stress=os.path.exists(STRESS))


@app.post("/ingest/sample")
def ingest_sample():
    which = request.form.get("which", "sample")
    path = STRESS if which == "stress" else SAMPLE
    with open(path, "rb") as f:
        sid = _ingest(f.read())
    return redirect(url_for("submission", sid=sid))


@app.post("/ingest/upload")
def ingest_upload():
    files = request.files.getlist("files")
    if not files or not files[0].filename:
        return redirect(url_for("inbox"))
    if len(files) == 1 and files[0].filename.lower().endswith(".eml"):
        sid = _ingest(files[0].read())
    else:
        tmp = os.path.join(DATA, "upload_tmp")
        os.makedirs(tmp, exist_ok=True)
        paths = []
        for f in files:
            p = os.path.join(tmp, os.path.basename(f.filename))
            f.save(p)
            paths.append(p)
        raw = intake.build_eml(paths, request.form.get("sender") or "portal-upload@agency.example",
                               request.form.get("subject") or "Uploaded submission", "Uploaded via underwriter workbench")
        sid = _ingest(raw)
    return redirect(url_for("submission", sid=sid))


@app.route("/s/<sid>")
def submission(sid):
    sub = store.get(sid) or abort(404)
    res = _load(sid)
    if res is None:
        return render_template("processing.html", sub=sub)
    return render_template("submission.html", sub=sub, r=res, audit=store.audit_log(sid))


STAGES = [("intake", "Email received"), ("read", "Reading & OCR"), ("extract", "Extracting facts"),
          ("reconcile", "Cross-checking documents"), ("rules", "Applying guideline"), ("ai", "AI review"),
          ("done", "Ready for underwriter")]
_STAGE_OF = {"intake": "intake", "step:load+classify": "read", "step:extract": "extract",
             "step:reconcile": "reconcile", "rules-evaluated": "rules", "ai-analysis": "ai", "completed": "done"}


@app.route("/s/<sid>/progress")
def progress(sid):
    """Live pipeline state for the processing screen (read from the audit log)."""
    sub = store.get(sid) or abort(404)
    log = store.audit_log(sid)
    reached = {_STAGE_OF[a["action"]] for a in log if a["action"] in _STAGE_OF}
    docs = [json.loads(a["detail"]) for a in log if a["action"] == "document-read"]
    meta_p = os.path.join(SUBS, sid, "intake.json")
    attachments = []
    if os.path.exists(meta_p):
        with open(meta_p) as f:
            attachments = [a["file"] for a in json.load(f)["attachments"] if not a.get("duplicate_of")]
    done_idx = max([i for i, (k, _) in enumerate(STAGES) if k in reached], default=-1)
    stages = [{"key": k, "label": l, "state": "done" if i <= done_idx else "active" if i == done_idx + 1 else "todo"}
              for i, (k, l) in enumerate(STAGES)]
    read = {d["file"]: d for d in docs}
    return jsonify({"status": sub["status"], "done": "done" in reached, "error": sub["status"].startswith("error"),
                    "stages": stages, "documents": [{**read.get(f, {}), "file": f, "read": f in read} for f in attachments],
                    "decision": sub.get("decision"), "team": sub.get("team"), "insured": sub.get("insured")})


@app.route("/s/<sid>/file/<path:rel>")
def page_image(sid, rel):
    return send_from_directory(os.path.join(SUBS, sid), rel)


@app.route("/s/<sid>/source")
def source_view(sid):
    res = _load(sid) or abort(404)
    file, page = request.args.get("file"), request.args.get("page", type=int)
    doc = next((d for d in res["documents"] if d["file"] == file), None) or abort(404)
    pg = next((p for p in doc["pages"] if p["n"] == (page or 1)), doc["pages"][0])
    from markupsafe import Markup, escape
    text = str(escape(pg["text"]))
    hl = request.args.get("hl", "").strip()
    # highlight the anchor text of the field the user clicked (best effort: first 3 words)
    key = " ".join(hl.split()[:3])
    if key and str(escape(key)) in text:
        text = text.replace(str(escape(key)), f"<mark id='hl'>{escape(key)}</mark>", 1)
    return render_template("source.html", sid=sid, doc=doc, pg=pg, text_html=Markup(text))


@app.route("/s/<sid>.json")
def submission_json(sid):
    return jsonify(_load(sid) or {})


@app.post("/s/<sid>/conflict/<cid>")
def resolve_conflict(sid, cid):
    res = _load(sid) or abort(404)
    c = next(x for x in res["conflicts"] if x["id"] == cid)
    choice = request.form.get("choice")
    note = request.form.get("note", "").strip()
    c["status"] = "resolved"
    c["resolution"] = {"value": choice, "note": note, "by": USER,
                       "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    store.audit(sid, USER, "conflict-resolved", {"conflict": cid, "field": c["field"], "value": choice, "note": note})
    _save(sid, _recompute(res))
    store.upsert(id=sid, decision=res["rules"]["decision"])
    return redirect(url_for("submission", sid=sid) + "#conflicts")


@app.post("/s/<sid>/rule/<rid>")
def waive_rule(sid, rid):
    res = _load(sid) or abort(404)
    r = next(x for x in res["rules"]["results"] if x["id"] == rid)
    reason = request.form.get("reason", "").strip()
    if not reason:
        return redirect(url_for("submission", sid=sid) + "#rules")
    r["waived"] = {"by": USER, "reason": reason, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "original_effect": r["effect"]}
    r["effect"] = "none"
    store.audit(sid, USER, "rule-waived", {"rule": rid, "reason": reason, "original": r["waived"]["original_effect"]})
    _save(sid, _recompute(res))
    store.upsert(id=sid, decision=res["rules"]["decision"])
    return redirect(url_for("submission", sid=sid) + "#rules")


@app.post("/s/<sid>/action")
def uw_action(sid):
    res = _load(sid) or abort(404)
    action = request.form.get("action")
    note = request.form.get("note", "")
    if action == "send_request":
        body = request.form.get("body", res["analysis"]["agent_email"]["body"])
        out = os.path.join(SUBS, sid, "outbox_request_to_agent.eml")
        with open(out, "wb") as f:
            f.write(intake.build_eml([], "trucking-uw@knight-casestudy.example", res["analysis"]["agent_email"]["subject"],
                                     body, to=res["analysis"]["agent_email"].get("to") or res["email"]["from"]))
        res.setdefault("actions", []).append({"action": "Information request sent to agent", "by": USER,
                                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        store.upsert(id=sid, status="awaiting agent")
        store.audit(sid, USER, "request-sent-to-agent", {"to": res["analysis"]["agent_email"].get("to"), "chars": len(body)})
    else:
        label = {"accept": f"Accepted recommendation ({res['rules']['decision']})", "decline": "Declined by underwriter",
                 "quote": "Released for quoting", "assign": f"Assigned to {res['rules']['team']}"}.get(action, action)
        res.setdefault("actions", []).append({"action": label, "note": note, "by": USER,
                                              "at": datetime.now(timezone.utc).isoformat(timespec="seconds")})
        status = {"decline": "declined", "quote": "released for quoting", "assign": "assigned"}.get(action, "reviewed")
        store.upsert(id=sid, status=status, assignee=USER if action == "assign" else None)
        store.audit(sid, USER, f"uw-{action}", {"note": note})
    _save(sid, res)
    return redirect(url_for("submission", sid=sid) + "#actions")


@app.route("/architecture")
def architecture():
    return render_template("architecture.html")


@app.route("/rules")
def rules_view():
    with open(run.RULES_PATH) as f:
        text = f.read()
    import yaml
    return render_template("rules.html", text=text, cfg=yaml.safe_load(text), path=os.path.relpath(run.RULES_PATH, ROOT))


@app.template_filter("pct")
def pct(v):
    return f"{v:.0%}" if isinstance(v, (int, float)) else "-"


@app.template_filter("money")
def money(v):
    return f"${v:,.0f}" if isinstance(v, (int, float)) else "-"


@app.template_filter("num")
def num(v):
    return f"{v:,}" if isinstance(v, (int, float)) else (v or "-")


@app.template_filter("us")
def us_date(v):
    if isinstance(v, str) and len(v) == 10 and v[4] == "-":
        return f"{v[5:7]}/{v[8:10]}/{v[:4]}"
    return "-" if v in (None, "") else v


@app.template_filter("src")
def src_parts(label):
    """'FILE.pdf p.3' -> (file, page)"""
    if not label:
        return ("", None)
    if " p." in label:
        f, p = label.rsplit(" p.", 1)
        return (f, int(p) if p.isdigit() else None)
    return (label, None)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 8000)), debug=False)
