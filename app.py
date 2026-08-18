# -*- coding: utf-8 -*-
r"""试卷合订本拆分器 —— 本地 Web 应用（FastAPI 后端 + 内嵌网页前端）。

把"多套混排、双栏/三栏"的扫描试卷 PDF，自动拆成每套单栏的独立 PDF。

支持一次选择【多份 PDF 并行处理】：后端用「任务队列 + 常驻消费者线程池」
限制同时运行的份数，在吞吐与机器负载间取得平衡。

并发上限（重要，已按性能权衡固化）：
  * MAX_CONCURRENT = 3  —— 默认同时处理 3 份。
  * 流水线最重的是 OCR（Tesseract 单实例约吃满 1 核）+ PyMuPDF 渲染（CPU 密集）。
    在 4~8 核机器上 3 份并发能让 OCR 接近饱和、又给系统/渲染留核；
    并发 >4 时 OCR 互相抢核，单份变慢、总吞吐反而下降；每份约 200~400MB 内存，
    3 份≈1GB 可承受，4 份≈1.3GB 已是上限。故硬上限 MAX_CONCURRENT_CAP = 4。

输出目录交互：
  * 点「Browse」由服务端弹出 Windows 原生文件夹选择框，返回并显示完整路径；
  * 也可以直接在输入框填写/粘贴路径；拆分结果由服务端自动写入所选文件夹
    （每份源 PDF 在输出文件夹下生成同名子目录，避免不同源文件产物互相覆盖）。

拆分流水线默认使用仓库内 scripts/（run_pipeline.py 及各 stage），
若 scripts/ 不存在则回退到 ~/.workbuddy/skills/pdf-exam-collection-analyze/scripts。

运行（需系统 Python 3.14，已含 fastapi/uvicorn/PyMuPDF/Pillow/numpy/Tesseract）：
  python app.py
启动后会自动打开浏览器 http://127.0.0.1:<port>。
"""
import os
import sys
import re
import base64
import json
import uuid
import socket
import subprocess
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
import uvicorn

# 打包为 exe 后，静态资源与流水线位于 PyInstaller 的临时资源目录；
# 运行中间产物则写到 exe 同级目录，避免写入只读临时资源目录。
BASE = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
APP_HOME = os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else BASE
RUNS_DIR = os.path.join(APP_HOME, "runs")
os.makedirs(RUNS_DIR, exist_ok=True)
SKILL_DIR = os.path.join(BASE, "scripts")
if not os.path.isdir(SKILL_DIR):
    SKILL_DIR = os.path.expanduser(r"~\.workbuddy\skills\pdf-exam-collection-analyze\scripts")

# Console output may be GBK under the zh-CN locale; Unicode progress markers
# (e.g. "✓") would otherwise crash print(). Force UTF-8 with lossy fallback.
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        pass

# --------------------------------------------------------------------------- #
# 并发控制：队列 + 常驻消费者线程池（固定并发数，绝不因任务多而爆线程）
# --------------------------------------------------------------------------- #
MAX_CONCURRENT = 3          # 默认同时处理 3 份（推荐平衡点）
MAX_CONCURRENT_CAP = 4      # 硬上限：并发最多 4 份

# 每个任务的状态：{name, status, stage, progress, log:[], exams, summary, out_dir, error}
RUNS = {}
RUNS_LOCK = threading.Lock()
BATCHES = {}                # batch_id -> [run_id, ...]

_TASK_Q = queue.Queue()


def _run_now(run_id, cmd):
    st = RUNS[run_id]
    try:
        with RUNS_LOCK:
            st["status"] = "running"
        proc = subprocess.Popen(
            cmd, cwd=SKILL_DIR, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
        with proc:
            for line in proc.stdout:
                line = line.rstrip("\n")
                with RUNS_LOCK:
                    st["log"].append(line)
                    if len(st["log"]) > 800:
                        st["log"] = st["log"][-800:]
                    parse_line(line, st)
            rc = proc.wait()
        with RUNS_LOCK:
            if rc == 0 and st["status"] == "running":
                st["status"] = "done"
                if not st.get("summary"):
                    st["summary"] = {"exams": st.get("exams"),
                                     "pages": None, "out_dir": st["out_dir"]}
            else:
                st["status"] = "error"
                st["error"] = "流水线返回非零退出码（%s）。请查看日志。" % rc
    except Exception as e:  # noqa
        with RUNS_LOCK:
            st["status"] = "error"
            st["error"] = str(e)


def _consumer():
    """常驻线程：从队列取任务并运行，并发数由队列消费者数天然限制。"""
    while True:
        item = _TASK_Q.get()
        if item is None:
            _TASK_Q.task_done()
            break
        run_id, cmd = item
        try:
            _run_now(run_id, cmd)
        finally:
            _TASK_Q.task_done()


# 启动常驻消费者线程（数量 = MAX_CONCURRENT，活跃流水线恒为此数）
for _ in range(MAX_CONCURRENT):
    threading.Thread(target=_consumer, daemon=True).start()


# --------------------------------------------------------------------------- #
# 前端页面（内嵌，单文件部署，商务简约风格）
# --------------------------------------------------------------------------- #



# --------------------------------------------------------------------------- #
# 进度解析
# --------------------------------------------------------------------------- #
def parse_line(line, st):
    if "Stage 1" in line:
        st["stage"] = "① 布局分类（双栏/三栏）"; st["progress"] = 8
    elif "2-up" in line and "3-up" in line:
        st["progress"] = 12
    elif "Stage 2" in line:
        st["stage"] = "② 高分 OCR（分栏识别校名/届次）"; st["progress"] = 15
    elif "ocr done" in line:
        m = re.search(r"ocr done (\d+)/(\d+)", line)
        if m:
            x, n = int(m.group(1)), int(m.group(2))
            st["progress"] = 15 + (55 * x / n) if n else 15
    elif "strips ->" in line:
        st["progress"] = 72
    elif "Stage 3" in line:
        st["stage"] = "③ 检测试卷套数 / 起止页"; st["progress"] = 75
    elif "TOTAL EXAMS" in line:
        m = re.search(r"TOTAL EXAMS:\s*(\d+)", line)
        if m: st["exams"] = int(m.group(1))
    elif "saved" in line and "exam_sets" in line:
        st["progress"] = 82
    elif "Stage 4" in line:
        st["stage"] = "④ 拆分成单栏 PDF"; st["progress"] = 85
    elif line.startswith("[") and ".pdf" in line and "/" in line:
        m = re.search(r"\[(\d+)/(\d+)\]", line)
        if m:
            i, t = int(m.group(1)), int(m.group(2))
            st["progress"] = 85 + (13 * i / t) if t else 85
    elif "DONE." in line:
        st["progress"] = 100; st["stage"] = "完成"
        m = re.search(r"(\d+) exam PDFs,\s*(\d+) total pages", line)
        if m:
            st["summary"] = {"exams": int(m.group(1)),
                             "pages": int(m.group(2)),
                             "out_dir": st["out_dir"]}


# --------------------------------------------------------------------------- #
# FastAPI
# --------------------------------------------------------------------------- #
app = FastAPI()
app.mount("/static", StaticFiles(directory=os.path.join(BASE, "static")), name="static")


# --------------------------------------------------------------------------- #
# 版式校正（UI 直改，替代手写 layout_correct JSON）
# --------------------------------------------------------------------------- #
_LAYOUT_VALID = {"1-up", "2-up", "3-up"}


def _ideal_cuts(layout):
    """Ideal column windows for a corrected page, with the same small overlap
    stage 1 uses, so no glyph is shaved off at the seam."""
    ov = 0.006
    if layout == "1-up":
        return [(0.0, 1.0)]
    if layout == "2-up":
        return [(0.0, 0.5 + ov), (0.5 - ov, 1.0)]
    if layout == "3-up":
        return [(0.0, 1 / 3 + ov), (1 / 3 - ov, 2 / 3 + ov), (2 / 3 - ov, 1.0)]
    return [(0.0, 1.0)]


def _ocr_page_columns(page, cuts, workdir):
    """OCR the header band of each column of one page (same recipe as stage 2)."""
    import fitz
    from PIL import Image
    if SKILL_DIR not in sys.path:
        sys.path.insert(0, SKILL_DIR)
    from common import tesseract_env

    exe, env = tesseract_env()
    dpi, top_frac, cap_long = 250, 0.60, 10000
    base = dpi / 72.0
    long_pts = max(page.rect.width, page.rect.height)
    s = base
    if long_pts * base > cap_long:
        s = cap_long / long_pts
    try:
        pix = page.get_pixmap(matrix=fitz.Matrix(s, s))
    except Exception:  # noqa: BLE001
        pix = page.get_pixmap(matrix=fitz.Matrix(1, 1))
    img = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    w, h = img.size

    def ocr_region(x0, x1):
        crop = img.crop((int(w * x0), 0, int(w * x1), int(h * top_frac)))
        png = os.path.join(workdir, "_corr_r.png")
        out = os.path.join(workdir, "_corr_r")
        crop.save(png)
        subprocess.run([exe, png, out, "-l", "chi_sim+eng", "--psm", "6"],
                       check=True, env=env, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL)
        with open(out + ".txt", encoding="utf-8") as f:
            return f.read()

    cols = [ocr_region(x0, x1) for (x0, x1) in cuts]
    full = "" if len(cuts) == 1 else ocr_region(0.0, 1.0)
    return {"col": cols, "full": full}


def _apply_corrections(run_id, corrections):
    """Patch layout for given pages, re-OCR those pages, then re-detect and
    re-split the affected task (no full-book OCR rerun)."""
    st = RUNS.get(run_id)
    if not st:
        return {"ok": False, "error": "未知任务"}
    workdir, src, out_dir = st.get("workdir"), st.get("src"), st.get("out_dir")
    if not workdir or not src or not out_dir:
        return {"ok": False, "error": "任务缺少运行目录"}
    if st["status"] not in ("done", "error"):
        return {"ok": False, "error": "任务尚未完成，不能校正"}

    fixed = []
    for c in corrections or []:
        try:
            p = int(c.get("page"))
        except (TypeError, ValueError):
            continue
        lay = str(c.get("layout", "")).strip()
        if lay in _LAYOUT_VALID:
            fixed.append((p, lay))
    if not fixed:
        return {"ok": False, "error": "没有有效的校正项"}

    layout_path = os.path.join(workdir, "layout_class.json")
    if not os.path.isfile(layout_path):
        return {"ok": False, "error": "找不到版式数据"}
    rows = json.load(open(layout_path, encoding="utf-8"))
    by_page = {r["page"]: r for r in rows}
    missing = [p for p, _ in fixed if p not in by_page]
    if missing:
        return {"ok": False,
                "error": "以下页码不在本任务范围内：" + ",".join(map(str, sorted(missing)))}

    for p, lay in fixed:
        by_page[p]["layout"] = lay
        by_page[p]["cuts"] = [[round(a, 4), round(b, 4)] for a, b in _ideal_cuts(lay)]
        by_page[p]["corrected"] = True
    json.dump(rows, open(layout_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    # Re-OCR only the corrected pages with their new column windows.
    strips_path = os.path.join(workdir, "strips.json")
    strips = {}
    if os.path.isfile(strips_path):
        strips = json.load(open(strips_path, encoding="utf-8"))
    try:
        import fitz as _fitz
        doc = _fitz.open(src)
        for p, lay in fixed:
            strips[str(p)] = _ocr_page_columns(doc[p - 1], _ideal_cuts(lay), workdir)
        doc.close()
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "校正页 OCR 失败：" + str(e)}
    json.dump(strips, open(strips_path, "w", encoding="utf-8"),
              ensure_ascii=False, indent=1)

    if SKILL_DIR not in sys.path:
        sys.path.insert(0, SKILL_DIR)
    try:
        from stage3_extract import run_extract
        from stage4_split import run_split
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "无法加载拆分模块：" + str(e)}

    try:
        run_extract(workdir, correct=None)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "重新检测试卷失败：" + str(e)}

    if os.path.isdir(out_dir):
        for f in os.listdir(out_dir):
            if f.endswith(".pdf") or f == "_manifest.json":
                try:
                    os.remove(os.path.join(out_dir, f))
                except OSError:
                    pass
    try:
        run_split(src, workdir, out_dir, dpi=200)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "重新拆分失败：" + str(e)}

    summary = None
    manifest = os.path.join(out_dir, "_manifest.json")
    if os.path.isfile(manifest):
        try:
            m = json.load(open(manifest, encoding="utf-8"))
            total = sum(e.get("pdf_pages", 0) for e in m)
            summary = {"exams": len(m), "pages": total, "out_dir": out_dir}
        except Exception:  # noqa: BLE001
            pass
    with RUNS_LOCK:
        st["exams"] = summary.get("exams") if summary else None
        st["summary"] = summary
        st["status"] = "done"
    return {"ok": True, "summary": summary}


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "static", "index.html"), media_type="text/html")




@app.post("/api/upload-preview")
async def api_upload_preview(pdf: UploadFile = File(...)):
    """流式生成缩略图和适合大图查看器的页面预览。"""
    filename = pdf.filename or ""
    if not filename.lower().endswith(".pdf"):
        return JSONResponse({"error": "请上传 PDF 文件"}, status_code=400)

    max_preview_bytes = 512 * 1024 * 1024
    preview_dir = os.path.join(RUNS_DIR, "_preview_uploads")
    os.makedirs(preview_dir, exist_ok=True)
    temp_path = os.path.join(preview_dir, uuid.uuid4().hex + ".pdf")
    written = 0
    doc = None
    try:
        with open(temp_path, "wb") as temp_file:
            while True:
                chunk = await pdf.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_preview_bytes:
                    return JSONResponse(
                        {"error": "文件超过 512 MB，暂不生成提交前预览；仍可直接开始处理。"},
                        status_code=413,
                    )
                temp_file.write(chunk)

        import fitz as _fitz
        doc = _fitz.open(temp_path)
        page_count = doc.page_count
        previews = []
        for index in range(min(page_count, 8)):
            page = doc[index]
            long_side = max(page.rect.width, page.rect.height) or 1
            thumb_scale = min(220.0 / long_side, 1.0)
            viewer_scale = min(1120.0 / long_side, 2.2)
            thumb = page.get_pixmap(matrix=_fitz.Matrix(thumb_scale, thumb_scale), alpha=False)
            viewer = page.get_pixmap(matrix=_fitz.Matrix(viewer_scale, viewer_scale), alpha=False)
            thumb_data = "data:image/png;base64," + base64.b64encode(thumb.tobytes("png")).decode("ascii")
            previews.append({
                "page": index + 1,
                "thumbnail": thumb_data,
                "image": thumb_data,
                "full_image": "data:image/png;base64," + base64.b64encode(viewer.tobytes("png")).decode("ascii"),
            })
        return {
            "page_count": page_count,
            "previews": previews,
            "preview_limit": 8,
            "uploaded_bytes": written,
        }
    except Exception as exc:  # noqa: BLE001
        return JSONResponse({"error": "无法生成 PDF 预览：" + str(exc)}, status_code=400)
    finally:
        if doc is not None:
            doc.close()
        await pdf.close()
        try:
            if os.path.isfile(temp_path):
                os.remove(temp_path)
        except OSError:
            pass


@app.post("/api/run")
async def api_run(
    pdf: list[UploadFile] = File(...),
    skip: list[int] = Form([]),
    out_dir: str = Form(""),
):
    if not isinstance(pdf, list):
        pdf = [pdf]
    if not pdf:
        return JSONResponse({"error": "请至少上传一个 PDF 文件"}, status_code=400)
    for p in pdf:
        if not p.filename.lower().endswith(".pdf"):
            return JSONResponse({"error": "只能上传 PDF 文件"}, status_code=400)

    batch_id = uuid.uuid4().hex[:12]
    batch_dir = os.path.join(RUNS_DIR, batch_id)
    os.makedirs(batch_dir, exist_ok=True)

    # 用户输出根目录：浏览器选文件夹时为空（服务端落到临时目录，最终前端写回）；
    # 手动模式为用户输入的绝对路径。
    if out_dir and out_dir.strip():
        out_root = os.path.abspath(out_dir.strip())
    else:
        out_root = os.path.join(batch_dir, "_out")

    run_ids = []
    names = []
    for idx, p in enumerate(pdf):
        rid = uuid.uuid4().hex[:12]
        run_dir = os.path.join(batch_dir, rid)
        os.makedirs(run_dir, exist_ok=True)
        src = os.path.join(run_dir, "source.pdf")
        with open(src, "wb") as f:
            f.write(await p.read())

        src_name = os.path.splitext(p.filename)[0]
        task_out = os.path.join(out_root, src_name)
        os.makedirs(task_out, exist_ok=True)
        workdir = os.path.join(run_dir, "_work")
        sk = skip[idx] if idx < len(skip) else 2

        if getattr(sys, "frozen", False):
            # 打包后由同一 exe 以 worker 模式运行四阶段流水线。
            cmd = [sys.executable, "--pipeline-worker",
                   "--pdf", src, "--out", task_out, "--workdir", workdir,
                   "--skip", str(sk)]
        else:
            cmd = [sys.executable, os.path.join(SKILL_DIR, "run_pipeline.py"),
                   "--pdf", src, "--out", task_out, "--workdir", workdir,
                   "--skip", str(sk)]

        RUNS[rid] = {"name": p.filename, "status": "queued", "stage": "排队中",
                     "progress": 0, "log": [], "exams": None, "summary": None,
                     "out_dir": task_out, "src": src, "workdir": workdir,
                     "error": None}
        _TASK_Q.put((rid, cmd))
        run_ids.append(rid)
        names.append(p.filename)

    BATCHES[batch_id] = run_ids
    return {"batch_id": batch_id, "run_ids": run_ids, "names": names,
            "concurrent": MAX_CONCURRENT}


@app.get("/api/batch")
def api_batch(batch_id: str):
    ids = BATCHES.get(batch_id)
    if not ids:
        return JSONResponse({"error": "未知批次"}, status_code=404)
    out = []
    with RUNS_LOCK:
        for rid in ids:
            st = RUNS.get(rid)
            if not st:
                continue
            out.append({
                "run_id": rid, "name": st.get("name"),
                "status": st["status"], "stage": st["stage"],
                "progress": st["progress"], "summary": st.get("summary"),
                "error": st.get("error"),
                "out_dir": st.get("out_dir"),
            })
    return {"tasks": out, "concurrent_cap": MAX_CONCURRENT}


@app.get("/api/status")
def api_status(run_id: str):
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    with RUNS_LOCK:
        return {
            "status": st["status"], "stage": st["stage"],
            "progress": st["progress"], "log": st["log"][-500:],
            "exams": st.get("exams"), "summary": st.get("summary"),
            "error": st.get("error"),
        }


@app.get("/api/files")
def api_files(run_id: str):
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    od = st.get("out_dir")
    if not od or not os.path.isdir(od):
        return JSONResponse({"error": "输出目录不存在"}, status_code=404)
    names = sorted(f for f in os.listdir(od)
                   if f.endswith(".pdf") or f == "_manifest.json")
    return {"files": names}


@app.get("/api/exams")
def api_exams(run_id: str):
    """返回该任务拆分出的每套试卷清单（来自 _manifest.json），供前端结果预览。"""
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    od = st.get("out_dir")
    if not od or not os.path.isdir(od):
        return JSONResponse({"error": "输出目录不存在"}, status_code=404)
    manifest = os.path.join(od, "_manifest.json")
    if not os.path.isfile(manifest):
        return {"exams": []}
    try:
        with open(manifest, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:  # noqa
        return JSONResponse({"error": "清单解析失败：" + str(e)}, status_code=500)
    exams = []
    for e in data:
        exams.append({
            "index": e.get("index"),
            "name": e.get("name") or e.get("file") or "",
            "file": e.get("file") or e.get("name") or "",
            "src_start": e.get("src_start"),
            "src_end": e.get("src_end"),
            "pdf_pages": e.get("pdf_pages"),
        })
    return {"exams": exams}


@app.get("/api/file")
def api_file(run_id: str, name: str, inline: bool = False):
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    od = st.get("out_dir")
    if not od:
        return JSONResponse({"error": "输出目录不存在"}, status_code=404)
    # 防目录穿越
    safe = os.path.basename(name)
    p = os.path.join(od, safe)
    if not os.path.isfile(p):
        return JSONResponse({"error": "文件不存在"}, status_code=404)
    return FileResponse(
        p, filename=safe,
        content_disposition_type="inline" if inline else "attachment",
    )


@app.post("/api/pick-folder")
def api_pick_folder():
    """调用独立 STA PowerShell 脚本打开资源管理器样式的文件夹选择窗口。"""
    if os.name != "nt":
        return JSONResponse({"error": "文件夹选择仅支持 Windows"}, status_code=400)
    picker_script = os.path.join(BASE, "folder_picker.ps1")
    if not os.path.isfile(picker_script):
        return JSONResponse({"error": "缺少 Windows 文件夹选择组件。"}, status_code=500)
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", picker_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except subprocess.TimeoutExpired:
        return JSONResponse({"error": "文件夹选择窗口超时，请重新选择。"}, status_code=504)

    encoded_path = result.stdout.strip()
    if result.returncode != 0:
        detail = result.stderr.strip() or "Windows 未能启动文件夹选择窗口。"
        return JSONResponse({"error": "无法打开文件夹选择窗口：" + detail}, status_code=500)
    if not encoded_path:
        return {"cancelled": True}
    try:
        # PowerShell returns Base64(UTF-16LE); use ASCII-only transport to avoid console code pages.
        path = base64.b64decode(encoded_path).decode("utf-16-le")
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "文件夹路径返回格式无效。"}, status_code=500)
    return {"path": os.path.abspath(path)}


@app.get("/api/open")
def api_open(run_id: str):
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    od = st.get("out_dir")
    if od and os.path.isdir(od):
        try:
            os.startfile(od)  # Windows: open the folder in Explorer
        except Exception as e:  # noqa
            return JSONResponse({"error": str(e)}, status_code=500)
        return {"ok": True}
    return JSONResponse({"error": "输出目录不存在"}, status_code=404)


@app.get("/api/layout")
def api_layout(run_id: str):
    """返回该任务中被判为 1-up（可疑：横版页未拆分）的页码列表。"""
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    workdir = st.get("workdir")
    if not workdir:
        return {"pages": []}
    lp = os.path.join(workdir, "layout_class.json")
    if not os.path.isfile(lp):
        return {"pages": []}
    try:
        rows = json.load(open(lp, encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {"pages": []}
    pages = [{"page": d["page"], "layout": d.get("layout", "1-up")}
             for d in rows if d.get("layout") == "1-up"]
    return {"pages": pages}


@app.get("/api/page-preview")
def api_page_preview(run_id: str, page: int):
    """渲染源 PDF 某一页为图片，供版式校正时查看原页。"""
    st = RUNS.get(run_id)
    if not st:
        return JSONResponse({"error": "未知任务"}, status_code=404)
    src = st.get("src")
    if not src or not os.path.isfile(src):
        return JSONResponse({"error": "源文件不存在"}, status_code=404)
    try:
        import fitz as _fitz
        doc = _fitz.open(src)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "无法打开源文件：" + str(e)}, status_code=500)
    if page < 1 or page > doc.page_count:
        doc.close()
        return JSONResponse({"error": "页码超出范围"}, status_code=404)
    pg = doc[page - 1]
    long_pts = max(pg.rect.width, pg.rect.height)
    s = min(1500.0 / long_pts, 3.0)
    try:
        pix = pg.get_pixmap(matrix=_fitz.Matrix(s, s))
    except Exception:  # noqa: BLE001
        pix = pg.get_pixmap(matrix=_fitz.Matrix(1, 1))
    doc.close()
    return Response(content=pix.tobytes("png"), media_type="image/png")


@app.post("/api/correct-layout")
async def api_correct_layout(request: Request):
    """接收 UI 提交的版式校正 {run_id, corrections:[{page, layout}]} 并重新拆分。"""
    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": "请求格式错误"}, status_code=400)
    run_id = data.get("run_id")
    if not run_id or run_id not in RUNS:
        return JSONResponse({"ok": False, "error": "未知任务"}, status_code=404)
    from starlette.concurrency import run_in_threadpool
    return await run_in_threadpool(_apply_corrections, run_id, data.get("corrections") or [])


# --------------------------------------------------------------------------- #
# 启动
# --------------------------------------------------------------------------- #
def find_free_port(start=8000, end=8020):
    for p in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    return start


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--pipeline-worker":
        # 移除内部 worker 标记，再把剩余参数交给既有流水线 CLI。
        sys.argv = [sys.argv[0], *sys.argv[2:]]
        sys.path.insert(0, SKILL_DIR)
        from run_pipeline import main as pipeline_main
        pipeline_main()
        return

    port = find_free_port()
    url = f"http://127.0.0.1:{port}/"
    print("=" * 60)
    print("试卷合订本拆分器已启动")
    print(f"  本地地址：{url}")
    print("  关闭：Ctrl + C")
    print("=" * 60)
    # Windows 自动打开浏览器
    try:
        import webbrowser
        webbrowser.open(url, new=2)
    except Exception:  # noqa
        pass
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")


if __name__ == "__main__":
    main()
