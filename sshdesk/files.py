"""远端文件面板：只显示服务器目录（不再要本地窗口）。

交互按常见 SFTP 客户端来：
- 列表第一行是「..」，双击进上一级；双击目录进去，双击文件交给上层处理
- 从资源管理器拖文件进来 = 上传到当前远端目录
- 把远端文件拖到资源管理器 = 先下到临时目录再交出 QDrag（拖出即下载）
- 工具栏保留 上传 / 下载到… / 新建 / 刷新 / 删除，作为拖拽的等价入口
"""

from __future__ import annotations

import os
import posixpath
import tempfile
import time
from typing import List, Optional, Tuple

from PySide6.QtCore import QMimeData, QThread, QUrl, Qt, Signal
from PySide6.QtGui import QDrag
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFileIconProvider, QHBoxLayout,
                               QHeaderView, QLabel,
                               QLineEdit, QMessageBox, QProgressDialog, QPushButton,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%d %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return str(n)


def human_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


class RemoteFS:
    """远端目录：走 SSHSession（appPublic.sshx / asyncssh）的同步接口。"""

    label = "远端"

    def __init__(self, session):
        self.session = session

    def home(self) -> str:
        return self.session.realpath(".")

    def listdir(self, path: str):
        return self.session.listdir(path)

    def is_dir(self, path: str) -> bool:
        return self.session.is_dir(path)

    def join(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def parent(self, path: str) -> str:
        p = posixpath.dirname(path.rstrip("/"))
        return p or "/"

    def mkdir(self, path: str) -> None:
        self.session.mkdir(path)

    def remove(self, path: str) -> None:
        if self.is_dir(path):
            self.session.rmdir(path)
        else:
            self.session.remove(path)


class _Transfer(QThread):
    progressed = Signal(int, int)
    failed = Signal(str)

    def __init__(self, fn, parent=None):
        super().__init__(parent)
        self.fn = fn

    def run(self) -> None:
        try:
            self.fn(lambda done, total: self.progressed.emit(done, total))
        except Exception as e:                  # noqa: BLE001
            self.failed.emit(str(e))


class RemoteTable(QTableWidget):
    """一条目录列表：支持拖入上传、拖出下载、双击进入。"""

    def __init__(self, parent=None):
        super().__init__(0, 3, parent)
        self.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        self.setShowGrid(False)
        self.verticalHeader().setDefaultSectionSize(24)
        self.verticalHeader().setVisible(False)
        self.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDrop)
        self.setDefaultDropAction(Qt.CopyAction)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)

        # 由 FileBrowser 注入：收拖入的本地路径 / 把远端文件落到临时文件返回本地路径
        self.collect_drop_paths = None
        self.fetch_to_temp = None

    # ---- 拖入：从资源管理器拖文件 → 上传 ----
    def dragEnterEvent(self, e) -> None:                # noqa: N802
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:                 # noqa: N802
        if e.mimeData().hasUrls():
            e.setDropAction(Qt.CopyAction)
            e.accept()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:                     # noqa: N802
        if not e.mimeData().hasUrls():
            super().dropEvent(e)
            return
        paths = []
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p and os.path.exists(p):
                paths.append(p)
        e.setDropAction(Qt.CopyAction)
        e.accept()
        if paths and self.collect_drop_paths:
            self.collect_drop_paths(paths)

    # ---- 拖出：拖到资源管理器 → 下载 ----
    def startDrag(self, actions) -> None:               # noqa: N802
        if not self.fetch_to_temp:
            return
        rows = sorted({i.row() for i in self.selectedIndexes()})
        names = []
        for r in rows:
            it = self.item(r, 0)
            data = it.data(Qt.UserRole) if it else None
            if data and not data[1] and not data[2]:    # 只拖文件，目录和 .. 不拖
                names.append(data[0])
        if not names:
            return
        local_files = [p for p in self.fetch_to_temp(names) if p]
        if not local_files:
            return
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(p) for p in local_files])
        drag = QDrag(self)
        drag.setMimeData(mime)
        drag.exec(Qt.CopyAction)


class FileBrowser(QWidget):
    """远端文件面板（单栏）。"""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        self.fs = RemoteFS(session)
        self.cwd = self.fs.home()
        self._th: Optional[_Transfer] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        tag = QLabel("远端文件")
        tag.setStyleSheet("color:#444; font-weight:600")
        tag.setFixedWidth(60)
        bar.addWidget(tag)
        self.path_edit = QLineEdit(self.cwd)
        self.path_edit.setMinimumWidth(120)
        self.path_edit.setToolTip("%s\n输入路径回车跳转" % self.cwd)
        self.path_edit.returnPressed.connect(lambda: self.chdir(self.path_edit.text().strip()))
        bar.addWidget(self.path_edit, 1)
        lay.addLayout(bar)

        ops = QHBoxLayout()
        ops.setSpacing(4)
        ops.setContentsMargins(60, 0, 0, 0)
        self.btn_upload = QPushButton("上传…")
        self.btn_upload.setToolTip("选择本地文件上传到当前目录（也可以直接从资源管理器拖进来）")
        self.btn_upload.clicked.connect(self.upload)
        self.btn_download = QPushButton("下载到…")
        self.btn_download.setToolTip("把选中的远端文件下载到本地目录（也可以直接拖到资源管理器）")
        self.btn_download.setEnabled(False)
        self.btn_download.clicked.connect(self.download)
        self.btn_mkdir = QPushButton("新建")
        self.btn_mkdir.setToolTip("在当前目录新建文件夹")
        self.btn_mkdir.clicked.connect(self.mkdir)
        self.btn_refresh = QPushButton("刷新")
        self.btn_refresh.setToolTip("重新读取目录（F5）")
        self.btn_refresh.clicked.connect(self.reload)
        self.btn_remove = QPushButton("删除")
        self.btn_remove.setToolTip("删除选中项")
        self.btn_remove.setEnabled(False)
        self.btn_remove.clicked.connect(self.remove)
        for b in (self.btn_upload, self.btn_download, self.btn_mkdir,
                  self.btn_refresh, self.btn_remove):
            ops.addWidget(b)
        ops.addStretch(1)
        self.hint = QLabel("拖文件进来上传 · 拖出去下载 · 双击目录进入")
        self.hint.setStyleSheet("color:#767676")
        ops.addWidget(self.hint)
        lay.addLayout(ops)

        self.table = RemoteTable(self)
        self.table.doubleClicked.connect(self._on_double)
        self.table.itemSelectionChanged.connect(self._on_sel)
        self.table.collect_drop_paths = self.upload_paths
        self.table.fetch_to_temp = self._fetch_to_temp
        lay.addWidget(self.table, 1)

        self.reload()

    # ---------------- 目录 ----------------
    def chdir(self, path: str, quiet: bool = False) -> bool:
        path = (path or self.cwd).strip()
        if not path:
            return False
        try:
            ok = self.fs.is_dir(path)
        except Exception as e:                  # noqa: BLE001
            if not quiet:
                QMessageBox.warning(self, "打不开", "%s\n%s" % (path, e))
            return False
        if not ok:
            if not quiet:
                QMessageBox.warning(self, "打不开", "%s 不是目录" % path)
            return False
        self.cwd = path
        self.path_edit.setText(path)
        self.path_edit.setToolTip("%s\n输入路径回车跳转" % path)
        self.reload()
        return True

    def reload(self) -> None:
        self.table.setRowCount(0)
        try:
            rows = self.fs.listdir(self.cwd)
        except Exception as e:                  # noqa: BLE001
            self.path_edit.setText("%s   ⚠ %s" % (self.cwd, str(e)[:60]))
            self.hint.setText("列目录失败：%s" % str(e)[:120])
            return
        ic = QFileIconProvider()
        dir_icon = ic.icon(QFileIconProvider.Folder)
        file_icon = ic.icon(QFileIconProvider.File)

        parent = self.fs.parent(self.cwd)
        up_row = 1 if parent != self.cwd else 0
        self.table.setRowCount(len(rows) + up_row)
        if up_row:
            it = QTableWidgetItem("..")
            it.setIcon(dir_icon)
            it.setData(Qt.UserRole, ("..", True, True))     # (name, is_dir, is_up)
            self.table.setItem(0, 0, it)
            self.table.setItem(0, 1, QTableWidgetItem(""))
            self.table.setItem(0, 2, QTableWidgetItem(""))
        for i, (name, size, mtime, is_dir) in enumerate(rows, start=up_row):
            item = QTableWidgetItem(name)
            item.setIcon(dir_icon if is_dir else file_icon)
            item.setData(Qt.UserRole, (name, is_dir, False))
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem("" if is_dir else human_size(size)))
            self.table.setItem(i, 2, QTableWidgetItem(human_time(mtime)))

    def _on_double(self, idx) -> None:
        it = self.table.item(idx.row(), 0)
        data = it.data(Qt.UserRole) if it else None
        if not data:
            return
        name, is_dir, is_up = data
        if is_up:
            self.chdir(self.fs.parent(self.cwd))
        elif is_dir:
            self.chdir(self.fs.join(self.cwd, name))
        else:
            self.hint.setText("已选：%s" % self.fs.join(self.cwd, name))

    def _on_sel(self, *_a) -> None:
        files = [d for _p, d in self.selected() if not d]
        self.btn_remove.setEnabled(bool(self.table.selectedIndexes()))
        self.btn_download.setEnabled(bool(files))
        self.hint.setText("" if files else "拖文件进来上传 · 拖出去下载 · 双击目录进入")

    def selected(self) -> List[Tuple[str, bool]]:
        out = []
        for idx in self.table.selectionModel().selectedRows():
            it = self.table.item(idx.row(), 0)
            data = it.data(Qt.UserRole) if it else None
            if not data or data[2]:             # 跳过 ..
                continue
            out.append((self.fs.join(self.cwd, data[0]), data[1]))
        return out

    def mkdir(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建目录", "目录名：")
        if not (ok and name.strip()):
            return
        try:
            self.fs.mkdir(self.fs.join(self.cwd, name.strip()))
            self.reload()
        except Exception as e:                  # noqa: BLE001
            QMessageBox.warning(self, "失败", str(e))

    def remove(self) -> None:
        items = self.selected()
        if not items:
            return
        if QMessageBox.question(self, "确认", "删除选中 %d 项？" % len(items)) != QMessageBox.Yes:
            return
        for path, _isdir in items:
            try:
                self.fs.remove(path)
            except Exception as e:              # noqa: BLE001
                QMessageBox.warning(self, "删除失败", "%s\n%s" % (path, e))
        self.reload()

    # ---------------- 传输 ----------------
    def _progress(self, title: str, fn) -> None:
        dlg = QProgressDialog(title, "取消", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        th = _Transfer(fn, self)

        def on_prog(done: int, total: int) -> None:
            dlg.setValue(int(done * 100 / total) if total else 0)
            dlg.setLabelText("%s  %s / %s" % (title, human_size(done), human_size(total)))

        th.progressed.connect(on_prog)
        th.failed.connect(lambda m: (dlg.close(), QMessageBox.warning(self, "传输失败", m)))
        th.finished.connect(lambda: (dlg.close(), self.reload()))
        dlg.canceled.connect(th.terminate)
        th.start()
        self._th = th

    def upload(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "上传到 %s" % self.cwd)
        if files:
            self.upload_paths(files)

    def upload_paths(self, paths: List[str]) -> None:
        """把本地路径（文件/目录）上传到当前远端目录。"""
        files: List[str] = []
        for p in paths:
            if os.path.isdir(p):
                for root, _dirs, fs in os.walk(p):
                    for f in fs:
                        files.append(os.path.join(root, f))
            elif os.path.isfile(p):
                files.append(p)
        if not files:
            return
        target = self.cwd

        def rel_name(local: str) -> str:
            for src in paths:
                if os.path.isdir(src) and local.startswith(src + os.sep):
                    return posixpath.join(os.path.basename(src),
                                          os.path.relpath(local, src).replace(os.sep, "/"))
            return os.path.basename(local)

        def work(cb) -> None:
            made = set()
            for local in files:
                rp = posixpath.join(target, rel_name(local))
                d = posixpath.dirname(rp)
                if d and d not in made:
                    try:
                        self.session.mkdir(d)
                    except Exception:            # noqa: BLE001
                        pass
                    made.add(d)
                self.session.upload(local, rp, cb)

        self.hint.setText("上传 %d 个文件到 %s" % (len(files), target))
        self._progress("上传到 %s" % target, work)

    def download(self) -> None:
        items = [p for p, d in self.selected() if not d]
        if not items:
            return
        target = QFileDialog.getExistingDirectory(self, "下载到", os.path.expanduser("~"))
        if not target:
            return

        def work(cb) -> None:
            for remote in items:
                local = os.path.join(target, posixpath.basename(remote))
                self.session.download(remote, local, cb)

        self._progress("下载到 %s" % target, work)

    def _fetch_to_temp(self, names: List[str]) -> List[str]:
        """拖出前先把远端文件下到临时目录，返回本地路径（失败返回空列表）。"""
        tmpdir = os.path.join(tempfile.gettempdir(), "sshdesk_drag")
        os.makedirs(tmpdir, exist_ok=True)
        out: List[str] = []
        errors: List[str] = []
        jobs = [(posixpath.join(self.cwd, n), os.path.join(tmpdir, posixpath.basename(n)))
                for n in names]

        def work(cb) -> None:
            for remote, local in jobs:
                try:
                    self.session.download(remote, local, cb)
                    out.append(local)
                except Exception as e:           # noqa: BLE001
                    errors.append(str(e))

        # 下载在后台线程，但拖放必须在 startDrag 返回前给到数据，所以这里同步等
        th = _Transfer(work, self)
        th.failed.connect(lambda m: errors.append(m))
        th.start()
        dlg = QProgressDialog("准备拖动（先下载到临时目录）", "取消", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(300)
        th.progressed.connect(lambda d, t: dlg.setValue(int(d * 100 / t) if t else 0))
        while th.isRunning():
            th.msleep(30)
        th.wait()
        dlg.close()
        if errors:
            QMessageBox.warning(self, "下载失败", errors[0])
        return out
