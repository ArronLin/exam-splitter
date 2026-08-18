from __future__ import annotations

import os
import sys
import traceback
from pathlib import Path

import fitz
from PySide6.QtCore import QByteArray, QProcess, QThread, Qt, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QIcon, QImage, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from desktop_runtime import LocalTask, ROOT, app_data_path, pipeline_command, resource_path, stage_from_log


class PreviewWorker(QThread):
    ready = Signal(str, int, object)
    failed = Signal(str, str)

    def __init__(self, task_key: str, pdf_path: Path) -> None:
        super().__init__()
        self.task_key = task_key
        self.pdf_path = pdf_path

    def run(self) -> None:
        doc = None
        try:
            doc = fitz.open(self.pdf_path)
            page_count = doc.page_count
            pages: list[bytes] = []
            for page_index in range(min(page_count, 8)):
                page = doc[page_index]
                long_side = max(page.rect.width, page.rect.height) or 1
                scale = min(1120.0 / long_side, 2.2)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                pages.append(pix.tobytes("png"))
            self.ready.emit(self.task_key, page_count, pages)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(self.task_key, str(exc))
        finally:
            if doc is not None:
                doc.close()


class SplitterDesktopWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.tasks: list[LocalTask] = []
        self.preview_pages: dict[str, tuple[int, list[QImage]]] = {}
        self.preview_workers: dict[str, PreviewWorker] = {}
        self.active_preview_key: str | None = None
        self.active_preview_index = 0
        self.pending_tasks: list[LocalTask] = []
        self.current_task: LocalTask | None = None
        self.process: QProcess | None = None

        self.setWindowTitle("试卷合订本拆分器 · 桌面版")
        self.setWindowIcon(QIcon(str(resource_path("assets", "app_icon.png"))))
        self.setMinimumSize(1180, 760)
        self.resize(1360, 900)
        self.setAcceptDrops(True)
        self._build_ui()
        self._apply_style()
        self._update_start_button()

    def _build_ui(self) -> None:
        root = QWidget()
        root_layout = QVBoxLayout(root)
        root_layout.setContentsMargins(24, 22, 24, 24)
        root_layout.setSpacing(16)

        header = QFrame()
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        title_box = QVBoxLayout()
        title = QLabel("试卷合订本拆分器")
        title.setObjectName("appTitle")
        subtitle = QLabel("Windows 原生桌面版 · 所有文件仅在本机处理 · 不启动浏览器")
        subtitle.setObjectName("subTitle")
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        header_layout.addLayout(title_box)
        header_layout.addStretch(1)
        self.local_badge = QLabel("● 本地桌面应用")
        self.local_badge.setObjectName("localBadge")
        header_layout.addWidget(self.local_badge)
        root_layout.addWidget(header)

        workflow = QLabel("1  添加 PDF 与设置前导页    →    2  本地拆分与查看日志    →    3  在输出目录验收结果")
        workflow.setObjectName("workflow")
        root_layout.addWidget(workflow)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)
        splitter.addWidget(self._build_setup_panel())
        splitter.addWidget(self._build_preview_panel())
        splitter.setSizes([620, 700])
        root_layout.addWidget(splitter, 3)

        root_layout.addWidget(self._build_task_panel(), 2)
        self.setCentralWidget(root)

    def _build_setup_panel(self) -> QWidget:
        panel = QGroupBox("准备任务")
        layout = QVBoxLayout(panel)
        layout.setSpacing(12)

        intro = QLabel("添加一份或多份合订本 PDF。每份文件都可独立选择正文首页，避免共享同一跳过页设置。")
        intro.setWordWrap(True)
        intro.setObjectName("sectionHint")
        layout.addWidget(intro)

        select_row = QHBoxLayout()
        self.add_files_button = QPushButton("添加 PDF 文件")
        self.add_files_button.clicked.connect(self.select_files)
        self.clear_files_button = QPushButton("清空列表")
        self.clear_files_button.setObjectName("secondaryButton")
        self.clear_files_button.clicked.connect(self.clear_files)
        select_row.addWidget(self.add_files_button)
        select_row.addWidget(self.clear_files_button)
        select_row.addStretch(1)
        layout.addLayout(select_row)

        self.file_table = QTableWidget(0, 4)
        self.file_table.setHorizontalHeaderLabels(["PDF 文件", "大小", "跳过前导页", "移除"])
        self.file_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.file_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.file_table.verticalHeader().setVisible(False)
        self.file_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.file_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.file_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.file_table.itemSelectionChanged.connect(self._table_selection_changed)
        self.file_table.setMinimumHeight(255)
        layout.addWidget(self.file_table, 1)

        output_box = QGroupBox("输出文件夹")
        output_layout = QVBoxLayout(output_box)
        output_label = QLabel("选择结果根目录。每份 PDF 会创建独立的同名子目录。")
        output_label.setObjectName("fieldHint")
        output_label.setWordWrap(True)
        output_layout.addWidget(output_label)
        output_row = QHBoxLayout()
        self.output_edit = QLineEdit()
        self.output_edit.setPlaceholderText("未填写时，将在源文件同级目录创建“拆分结果”")
        self.output_edit.textChanged.connect(self._update_start_button)
        output_row.addWidget(self.output_edit, 1)
        browse = QPushButton("选择文件夹")
        browse.setObjectName("secondaryButton")
        browse.clicked.connect(self.choose_output_dir)
        output_row.addWidget(browse)
        output_layout.addLayout(output_row)
        layout.addWidget(output_box)

        action_row = QHBoxLayout()
        self.start_button = QPushButton("开始本地拆分")
        self.start_button.setObjectName("primaryButton")
        self.start_button.clicked.connect(self.start_processing)
        self.cancel_button = QPushButton("停止当前任务")
        self.cancel_button.setObjectName("dangerButton")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.stop_current_task)
        action_row.addWidget(self.start_button)
        action_row.addWidget(self.cancel_button)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        return panel

    def _build_preview_panel(self) -> QWidget:
        panel = QGroupBox("提交前预览")
        layout = QVBoxLayout(panel)
        layout.setSpacing(10)
        self.preview_description = QLabel("选择左侧的 PDF 后，将读取前 8 页。使用前后按钮或键盘 ← / → 浏览，并将当前页设为正文首页。")
        self.preview_description.setWordWrap(True)
        self.preview_description.setObjectName("sectionHint")
        layout.addWidget(self.preview_description)

        navigation = QHBoxLayout()
        self.preview_prev = QPushButton("‹ 上一页")
        self.preview_prev.setObjectName("secondaryButton")
        self.preview_prev.clicked.connect(lambda: self.change_preview_page(-1))
        self.preview_next = QPushButton("下一页 ›")
        self.preview_next.setObjectName("secondaryButton")
        self.preview_next.clicked.connect(lambda: self.change_preview_page(1))
        self.preview_counter = QLabel("尚未选择 PDF")
        self.preview_counter.setAlignment(Qt.AlignmentFlag.AlignCenter)
        navigation.addWidget(self.preview_prev)
        navigation.addWidget(self.preview_counter, 1)
        navigation.addWidget(self.preview_next)
        layout.addLayout(navigation)

        self.preview_image = QLabel("从左侧选择或添加 PDF 后，这里会显示可放大的页面预览。")
        self.preview_image.setObjectName("previewImage")
        self.preview_image.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_image.setWordWrap(True)
        self.preview_image.setMinimumHeight(390)
        self.preview_image.setScaledContents(False)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(self.preview_image)
        layout.addWidget(scroll, 1)

        set_row = QHBoxLayout()
        self.set_first_page_button = QPushButton("将当前页设为正文首页")
        self.set_first_page_button.setObjectName("primaryButton")
        self.set_first_page_button.clicked.connect(self.set_current_as_first_page)
        set_row.addWidget(self.set_first_page_button)
        set_row.addStretch(1)
        layout.addLayout(set_row)

        thumbs_label = QLabel("已载入页面")
        thumbs_label.setObjectName("fieldHint")
        layout.addWidget(thumbs_label)
        self.thumbnail_list = QListWidget()
        self.thumbnail_list.setViewMode(QListWidget.ViewMode.IconMode)
        self.thumbnail_list.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.thumbnail_list.setMovement(QListWidget.Movement.Static)
        self.thumbnail_list.setIconSize(self.thumbnail_list.iconSize().expandedTo(self.thumbnail_list.sizeHint()))
        self.thumbnail_list.setFixedHeight(118)
        self.thumbnail_list.currentRowChanged.connect(self.preview_page_selected)
        layout.addWidget(self.thumbnail_list)
        self._update_preview_controls()
        return panel

    def _build_task_panel(self) -> QWidget:
        panel = QGroupBox("处理状态与实时日志")
        layout = QVBoxLayout(panel)
        layout.setSpacing(8)
        task_hint = QLabel("任务会在本机顺序执行。选择任务可查看持续追加的日志；日志不会因界面刷新而关闭。")
        task_hint.setObjectName("sectionHint")
        layout.addWidget(task_hint)

        split = QSplitter(Qt.Orientation.Horizontal)
        self.task_tree = QTreeWidget()
        self.task_tree.setHeaderLabels(["文件", "状态", "阶段", "进度", "输出"])
        self.task_tree.setRootIsDecorated(False)
        self.task_tree.setAlternatingRowColors(True)
        self.task_tree.itemSelectionChanged.connect(self.show_selected_log)
        self.task_tree.setMinimumHeight(150)
        self.task_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.task_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.task_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.task_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.task_tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        split.addWidget(self.task_tree)

        log_container = QWidget()
        log_layout = QVBoxLayout(log_container)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_head = QHBoxLayout()
        log_head.addWidget(QLabel("实时日志"))
        log_head.addStretch(1)
        self.open_output_button = QPushButton("打开选中输出目录")
        self.open_output_button.setObjectName("secondaryButton")
        self.open_output_button.clicked.connect(self.open_selected_output)
        log_head.addWidget(self.open_output_button)
        log_layout.addLayout(log_head)
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setPlaceholderText("选择处理任务后，实时日志会显示在这里。")
        log_layout.addWidget(self.log_view, 1)
        split.addWidget(log_container)
        split.setSizes([700, 580])
        layout.addWidget(split, 1)
        return panel

    def _apply_style(self) -> None:
        self.setStyleSheet("""
            QMainWindow { background: #f5f2ec; color: #253244; }
            QWidget { font-family: 'Segoe UI', 'Microsoft YaHei UI', 'Microsoft YaHei', Arial; font-size: 13px; }
            QGroupBox { background: #ffffff; border: 1px solid #dedbd3; border-radius: 12px; margin-top: 10px; padding: 18px 16px 16px 16px; font-weight: 700; color: #253244; }
            QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; }
            QLabel#appTitle { font-size: 27px; font-weight: 800; color: #253244; }
            QLabel#subTitle, QLabel#sectionHint, QLabel#fieldHint { color: #667085; }
            QLabel#localBadge { border: 1px solid #b9dbc8; background: #e8f4ed; color: #2f7d5d; border-radius: 999px; padding: 6px 11px; font-weight: 700; }
            QLabel#workflow { background: #f3e9d8; color: #68471f; border-radius: 8px; padding: 10px 13px; font-weight: 700; }
            QPushButton { min-height: 34px; border: 1px solid transparent; border-radius: 7px; padding: 0 13px; background: #825d2d; color: #ffffff; font-weight: 700; }
            QPushButton:hover { background: #68471f; }
            QPushButton:disabled { background: #dedbd3; color: #667085; }
            QPushButton#secondaryButton { background: #ffffff; border-color: #dedbd3; color: #253244; }
            QPushButton#secondaryButton:hover { background: #fbfaf7; }
            QPushButton#primaryButton { background: #825d2d; color: #ffffff; }
            QPushButton#dangerButton { background: #ffffff; border-color: #e7bdbc; color: #b54745; }
            QLineEdit, QSpinBox { min-height: 34px; border: 1px solid #dedbd3; border-radius: 7px; padding: 0 9px; background: #ffffff; color: #253244; }
            QLineEdit:focus, QSpinBox:focus { border: 2px solid #c9b28d; }
            QTableWidget, QTreeWidget, QListWidget, QPlainTextEdit { border: 1px solid #dedbd3; border-radius: 8px; background: #ffffff; alternate-background-color: #fbfaf7; }
            QHeaderView::section { background: #f7f5f1; color: #667085; border: 0; border-bottom: 1px solid #dedbd3; padding: 8px; font-weight: 700; }
            QTableWidget::item:selected, QTreeWidget::item:selected, QListWidget::item:selected { background: #f3e9d8; color: #253244; }
            QLabel#previewImage { border: 1px dashed #b7ac9c; border-radius: 8px; background: #fbfaf7; color: #667085; padding: 18px; }
            QPlainTextEdit { background: #1f2937; color: #e5e7eb; font-family: Consolas, 'Cascadia Mono', monospace; }
        """)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event: QDropEvent) -> None:
        paths = [Path(url.toLocalFile()) for url in event.mimeData().urls()]
        self.add_pdf_files(paths)
        event.acceptProposedAction()

    def select_files(self) -> None:
        names, _ = QFileDialog.getOpenFileNames(self, "选择 PDF 文件", str(Path.home()), "PDF 文件 (*.pdf)")
        self.add_pdf_files(Path(name) for name in names)

    def add_pdf_files(self, paths: object) -> None:
        existing = {str(task.source).lower() for task in self.tasks}
        added = 0
        for candidate in paths:  # type: ignore[union-attr]
            path = Path(candidate)
            if path.suffix.lower() != ".pdf" or not path.is_file() or str(path).lower() in existing:
                continue
            self.tasks.append(LocalTask(source=path))
            existing.add(str(path).lower())
            added += 1
        if added:
            self.refresh_file_table()
            self.file_table.selectRow(len(self.tasks) - 1)
        else:
            QMessageBox.information(self, "未添加文件", "请选择尚未添加的 PDF 文件。")
        self._update_start_button()

    def clear_files(self) -> None:
        if self.process is not None:
            QMessageBox.warning(self, "无法清空", "当前正在处理任务，请先停止或等待处理完成。")
            return
        self.tasks.clear()
        self.preview_pages.clear()
        self.active_preview_key = None
        self.thumbnail_list.clear()
        self.preview_image.setText("从左侧选择或添加 PDF 后，这里会显示可放大的页面预览。")
        self.preview_image.setPixmap(QPixmap())
        self.refresh_file_table()
        self._update_start_button()

    def refresh_file_table(self) -> None:
        selected_key = self.active_preview_key
        self.file_table.blockSignals(True)
        self.file_table.setRowCount(0)
        for row, task in enumerate(self.tasks):
            self.file_table.insertRow(row)
            name_item = QTableWidgetItem(task.display_name)
            name_item.setToolTip(str(task.source))
            name_item.setData(Qt.ItemDataRole.UserRole, str(task.source))
            self.file_table.setItem(row, 0, name_item)
            size = task.source.stat().st_size / 1024 / 1024
            self.file_table.setItem(row, 1, QTableWidgetItem(f"{size:.1f} MB"))
            spin = QSpinBox()
            spin.setRange(0, 100)
            spin.setValue(task.skip)
            spin.setSuffix(" 页")
            spin.valueChanged.connect(lambda value, current=task: self._set_skip(current, value))
            self.file_table.setCellWidget(row, 2, spin)
            remove = QPushButton("移除")
            remove.setObjectName("secondaryButton")
            remove.clicked.connect(lambda _checked=False, current=task: self.remove_task(current))
            self.file_table.setCellWidget(row, 3, remove)
            if selected_key and str(task.source) == selected_key:
                self.file_table.selectRow(row)
        self.file_table.blockSignals(False)

    def _set_skip(self, task: LocalTask, value: int) -> None:
        task.skip = value
        if self.active_preview_key == str(task.source):
            self._render_current_preview()

    def remove_task(self, task: LocalTask) -> None:
        if self.process is not None:
            QMessageBox.warning(self, "无法移除", "当前正在处理任务，请先停止或等待处理完成。")
            return
        self.tasks.remove(task)
        self.preview_pages.pop(str(task.source), None)
        if self.active_preview_key == str(task.source):
            self.active_preview_key = None
            self.thumbnail_list.clear()
            self.preview_image.setText("请选择另一份 PDF。")
            self.preview_image.setPixmap(QPixmap())
        self.refresh_file_table()
        self._update_start_button()

    def _table_selection_changed(self) -> None:
        row = self.file_table.currentRow()
        if row < 0 or row >= len(self.tasks):
            return
        task = self.tasks[row]
        self.active_preview_key = str(task.source)
        self.active_preview_index = min(self.active_preview_index, 7)
        if self.active_preview_key not in self.preview_pages and self.active_preview_key not in self.preview_workers:
            self._load_preview(task)
        else:
            self._render_current_preview()

    def _load_preview(self, task: LocalTask) -> None:
        self.preview_image.setText("正在本地渲染前 8 页预览…")
        self.preview_image.setPixmap(QPixmap())
        worker = PreviewWorker(str(task.source), task.source)
        worker.ready.connect(self._preview_ready)
        worker.failed.connect(self._preview_failed)
        worker.finished.connect(lambda key=str(task.source): self.preview_workers.pop(key, None))
        self.preview_workers[str(task.source)] = worker
        worker.start()

    def _preview_ready(self, task_key: str, page_count: int, pages: list[bytes]) -> None:
        images = [QImage.fromData(QByteArray(data)) for data in pages]
        self.preview_pages[task_key] = (page_count, images)
        if self.active_preview_key == task_key:
            self.active_preview_index = min(self.active_preview_index, max(0, len(images) - 1))
            self._render_current_preview()

    def _preview_failed(self, task_key: str, message: str) -> None:
        if self.active_preview_key == task_key:
            self.preview_image.setText("无法生成预览：" + message)
        QMessageBox.warning(self, "预览失败", "无法生成该 PDF 的页面预览。\n\n" + message)

    def _active_preview_task(self) -> LocalTask | None:
        if not self.active_preview_key:
            return None
        return next((task for task in self.tasks if str(task.source) == self.active_preview_key), None)

    def _render_current_preview(self) -> None:
        task = self._active_preview_task()
        if task is None or self.active_preview_key not in self.preview_pages:
            self._update_preview_controls()
            return
        page_count, images = self.preview_pages[self.active_preview_key]
        if not images:
            self.preview_image.setText("此 PDF 没有可显示的页面。")
            self._update_preview_controls()
            return
        self.active_preview_index = max(0, min(self.active_preview_index, len(images) - 1))
        image = images[self.active_preview_index]
        pixmap = QPixmap.fromImage(image)
        viewport_size = self.preview_image.parentWidget().size() if self.preview_image.parentWidget() else self.preview_image.size()
        target = viewport_size.boundedTo(pixmap.size())
        self.preview_image.setPixmap(pixmap.scaled(target, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation))
        self.preview_image.setText("")
        current_page = self.active_preview_index + 1
        limit_note = f" · 仅加载前 {len(images)} 页" if page_count > len(images) else ""
        self.preview_counter.setText(f"第 {current_page} 页 / 共 {page_count} 页{limit_note}")
        self.set_first_page_button.setText("当前已是正文首页" if task.skip == current_page - 1 else "将当前页设为正文首页")
        self._refresh_thumbnails(images)
        self._update_preview_controls()

    def _refresh_thumbnails(self, images: list[QImage]) -> None:
        self.thumbnail_list.blockSignals(True)
        self.thumbnail_list.clear()
        for index, image in enumerate(images):
            pixmap = QPixmap.fromImage(image).scaled(76, 76, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
            item = QListWidgetItem(QIcon(pixmap), f"第 {index + 1} 页")
            item.setTextAlignment(Qt.AlignmentFlag.AlignHCenter)
            self.thumbnail_list.addItem(item)
        self.thumbnail_list.setCurrentRow(self.active_preview_index)
        self.thumbnail_list.blockSignals(False)

    def preview_page_selected(self, row: int) -> None:
        if row >= 0 and row != self.active_preview_index:
            self.active_preview_index = row
            self._render_current_preview()

    def change_preview_page(self, offset: int) -> None:
        if not self.active_preview_key or self.active_preview_key not in self.preview_pages:
            return
        _count, images = self.preview_pages[self.active_preview_key]
        self.active_preview_index = max(0, min(len(images) - 1, self.active_preview_index + offset))
        self._render_current_preview()

    def _update_preview_controls(self) -> None:
        has_preview = self.active_preview_key in self.preview_pages if self.active_preview_key else False
        page_count = len(self.preview_pages[self.active_preview_key][1]) if has_preview else 0
        self.preview_prev.setEnabled(has_preview and self.active_preview_index > 0)
        self.preview_next.setEnabled(has_preview and self.active_preview_index < page_count - 1)
        self.set_first_page_button.setEnabled(has_preview)

    def set_current_as_first_page(self) -> None:
        task = self._active_preview_task()
        if task is None or self.active_preview_key not in self.preview_pages:
            return
        task.skip = self.active_preview_index
        self.refresh_file_table()
        self._render_current_preview()
        QMessageBox.information(self, "已设置", f"已将第 {self.active_preview_index + 1} 页设为正文首页。")

    def choose_output_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "选择输出文件夹", self.output_edit.text() or str(Path.home()))
        if folder:
            self.output_edit.setText(folder)

    def default_output_root(self) -> Path:
        value = self.output_edit.text().strip()
        if value:
            return Path(value)
        if self.tasks:
            return self.tasks[0].source.parent / "拆分结果"
        return app_data_path("拆分结果")

    def _update_start_button(self) -> None:
        self.start_button.setEnabled(bool(self.tasks) and self.process is None)
        self.clear_files_button.setEnabled(self.process is None)
        self.add_files_button.setEnabled(self.process is None)

    def start_processing(self) -> None:
        if not self.tasks:
            return
        root = self.default_output_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            QMessageBox.critical(self, "无法创建输出目录", str(exc))
            return
        self.pending_tasks = list(self.tasks)
        self.task_tree.clear()
        self.log_view.clear()
        for task in self.tasks:
            task.prepare_directories(root)
            task.status = "等待处理"
            task.stage = "已加入队列"
            task.progress = 0
            task.logs.clear()
            task.error = None
        self._refresh_task_tree()
        self.cancel_button.setEnabled(True)
        self._update_start_button()
        self._start_next_task()

    def _start_next_task(self) -> None:
        if not self.pending_tasks:
            self.process = None
            self.current_task = None
            self.cancel_button.setEnabled(False)
            self._update_start_button()
            QMessageBox.information(self, "处理完成", "所有任务已处理完成。可在下方选择任务并打开输出目录验收结果。")
            return
        task = self.pending_tasks.pop(0)
        self.current_task = task
        task.status = "处理中"
        task.stage = "正在启动本地流水线"
        task.progress = 5
        task.append_log("[桌面版] 已启动本地处理任务")
        program, arguments = pipeline_command(task.source, task.output_dir, task.work_dir, task.skip)  # type: ignore[arg-type]
        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.setProgram(program)
        process.setArguments(arguments)
        process.setWorkingDirectory(str(ROOT))
        process.readyReadStandardOutput.connect(self._read_process_output)
        process.errorOccurred.connect(self._process_error)
        process.finished.connect(self._process_finished)
        self.process = process
        self._refresh_task_tree()
        process.start()

    def _read_process_output(self) -> None:
        if self.process is None or self.current_task is None:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        for line in text.splitlines():
            self.current_task.append_log(line)
            stage = stage_from_log(line)
            if stage:
                self.current_task.stage, self.current_task.progress = stage
        self._refresh_task_tree(preserve_selection=True)
        self._show_log_for_task(self.current_task)

    def _process_error(self, error: QProcess.ProcessError) -> None:
        if self.current_task is not None:
            self.current_task.append_log("[桌面版] 进程错误：" + str(error))

    def _process_finished(self, exit_code: int, _status: QProcess.ExitStatus) -> None:
        task = self.current_task
        if task is None:
            return
        if exit_code == 0:
            task.status = "已完成"
            task.stage = "处理完成"
            task.progress = 100
            task.append_log("[桌面版] 任务已完成")
        else:
            task.status = "失败"
            task.stage = "处理失败"
            task.error = f"流水线异常退出（代码 {exit_code}）"
            task.append_log("[桌面版] " + task.error)
        self.process = None
        self.current_task = None
        self._refresh_task_tree(preserve_selection=True)
        self._start_next_task()

    def stop_current_task(self) -> None:
        if self.process is None or self.current_task is None:
            return
        answer = QMessageBox.question(self, "停止任务", "确定要停止当前任务并取消后续队列吗？")
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.pending_tasks.clear()
        self.current_task.append_log("[桌面版] 用户请求停止任务")
        self.current_task.status = "已停止"
        self.current_task.stage = "已由用户停止"
        self.process.kill()

    def _refresh_task_tree(self, preserve_selection: bool = False) -> None:
        selected_source = None
        if preserve_selection and self.task_tree.selectedItems():
            selected_source = self.task_tree.selectedItems()[0].data(0, Qt.ItemDataRole.UserRole)
        self.task_tree.blockSignals(True)
        self.task_tree.clear()
        for task in self.tasks:
            item = QTreeWidgetItem([task.display_name, task.status, task.stage, f"{task.progress}%", str(task.output_dir or "—")])
            item.setData(0, Qt.ItemDataRole.UserRole, str(task.source))
            if task.status == "失败":
                item.setForeground(1, Qt.GlobalColor.red)
            elif task.status == "已完成":
                item.setForeground(1, Qt.GlobalColor.darkGreen)
            self.task_tree.addTopLevelItem(item)
            if selected_source == str(task.source):
                item.setSelected(True)
        self.task_tree.blockSignals(False)
        if self.task_tree.topLevelItemCount() and not self.task_tree.selectedItems():
            self.task_tree.setCurrentItem(self.task_tree.topLevelItem(0))

    def show_selected_log(self) -> None:
        items = self.task_tree.selectedItems()
        if not items:
            return
        key = items[0].data(0, Qt.ItemDataRole.UserRole)
        task = next((item for item in self.tasks if str(item.source) == key), None)
        if task:
            self._show_log_for_task(task)

    def _show_log_for_task(self, task: LocalTask) -> None:
        self.log_view.setPlainText("\n".join(task.logs[-500:]) or "暂时没有日志。")
        cursor = self.log_view.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self.log_view.setTextCursor(cursor)

    def open_selected_output(self) -> None:
        items = self.task_tree.selectedItems()
        if not items:
            QMessageBox.information(self, "请选择任务", "请先在任务列表中选择一个任务。")
            return
        key = items[0].data(0, Qt.ItemDataRole.UserRole)
        task = next((item for item in self.tasks if str(item.source) == key), None)
        if task is None or task.output_dir is None:
            return
        task.output_dir.mkdir(parents=True, exist_ok=True)
        if os.name == "nt":
            os.startfile(task.output_dir)  # type: ignore[attr-defined]

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() == Qt.Key.Key_Left:
            self.change_preview_page(-1)
            event.accept()
            return
        if event.key() == Qt.Key.Key_Right:
            self.change_preview_page(1)
            event.accept()
            return
        super().keyPressEvent(event)

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.process is not None:
            answer = QMessageBox.question(self, "正在处理", "当前仍有任务在运行。确定要关闭桌面应用吗？")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.kill()
        event.accept()


def run_pipeline_worker() -> None:
    sys.argv = [sys.argv[0], *sys.argv[2:]]
    script_dir = str(Path(getattr(sys, "_MEIPASS", ROOT)) / "scripts")
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)
    from run_pipeline import main as pipeline_main

    pipeline_main()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--pipeline-worker":
        run_pipeline_worker()
        return 0
    app = QApplication(sys.argv)
    app.setApplicationName("试卷合订本拆分器")
    app.setWindowIcon(QIcon(str(resource_path("assets", "app_icon.png"))))
    window = SplitterDesktopWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception:  # noqa: BLE001
        error = traceback.format_exc()
        QMessageBox.critical(None, "桌面应用启动失败", error)
        raise
