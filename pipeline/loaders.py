"""Turn any attachment into pages of text (+ images), regardless of file name.

* File type is sniffed from magic bytes, not the extension.
* Digital PDF pages use the text layer.
* Pages with no usable text (scans, phone photos, embedded license photos)
  are rendered at 300 dpi, orientation-corrected, deskewed, denoised and OCR'd.
  Mean tesseract word confidence is kept so it can flow into field confidence.
* Spreadsheets keep every sheet as rows of cells (structure is preserved for
  the extractors) plus a text rendering for the classifier.
"""
from __future__ import annotations

import io
import os
import re
import zipfile
from dataclasses import dataclass, field

import cv2
import numpy as np
import openpyxl
import pdfplumber
import pypdfium2 as pdfium
import pytesseract
from PIL import Image

MIN_TEXT_CHARS = 120   # below this a PDF page is treated as an image page
import threading
_PDFIUM_LOCK = threading.Lock()  # pdfium is not thread-safe; OCR (tesseract processes) still runs in parallel


@dataclass
class Page:
    n: int
    text: str
    method: str                 # text-layer | ocr | xlsx
    ocr_conf: float | None = None
    image: str | None = None    # path of rendered PNG (for UI + vision LLM)
    quality: dict = field(default_factory=dict)
    doc_type: str | None = None
    type_conf: float | None = None
    type_reason: str = ""
    type_engine: str = ""


@dataclass
class Document:
    file: str
    path: str
    kind: str                   # pdf | xlsx | image | eml | unknown
    sha256: str
    pages: list[Page] = field(default_factory=list)
    sheets: dict = field(default_factory=dict)  # xlsx: {sheet: [[cells]]}
    errors: list[str] = field(default_factory=list)


def sniff(path: str) -> str:
    with open(path, "rb") as f:
        head = f.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK"):
        try:
            with zipfile.ZipFile(path) as z:
                names = z.namelist()
            if any(n.startswith("xl/") for n in names):
                return "xlsx"
            if any(n.startswith("word/") for n in names):
                return "docx"
            return "zip"
        except zipfile.BadZipFile:
            return "unknown"
    if head[:3] == b"\xff\xd8\xff" or head.startswith(b"\x89PNG") or head[:4] in (b"II*\x00", b"MM\x00*"):
        return "image"
    if head[4:8] == b"ftyp":
        return "image"  # HEIC from iPhones (needs pillow-heif in production)
    return "unknown"


# --- image cleanup + OCR ----------------------------------------------------
def _fix_orientation(img: Image.Image) -> tuple[Image.Image, int]:
    try:
        osd = pytesseract.image_to_osd(img, config="--psm 0 -c min_characters_to_try=10")
        rot = int(re.search(r"Rotate: (\d+)", osd).group(1))
        conf = float(re.search(r"Orientation confidence: ([\d.]+)", osd).group(1))
        if rot and conf > 1.5:
            return img.rotate(-rot, expand=True, fillcolor="white"), rot
    except Exception:
        pass
    return img, 0


def _deskew(gray: np.ndarray) -> tuple[np.ndarray, float]:
    """Estimate small skew from text-line angles (Hough) and rotate it out."""
    edges = cv2.Canny(gray, 50, 150, apertureSize=3)
    lines = cv2.HoughLinesP(edges, 1, np.pi / 1800, threshold=200,
                            minLineLength=gray.shape[1] // 6, maxLineGap=20)
    if lines is None:
        return gray, 0.0
    angles = []
    for x1, y1, x2, y2 in np.asarray(lines).reshape(-1, 4):
        a = np.degrees(np.arctan2(y2 - y1, x2 - x1))
        if abs(a) < 15:
            angles.append(a)
    if not angles:
        return gray, 0.0
    angle = float(np.median(angles))
    if abs(angle) < 0.15:
        return gray, 0.0
    h, w = gray.shape
    M = cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0)
    return cv2.warpAffine(gray, M, (w, h), flags=cv2.INTER_CUBIC, borderValue=255), angle


def _tess(img: np.ndarray, psm: int) -> tuple[str, float]:
    """One tesseract pass; rebuild line text from word boxes so we only call it once."""
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config=f"--psm {psm}")
    lines, confs = {}, []
    for i, w in enumerate(d["text"]):
        if not w.strip():
            continue
        c = float(d["conf"][i])
        if c >= 0:
            confs.append(c)
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        lines.setdefault(key, []).append(w)
    text = "\n".join(" ".join(ws) for _, ws in sorted(lines.items()))
    return text, (round(sum(confs) / len(confs), 1) if confs else 0.0)


def ocr_image(img: Image.Image) -> tuple[str, float, dict]:
    img = img.convert("RGB")
    img, rot = _fix_orientation(img)
    gray = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2GRAY)
    sharp = cv2.Laplacian(gray, cv2.CV_64F).var()           # focus measure
    gray, skew = _deskew(gray)
    # flatten uneven lighting (phone photos, shadows) before thresholding
    bg = cv2.medianBlur(gray, 41)
    norm = cv2.divide(gray, bg, scale=255)
    variants = []
    text, conf = _tess(norm, 6)
    variants.append(("flattened/psm6", conf))
    if conf < 85:
        # weak read: try other preprocessing + segmentation and keep the union of
        # lines, so parsers see everything any pass could read (ID cards, colour)
        extra = []
        for name, im, psm in (("gray/psm3", gray, 3), ("denoised/psm3", cv2.fastNlMeansDenoising(norm, h=10), 3)):
            t2, c2 = _tess(im, psm)
            variants.append((name, c2))
            extra.append(t2)
            conf = max(conf, c2)
        seen = set(text.splitlines())
        text += "\n" + "\n".join(l for t2 in extra for l in t2.splitlines() if l not in seen)
    quality = {"rotation_fixed": rot, "skew_deg": round(skew, 2), "sharpness": round(float(sharp), 1),
               "passes": variants, "low_quality": bool(conf < 80)}
    return text, conf, quality


# --- loaders ----------------------------------------------------------------
def load_pdf(doc: Document, img_dir: str):
    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(doc.path)
        rendered = [pdf[i].render(scale=300 / 72).to_pil() for i in range(len(pdf))]
        pdf.close()
    with pdfplumber.open(doc.path) as plumb:
        texts = [pp.extract_text(layout=True) or "" for pp in plumb.pages]
    for i, (text, pil) in enumerate(zip(texts, rendered)):
        img_path = os.path.join(img_dir, f"{doc.sha256[:10]}_p{i + 1}.png")
        pil.copy().resize((pil.width // 2, pil.height // 2)).save(img_path)  # preview for UI / vision LLM
        if len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS:
            doc.pages.append(Page(i + 1, text, "text-layer", image=img_path))
        else:
            t, conf, q = ocr_image(pil)
            doc.pages.append(Page(i + 1, t, "ocr", conf, img_path, q))


def load_image(doc: Document, img_dir: str):
    pil = Image.open(doc.path)
    img_path = os.path.join(img_dir, f"{doc.sha256[:10]}_p1.png")
    pil.convert("RGB").save(img_path)
    t, conf, q = ocr_image(pil)
    doc.pages.append(Page(1, t, "ocr", conf, img_path, q))


def load_xlsx(doc: Document):
    wb = openpyxl.load_workbook(doc.path, data_only=True)
    for n, ws in enumerate(wb.worksheets, start=1):
        rows = []
        for r in ws.iter_rows(values_only=True):
            if any(c not in (None, "") for c in r):
                rows.append(list(r))
        doc.sheets[ws.title] = rows
        text = "\n".join(" | ".join(str(c) for c in r if c not in (None, "")) for r in rows)
        doc.pages.append(Page(n, f"[sheet: {ws.title}]\n{text}", "xlsx"))


def load(path: str, file_label: str, sha: str, img_dir: str) -> Document:
    kind = sniff(path)
    doc = Document(file_label, path, kind, sha)
    try:
        if kind == "pdf":
            load_pdf(doc, img_dir)
        elif kind == "xlsx":
            load_xlsx(doc)
        elif kind == "image":
            load_image(doc, img_dir)
        else:
            doc.errors.append(f"Unsupported file type ({kind}); routed to manual review")
    except Exception as e:  # one bad attachment must never sink the submission
        doc.errors.append(f"{type(e).__name__}: {e}")
    return doc
