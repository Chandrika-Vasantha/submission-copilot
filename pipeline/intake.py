"""Email intake.

Production: a mailbox subscription (Microsoft Graph change notifications or
SES inbound -> S3) drops each raw .eml into object storage and enqueues its id.
Prototype: the same contract, simulated. `ingest_eml` accepts a raw RFC-822
message (what any mail gateway hands over); `ingest_files` wraps a set of
uploaded files or a zip in a synthetic email so both paths converge.
"""
from __future__ import annotations

import email
import hashlib
import json
import os
import re
import shutil
import uuid
import zipfile
from datetime import datetime, timezone
from email import policy
from email.message import EmailMessage


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _safe(name: str) -> str:
    return re.sub(r"[^\w.\- ]", "_", os.path.basename(name or "attachment"))


def new_submission_dir(root: str) -> tuple[str, str]:
    sid = datetime.now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]
    d = os.path.join(root, sid)
    os.makedirs(os.path.join(d, "attachments"))
    os.makedirs(os.path.join(d, "pages"))
    return sid, d


def ingest_eml(raw: bytes, root: str) -> tuple[str, dict]:
    sid, d = new_submission_dir(root)
    with open(os.path.join(d, "message.eml"), "wb") as f:
        f.write(raw)  # immutable original, kept for audit
    msg = email.message_from_bytes(raw, policy=policy.default)
    meta = {"submission_id": sid, "from": str(msg.get("From", "")), "to": str(msg.get("To", "")),
            "subject": str(msg.get("Subject", "")), "date": str(msg.get("Date", "")),
            "message_id": str(msg.get("Message-ID", "")), "received_at": datetime.now(timezone.utc).isoformat(),
            "raw_sha256": _sha(raw), "body": "", "attachments": []}
    body = msg.get_body(preferencelist=("plain", "html"))
    if body is not None:
        meta["body"] = body.get_content()[:5000]
    for part in msg.iter_attachments():
        data = part.get_payload(decode=True) or b""
        name = _safe(part.get_filename())
        _store(d, name, data, meta)
    with open(os.path.join(d, "intake.json"), "w") as f:
        json.dump(meta, f, indent=2)
    return sid, meta


def _store(d, name, data, meta):
    # zips inside emails are common: expand them, keep lineage
    if data[:2] == b"PK" and name.lower().endswith(".zip"):
        import io
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for zi in z.infolist():
                if zi.is_dir() or zi.filename.startswith("__MACOSX"):
                    continue
                _store(d, _safe(zi.filename), z.read(zi), meta)
        return
    path = os.path.join(d, "attachments", name)
    i = 1
    while os.path.exists(path):
        root, ext = os.path.splitext(name)
        path = os.path.join(d, "attachments", f"{root}_{i}{ext}")
        i += 1
    with open(path, "wb") as f:
        f.write(data)
    sha = _sha(data)
    dup = next((a["file"] for a in meta["attachments"] if a["sha256"] == sha), None)
    meta["attachments"].append({"file": os.path.basename(path), "path": path, "bytes": len(data),
                                "sha256": sha, "duplicate_of": dup})


def build_eml(files: list[str], sender: str, subject: str, body: str, to="submissions@knight-casestudy.example") -> bytes:
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = sender, to, subject
    msg["Date"] = email.utils.formatdate(localtime=True)
    msg["Message-ID"] = email.utils.make_msgid(domain="baylineinsurance.example")
    msg.set_content(body)
    for p in files:
        with open(p, "rb") as f:
            data = f.read()
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=os.path.basename(p))
    return msg.as_bytes()
