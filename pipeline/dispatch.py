"""Route classified pages to the right extractor (by content type, never by file name)."""
from __future__ import annotations

from itertools import groupby

from . import extract as X


def extract_document(doc, bag: X.Bag):
    if doc.kind == "xlsx":
        for p in doc.pages:
            sheet = p.text.split("\n", 1)[0].replace("[sheet: ", "").rstrip("]")
            rows = doc.sheets.get(sheet, [])
            if p.doc_type == "application":
                X.application_sheet(rows, doc.file, p.n, bag)
            # every sheet is scanned for schedule tables: an application tab or a
            # mislabelled file can carry drivers or vehicles
            if p.doc_type != "reference":
                X.schedule_sheet(rows, doc.file, p.n, bag, sheet)
        return
    # group consecutive pages of the same type into logical documents
    for dtype, grp in groupby(doc.pages, key=lambda p: p.doc_type):
        pages = list(grp)
        if dtype == "quote_form":
            for p in pages:
                X.quote_form(p.text, doc.file, p.n, bag)
        elif dtype == "mvr":
            X.mvr_pages(pages, doc.file, bag)
        elif dtype == "drivers_license":
            for p in pages:
                X.license_page(p.text, doc.file, p.n, p.ocr_conf, bag)
        elif dtype == "loss_run":
            for p in pages:
                X.loss_run(p.text, doc.file, p.n, bag)
        elif dtype == "no_loss_letter":
            for p in pages:
                X.no_loss_letter(p.text, doc.file, p.n, bag)
        elif dtype == "ifta_return":
            X.ifta_pages(pages, doc.file, bag)
        elif dtype in ("unknown",):
            bag.notes.append(f"{doc.file} p.{','.join(str(p.n) for p in pages)}: unclassified, sent to manual review")
