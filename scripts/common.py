# -*- coding: utf-8 -*-
"""Shared helpers for the pdf-exam-collection-analyze pipeline.

These keep the stage scripts machine/PDF agnostic: Tesseract location is
resolved at runtime (env override -> PATH -> known Windows default), and the
layout index is loaded into a uniform {page: layout} dict regardless of how it
was serialized.
"""
import os
import shutil
import json

# A known Windows install location; only used as a last resort.
_DEFAULT_TESS = r"C:\Program Files\Tesseract-OCR\tesseract.exe"


def tesseract_env(tesseract=None):
    """Return (executable, env) for running Tesseract.

    Resolution order:
      1. explicit --tesseract path (or TESSERACT_EXE env)
      2. `tesseract` found on PATH
      3. the hard-coded Windows default
    TESSDATA_PREFIX is set from the executable's directory if not already set.
    """
    exe = tesseract or os.environ.get("TESSERACT_EXE") or shutil.which("tesseract") or _DEFAULT_TESS
    env = os.environ.copy()
    if "TESSDATA_PREFIX" not in env:
        base = os.path.dirname(exe)
        cand = os.path.join(base, "tessdata")
        if os.path.isdir(cand):
            env["TESSDATA_PREFIX"] = cand
    return exe, env


def load_layout(path):
    """Load layout_class.json into {int page: '2-up'|'3-up'|'1-up'}."""
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        return {int(k): v for k, v in data.items()}
    return {d["page"]: d["layout"] for d in data}


# Fallback column windows, used only for layout files written before stage 1
# started exporting measured gutters.
_DEFAULT_CUTS = {
    "1-up": [(0.0, 1.0)],
    "2-up": [(0.0, 0.5), (0.5, 1.0)],
    "3-up": [(0.0, 1 / 3), (1 / 3, 2 / 3), (2 / 3, 1.0)],
}


def load_cuts(path):
    """Load the per-page column windows into {int page: [(x0, x1), ...]}.

    Stage 1 measures where the gutters actually are (a scanned spread is never
    split at an exact 1/3) and stores them as `cuts`.  Slicing on the measured
    seam is what keeps a column's first characters from being shaved off.
    Layout files without `cuts` fall back to the idealised windows.
    """
    data = json.load(open(path, encoding="utf-8"))
    if isinstance(data, dict):
        rows = [{"page": int(k), "layout": v} for k, v in data.items()]
    else:
        rows = data
    out = {}
    for d in rows:
        cuts = d.get("cuts")
        if cuts:
            out[int(d["page"])] = [(float(a), float(b)) for a, b in cuts]
        else:
            out[int(d["page"])] = list(_DEFAULT_CUTS.get(d.get("layout"),
                                                         _DEFAULT_CUTS["2-up"]))
    return out
