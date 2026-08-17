# -*- coding: utf-8 -*-
"""Stage 2 — high-DPI OCR with column-strip splitting.

The whole point: in a 2-up / 3-up page the school name and the "初2027届"
keyword often land in DIFFERENT columns, so a single full-page OCR read makes
the regex miss the year. We OCR each column independently AND a full-page top
band (as a robustness fallback), so stage 3 can re-assemble the title reliably.

Output: <workdir>/strips.json  ({str page: [col0, col1, (col2,), full_top]})
"""
import os
import json
import argparse
import fitz
import subprocess
from PIL import Image
from common import tesseract_env, load_layout, load_cuts

DPI = 250
TOP_FRAC = 0.60  # only the header band matters; faster + less noise
# PyMuPDF refuses to rasterize a pixmap whose long side is too large
# ("Overly large image"). Some source PDFs declare a giant MediaBox (e.g. a
# 2.5 m-wide double spread scanned at 250 DPI -> ~25000 px), so we cap the
# long side and downscale just enough to fit. OCR quality stays acceptable
# because the text on such pages is proportionally large.
CAP_LONG = 10000


def _render(page):
    base = DPI / 72.0
    long_pts = max(page.rect.width, page.rect.height)
    s = base
    if long_pts * base > CAP_LONG:
        s = CAP_LONG / long_pts
    try:
        return page.get_pixmap(matrix=fitz.Matrix(s, s))
    except Exception:
        # last-resort: 1:1 (72 DPI) render, never let one page abort the run
        return page.get_pixmap(matrix=fitz.Matrix(1, 1))


def run_ocr(pdf, workdir, tesseract=None, skip=2):
    os.makedirs(workdir, exist_ok=True)
    exe, env = tesseract_env(tesseract)
    layout = load_layout(os.path.join(workdir, "layout_class.json"))
    cuts_by_page = load_cuts(os.path.join(workdir, "layout_class.json"))

    def ocr_region(img, x0, x1):
        w, h = img.size
        crop = img.crop((int(w * x0), 0, int(w * x1), int(h * TOP_FRAC)))
        png = os.path.join(workdir, "_r.png")
        out = os.path.join(workdir, "_r")
        crop.save(png)
        subprocess.run([exe, png, out, "-l", "chi_sim+eng", "--psm", "6"],
                       check=True, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        with open(out + ".txt", encoding="utf-8") as f:
            return f.read()

    doc = fitz.open(pdf)
    N = len(doc)
    strips = {}
    for i in range(skip, N):
        p = i + 1
        page = doc[p - 1]
        try:
            pix = _render(page)
            img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
        except Exception:
            # page unrenderable: record empty so stage 3 inherits current exam
            strips[p] = {"col": ["", "", ""], "full": ""}
            if (p - skip) % 20 == 0:
                print(f"  ocr skip {p}/{N} (unrenderable)", flush=True)
            continue
        lay = layout.get(p, "2-up")
        # Stage 1 measured where the gutters really are on THIS page; using the
        # measured seam (instead of an idealised 1/3) stops a column's first
        # characters — including the school name in the header — from being
        # sliced away on a skewed or off-centre scan.
        cuts = cuts_by_page.get(p) or [(0.0, 0.5), (0.5, 1.0)]
        # column strips (primary) + one full-page top band kept SEPARATELY
        # as a fallback (only consulted when NO column yields a header).
        # Appending it as a peer would let a garbled full-page read become a
        # second, different "exam key" on the same page and force a false split.
        res = [ocr_region(img, x0, x1) for (x0, x1) in cuts]
        # For 1-up the "column" already is the full width, so the separate full
        # fallback would be identical; skip it to save a useless OCR pass.
        full = "" if lay == "1-up" else ocr_region(img, 0.0, 1.0)
        strips[p] = {"col": res, "full": full}
        # 逐页上报 OCR 进度（不要每隔 N 页才报，否则大文件 OCR 期间进度长时间不动，
        # 前端看起来像卡死）。父进程按 "ocr done x/y" 把进度从 15% 平滑推到 70%。
        print(f"  ocr done {p}/{N}", flush=True)
    doc.close()

    outp = os.path.join(workdir, "strips.json")
    json.dump({str(k): v for k, v in strips.items()},
              open(outp, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    for fn in ["_r.png", "_r.txt"]:
        p = os.path.join(workdir, fn)
        if os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                # Benign: a temp cleanup failure (e.g. sandbox without a
                # recycle bin) must not abort the whole pipeline.
                pass
    print(f"[ocr] strips -> {outp}  ({len(strips)} pages)")
    return outp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--tesseract", default=None)
    ap.add_argument("--skip", type=int, default=2)
    a = ap.parse_args()
    run_ocr(a.pdf, a.workdir, tesseract=a.tesseract, skip=a.skip)


if __name__ == "__main__":
    main()
