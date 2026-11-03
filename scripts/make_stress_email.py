"""Build a deliberately messy variant of the sample submission to demonstrate robustness.

* Files renamed to misleading names (e.g. 'Drivers.xlsx' holds the VEHICLE list,
  'LossRuns.jpg' is a driver-license photo).
* One IFTA return re-shot as a 'phone photo': perspective warp, ~7 degree rotation,
  uneven lighting, sensor noise, JPEG compression - then wrapped back into a PDF
  the way phone scanning apps do. Its table page is deliberately harsh (low
  resolution, blur, heavy JPEG) so OCR makes mistakes the validators must catch.
Same underlying facts as the sample, so outcomes should match.
"""
import glob
import os
import shutil
import sys

import cv2
import numpy as np
import pypdfium2 as pdfium
from PIL import Image

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
from pipeline.intake import build_eml  # noqa: E402

SRC = os.path.join(HERE, "samples", "cobalt_mesa_attachments")
OUT = os.path.join(HERE, "samples", "stress_attachments")


def phone_photo(pil: Image.Image, seed=7, harsh=False) -> Image.Image:
    rng = np.random.default_rng(seed)
    img = np.array(pil.convert("RGB")).astype(np.float32)
    h, w = img.shape[:2]
    # perspective: photographed at an angle
    src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    dst = np.float32([[w * 0.04, h * 0.02], [w * 0.97, h * 0.05], [w * 0.93, h * 0.98], [w * 0.01, h * 0.95]])
    M = cv2.getPerspectiveTransform(src, dst)
    img = cv2.warpPerspective(img, M, (w, h), borderValue=(90, 85, 80))
    # small rotation
    R = cv2.getRotationMatrix2D((w / 2, h / 2), 7, 1.0)
    img = cv2.warpAffine(img, R, (w, h), borderValue=(90, 85, 80))
    # uneven lighting / shadow gradient
    yy, xx = np.mgrid[0:h, 0:w]
    light = 0.62 + 0.38 * (xx / w) * (1 - 0.35 * yy / h)
    img = img * light[..., None]
    # warm cast + noise + slight blur
    img[..., 2] *= 0.92
    img += rng.normal(0, 9 if harsh else 7, img.shape)
    img = cv2.GaussianBlur(np.clip(img, 0, 255).astype(np.uint8), (3, 3), 0)
    out = Image.fromarray(img)
    tmp = os.path.join(OUT, "_tmp.jpg")
    out.save(tmp, quality=40 if harsh else 55)  # JPEG artefacts
    return Image.open(tmp).copy()


def degrade(im, noise=9, blur=3, quality=40, seed=1):
    """Extra sensor noise + blur + heavy JPEG on top of the phone-photo effect."""
    rng = np.random.default_rng(seed)
    a = np.array(im).astype(np.float32) + rng.normal(0, noise, np.array(im).shape)
    a = cv2.GaussianBlur(np.clip(a, 0, 255).astype(np.uint8), (blur, blur), 0)
    tmp = os.path.join(OUT, "_tmp2.jpg")
    Image.fromarray(a).save(tmp, quality=quality)
    out = Image.open(tmp).copy()
    os.remove(tmp)
    return out


def main():
    shutil.rmtree(OUT, ignore_errors=True)
    os.makedirs(OUT)
    rename = {
        "VEHICLE LIST - COBALT MESA TRANSPORT INC.xlsx": "Drivers.xlsx",           # wrong on purpose
        "DRIVER LIST - COBALT MESA TRANSPORT INC.xlsx": "Equipment.xlsx",          # wrong on purpose
        "COBALT MESA TRANSPORT INC - KNIGHT.xlsx": "scan0001.xlsx",
        "AL 24-25.pdf": "IFTA_2025.pdf",                                           # a loss run named as IFTA
        "MVR REPORT - COBALT MESA TRANSPORT INC.pdf": "doc_final_v2.pdf",
    }
    for f in glob.glob(os.path.join(SRC, "*")):
        name = os.path.basename(f)
        if name == "Q2 2026.pdf":
            continue
        shutil.copy(f, os.path.join(OUT, rename.get(name, name)))
    # Q2 2026 IFTA re-shot with a phone, both pages, saved as one PDF
    pdf = pdfium.PdfDocument(os.path.join(SRC, "Q2 2026.pdf"))
    pages = [phone_photo(pdf[i].render(scale=200 / 72).to_pil(), seed=1 if i == 1 else 0) for i in range(len(pdf))]
    pages[1] = degrade(pages[1])  # the jurisdiction table: a second, worse shot
    pages[0].save(os.path.join(OUT, "IMG_4471.pdf"), save_all=True, append_images=pages[1:], resolution=200)
    # a licence photo as a bare JPEG with a misleading name
    mvr = pdfium.PdfDocument(os.path.join(SRC, "MVR REPORT - COBALT MESA TRANSPORT INC.pdf"))
    lic = mvr[9].render(scale=200 / 72).to_pil()
    phone_photo(lic, seed=3).save(os.path.join(OUT, "LossRuns.jpg"), quality=70)
    os.remove(os.path.join(OUT, "_tmp.jpg"))
    files = sorted(glob.glob(os.path.join(OUT, "*")))
    raw = build_eml(files, "Daniel Okafor <dokafor@baylineinsurance.example>",
                    "FW: Cobalt Mesa - resending docs (phone scans)",
                    "Resending - some docs were scanned on my phone, sorry for the names.\n\nDaniel")
    out = os.path.join(HERE, "samples", "stress_test_submission.eml")
    with open(out, "wb") as f:
        f.write(raw)
    print(out, len(files), "attachments")


if __name__ == "__main__":
    main()
