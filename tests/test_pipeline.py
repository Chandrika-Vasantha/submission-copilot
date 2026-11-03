"""End-to-end tests on the sample package (run: python -m pytest -q tests)."""
import json
import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from pipeline import ai, intake, run  # noqa: E402
from pipeline.knowledge import fuzzy_state, vin_check  # noqa: E402
from pipeline.store import Store  # noqa: E402

_CACHE = {}


def _run(eml="cobalt_mesa_submission.eml", engine=None):
    key = (eml, type(engine).__name__)
    if key in _CACHE:
        return _CACHE[key]
    d = tempfile.mkdtemp()
    store = Store(os.path.join(d, "t.db"))
    sid, _ = intake.ingest_eml(open(os.path.join(ROOT, "samples", eml), "rb").read(), d)
    res = json.loads(json.dumps(run.process(sid, os.path.join(d, sid), store, engine or ai.OfflineEngine()), default=str))
    _CACHE[key] = res
    return res


def test_vin_and_state_knowledge():
    assert vin_check("4V4NC9EH9LN248615")["make"] == "VOLVO"
    assert vin_check("4V4NC9EH9LN248615")["check_digit_ok"] is True
    assert vin_check("3AKJGLD59HSKR736")["valid_length"] is False
    assert fuzzy_state("IIhnors") == "IL" and fuzzy_state("lowa") == "IA" and fuzzy_state("Surcharge") is None


def test_classification_ignores_file_names():
    r = _run()
    types_ = {d["file"]: d["content_types"] for d in r["documents"]}
    assert types_["MTC 23-24.pdf"] == ["no_loss_letter"]
    assert set(types_["MVR REPORT - COBALT MESA TRANSPORT INC.pdf"]) == {"mvr", "drivers_license"}
    assert "application" in types_["COBALT MESA TRANSPORT INC - KNIGHT.xlsx"]


def test_extraction_and_validators():
    r = _run()
    assert r["ifta"]["total_miles"] == 514435
    assert all(q["reconciles"] for q in r["ifta"]["quarters"])
    al = [t for t in r["loss_terms"] if t["line"] == "AL"]
    assert sum(t["incurred"] for t in al) == 77459
    assert len(r["drivers"]) == 5 and r["unit_count"] == 4


def test_conflicts_detected():
    fields = {c["field"] for c in _run()["conflicts"]}
    for f in ("insured_name", "power_unit_count", "date_of_birth", "farthest_territory", "owner_is_driver",
              "vehicle_make", "prior_carrier", "reefer"):
        assert f in fields, f


def test_rules_outcome():
    r = _run()["rules"]
    res = {x["id"]: x for x in r["results"]}
    assert r["decision"] == "REFER" and r["team"] == "Small Fleet Underwriting"
    for rid in ("SUB-01", "SUB-04", "SUB-07", "SUB-08", "DRV-01", "DRV-07", "PRI-02", "PRI-03", "APP-04"):
        assert res[rid]["status"] == "fail", rid
    for rid in ("SUB-03", "SUB-05", "SUB-06", "DRV-03", "DRV-05", "DRV-06", "PRI-01", "APP-03"):
        assert res[rid]["status"] == "pass", rid


def test_stress_variant_misnamed_and_photo():
    if not os.path.exists(os.path.join(ROOT, "samples", "stress_test_submission.eml")):
        return
    r = _run("stress_test_submission.eml")
    mism = {d["file"] for d in r["documents"] if d["filename_mismatch"]}
    assert {"Drivers.xlsx", "Equipment.xlsx", "IFTA_2025.pdf", "LossRuns.jpg"} <= mism
    assert r["rules"]["decision"] == "REFER"


def test_claude_engine_contract_with_fake_client():
    """Exercise the LLM code path (prompting, JSON parsing, grounding check) without network."""
    class FakeMessages:
        def create(self, model, max_tokens, system, messages):
            text = json.dumps({"summary": "Fake summary.", "findings": [
                {"title": "Grounded", "detail": "d", "severity": "high", "confidence": 0.9, "category": "x",
                 "evidence": ["AL 24-25.pdf p.1"]},
                {"title": "Made up", "detail": "d", "severity": "low", "confidence": 0.9, "category": "x",
                 "evidence": ["nonexistent.pdf"]}],
                "positives": ["p"], "recommendations": [], "agent_email": {"subject": "s", "body": "b"}})
            return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)],
                                         usage=types.SimpleNamespace(input_tokens=1, output_tokens=1))

    eng = ai.ClaudeEngine.__new__(ai.ClaudeEngine)
    eng.client = types.SimpleNamespace(messages=FakeMessages())
    eng.calls = []
    eng.available = False  # skip per-page calls; test the analysis contract
    out = _run(engine=eng)["analysis"]
    g = {f["title"]: f for f in out["findings"]}
    assert g["Grounded"]["grounded"] is True
    assert g["Made up"]["grounded"] is False and g["Made up"]["confidence"] <= 0.4
    assert out["summary"] == "Fake summary."
