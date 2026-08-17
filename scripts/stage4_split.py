# -*- coding: utf-8 -*-
"""Stage 4 — split each exam into its own single-column PDF.

Each source page is sliced into its columns and emitted left -> right:
  3-up -> 3 sub-pages, 2-up -> 2 sub-pages, 1-up -> 1 sub-page (whole).
Each column is rendered clipped at DPI=200 and embedded as a **JPEG** (DCTDecode,
q88); for scanned/photographic pages this keeps output in the MB range — PNG/flate
would balloon to GB. Visually lossless for exam scans.
Output filename = the extracted title (cleaned of OCR whitespace); Windows-illegal
chars are replaced with "_". A _manifest.json maps each output file back to its
source page range and split-page count.
"""
import os
import re
import json
import argparse
import fitz
from common import load_cuts

# PyMuPDF refuses to rasterize a pixmap whose long side is too large
# ("Overly large image"). Some source pages are giant spreads (e.g. a 2.5 m
# double spread scanned at high DPI -> ~20000 px), so a clipped column can
# still exceed the limit on its long (full-page-height) axis. Cap the long
# side and downscale just enough to fit; OCR/text quality stays acceptable.
CAP_LONG = 10000


def render_clip(page, clip, zoom):
    long_pts = max(clip.width, clip.height)
    z = zoom
    if long_pts * z > CAP_LONG:
        z = CAP_LONG / long_pts
    try:
        return page.get_pixmap(matrix=fitz.Matrix(z, z), clip=clip)
    except Exception:
        return page.get_pixmap(matrix=fitz.Matrix(1, 1), clip=clip)


def pix_to_jpeg(pix, quality=88):
    """Return JPEG-encoded bytes for a pixmap.

    Why this exists: the original code did
        newp.insert_image(rect, pixmap=pix)
    which stores the raster as a *PNG / flate* image. For photographic or
    halftone-scanned pages (i.e. every page in these exam books) PNG barely
    compresses, so a single 200-DPI column could embed 20-30 MB and a whole
    exam ballooned to 1-2 GB (one source book of 168 MB produced 20 GB of
    splits). JPEG (DCTDecode) compresses the same scanned content 5-10x with
    no visible loss, keeping output PDFs in the MB range.

    JPEG has no alpha channel, so drop alpha first if present.
    """
    if pix.alpha:
        pix = fitz.Pixmap(pix, 0)
    try:
        return pix.tobytes("jpeg", jpg_quality=quality)
    except TypeError:
        # Older PyMuPDF without the jpg_quality kwarg — fall back to default.
        return pix.tobytes("jpeg")


def make_name(e):
    # Prefer the full extracted title (e.g. "成都某实验外国语学校初2025级新初一分班...")
    # Fall back to the structured school/branch/year form.
    if e.get("title"):
        s = e["title"]
    else:
        s = e["school"]
        if e.get("branch"):
            s += f"({e['branch']})"
        if e.get("year"):
            s += f" 初{e['year']}届"
        else:
            s += " 未知届"
    # OCR inserts stray spaces between CJK chars / digits / parens; these are
    # meaningless in Chinese exam titles, so collapse ALL whitespace for a clean
    # filename (and a cleaner display name in the manifest).
    s = re.sub(r'\s+', '', s).strip()
    for ch in '<>:"/\\|?*':
        s = s.replace(ch, "_")
    return s


def run_split(pdf, workdir, outdir, dpi=200):
    os.makedirs(outdir, exist_ok=True)
    cuts_by_page = load_cuts(os.path.join(workdir, "layout_class.json"))
    exams = json.load(open(os.path.join(workdir, "exam_sets.json"), encoding="utf-8"))
    ZOOM = dpi / 72.0

    def cols_for(page):
        # Measured gutters from stage 1 (they are never at an exact 1/3 on a
        # scan); the windows already carry a small overlap so nothing is
        # clipped at the seam.
        return cuts_by_page.get(page) or [(0.0, 0.5), (0.5, 1.0)]

    doc = fitz.open(pdf)
    manifest = []
    total_pages = 0
    used_names = {}          # base name -> count, for collision disambiguation
    for idx, e in enumerate(exams, 1):
        name = make_name(e)
        # Disambiguate identical filenames (e.g. OCR reads 三/四 the same way,
        # or a 序号 reset reuses a title). Append (2), (3) … instead of
        # overwriting a previously written PDF.
        if name in used_names:
            used_names[name] += 1
            name = "%s(%d)" % (name, used_names[name])
        else:
            used_names[name] = 1
        out_path = os.path.join(outdir, name + ".pdf")
        out = fitz.open()
        npages = 0
        segs = e.get("segments")
        if segs:
            # column-level split: emit only the column that belongs to this exam
            for (pg, col) in segs:
                page = doc[pg - 1]
                rect = page.rect
                x0f, x1f = cols_for(pg)[col]
                x0 = x0f * rect.width
                x1 = x1f * rect.width
                clip = fitz.Rect(x0, 0, x1, rect.height)
                pix = render_clip(page, clip, ZOOM)
                w, h = pix.width, pix.height
                if w <= 0 or h <= 0:
                    continue
                newp = out.new_page(width=w, height=h)
                newp.insert_image(fitz.Rect(0, 0, w, h), stream=pix_to_jpeg(pix))
                npages += 1
        else:
            # legacy fallback: whole source pages (1-up / 2-up / 3-up)
            for p in range(e["start"], e["end"] + 1):
                page = doc[p - 1]
                rect = page.rect
                for (x0f, x1f) in cols_for(p):
                    x0 = x0f * rect.width
                    x1 = x1f * rect.width
                    clip = fitz.Rect(x0, 0, x1, rect.height)
                    pix = render_clip(page, clip, ZOOM)
                    w, h = pix.width, pix.height
                    if w <= 0 or h <= 0:
                        continue
                    newp = out.new_page(width=w, height=h)
                    newp.insert_image(fitz.Rect(0, 0, w, h), stream=pix_to_jpeg(pix))
                    npages += 1
        out.save(out_path, garbage=3, deflate=True)
        out.close()
        total_pages += npages
        manifest.append({
            "index": idx, "name": name, "file": name + ".pdf",
            "src_start": e["start"], "src_end": e["end"], "pdf_pages": npages,
            "school": e["school"], "branch": e.get("branch"), "year": e["year"],
        })
        print(f"[{idx:>2}/{len(exams)}] {name}.pdf  (src p{e['start']}-{e['end']}, {npages} pages)")

    doc.close()
    json.dump(manifest, open(os.path.join(outdir, "_manifest.json"), "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)
    print(f"\nDONE. {len(exams)} exam PDFs, {total_pages} total pages -> {outdir}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--dpi", type=int, default=200)
    a = ap.parse_args()
    run_split(a.pdf, a.workdir, a.out, dpi=a.dpi)


if __name__ == "__main__":
    main()
