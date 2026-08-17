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
import json
import uuid
import socket
import subprocess
import threading
import queue
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, Request, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, Response
import uvicorn

BASE = os.path.dirname(os.path.abspath(__file__))
RUNS_DIR = os.path.join(BASE, "runs")
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
HTML = r"""<!doctype html>
<html lang="zh-CN" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>试卷合订本拆分器</title>
<style>
  :root{
    /* 纸感设计系统（Cloud Dancer 云舞者 × 纸张肌理），与主应用一致 */
    --bg:#f4f0e8;
    --bg-grad:
      radial-gradient(rgba(120,100,70,.045) .7px, transparent .8px),
      radial-gradient(1000px 460px at 85% -8%, rgba(169,141,95,.07), transparent 62%),
      radial-gradient(900px 420px at -10% 0%, rgba(143,116,74,.06), transparent 60%),
      #f4f0e8;
    --card:#fdfbf6;
    --card-2:#faf7f0;
    --ink:#3d3a35;
    --muted:#6f6a61;
    --faint:#9b958a;
    --line:#e5dfd3;
    --line-2:#efe9dd;
    --accent:#a98d5f;
    --accent-2:#c3ab7d;
    --accent-grad:linear-gradient(145deg,#b8a077 0%,#9a815a 100%);
    --accent-soft:#ede6d8;
    --accent-ink:#7a6542;
    --ok:#6d8f5e;
    --ok-soft:#eef2e6;
    --err:#b0524d;
    --err-soft:#f7e9e7;
    --warn:#b98a3e;
    --warn-soft:#f8f0dd;
    --wait:#9b958a;
    --shadow:0 1px 3px rgba(90,78,60,.06),0 10px 26px -14px rgba(90,78,60,.16);
    --shadow-sm:0 1px 2px rgba(90,78,60,.06);
    --radius:14px;
    --radius-sm:10px;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,"Liberation Mono",monospace;
    --font:-apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft YaHei","Noto Sans SC",system-ui,sans-serif;
  }
  *{box-sizing:border-box}
  html,body{margin:0;height:100%}
  body{
    background:var(--bg);
    background-image:var(--bg-grad);
    background-size:22px 22px,100% 100%,100% 100%,100% 100%;
    background-attachment:fixed;
    color:var(--ink);
    font:14px/1.6 var(--font);
    -webkit-font-smoothing:antialiased;
    text-rendering:optimizeLegibility;
  }
  ::selection{background:var(--accent-soft)}
  a{color:var(--accent);text-decoration:none}
  a:hover{text-decoration:underline}

  /* ---------- top bar ---------- */
  .topbar{
    position:sticky;top:0;z-index:20;
    display:flex;align-items:center;gap:12px;
    padding:14px 22px;
    background:color-mix(in srgb, var(--card) 78%, transparent);
    backdrop-filter:saturate(1.4) blur(12px);
    border-bottom:1px solid var(--line);
  }
  .brand{display:flex;align-items:center;gap:11px;font-weight:700;font-size:15.5px;letter-spacing:-.2px}
  .logo{
    width:34px;height:34px;border-radius:10px;flex:none;
    background:var(--accent-grad);
    display:flex;align-items:center;justify-content:center;color:#fff;
    box-shadow:0 6px 16px -6px var(--accent);
  }
  .brand small{display:block;font-weight:500;font-size:11px;color:var(--muted);letter-spacing:0;margin-top:1px}
  .topbar .spacer{flex:1}
  .pill{
    display:inline-flex;align-items:center;gap:6px;
    font-size:11.5px;color:var(--muted);
    background:var(--card-2);border:1px solid var(--line);
    padding:5px 10px;border-radius:999px;
  }
  .pill .dot{width:7px;height:7px;border-radius:50%;background:var(--ok)}
  .iconbtn{
    width:36px;height:36px;flex:none;border-radius:10px;
    border:1px solid var(--line);background:var(--card);
    color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;
    transition:.15s;
  }
  .iconbtn:hover{color:var(--ink);border-color:var(--accent);background:var(--accent-soft)}

  /* ---------- layout ---------- */
  .wrap{max-width:840px;margin:0 auto;padding:30px 18px 80px}
  .hero{text-align:center;margin:18px 0 26px}
  .hero h1{margin:0;font-size:27px;font-weight:750;letter-spacing:-.6px}
  .hero p{margin:9px 0 0;color:var(--muted);font-size:14px}
  .steps{display:flex;justify-content:center;gap:8px;margin-top:18px;flex-wrap:wrap}
  .steps .st{
    display:inline-flex;align-items:center;gap:7px;
    font-size:12px;color:var(--muted);
    background:var(--card);border:1px solid var(--line);
    padding:6px 12px;border-radius:999px;box-shadow:var(--shadow-sm)
  }
  .steps .st b{
    width:18px;height:18px;border-radius:50%;flex:none;
    background:var(--accent-soft);color:var(--accent-ink);
    display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700
  }

  .card{
    background:var(--card);border:1px solid var(--line);
    border-radius:var(--radius);padding:24px;margin-bottom:18px;box-shadow:var(--shadow)
  }
  .card .ctitle{display:flex;align-items:center;gap:9px;font-size:14px;font-weight:650;margin:0 0 16px}
  .card .ctitle .tag{margin-left:auto;font-size:11.5px;color:var(--muted);font-weight:500}

  /* ---------- drop zone ---------- */
  .drop{
    border:1.6px dashed var(--line);
    border-radius:var(--radius-sm);
    padding:34px 20px;text-align:center;cursor:pointer;
    background:var(--card-2);transition:.18s;position:relative;overflow:hidden
  }
  .drop:hover{border-color:var(--accent);background:var(--accent-soft)}
  .drop.over{border-color:var(--accent);background:var(--accent-soft);transform:scale(1.005)}
  .drop .art{margin:0 auto 12px;width:60px;height:60px;display:flex;align-items:center;justify-content:center;
    border-radius:16px;background:var(--accent-grad);color:#fff;box-shadow:0 10px 24px -10px var(--accent)}
  .drop .big{font-size:15.5px;font-weight:650}
  .drop .big em{font-style:normal;color:var(--accent-ink);text-decoration:underline}
  .drop .sub{color:var(--muted);font-size:12.5px;margin-top:5px}

  .filelist{margin-top:14px;display:flex;flex-direction:column;gap:9px}
  .file{
    display:flex;align-items:center;gap:11px;padding:10px 12px;
    background:var(--card-2);border:1px solid var(--line);border-radius:11px
  }
  .file .fic{width:34px;height:34px;border-radius:9px;flex:none;background:var(--accent-soft);color:var(--accent-ink);
    display:flex;align-items:center;justify-content:center}
  .file .fn{flex:1;min-width:0}
  .file .fn .nm{font-weight:550;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .file .fn .sz{font-size:11.5px;color:var(--faint);margin-top:1px}
  .file .sk{display:flex;align-items:center;gap:5px;font-size:11.5px;color:var(--faint);flex:none;white-space:nowrap}
  .file .sk input{width:46px;border:1px solid var(--line);border-radius:7px;padding:4px 6px;background:var(--card);color:var(--ink);font-size:12px;text-align:center}
  .file .rm{width:28px;height:28px;flex:none;border-radius:8px;border:1px solid transparent;background:transparent;
    color:var(--faint);cursor:pointer;font-size:15px;display:flex;align-items:center;justify-content:center;transition:.15s}
  .file .rm:hover{color:var(--err);background:var(--err-soft)}

  /* ---------- options ---------- */
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:18px}
  .field{display:flex;flex-direction:column;gap:6px;font-size:12.5px;color:var(--muted)}
  .field > label{font-weight:550;color:var(--ink)}
  .outwrap{display:flex;align-items:stretch;border:1px solid var(--line);border-radius:11px;overflow:hidden;background:var(--card)}
  .outwrap:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
  .outwrap input{flex:1;border:0;padding:11px 12px;font-size:13.5px;outline:none;background:transparent;color:var(--ink);min-width:0}
  .outwrap .browse{border:0;border-left:1px solid var(--line);background:var(--card-2);padding:0 16px;
    font-size:12.5px;font-weight:600;color:var(--accent-ink);cursor:pointer;transition:.15s}
  .outwrap .browse:hover{background:var(--accent-soft)}
  .outwrap .browse:disabled{opacity:.45;cursor:not-allowed}
  .field .hint{font-size:11.5px;color:var(--faint)}

  .run{
    margin-top:22px;width:100%;padding:14px;border:0;border-radius:12px;
    background:var(--accent-grad);color:#fff;font-size:15px;font-weight:650;cursor:pointer;
    transition:.16s;box-shadow:0 10px 26px -10px var(--accent);display:flex;align-items:center;justify-content:center;gap:9px
  }
  .run:hover{filter:brightness(1.05);transform:translateY(-1px)}
  .run:disabled{opacity:.55;cursor:not-allowed;transform:none;box-shadow:none}
  .spinner{width:16px;height:16px;border:2.4px solid rgba(255,255,255,.45);border-top-color:#fff;border-radius:50%;animation:spin .7s linear infinite}
  @keyframes spin{to{transform:rotate(360deg)}}

  .err{margin-top:13px;color:var(--err);background:var(--err-soft);border:1px solid color-mix(in srgb,var(--err) 35%,transparent);
    padding:11px 13px;border-radius:11px;font-size:13px;display:flex;gap:9px;align-items:flex-start}
  .err svg{flex:none;margin-top:1px}

  /* ---------- processing ---------- */
  .summary{
    display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:12px;
    background:var(--card-2);border:1px solid var(--line)
  }
  .summary .big{font-size:22px;font-weight:750;line-height:1}
  .summary .lbl{font-size:12px;color:var(--muted);margin-top:3px}
  .summary .barwrap{flex:1;min-width:0}
  .summary .bar{height:9px;background:var(--line-2);border-radius:999px;overflow:hidden}
  .summary .fill{height:100%;width:0;background:var(--accent-grad);transition:width .4s}
  .summary .note{font-size:12.5px;color:var(--muted);margin-top:7px;display:flex;gap:10px}
  .summary .save-all{margin-left:auto;flex:none;padding:9px 15px;border-radius:10px;border:1px solid var(--accent);
    background:var(--accent-soft);color:var(--accent-ink);font-weight:600;font-size:13px;cursor:pointer;transition:.15s}
  .summary .save-all:hover{background:var(--accent);color:#fff}
  .summary .save-all:disabled{opacity:.5;cursor:not-allowed}

  .tasks{display:flex;flex-direction:column;gap:13px;margin-top:16px}
  .task{border:1px solid var(--line);border-radius:13px;background:var(--card);overflow:hidden;box-shadow:var(--shadow-sm)}
  .task.done{border-color:color-mix(in srgb,var(--ok) 35%,var(--line))}
  .task.error{border-color:color-mix(in srgb,var(--err) 40%,var(--line))}
  .task .thead{display:flex;align-items:center;gap:11px;padding:13px 15px}
  .task .fic{width:32px;height:32px;border-radius:9px;flex:none;background:var(--card-2);border:1px solid var(--line);
    color:var(--muted);display:flex;align-items:center;justify-content:center}
  .task .tname{flex:1;min-width:0;font-weight:600;font-size:13.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .task .sub{font-size:11.5px;color:var(--faint);margin-top:1px}
  .badge{padding:3px 10px;border-radius:999px;font-size:11px;font-weight:650;color:#fff;flex:none}
  .badge.queued{background:var(--wait)}
  .badge.running{background:var(--accent)}
  .badge.done{background:var(--ok)}
  .badge.error{background:var(--err)}
  .task .tbar{height:5px;background:var(--line-2)}
  .task .tbar .f{height:100%;width:0;background:var(--accent-grad);transition:width .35s}
  .task .tbar .f.done{background:var(--ok)}
  .task .tmeta{display:flex;align-items:center;gap:10px;padding:9px 15px;font-size:12px;color:var(--muted)}
  .task .tmeta .pct{font-weight:650;color:var(--ink);font-variant-numeric:tabular-nums}
  .task .tmeta .stage{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex:1;min-width:0}
  .task .tmeta .logbtn{color:var(--accent-ink);cursor:pointer;font-weight:550;flex:none}
  .task .tmeta .logbtn:hover{text-decoration:underline}
  .task .tmeta .save-one{color:var(--accent-ink);cursor:pointer;font-weight:600;flex:none}
  .task .tmeta .save-one:hover{text-decoration:underline}

  .log{max-height:0;overflow:hidden;transition:max-height .25s ease;border-top:0}
  .log.open{max-height:300px;border-top:1px solid var(--line-2)}
  .log pre{margin:0;padding:12px 15px;font:11.5px/1.5 var(--mono);color:var(--muted);
    overflow:auto;max-height:300px;white-space:pre-wrap;word-break:break-all;background:var(--card-2)}

  /* ---------- results ---------- */
  .banner{
    display:flex;align-items:center;gap:14px;padding:16px 18px;border-radius:13px;margin-bottom:16px;
    background:var(--ok-soft);border:1px solid color-mix(in srgb,var(--ok) 35%,transparent)
  }
  .banner .bic{width:40px;height:40px;border-radius:11px;flex:none;background:var(--ok);color:#fff;
    display:flex;align-items:center;justify-content:center}
  .banner .bt{font-weight:700;font-size:15px}
  .banner .bs{font-size:12.5px;color:var(--muted);margin-top:2px}
  .banner .bact{margin-left:auto;flex:none;display:flex;gap:9px}
  .btn{display:inline-flex;align-items:center;gap:7px;padding:9px 14px;border-radius:10px;font-weight:600;font-size:12.5px;cursor:pointer;border:1px solid var(--line);background:var(--card);color:var(--ink);transition:.15s}
  .btn:hover{border-color:var(--accent);color:var(--accent-ink);background:var(--accent-soft)}
  .btn.primary{background:var(--accent-grad);color:#fff;border:0;box-shadow:0 8px 20px -8px var(--accent)}
  .btn.primary:hover{filter:brightness(1.05);color:#fff}
  .btn:disabled{opacity:.5;cursor:not-allowed}

  .exams{margin-top:13px;border:1px solid var(--line);border-radius:12px;overflow:hidden}
  .exams .ehead{display:flex;align-items:center;gap:8px;padding:10px 14px;background:var(--card-2);
    font-size:12.5px;font-weight:600;color:var(--muted);border-bottom:1px solid var(--line)}
  .exams .ehead .cnt{margin-left:auto;color:var(--accent-ink);background:var(--accent-soft);padding:2px 9px;border-radius:999px;font-size:11.5px}
  .erow{display:flex;align-items:center;gap:11px;padding:10px 14px;border-bottom:1px solid var(--line-2)}
  .erow:last-child{border-bottom:0}
  .erow:hover{background:var(--card-2)}
  .erow .idx{width:22px;height:22px;flex:none;border-radius:6px;background:var(--accent-soft);color:var(--accent-ink);
    font-size:11px;font-weight:700;display:flex;align-items:center;justify-content:center}
  .erow .en{flex:1;min-width:0;font-size:13px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .erow .meta{display:flex;gap:7px;flex:none}
  .erow .chip2{font-size:11px;color:var(--muted);background:var(--card-2);border:1px solid var(--line);
    padding:2px 8px;border-radius:7px;white-space:nowrap}
  .erow .dl{flex:none;width:30px;height:30px;border-radius:8px;border:1px solid var(--line);background:var(--card);
    color:var(--muted);cursor:pointer;display:flex;align-items:center;justify-content:center;transition:.15s}
  .erow .dl:hover{color:var(--accent-ink);border-color:var(--accent);background:var(--accent-soft)}

  /* ---------- layout correction ---------- */
  .corr{border-top:1px solid var(--line-2);margin-top:12px;padding-top:12px}
  .corr-head{display:flex;align-items:center;gap:9px;flex-wrap:wrap;font-size:12.5px;font-weight:650;margin-bottom:4px}
  .corr-head .hint{font-weight:400;color:var(--faint);font-size:11.5px}
  .corr-row{display:flex;align-items:center;gap:10px;padding:6px 2px;font-size:12.5px;flex-wrap:wrap}
  .corr-row .corr-pg{width:72px;flex:none;font-weight:600}
  .corr-row .chip2{margin-left:auto}
  .corr-row select,.corr-add select{border:1px solid var(--line);border-radius:8px;padding:5px 8px;background:var(--card);color:var(--ink);font-size:12.5px}
  .corr-row a.lnk,.corr-add a.lnk{color:var(--accent-ink);font-weight:550;cursor:pointer}
  .corr-row a.lnk:hover{text-decoration:underline}
  .corr-add{display:flex;align-items:center;gap:8px;margin-top:10px;flex-wrap:wrap}
  .corr-add input{width:84px;border:1px solid var(--line);border-radius:8px;padding:6px 8px;background:var(--card);color:var(--ink);font-size:12.5px}
  .corr-add .btn{margin-left:auto}
  #pv{position:fixed;inset:0;background:rgba(10,14,26,.74);display:none;align-items:center;justify-content:center;z-index:60;cursor:zoom-out}
  #pv img{max-width:94vw;max-height:92vh;border-radius:10px;box-shadow:var(--shadow)}

  .reset{display:flex;justify-content:center;margin-top:6px}
  .skeleton{color:var(--faint);font-size:12.5px;padding:14px;text-align:center}

  /* ---------- toast ---------- */
  #toasts{position:fixed;right:18px;bottom:18px;z-index:50;display:flex;flex-direction:column;gap:10px;max-width:340px}
  .toast{display:flex;gap:10px;align-items:flex-start;padding:12px 14px;border-radius:11px;background:var(--card);
    border:1px solid var(--line);box-shadow:var(--shadow);font-size:13px;animation:tin .25s ease}
  .toast.err{border-color:color-mix(in srgb,var(--err) 45%,var(--line))}
  .toast.ok{border-color:color-mix(in srgb,var(--ok) 45%,var(--line))}
  .toast svg{flex:none;margin-top:1px}
  @keyframes tin{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
  .toast .x{margin-left:auto;color:var(--faint);cursor:pointer;flex:none}

  footer{text-align:center;color:var(--faint);font-size:11.5px;margin-top:14px}
  @media (max-width:600px){
    .grid2{grid-template-columns:1fr}
    .steps .st{font-size:11px;padding:5px 9px}
    .erow .meta{display:none}
    .wrap{padding:18px 12px 70px}
  }
</style>
</head>
<body>
  <div class="topbar">
    <div class="brand">
      <span class="logo">
        <svg width="19" height="19" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z"/><path d="M9 13h6"/><path d="M9 17h6"/></svg>
      </span>
      <span>试卷合订本拆分器<small>混排扫描卷 → 每套单栏独立 PDF</small></span>
    </div>
    <span class="spacer"></span>
    <span class="pill"><span class="dot"></span>本地运行 · 数据不上传</span>
  </div>

  <div class="wrap">
    <div class="hero">
      <h1>把合订本拆成一套一套的试卷</h1>
      <p>自动识别双栏 / 三栏版式与每套试卷的起止，输出单栏高清 PDF</p>
      <div class="steps">
        <span class="st"><b>1</b>版式识别</span>
        <span class="st"><b>2</b>分栏 OCR</span>
        <span class="st"><b>3</b>检测套数</span>
        <span class="st"><b>4</b>拆分导出</span>
      </div>
    </div>

    <!-- 上传卡片 -->
    <section class="card" id="inputCard">
      <h2 class="ctitle">上传合订本
        <span class="tag" id="rcTag">最多并行 3 份</span>
      </h2>
      <div id="drop" class="drop">
        <div class="art">
          <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" y1="3" x2="12" y2="15"/></svg>
        </div>
        <div class="big">点击选择，或把 PDF <em>拖拽到此处</em></div>
        <div class="sub">支持一次多选多份合订本 · 仅本地处理</div>
        <input type="file" id="file" accept="application/pdf,.pdf" multiple hidden>
      </div>
      <div id="chips" class="filelist"></div>

      <div class="grid2">
        <div class="field" style="grid-column:1 / -1">
          <label>输出文件夹</label>
          <div class="outwrap" id="outWrap">
            <input type="text" id="outDir" placeholder="点击右侧 Browse 选择，或直接输入路径">
            <button type="button" class="browse" id="browseBtn">Browse</button>
          </div>
          <span class="hint" id="pickHint"></span>
        </div>
      </div>

      <button id="runBtn" class="run" disabled>
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>
        开始处理
      </button>
      <div id="err" class="err" style="display:none">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>
        <span id="errMsg"></span>
      </div>
    </section>

    <!-- 处理中 -->
    <section class="card" id="processCard" style="display:none">
      <h2 class="ctitle">处理进度
        <span class="tag" id="procTag"></span>
      </h2>
      <div class="summary">
        <div>
          <div class="big" id="overallTxt">0 / 0</div>
          <div class="lbl">已完成</div>
        </div>
        <div class="barwrap">
          <div class="bar"><div id="overallFill" class="fill"></div></div>
          <div class="note" id="overallNote"></div>
        </div>
      </div>
      <div class="tasks" id="tasksCard"></div>
    </section>

    <!-- 结果 -->
    <section class="card" id="resultCard" style="display:none">
      <h2 class="ctitle">拆分结果
        <span class="tag" id="resTag"></span>
      </h2>
      <div id="resultBody"></div>
      <div class="reset">
        <button class="btn" id="newTaskBtn">＋ 处理新文件</button>
      </div>
    </section>

    <footer>本地运行 · 数据不上传服务器 · 最多并行 3 份</footer>
  </div>

  <div id="pv" onclick="this.style.display='none'"><img id="pvImg" alt="原页预览"></div>
  <div id="toasts"></div>

<script>
const $ = id => document.getElementById(id);
let files=[], tasks=[], batchId=null, pollTimer=null;

/* ---------- init ---------- */
function init(){
  $('pickHint').textContent='点击 Browse 由服务端选择文件夹，将显示完整路径';
}
window.addEventListener('DOMContentLoaded', init);

/* ---------- file select ---------- */
$('drop').addEventListener('click', ()=> $('file').click());
$('file').addEventListener('change', e=>{ addFiles(e.target.files); });
['dragover','dragenter'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.add('over');}));
['dragleave','drop'].forEach(ev=>$('drop').addEventListener(ev,e=>{e.preventDefault();$('drop').classList.remove('over');}));
$('drop').addEventListener('drop', e=>{ addFiles(e.dataTransfer.files); });

function fmtMB(b){ return (b/1048576).toFixed(1)+' MB'; }
function addFiles(list){
  for(const f of list){
    if(f.name.toLowerCase().endsWith('.pdf') && !files.some(x=>x.name===f.name && x.size===f.size)){
      files.push({name:f.name, size:f.size, file:f, skip:2});
    }
  }
  renderChips();
}
function renderChips(){
  const box=$('chips'); box.innerHTML='';
  files.forEach((f,i)=>{
    const c=document.createElement('div'); c.className='file';
    c.innerHTML='<span class="fic"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z"/></svg></span>'
      +'<span class="fn"><div class="nm"></div><div class="sz"></div></span>'
      +'<span class="sk">跳过前导页数<input type="number" min="0" max="20" value="'+f.skip+'" title="合订本封面/目录通常无需拆分，默认 2"></span>'
      +'<button class="rm" title="移除">✕</button>';
    c.querySelector('.nm').textContent=f.name;
    c.querySelector('.sz').textContent=fmtMB(f.size);
    c.querySelector('.sk input').addEventListener('change', e=>{
      f.skip=Math.max(0,Math.min(20,parseInt(e.target.value,10)||0));
      e.target.value=f.skip;
    });
    c.querySelector('.rm').addEventListener('click', ()=>{ files.splice(i,1); renderChips(); });
    box.appendChild(c);
  });
  $('runBtn').disabled = files.length===0;
}

/* ---------- output folder ---------- */
$('browseBtn').addEventListener('click', async ()=>{
  $('browseBtn').disabled=true;
  $('pickHint').textContent='请在弹出的文件夹窗口中选择…';
  try{
    const r=await fetch('/api/pick-folder',{method:'POST'});
    const j=await r.json();
    if(j.path){ $('outDir').value=j.path; $('pickHint').textContent='输出文件夹：'+j.path; }
    else if(j.cancelled){ $('pickHint').textContent='已取消选择，可重新 Browse 或直接输入路径'; }
    else if(j.error){ toast('选择文件夹失败：'+j.error,'err'); $('pickHint').textContent='可直接在输入框里填写完整路径'; }
  }catch(e){
    toast('选择文件夹失败：'+e.message,'err');
    $('pickHint').textContent='可直接在输入框里填写完整路径';
  }
  finally{ $('browseBtn').disabled=false; }
});

/* ---------- run ---------- */
function outPath(){ return $('outDir').value.trim(); }
$('runBtn').addEventListener('click', start);

async function start(){
  if(files.length===0) return;
  const p=outPath();
  if(!p){ showErr('请选择或输入输出文件夹'); return; }
  $('err').style.display='none';
  const fd=new FormData();
  for(const f of files) fd.append('pdf', f.file);
  for(const f of files) fd.append('skip', String(f.skip));
  fd.append('out_dir', p);
  $('runBtn').disabled=true;
  $('runBtn').innerHTML='<span class="spinner"></span> 处理中…';
  $('inputCard').style.opacity='.65';
  $('processCard').style.display='block';
  $('resultCard').style.display='none';
  $('tasksCard').innerHTML=''; tasks=[];
  try{
    const r=await fetch('/api/run',{method:'POST',body:fd});
    const j=await r.json();
    if(j.error) throw new Error(j.error);
    batchId=j.batch_id;
    tasks=j.run_ids.map((rid,i)=>({run_id:rid,name:j.names[i],
      status:'queued',progress:0,stage:'排队中',error:null,exams:null,logOpen:false,
      src:j.names[i].replace(/\.pdf$/i,''),summary:null,out_dir:''}));
    $('procTag').textContent = j.names.length+' 份文件';
    renderTasks();
    poll();
  }catch(err){ showErr(err.message); resetBtn(); }
}

function poll(){
  if(pollTimer) clearInterval(pollTimer);
  pollTimer=setInterval(async ()=>{
    try{
      const r=await fetch('/api/batch?batch_id='+encodeURIComponent(batchId));
      const j=await r.json();
      const map={}; j.tasks.forEach(t=> map[t.run_id]=t);
      let done=0;
      tasks.forEach(t=>{
        const s=map[t.run_id];
        if(s){ t.status=s.status; t.progress=s.progress; t.stage=s.stage; t.summary=s.summary; t.error=s.error; t.out_dir=s.out_dir||''; }
        if(t.status==='done'||t.status==='error') done++;
        if(t.status==='done' && !t.exams) loadExams(t);
      });
      renderTasks();
      updateOverall(done);
      refreshOpenLogs();
      if(done===tasks.length){ clearInterval(pollTimer); resetBtn(); showResults(); }
    }catch(e){}
  },1200);
}

function updateOverall(done){
  const total=tasks.length;
  $('overallTxt').textContent=done+' / '+total;
  const pct = total? Math.round(100*done/total):0;
  $('overallFill').style.width=pct+'%';
  const running=tasks.filter(t=>t.status==='running').length;
  const queued=tasks.filter(t=>t.status==='queued').length;
  $('overallNote').innerHTML = (running||queued)
    ? ('运行中 <b style="color:var(--accent-ink)">'+running+'</b> · 排队 <b>'+queued+'</b>')
    : (total? '<span style="color:var(--ok)">✓ 全部处理完成</span>' : '');
}

/* ---------- task rendering ---------- */
function renderTasks(){
  const box=$('tasksCard'); box.innerHTML='';
  tasks.forEach(t=>{
    const el=document.createElement('div'); el.className='task '+(t.status==='done'?'done':t.status==='error'?'error':'');
    const cls = t.status==='done'?'done':t.status==='error'?'error':t.status==='running'?'running':'queued';
    const tx = t.status==='done'?'已完成':t.status==='error'?'出错':t.status==='running'?'运行中':'排队中';
    const pct=Math.round(t.progress||0);
    let saveOne='';
    if(t.status==='done'){
      saveOne = '<span class="save-one" data-rid="'+t.run_id+'">打开目录</span>';
    }
    let examsHtml='';
    if(t.status==='done'){
      if(t.exams===null) examsHtml='<div class="skeleton">读取试卷清单…</div>';
      else if(t.exams.error) examsHtml='<div class="skeleton" style="color:var(--err)">'+t.exams.error+'</div>';
      else if(t.exams.length===0) examsHtml='<div class="skeleton">未检测到试卷</div>';
      else examsHtml = renderExams(t);
    }
    el.innerHTML=
      '<div class="thead">'
      +'<span class="fic"><svg width="17" height="17" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 3v4a1 1 0 0 0 1 1h4"/><path d="M17 21H7a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h7l5 5v11a2 2 0 0 1-2 2Z"/></svg></span>'
      +'<div style="flex:1;min-width:0"><div class="tname"></div><div class="sub">'+(t.summary&&t.summary.pages?('共 '+t.summary.exams+' 套 · '+t.summary.pages+' 页'):'等待拆分…')+'</div></div>'
      +'<span class="badge '+cls+'">'+tx+'</span></div>'
      +'<div class="tbar"><div class="f '+(t.status==='done'?'done':'')+'" style="width:'+pct+'%"></div></div>'
      +'<div class="tmeta"><span class="pct">'+pct+'%</span><span class="stage"></span>'
      +(t.status==='error'?'<span class="logbtn" data-rid="'+t.run_id+'">查看日志</span>':'')
      +saveOne+'</div>'
      +'<div class="log" id="log-'+t.run_id+'"><pre></pre></div>'
      +'<div id="exams-'+t.run_id+'">'+examsHtml+'</div>';
    el.querySelector('.tname').textContent=t.name;
    el.querySelector('.stage').textContent = t.status==='error' ? ('出错：'+(t.error||'').slice(0,60)) : (t.stage||'');
    if(t.status==='error') el.querySelector('.tmeta').title=t.error||'';
    box.appendChild(el);
    bindCorr(t);
  });
  // bind log toggles
  box.querySelectorAll('.logbtn').forEach(b=>{
    b.addEventListener('click', ()=> toggleLog(b.getAttribute('data-rid')));
  });
  box.querySelectorAll('.save-one').forEach(s=>{
    s.addEventListener('click', ()=>{ openDir(s.getAttribute('data-rid')); });
  });
}

function renderExams(t){
  let rows='';
  t.exams.forEach(e=>{
    const range='源 p'+e.src_start+'–p'+e.src_end;
    const base='/api/file?run_id='+t.run_id+'&name='+encodeURIComponent(e.file);
    const dl = '<a class="dl" href="'+base+'&inline=1" target="_blank" title="打开查看" style="text-decoration:none;color:inherit"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2.062 12.348a1 1 0 0 1 0-.696 10.75 10.75 0 0 1 19.876 0 1 1 0 0 1 0 .696 10.75 10.75 0 0 1-19.876 0"/><circle cx="12" cy="12" r="3"/></svg></a>'
      +'<a class="dl" href="'+base+'" download title="下载" style="text-decoration:none;color:inherit"><svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg></a>';
    rows+='<div class="erow"><span class="idx">'+e.index+'</span>'
      +'<span class="en" title="'+e.name+'">'+e.name+'</span>'
      +'<span class="meta"><span class="chip2">'+range+'</span><span class="chip2">'+e.pdf_pages+' 页</span></span>'
      +dl+'</div>';
  });
  return '<div class="exams"><div class="ehead">拆分出的试卷<span class="cnt">'+t.exams.length+' 套</span></div>'+rows+'</div>'
    +corrPanel(t);
}

async function loadExams(t){
  try{
    const r=await fetch('/api/exams?run_id='+encodeURIComponent(t.run_id));
    const j=await r.json();
    t.exams = j.exams || [];
    try{
      const lr=await fetch('/api/layout?run_id='+encodeURIComponent(t.run_id));
      const lj=await lr.json();
      t.corrPages=(lj.pages||[]).map(p=>({page:p.page,layout:p.layout}));
    }catch(e){ t.corrPages=[]; }
    // re-render only this task's exam area
    const box=document.getElementById('exams-'+t.run_id);
    if(box) box.innerHTML = renderExams(t);
    bindCorr(t);
  }catch(e){ t.exams={error:'无法读取试卷清单'}; renderTasks(); }
}

function corrPanel(t){
  const list=(t.corrPages||[]).map(p=>{
    return '<div class="corr-row"><span class="corr-pg">第 '+p.page+' 页</span>'
      +'<span class="chip2">当前判为 '+p.layout+'</span>'
      +'<select class="corr-sel" data-page="'+p.page+'">'
      +'<option value="keep">保持不变</option>'
      +'<option value="1-up">单栏（1-up）</option>'
      +'<option value="2-up">双栏（2-up）</option>'
      +'<option value="3-up">三栏（3-up）</option>'
      +'</select>'
      +'<a class="lnk" data-preview="'+p.page+'" href="javascript:void(0)">查看原页</a>'
      +'</div>';
  }).join('');
  return '<div class="corr"><div class="corr-head">版式校正'
    +'<span class="hint">若某套试卷里仍有整张横版页，说明该页版式被误判；改成实际版式后点下方按钮重新拆分（约 1 分钟内完成）</span></div>'
    +(list || '<div class="skeleton">未发现可疑页（本任务所有页均已按多栏拆分）</div>')
    +'<div class="corr-add"><input type="number" id="corrAdd-'+t.run_id+'" min="1" placeholder="页码">'
    +'<select id="corrLay-'+t.run_id+'"><option value="2-up">双栏</option><option value="3-up">三栏</option><option value="1-up">单栏</option></select>'
    +'<button type="button" class="btn" data-add="'+t.run_id+'">添加</button>'
    +'<button type="button" class="btn primary" data-apply="'+t.run_id+'">应用校正并重新拆分</button>'
    +'</div></div>';
}

function bindCorr(t){
  const area=document.getElementById('exams-'+t.run_id);
  if(!area) return;
  area.querySelectorAll('a[data-preview]').forEach(a=>{
    a.addEventListener('click', ()=>{ openPagePreview(t.run_id, a.getAttribute('data-preview')); });
  });
  const addBtn=area.querySelector('[data-add="'+t.run_id+'"]');
  if(addBtn) addBtn.addEventListener('click', ()=>{
    const inp=document.getElementById('corrAdd-'+t.run_id);
    const lay=document.getElementById('corrLay-'+t.run_id).value;
    const p=parseInt(inp.value,10);
    if(!p || p<1){ toast('请输入正确的页码','err'); return; }
    if((t.corrPages||[]).some(x=>x.page===p)){ toast('第 '+p+' 页已在列表中','err'); return; }
    t.corrPages=t.corrPages||[];
    t.corrPages.push({page:p,layout:'手动添加'});
    inp.value='';
    area.innerHTML=renderExams(t);
    bindCorr(t);
  });
  const applyBtn=area.querySelector('[data-apply="'+t.run_id+'"]');
  if(applyBtn) applyBtn.addEventListener('click', ()=> applyCorr(t));
}

function openPagePreview(runId, page){
  $('pvImg').src='/api/page-preview?run_id='+encodeURIComponent(runId)+'&page='+page;
  $('pv').style.display='flex';
}

async function applyCorr(t){
  const area=document.getElementById('exams-'+t.run_id);
  const corr=[];
  (t.corrPages||[]).forEach(p=>{
    const sel=area?area.querySelector('.corr-sel[data-page="'+p.page+'"]'):null;
    const v=sel?sel.value:'keep';
    if(v!=='keep') corr.push({page:p.page,layout:v});
  });
  if(!corr.length){ toast('没有需要校正的页面','err'); return; }
  const btn=area?area.querySelector('[data-apply="'+t.run_id+'"]'):null;
  if(btn){ btn.disabled=true; btn.textContent='重新拆分中…'; }
  try{
    const r=await fetch('/api/correct-layout',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({run_id:t.run_id,corrections:corr})});
    const j=await r.json();
    if(!j.ok){ toast(j.error||'校正失败','err'); return; }
    toast('已重新拆分：'+t.name,'ok');
    if(j.summary) t.summary=j.summary;
    t.exams=null; t.corrPages=[];
    await loadExams(t);
    showResults();
    renderTasks();
  }catch(e){ toast('校正失败：'+e.message,'err'); }
  finally{ if(btn){ btn.disabled=false; btn.textContent='应用校正并重新拆分'; } }
}

/* ---------- live logs ---------- */
function toggleLog(rid){
  const el=document.getElementById('log-'+rid);
  if(!el) return;
  const open=el.classList.toggle('open');
  const t=tasks.find(x=>x.run_id===rid);
  if(t) t.logOpen=open;
  if(open) refreshLog(rid);
}
async function refreshLog(rid){
  const el=document.getElementById('log-'+rid);
  if(!el || !el.classList.contains('open')) return;
  try{
    const r=await fetch('/api/status?run_id='+encodeURIComponent(rid));
    const j=await r.json();
    el.querySelector('pre').textContent = (j.log&&j.log.length)? j.log.join('\n') : '（暂无日志）';
    el.querySelector('pre').scrollTop = el.querySelector('pre').scrollHeight;
  }catch(e){}
}
function refreshOpenLogs(){
  tasks.forEach(t=>{ if(t.logOpen && (t.status==='running'||t.status==='queued')) refreshLog(t.run_id); });
}

/* ---------- results banner ---------- */
function showResults(){
  const totalExams = tasks.reduce((a,t)=> a + ((t.exams&&t.exams.length)||0), 0);
  const totalPages = tasks.reduce((a,t)=> a + ((t.summary&&t.summary.pages)||0), 0);
  const files = tasks.length;
  $('resTag').textContent = (totalExams? totalExams+' 套试卷':'') + (files? ' · '+files+' 份文件':'');
  const doneTask = tasks.find(t=>t.status==='done');
  const firstOut = doneTask && doneTask.out_dir ? doneTask.out_dir : '';
  let html='<div class="banner"><span class="bic"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg></span>'
    +'<div><div class="bt">拆分完成 · 已自动保存</div><div class="bs">共 '+files+' 份文件 · 拆出 '+totalExams+' 套试卷 · '+totalPages+' 页</div>'
    +(firstOut? '<div class="bs" style="font-family:var(--mono);font-size:11.5px;word-break:break-all">'+firstOut+'</div>':'')
    +'</div><div class="bact"><button class="btn primary" id="openAll">打开输出目录</button></div></div>';
  $('resultBody').innerHTML = html;
  $('resultCard').style.display='block';
  $('openAll').addEventListener('click', ()=>{ if(tasks[0]) openDir(tasks[0].run_id); });
}

/* ---------- save / open ---------- */
$('newTaskBtn').addEventListener('click', ()=>{
  files=[]; tasks=[]; batchId=null;
  $('chips').innerHTML='';
  $('runBtn').disabled=true; $('runBtn').innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 开始处理';
  $('inputCard').style.opacity='1';
  $('processCard').style.display='none'; $('resultCard').style.display='none';
  $('pickHint').textContent='点击 Browse 由服务端选择文件夹，将显示完整路径';
});
async function openDir(rid){
  try{ const r=await fetch('/api/open?run_id='+encodeURIComponent(rid)); const j=await r.json(); if(!j.ok) toast(j.error||'无法打开目录','err'); }
  catch(e){ toast('无法打开目录','err'); }
}

/* ---------- toast ---------- */
function toast(msg, type){
  const c=$('toasts');
  const ic = type==='err'
    ? '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--err)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="12" y1="8" x2="12" y2="12"/><line x1="12" y1="16" x2="12.01" y2="16"/></svg>'
    : '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--ok)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg>';
  const el=document.createElement('div'); el.className='toast '+(type||'');
  el.innerHTML=ic+'<span></span><span class="x">✕</span>';
  el.querySelector('span').textContent=msg;
  el.querySelector('.x').addEventListener('click', ()=> el.remove());
  c.appendChild(el);
  setTimeout(()=>{ el.style.opacity='0'; el.style.transform='translateY(8px)'; setTimeout(()=>el.remove(),250); }, 4200);
}
function showErr(msg){ $('err').style.display='flex'; $('errMsg').textContent=msg; }
function resetBtn(){ $('runBtn').disabled=false; $('runBtn').innerHTML='<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"/></svg> 开始处理'; }
</script>
</body>
</html>"""


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


@app.get("/", response_class=HTMLResponse)
def index():
    return HTML


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
    """在服务端弹出 Windows 原生文件夹选择框，返回完整路径。

    浏览器无法直接拿到 showDirectoryPicker 选择目录的绝对路径，
    因此改为由服务端（与浏览器同机）弹原生对话框，返回真实全路径。
    对话框挂在“置顶”的隐藏窗口下，确保出现在所有窗口最前面。
    取消选择时返回 {"path": null}。
    """
    ps = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    script = (
        "Add-Type -AssemblyName System.Windows.Forms; "
        "$owner = New-Object System.Windows.Forms.Form; "
        "$owner.TopMost = $true; "
        "$owner.ShowInTaskbar = $false; "
        "$owner.Opacity = 0; "
        "$owner.StartPosition = 'CenterScreen'; "
        "$owner.Show(); "
        "$owner.Activate(); "
        "$f = New-Object System.Windows.Forms.FolderBrowserDialog; "
        "$f.Description = '选择输出文件夹'; "
        "$f.ShowNewFolderButton = $true; "
        "if ($f.ShowDialog($owner) -eq [System.Windows.Forms.DialogResult]::OK) { Write-Output $f.SelectedPath } "
        "$owner.Close()"
    )
    try:
        proc = subprocess.run(
            [ps, "-NoProfile", "-STA", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,
        )
    except subprocess.TimeoutExpired:
        return JSONResponse(
            {"error": "选择超时。若没有弹出窗口，请直接在输入框里填写完整路径。"},
            status_code=408,
        )
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"error": "无法打开文件夹选择框：" + str(e)}, status_code=500)
    path = (proc.stdout or "").strip()
    if not path:
        return {"path": None, "cancelled": True}
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
