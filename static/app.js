(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const state = { files: [], tasks: [], batchId: null, pollTimer: null, activeCorrectionTask: null, activeUploadPage: null, openLogRunId: null };
  const els = {
    fileInput: $("file-input"), dropZone: $("drop-zone"), fileError: $("file-error"), fileList: $("file-list"),
    summaryFiles: $("summary-files"), summarySize: $("summary-size"), previewPanel: $("upload-preview-panel"), previewList: $("upload-preview-list"), outDir: $("out-dir"), browseBtn: $("browse-btn"),
    startBtn: $("start-btn"), dashboard: $("dashboard"), taskList: $("task-list"), batchStatus: $("batch-status"),
    overall: $("overall-progress"), overallText: $("overall-text"), overallCount: $("overall-count"), overallBar: $("overall-bar"),
    results: $("results"), resultSummary: $("result-summary"), resultList: $("result-list"), newTaskBtn: $("new-task-btn"),
    previewDialog: $("preview-dialog"), previewImage: $("preview-image"), previewTitle: $("preview-title"),
    uploadPageDialog: $("upload-page-dialog"), uploadPageImage: $("upload-page-image"), uploadPageTitle: $("upload-page-title"), uploadPageMeta: $("upload-page-meta"), uploadPageCounter: $("upload-page-counter"), uploadPrevPage: $("upload-prev-page"), uploadNextPage: $("upload-next-page"), setContentFirstBtn: $("set-content-first-btn"),
    correctionDialog: $("correction-dialog"), correctionForm: $("correction-form"), correctionDescription: $("correction-description"),
    applyCorrectionBtn: $("apply-correction-btn"), toastRegion: $("toast-region")
  };

  function el(tag, options = {}) {
    const node = document.createElement(tag);
    if (options.className) node.className = options.className;
    if (options.text !== undefined) node.textContent = options.text;
    if (options.attrs) Object.entries(options.attrs).forEach(([name, value]) => node.setAttribute(name, value));
    return node;
  }

  function formatSize(bytes) {
    if (!bytes) return "0 MB";
    const mb = bytes / 1024 / 1024;
    return mb < 1 ? `${Math.max(1, Math.round(mb * 1024))} KB` : `${mb.toFixed(mb >= 10 ? 0 : 1)} MB`;
  }

  function escapeFileName(name) { return (name || "未命名文件").replace(/[\\/:*?"<>|]/g, "_"); }

  function statusLabel(status) {
    return { queued: "排队中", running: "处理中", done: "已完成", error: "处理失败" }[status] || "等待处理";
  }

  function setFileError(message = "") {
    els.fileError.hidden = !message;
    els.fileError.textContent = message;
  }

  function addFiles(fileList) {
    const accepted = [];
    let rejected = 0;
    Array.from(fileList || []).forEach((file) => {
      const isPdf = file.type === "application/pdf" || file.name.toLowerCase().endsWith(".pdf");
      if (!isPdf) { rejected += 1; return; }
      const duplicate = state.files.some((entry) => entry.file.name === file.name && entry.file.size === file.size && entry.file.lastModified === file.lastModified);
      if (!duplicate) accepted.push({ file, skip: 2, previewState: "idle", previewData: null, previewError: "", previewProgress: 0, previewStage: "idle" });
    });
    state.files.push(...accepted);
    setFileError(rejected ? "已忽略非 PDF 文件。" : "");
    renderFiles();
    void loadUploadPreviews(accepted);
  }

  function renderFiles() {
    els.fileList.replaceChildren();
    const total = state.files.reduce((sum, entry) => sum + entry.file.size, 0);
    els.summaryFiles.textContent = `${state.files.length} 份`;
    els.summarySize.textContent = formatSize(total);
    els.startBtn.disabled = state.files.length === 0;

    state.files.forEach((entry, index) => {
      const row = el("div", { className: "file-row" });
      const meta = el("div", { className: "file-meta" });
      meta.append(el("div", { className: "file-name", text: entry.file.name, attrs: { title: entry.file.name } }));
      meta.append(el("div", { className: "file-size", text: formatSize(entry.file.size) }));
      const skip = el("div", { className: "skip-control" });
      const label = el("label", { text: "跳过前导页" });
      const select = el("select", { attrs: { "aria-label": `${entry.file.name} 的跳过前导页数` } });
      for (let value = 0; value <= 20; value += 1) {
        const option = el("option", { text: `${value} 页`, attrs: { value } });
        if (value === entry.skip) option.selected = true;
        select.append(option);
      }
      select.addEventListener("change", () => { entry.skip = Number(select.value); renderUploadPreviews(); });
      skip.append(label, select);
      const remove = el("button", { className: "icon-button", text: "×", attrs: { type: "button", "aria-label": `移除 ${entry.file.name}`, title: "移除文件" } });
      remove.addEventListener("click", () => { state.files.splice(index, 1); renderFiles(); });
      row.append(meta, skip, remove);
      els.fileList.append(row);
    });
    renderUploadPreviews();
  }

  async function requestJson(url, options) {
    const response = await fetch(url, options);
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || "请求失败，请稍后重试。");
    return data;
  }

  function uploadPreviewWithProgress(entry) {
    return new Promise((resolve, reject) => {
      const xhr = new XMLHttpRequest();
      const payload = new FormData();
      payload.append("pdf", entry.file, entry.file.name);
      xhr.open("POST", "/api/upload-preview");
      xhr.responseType = "json";
      xhr.upload.addEventListener("loadstart", () => {
        entry.previewStage = "uploading";
        entry.previewProgress = 0;
        renderUploadPreviews();
      });
      xhr.upload.addEventListener("progress", (event) => {
        if (event.lengthComputable) entry.previewProgress = Math.min(100, Math.round((event.loaded / event.total) * 100));
        renderUploadPreviews();
      });
      xhr.upload.addEventListener("load", () => {
        entry.previewStage = "rendering";
        entry.previewProgress = 100;
        renderUploadPreviews();
      });
      xhr.addEventListener("load", () => {
        const data = xhr.response || (() => { try { return JSON.parse(xhr.responseText); } catch { return {}; } })();
        if (xhr.status >= 200 && xhr.status < 300) resolve(data);
        else reject(new Error(data.error || "请求失败，请稍后重试。"));
      });
      xhr.addEventListener("error", () => reject(new Error("网络连接中断，无法生成预览。")));
      xhr.addEventListener("abort", () => reject(new Error("预览上传已取消。")));
      xhr.send(payload);
    });
  }

  async function loadUploadPreviews(entries) {
    for (const entry of entries) {
      entry.previewState = "loading";
      entry.previewStage = "uploading";
      entry.previewProgress = 0;
      entry.previewError = "";
      renderUploadPreviews();
      try {
        entry.previewData = await uploadPreviewWithProgress(entry);
        entry.previewState = "ready";
        entry.previewStage = "ready";
      } catch (error) {
        entry.previewState = "error";
        entry.previewStage = "error";
        entry.previewError = error.message || "无法生成预览";
      }
      renderUploadPreviews();
    }
  }

  function renderUploadPreviews() {
    els.previewList.replaceChildren();
    els.previewPanel.hidden = state.files.length === 0;
    state.files.forEach((entry) => {
      const card = el("article", { className: "upload-preview-card" });
      const head = el("div", { className: "upload-preview-card-head" });
      head.append(el("strong", { text: entry.file.name, attrs: { title: entry.file.name } }));
      head.append(el("span", { className: "field-help", text: `当前将跳过前 ${entry.skip} 页` }));
      card.append(head);

      if (entry.previewState === "loading") {
        const loadingText = entry.previewStage === "rendering" ? "文件已上传，正在渲染前 8 页缩略图…" : `正在上传预览文件${entry.previewProgress ? `：${entry.previewProgress}%` : "…"}`;
        card.append(el("p", { className: "preview-loading", text: loadingText }));
        const uploadTrack = el("div", { className: "progress-track preview-upload-progress", attrs: { role: "progressbar", "aria-label": `${entry.file.name} 的预览上传进度`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": String(entry.previewProgress) } });
        const uploadFill = el("div", { className: "progress-fill" });
        uploadFill.style.width = `${entry.previewProgress}%`;
        uploadTrack.append(uploadFill);
        card.append(uploadTrack);
      } else if (entry.previewState === "error") {
        card.append(el("p", { className: "preview-error", text: `${entry.previewError}；你仍可通过“跳过前导页”下拉菜单继续设置。` }));
      } else if (entry.previewState === "ready" && entry.previewData) {
        const pageCount = Number(entry.previewData.page_count) || 0;
        const intro = el("p", { className: "preview-intro", text: `共 ${pageCount} 页。点击缩略图可放大查看；确认后再将该页设为正文首页。` });
        card.append(intro);
        const pages = el("div", { className: "preview-pages", attrs: { role: "group", "aria-label": `${entry.file.name} 的前导页预览` } });
        (entry.previewData.previews || []).forEach((preview) => {
          const page = Number(preview.page);
          const button = el("button", { className: `preview-page${page === entry.skip + 1 ? " selected" : ""}`, attrs: { type: "button", "aria-pressed": String(page === entry.skip + 1), title: `查看第 ${page} 页并设置正文首页` } });
          const image = el("img", { attrs: { src: preview.thumbnail || preview.image, alt: `第 ${page} 页缩略图` } });
          const label = el("span", { text: `第 ${page} 页` });
          button.append(image, label);
          button.addEventListener("click", () => openUploadPage(entry, preview));
          pages.append(button);
        });
        card.append(pages);
        if (pageCount > (entry.previewData.previews || []).length) {
          card.append(el("p", { className: "field-help", text: `仅预览前 ${(entry.previewData.previews || []).length} 页；如正文更靠后，可使用下方下拉菜单继续设置。` }));
        }
      } else {
        card.append(el("p", { className: "preview-loading", text: "等待预览生成…" }));
      }
      els.previewList.append(card);
    });
  }

  async function chooseOutputDir() {
    els.browseBtn.disabled = true;
    try {
      const result = await requestJson("/api/pick-folder", { method: "POST" });
      if (result.path) {
        els.outDir.value = result.path;
        toast("已选择输出文件夹", "success");
      }
    } catch (error) {
      toast(error.message, "error");
    } finally {
      els.browseBtn.disabled = false;
    }
  }

  async function start() {
    if (!state.files.length) return;
    setFileError("");
    els.startBtn.disabled = true;
    els.startBtn.textContent = "正在创建任务…";
    const payload = new FormData();
    state.files.forEach((entry) => { payload.append("pdf", entry.file, entry.file.name); payload.append("skip", String(entry.skip)); });
    payload.append("out_dir", els.outDir.value.trim());
    try {
      const data = await requestJson("/api/run", { method: "POST", body: payload });
      state.batchId = data.batch_id;
      state.tasks = (data.run_ids || []).map((runId, index) => ({
        run_id: runId,
        name: (data.names || [])[index] || state.files[index]?.file.name || "未命名文件",
        status: "queued", stage: "正在排队", progress: 0, log: [], summary: null, error: null, exams: []
      }));
      els.dashboard.hidden = false;
      els.results.hidden = true;
      els.overall.hidden = false;
      els.batchStatus.textContent = `已创建 ${state.tasks.length} 个任务`;
      renderTasks();
      window.location.hash = "dashboard";
      startPolling();
    } catch (error) {
      setFileError(error.message);
      toast("未能创建任务", "error");
    } finally {
      els.startBtn.textContent = "开始处理 →";
      els.startBtn.disabled = state.files.length === 0;
    }
  }

  function renderTasks() {
    els.taskList.replaceChildren();
    state.tasks.forEach((task) => {
      const card = el("article", { className: `task-card status-${task.status}` });
      const head = el("div", { className: "task-head" });
      const nameBlock = el("div");
      nameBlock.append(el("h3", { className: "task-name", text: task.name }));
      nameBlock.append(el("p", { className: "task-stage", text: task.stage || "等待处理" }));
      const badge = el("span", { className: `task-status status-${task.status}`, text: statusLabel(task.status) });
      head.append(nameBlock, badge);
      card.append(head);

      const progress = Math.max(0, Math.min(100, Number(task.progress) || 0));
      const track = el("div", { className: "progress-track", attrs: { role: "progressbar", "aria-label": `${task.name} 的处理进度`, "aria-valuemin": "0", "aria-valuemax": "100", "aria-valuenow": progress } });
      const fill = el("div", { className: "progress-fill" }); fill.style.width = `${progress}%`; track.append(fill); card.append(track);
      if (task.error) card.append(el("p", { className: "inline-note", text: task.error }));

      const actions = el("div", { className: "task-actions" });
      if (task.status === "done") {
        actions.append(taskAction("查看结果", "show-results", task.run_id));
        actions.append(taskAction("打开输出文件夹", "open-dir", task.run_id));
        actions.append(taskAction("检查疑似页面", "correct-layout", task.run_id, "secondary"));
      }
      if (task.status === "error") actions.append(taskAction("查看诊断日志", "toggle-log", task.run_id, "secondary"));
      if (task.status === "running" || task.status === "queued") actions.append(el("span", { className: "field-help", text: task.status === "queued" ? "任务正在等待可用处理槽位。" : "处理中；完成后将自动显示验收结果。" }));
      if (actions.childNodes.length) card.append(actions);

      if (task.log?.length) {
        const details = el("details", { className: "log-details" });
        details.open = state.openLogRunId === task.run_id;
        details.addEventListener("toggle", () => {
          if (details.open) state.openLogRunId = task.run_id;
          else if (state.openLogRunId === task.run_id) state.openLogRunId = null;
        });
        details.append(el("summary", { text: "查看处理日志" }));
        details.append(el("pre", { text: task.log.slice(-120).join("\n") }));
        card.append(details);
      }
      els.taskList.append(card);
    });
    updateOverall();
  }

  function toggleLog(runId) {
    state.openLogRunId = state.openLogRunId === runId ? null : runId;
    renderTasks();
  }

  function taskAction(text, action, runId, variant = "secondary") {
    const button = el("button", { className: `button ${variant}`, text, attrs: { type: "button", "data-action": action, "data-run-id": runId } });
    return button;
  }

  function updateOverall() {
    const total = state.tasks.length;
    const completed = state.tasks.filter((task) => task.status === "done" || task.status === "error").length;
    const running = state.tasks.filter((task) => task.status === "running").length;
    const queued = state.tasks.filter((task) => task.status === "queued").length;
    const percent = total ? Math.round(state.tasks.reduce((sum, task) => sum + (Number(task.progress) || 0), 0) / total) : 0;
    els.overallCount.textContent = `${completed} / ${total}`;
    els.overallBar.style.width = `${percent}%`;
    els.overallText.textContent = running ? `正在处理 ${running} 个任务${queued ? `，另有 ${queued} 个排队` : ""}` : (queued ? `等待执行：${queued} 个任务` : `所有任务已结束`);
    els.batchStatus.textContent = completed === total && total ? `已完成 ${state.tasks.filter((task) => task.status === "done").length} 个任务` : `${running} 个处理中 · ${queued} 个排队`;
  }

  function startPolling() {
    stopPolling();
    poll();
    state.pollTimer = window.setInterval(poll, 1600);
  }

  function stopPolling() { if (state.pollTimer) { window.clearInterval(state.pollTimer); state.pollTimer = null; } }

  async function poll() {
    if (!state.tasks.length) return;
    try {
      await Promise.all(state.tasks.map(async (task) => {
        if (task.status === "done" || task.status === "error") return;
        const data = await requestJson(`/api/status?run_id=${encodeURIComponent(task.run_id)}`);
        Object.assign(task, data);
      }));
      renderTasks();
      const complete = state.tasks.every((task) => task.status === "done" || task.status === "error");
      if (complete) {
        stopPolling();
        await showResults();
        toast("本批任务已完成，请查看结果与异常页。", "success");
      }
    } catch (error) {
      els.batchStatus.textContent = "状态更新暂时失败";
    }
  }

  async function showResults() {
    const completed = state.tasks.filter((task) => task.status === "done");
    if (!completed.length) return;
    els.results.hidden = false;
    els.resultSummary.replaceChildren();
    const totalPages = completed.reduce((sum, task) => sum + (Number(task.summary?.pages) || 0), 0);
    const totalExams = completed.reduce((sum, task) => sum + (Number(task.summary?.exams) || 0), 0);
    [["处理成功", `${completed.length} 份`], ["识别套数", totalExams ? `${totalExams} 套` : "待查看"], ["输出页数", totalPages ? `${totalPages} 页` : "待查看"]].forEach(([label, value]) => {
      const metric = el("div", { className: "metric" }); metric.append(el("span", { className: "metric-label", text: label }), el("span", { className: "metric-value", text: value })); els.resultSummary.append(metric);
    });
    els.resultList.replaceChildren();
    for (const task of completed) {
      try { const data = await requestJson(`/api/exams?run_id=${encodeURIComponent(task.run_id)}`); task.exams = data.exams || []; } catch { task.exams = []; }
      const card = el("article", { className: "result-card" });
      card.append(el("h3", { text: task.name }));
      card.append(el("p", { text: task.exams.length ? `已生成 ${task.exams.length} 套独立试卷；可在输出文件夹中验收。` : "任务已完成；请在输出文件夹中查看拆分结果。" }));
      const actions = el("div", { className: "task-actions" });
      actions.append(taskAction("打开输出文件夹", "open-dir", task.run_id));
      actions.append(taskAction("检查疑似页面", "correct-layout", task.run_id, "secondary"));
      if (task.exams.length) {
        task.exams.slice(0, 4).forEach((exam) => {
          if (exam.file) {
            const download = taskAction(`下载：${exam.name || exam.file}`, "download-file", task.run_id, "secondary");
            download.dataset.file = exam.file;
            actions.append(download);
          }
        });
      }
      card.append(actions); els.resultList.append(card);
    }
    window.location.hash = "results";
  }

  function openPreview(runId, page) {
    els.previewTitle.textContent = `第 ${page} 页原图`;
    els.previewImage.src = `/api/page-preview?run_id=${encodeURIComponent(runId)}&page=${encodeURIComponent(page)}`;
    els.previewDialog.showModal();
  }

  function openUploadPage(entry, preview) {
    const previews = entry.previewData?.previews || [];
    const index = Math.max(0, previews.findIndex((item) => Number(item.page) === Number(preview.page)));
    state.activeUploadPage = { entry, index };
    renderActiveUploadPage();
    if (!els.uploadPageDialog.open) els.uploadPageDialog.showModal();
  }

  function renderActiveUploadPage() {
    const active = state.activeUploadPage;
    if (!active) return;
    const previews = active.entry.previewData?.previews || [];
    if (!previews.length) return;
    active.index = Math.max(0, Math.min(previews.length - 1, active.index));
    const preview = previews[active.index];
    const page = Number(preview.page);
    const pageCount = Number(active.entry.previewData?.page_count) || previews.length;
    els.uploadPageTitle.textContent = `第 ${page} 页放大预览`;
    els.uploadPageMeta.textContent = page === active.entry.skip + 1 ? "当前已设为正文首页。" : "确认后将该页设为正文首页，系统会跳过此前页面。";
    els.uploadPageCounter.textContent = `第 ${page} 页 / 共 ${pageCount} 页${pageCount > previews.length ? `（可浏览前 ${previews.length} 页）` : ""}`;
    els.uploadPageImage.src = preview.full_image || preview.image || preview.thumbnail;
    els.uploadPrevPage.disabled = active.index === 0;
    els.uploadNextPage.disabled = active.index === previews.length - 1;
  }

  function navigateUploadPage(offset) {
    const active = state.activeUploadPage;
    if (!active) return;
    active.index += offset;
    renderActiveUploadPage();
  }

  function setContentFirstPage() {
    const active = state.activeUploadPage;
    const preview = active?.entry.previewData?.previews?.[active.index];
    if (!preview) return;
    const page = Number(preview.page);
    active.entry.skip = page - 1;
    els.uploadPageDialog.close();
    renderFiles();
    toast(`已将第 ${page} 页设为正文首页。`, "success");
  }

  async function openCorrection(runId) {
    const task = state.tasks.find((item) => item.run_id === runId);
    if (!task) return;
    state.activeCorrectionTask = task;
    els.correctionForm.replaceChildren();
    els.correctionDescription.textContent = "正在读取疑似单栏页面…";
    els.applyCorrectionBtn.disabled = true;
    try {
      const data = await requestJson(`/api/layout?run_id=${encodeURIComponent(runId)}`);
      const pages = data.pages || [];
      if (!pages.length) {
        toast("未发现需要人工确认的疑似页面。", "success");
        return;
      }
      els.correctionDescription.textContent = `系统列出 ${pages.length} 个疑似页面。仅对你修改的页面重新识别；点击缩略图旁的“查看原页”可辅助判断。`;
      pages.forEach((entry) => {
        const item = el("div", { className: "correction-item" });
        const image = el("img", { attrs: { src: `/api/page-preview?run_id=${encodeURIComponent(runId)}&page=${entry.page}`, alt: `第 ${entry.page} 页缩略图` } });
        const main = el("div", { className: "correction-main" });
        main.append(el("strong", { text: `第 ${entry.page} 页` }), el("span", { text: `当前判断：${entry.layout || "1-up"}（可疑）` }));
        const preview = el("button", { className: "button secondary", text: "查看原页", attrs: { type: "button" } });
        preview.addEventListener("click", () => openPreview(runId, entry.page));
        const select = el("select", { attrs: { "data-page": entry.page, "aria-label": `第 ${entry.page} 页的版式校正` } });
        [["keep", "保持不变"], ["1-up", "单栏（1-up）"], ["2-up", "双栏（2-up）"], ["3-up", "三栏（3-up）"]].forEach(([value, text]) => select.append(el("option", { text, attrs: { value } })));
        item.append(image, main, preview, select); els.correctionForm.append(item);
      });
      els.applyCorrectionBtn.disabled = false;
      els.correctionDialog.showModal();
    } catch (error) { toast(error.message, "error"); }
  }

  async function applyCorrection() {
    const task = state.activeCorrectionTask;
    if (!task) return;
    const corrections = Array.from(els.correctionForm.querySelectorAll("select[data-page]")).filter((select) => select.value !== "keep").map((select) => ({ page: Number(select.dataset.page), layout: select.value }));
    if (!corrections.length) { toast("尚未选择需要修改的页面。", "error"); return; }
    els.applyCorrectionBtn.disabled = true; els.applyCorrectionBtn.textContent = "正在重新拆分…";
    try {
      const data = await requestJson("/api/correct-layout", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ run_id: task.run_id, corrections }) });
      if (!data.ok) throw new Error(data.error || "重新拆分失败");
      task.status = "done"; task.progress = 100; task.summary = data.summary || task.summary;
      els.correctionDialog.close(); renderTasks(); await showResults(); toast(`已按 ${corrections.length} 个校正项重新拆分。`, "success");
    } catch (error) { toast(error.message, "error"); }
    finally { els.applyCorrectionBtn.disabled = false; els.applyCorrectionBtn.textContent = "应用校正并重新拆分"; }
  }

  async function openDir(runId) {
    try { const data = await requestJson(`/api/open?run_id=${encodeURIComponent(runId)}`); toast(data.path ? "已在文件资源管理器中打开输出文件夹。" : "已尝试打开输出文件夹。", "success"); }
    catch (error) { toast(error.message, "error"); }
  }

  function downloadFile(runId, fileName) { window.location.href = `/api/file?run_id=${encodeURIComponent(runId)}&name=${encodeURIComponent(fileName)}`; }

  function toast(message, type = "") {
    const note = el("div", { className: `toast ${type}`, text: message, attrs: { role: type === "error" ? "alert" : "status" } });
    els.toastRegion.append(note); window.setTimeout(() => note.remove(), 4500);
  }

  function reset() {
    stopPolling(); state.files = []; state.tasks = []; state.batchId = null; state.activeCorrectionTask = null; state.activeUploadPage = null; state.openLogRunId = null;
    els.fileInput.value = ""; els.outDir.value = ""; els.dashboard.hidden = true; els.results.hidden = true; els.taskList.replaceChildren(); els.resultList.replaceChildren(); setFileError(""); renderFiles(); window.scrollTo({ top: 0, behavior: "smooth" });
  }

  function bindEvents() {
    els.fileInput.addEventListener("change", (event) => { addFiles(event.target.files); els.fileInput.value = ""; });
    els.dropZone.addEventListener("keydown", (event) => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); els.fileInput.click(); } });
    ["dragenter", "dragover"].forEach((eventName) => els.dropZone.addEventListener(eventName, (event) => { event.preventDefault(); els.dropZone.classList.add("drag-active"); }));
    ["dragleave", "drop"].forEach((eventName) => els.dropZone.addEventListener(eventName, (event) => { event.preventDefault(); els.dropZone.classList.remove("drag-active"); }));
    els.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));
    els.browseBtn.addEventListener("click", chooseOutputDir); els.startBtn.addEventListener("click", start); els.newTaskBtn.addEventListener("click", reset); els.applyCorrectionBtn.addEventListener("click", applyCorrection); els.setContentFirstBtn.addEventListener("click", setContentFirstPage); els.uploadPrevPage.addEventListener("click", () => navigateUploadPage(-1)); els.uploadNextPage.addEventListener("click", () => navigateUploadPage(1));
    document.addEventListener("click", (event) => {
      const close = event.target.closest("[data-close-dialog]"); if (close) { $(close.dataset.closeDialog)?.close(); return; }
      const action = event.target.closest("[data-action]"); if (!action) return;
      const runId = action.dataset.runId;
      if (action.dataset.action === "open-dir") openDir(runId);
      else if (action.dataset.action === "correct-layout") openCorrection(runId);
      else if (action.dataset.action === "show-results") showResults();
      else if (action.dataset.action === "toggle-log") toggleLog(runId);
      else if (action.dataset.action === "download-file") downloadFile(runId, action.dataset.file);
    });
    [els.previewDialog, els.uploadPageDialog, els.correctionDialog].forEach((dialog) => dialog.addEventListener("click", (event) => { if (event.target === dialog) dialog.close(); }));
    els.uploadPageDialog.addEventListener("close", () => { state.activeUploadPage = null; });
    document.addEventListener("keydown", (event) => {
      if (!els.uploadPageDialog.open) return;
      if (event.key === "ArrowLeft") { event.preventDefault(); navigateUploadPage(-1); }
      if (event.key === "ArrowRight") { event.preventDefault(); navigateUploadPage(1); }
    });
  }

  bindEvents(); renderFiles();
})();
