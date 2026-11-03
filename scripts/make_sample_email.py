"""Build the simulated agent email (.eml) from the sample package attachments."""
import glob, os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pipeline.intake import build_eml

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
files = sorted(glob.glob(os.path.join(HERE, "samples", "cobalt_mesa_attachments", "*")))
body = """Hi team,

Please see attached new business submission for COBALT MESA TRANSPORT INC, requested effective 12/15/2026.
Application, quote form, driver and vehicle lists, MVRs, loss runs and IFTAs attached.

Thanks,
Daniel Okafor
Bayline Insurance Agency Inc
(925) 555-0164
"""
raw = build_eml(files, "Daniel Okafor <dokafor@baylineinsurance.example>",
                "New Business Submission - COBALT MESA TRANSPORT INC - Eff 12/15/2026", body)
out = os.path.join(HERE, "samples", "cobalt_mesa_submission.eml")
open(out, "wb").write(raw)
print(out, len(raw), "bytes,", len(files), "attachments")
