# -*- coding: utf-8 -*-
"""One-command pipeline: image-only multi-exam PDF -> per-exam single-column PDFs.

Stages:
  1. layout classification (2-up / 3-up)        -> <workdir>/layout_class.json
  2. high-DPI column-strip OCR                  -> <workdir>/strips.json
  3. exam-set boundary detection                -> <workdir>/exam_sets.json
  4. split each set into a single-column PDF    -> <out>/*.pdf + _manifest.json

Requires a Python with PyMuPDF (fitz), Pillow and numpy, plus Tesseract OCR
(chi_sim+eng). On this machine that is the system Python 3.14.

Example
-------
  python scripts/run_pipeline.py \
      --pdf "C:/Github/School/分班考真题/未拆分版/数学_2026.pdf" \
      --out "C:/Github/School/分班考真题/拆分版" \
      --correct scripts/correct_sample.json
"""
import os
import sys
import json
import argparse

# The pipeline prints Unicode progress markers (e.g. "✓"). Under a GBK/CP936
# console (zh-CN locale) those chars crash print() with UnicodeEncodeError and
# abort the whole split. Force UTF-8 output with lossy fallback so the pipeline
# runs no matter how it is launched (direct CLI or via the splitter app).
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from stage1_layout import run_layout
from stage2_ocr import run_ocr
from stage3_extract import run_extract
from stage4_split import run_split


def main():
    ap = argparse.ArgumentParser(description="Split an image-only multi-exam PDF into per-exam single-column PDFs.")
    ap.add_argument("--pdf", required=True, help="path to the combined source PDF")
    ap.add_argument("--out", required=True, help="output directory for the per-exam PDFs")
    ap.add_argument("--workdir", default=None, help="intermediate files dir (default: <out>/_work)")
    ap.add_argument("--skip", type=int, default=2, help="leading non-content pages (TOC) to ignore")
    ap.add_argument("--tesseract", default=None, help="explicit path to tesseract.exe")
    ap.add_argument("--correct", default=None, help="JSON: {page:[school,branch,year]} overrides")
    ap.add_argument("--layout-correct", default=None,
                    help="JSON: {page:\"1-up\"|\"2-up\"|\"3-up\"} manual layout override "
                         "for pages mis-detected by Stage 1 (e.g. watermark hides the gutters).")
    ap.add_argument("--dpi", type=int, default=200, help="render DPI for the output pages (internal)")
    a = ap.parse_args()

    workdir = a.workdir or os.path.join(a.out, "_work")
    correct = json.load(open(a.correct, encoding="utf-8")) if a.correct else None
    layout_correct = json.load(open(a.layout_correct, encoding="utf-8")) if a.layout_correct else None

    print("== Stage 1: layout ==")
    run_layout(a.pdf, workdir, skip=a.skip)
    if layout_correct:
        _patch_layout(workdir, layout_correct)
    print("== Stage 2: OCR ==")
    run_ocr(a.pdf, workdir, tesseract=a.tesseract, skip=a.skip)
    print("== Stage 3: detect exams ==")
    run_extract(workdir, correct=correct)
    print("== Stage 4: split ==")
    run_split(a.pdf, workdir, a.out, dpi=a.dpi)
    print("PIPELINE DONE ->", a.out)


_VALID_LAYOUT = {"1-up", "2-up", "3-up"}


def _patch_layout(workdir, layout_correct):
    """Override per-page layout after Stage 1 (e.g. watermark hid the gutters)."""
    path = os.path.join(workdir, "layout_class.json")
    rows = json.load(open(path, encoding="utf-8"))
    by_page = {r["page"]: r for r in rows}
    patched = []
    for k, v in layout_correct.items():
        p = int(k)
        lab = str(v).strip()
        if lab not in _VALID_LAYOUT:
            print(f"[layout-correct] SKIP page {p}: invalid layout {v!r} "
                  f"(want one of {sorted(_VALID_LAYOUT)})")
            continue
        if p not in by_page:
            print(f"[layout-correct] SKIP page {p}: not in layout_class.json")
            continue
        old = by_page[p]["layout"]
        if old != lab:
            by_page[p]["layout"] = lab
            patched.append((p, old, lab))
            print(f"[layout-correct] page {p}: {old} -> {lab}")
        else:
            print(f"[layout-correct] page {p}: already {lab}, no change")
    if patched:
        json.dump(rows, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"[layout-correct] patched {len(patched)} page(s) -> {path}")


if __name__ == "__main__":
    main()
