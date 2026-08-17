# -*- coding: utf-8 -*-
"""Stage 1 — 1-up / 2-up / 3-up layout classification (band-vote gutter probe).

A page in these books is a *spread* of N portrait exam pages printed side by
side, separated by a white gutter.  Finding the gutters is therefore the whole
job, and everything downstream (OCR strips, column-level exam segmentation, the
final PDF slicing) depends on getting it right.

Why the naive probe failed
--------------------------
The previous version averaged the ink over the **full page height** and asked
for a run of columns with < 1% ink.  A gutter only has to be crossed *once* to
destroy that signal, and on a scanned book it always is:

  * the scan is very slightly skewed, so a 40 px gutter drifts sideways over
    1200 px of height and never lines up as one clean column;
  * a figure, a table, a page-footer rule or the diagonal "绿色真卷水印"
    watermark spills across the gutter in one or two places.

Result: densely packed 3-up pages measured 7-10% ink *everywhere* and were
labelled "1-up", so the three exam pages on them were emitted as ONE wide
output page (the user's "拆分后仅有一页" bug).

The fix (general, not per-page)
-------------------------------
Cut the page into `N_BANDS` horizontal bands and let each band **vote**: a
gutter exists at x if a blank run of sufficient width sits there in a healthy
fraction of the bands.  A figure crossing the gutter now only costs the votes
of the bands it covers, and skew is absorbed because we search a window
(`SEARCH_SPAN`) around each nominal anchor instead of a fixed pixel column.

We also record where the gutter *actually* is (median of the voting bands) and
export it as explicit `cuts`, so stage 2/4 slice on the measured seam rather
than on an idealised 1/3 - 2/3, which would shave the edge off a column on a
scan whose gutter sits at 0.312.

Decision rule: a real 3-up page has NO gutter at the centre (the centre falls
inside the middle column), so the 1/3 & 2/3 evidence must also *beat* the
centre evidence before we call it 3-up.  That keeps a 2-up page with ragged
inner margins from being promoted.

Output: <workdir>/layout_class.json — list of
  {page, layout, score_c, score_13, score_23, cuts}
"""
import os
import json
import argparse
import fitz
import numpy as np

SCALE = 2.0           # render scale for the ink probe (fast, plenty accurate)
N_BANDS = 12          # horizontal bands used for gutter voting
BLANK_INK = 1.0       # a band-column with < 1% inked rows counts as blank
MIN_GUTTER = 0.018    # a gutter must span >= 1.8% of the page width
SEARCH_SPAN = 0.045   # how far a gutter may sit from its nominal anchor
VOTE_RATIO = 0.40     # a gutter needs blank votes from >= 40% of inked bands
OVERLAP = 0.006       # cut overlap so no glyph is sliced off at the seam
BAND_INK_MIN = 2.0    # bands with almost no ink (margins) don't get to vote


def _ink_mask(page, scale=SCALE):
    """Boolean ink mask of the rendered page, plus its pixel size."""
    pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
    w, h = pix.width, pix.height
    arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(h, w, pix.n)
    if pix.n >= 3:
        lum = (0.299 * arr[:, :, 0] + 0.587 * arr[:, :, 1]
               + 0.114 * arr[:, :, 2]).astype(np.float32)
    else:
        lum = arr[:, :, 0].astype(np.float32)
    bg = np.percentile(lum, 95)          # paper white, robust to a dark scan
    return (lum < (bg - 40)), w, h


def _blank_runs(profile, min_w):
    """[(start, end)] runs of columns whose ink% is below BLANK_INK."""
    blank = profile < BLANK_INK
    runs, start = [], None
    for i, v in enumerate(blank):
        if v:
            if start is None:
                start = i
        elif start is not None:
            if i - start >= min_w:
                runs.append((start, i))
            start = None
    if start is not None and len(blank) - start >= min_w:
        runs.append((start, len(blank)))
    return runs


def _band_runs(ink, w, h):
    """Blank runs per horizontal band (bands that carry no ink don't vote)."""
    min_w = max(4, int(w * MIN_GUTTER))
    out = []
    for b in range(N_BANDS):
        y0, y1 = int(h * b / N_BANDS), int(h * (b + 1) / N_BANDS)
        prof = ink[y0:y1].mean(axis=0) * 100.0
        if prof.max() < BAND_INK_MIN:
            continue                      # empty band: no evidence either way
        out.append(_blank_runs(prof, min_w))
    return out, min_w


def _probe(bands, w, anchor, min_w):
    """Vote for a gutter near `anchor`.  Returns (score 0-1, centre fraction)."""
    if not bands:
        return 0.0, anchor
    lo = int(w * max(0.0, anchor - SEARCH_SPAN))
    hi = int(w * min(1.0, anchor + SEARCH_SPAN))
    need = min_w * 0.6                    # the run may stick out of the window
    votes, centres = 0, []
    for runs in bands:
        best = None
        for (s, e) in runs:
            ov = min(e, hi) - max(s, lo)
            if ov >= need and (best is None or ov > best[0]):
                best = (ov, (max(s, lo) + min(e, hi)) / 2.0)
        if best:
            votes += 1
            centres.append(best[1])
    score = votes / len(bands)
    centre = (float(np.median(centres)) / w) if centres else anchor
    return score, centre


def _cuts_for(layout, g_c, g_13, g_23):
    """Column windows (x0, x1) as page-width fractions, with a small overlap."""
    def clamp(v):
        return max(0.0, min(1.0, v))
    if layout == "3-up":
        a, b = sorted((g_13, g_23))
        return [(0.0, clamp(a + OVERLAP)),
                (clamp(a - OVERLAP), clamp(b + OVERLAP)),
                (clamp(b - OVERLAP), 1.0)]
    if layout == "2-up":
        return [(0.0, clamp(g_c + OVERLAP)), (clamp(g_c - OVERLAP), 1.0)]
    return [(0.0, 1.0)]


def classify_page(page):
    """Classify one page. Returns (layout, scores dict, cuts)."""
    ink, w, h = _ink_mask(page)
    bands, min_w = _band_runs(ink, w, h)
    s_c, g_c = _probe(bands, w, 0.5, min_w)
    s_13, g_13 = _probe(bands, w, 1.0 / 3.0, min_w)
    s_23, g_23 = _probe(bands, w, 2.0 / 3.0, min_w)

    three = min(s_13, s_23)
    # A genuine 3-up spread has text (not a gutter) at the centre, so the
    # 1/3 & 2/3 evidence must be at least as strong as the centre evidence.
    if three >= VOTE_RATIO and three > s_c:
        layout = "3-up"
    elif s_c >= VOTE_RATIO:
        layout = "2-up"
    else:
        layout = "1-up"
    scores = {"score_c": round(s_c, 3), "score_13": round(s_13, 3),
              "score_23": round(s_23, 3)}
    return layout, scores, _cuts_for(layout, g_c, g_13, g_23)


def run_layout(pdf, workdir, skip=2):
    os.makedirs(workdir, exist_ok=True)
    doc = fitz.open(pdf)
    rows = []
    for pno in range(skip + 1, doc.page_count + 1):   # content pages only
        layout, scores, cuts = classify_page(doc[pno - 1])
        rows.append({"page": pno, "layout": layout,
                     "cuts": [[round(a, 4), round(b, 4)] for a, b in cuts],
                     **scores})
    doc.close()

    out = os.path.join(workdir, "layout_class.json")
    json.dump(rows, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    n1 = sum(1 for r in rows if r["layout"] == "1-up")
    n2 = sum(1 for r in rows if r["layout"] == "2-up")
    n3 = sum(1 for r in rows if r["layout"] == "3-up")
    print(f"[layout] 1-up={n1}  2-up={n2}  3-up={n3}  -> {out}")
    if n1:
        pages = [r["page"] for r in rows if r["layout"] == "1-up"]
        # A 1-up classification on a landscape spread is nearly always a miss;
        # surface it so a bad scan is noticed instead of silently merged.
        print(f"[layout] note: pages kept as single column -> {pages}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pdf", required=True)
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--skip", type=int, default=2,
                    help="leading non-content pages (TOC) to ignore")
    a = ap.parse_args()
    run_layout(a.pdf, a.workdir, skip=a.skip)


if __name__ == "__main__":
    main()
