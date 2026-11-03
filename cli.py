"""Command line: python cli.py samples/cobalt_mesa_submission.eml"""
import json, os, sys
from pipeline import intake, run
from pipeline.store import Store

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")


def main(path):
    store = Store(os.path.join(DATA, "submissions.db"))
    raw = open(path, "rb").read()
    sid, meta = intake.ingest_eml(raw, os.path.join(DATA, "submissions"))
    print(f"Ingested {sid}: {len(meta['attachments'])} attachments from {meta['from']}")
    res = run.process(sid, os.path.join(DATA, "submissions", sid), store)
    r = res["rules"]
    print(f"Decision: {r['decision']} -> {r['team']}  ({r['reason']})")
    print(f"Conflicts: {len(res['conflicts'])}  Missing items: {len(res['missing'])}  "
          f"AI findings: {len(res['analysis']['findings'])}  ({res['processing_seconds']}s, engine {res['analysis']['engine']})")
    return sid


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "samples", "cobalt_mesa_submission.eml"))
