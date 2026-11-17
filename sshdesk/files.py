"""文件面板：左侧本地、右侧远端（SFTP），支持上传/下载/新建目录/删除，传输在后台线程。"""

from __future__ import annotations

import os
import posixpath
import stat as stat_mod
import time
from typing import Callable, List, Optional, Tuple

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QComboBox, QFileDialog, QFileIconProvider, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QProgressDialog, QPushButton, QSplitter,
                               QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)


def human_size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return "%.0f %s" % (n, unit) if unit == "B" else "%.1f %s" % (n, unit)
        n /= 1024.0
    return str(n)


def human_time(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


class LocalFS:
    label = "本地"

    def home(self) -> str:
        return os.path.expanduser("~")

    def listdir(self, path: str) -> List[Tuple[str, int, float, bool]]:
        out = []
        for name in os.listdir(path):
            full = os.path.join(path, name)
            try:
                st = os.stat(full)
                out.append((name, st.st_size, st.st_mtime, os.path.isdir(full)))
            except OSError:
                out.append((name, 0, 0.0, False))
        return sorted(out, key=lambda x: (not x[3], x[0].lower()))

    def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    def join(self, *parts: str) -> str:
        return os.path.join(*parts)

    def parent(self, path: str) -> str:
        return os.path.dirname(os.path.abspath(path)) or path

    def mkdir(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    def remove(self, path: str) -> None:
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.remove(path)


class RemoteFS:
    """远端目录：走 SSHSession（appPublic.sshx/asyncssh）的同步接口。"""

    label = "远端"

    def __init__(self, session, root: str = "."):
        self.session = session
        self._root = root

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


class FileTable(QWidget):
    """一个目录列表（本地或远端各一个）。"""

    activated = Signal(str)          # 双击文件
    dir_changed = Signal(str)
    sel_changed = Signal(bool)       # 是否有选中项

    def __init__(self, fs, title: str, parent=None):
        super().__init__(parent)
        self.fs = fs
        self.cwd = fs.home()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        bar = QHBoxLayout()
        bar.setSpacing(4)
        tag = QLabel(title.split(" ")[0] if title.startswith(("本地", "远端")) else title)
        tag.setStyleSheet("color:#444; font-weight:600")
        tag.setFixedWidth(34)
        tag.setToolTip(title)
        bar.addWidget(tag)
        self.path_edit = QLineEdit(self.cwd)
        self.path_edit.setMinimumWidth(100)
        self.path_edit.setToolTip("%s\n%s" % (title, self.cwd))
        self.path_edit.returnPressed.connect(lambda: self.chdir(self.path_edit.text()))
        bar.addWidget(self.path_edit, 1)
        lay.addLayout(bar)

        ops = QHBoxLayout()
        ops.setSpacing(4)
        ops.setContentsMargins(32, 0, 0, 0)
        for text, tip, fn, width in (
                ("上级", "上一级目录", lambda: self.chdir(self.fs.parent(self.cwd)), 44),
                ("刷新", "重新读取目录", self.reload, 44),
                ("新建", "在当前目录新建文件夹", self._mkdir, 44),
                ("删除", "删除选中项", self._remove, 44)):
            b = QPushButton(text)
            b.setToolTip(tip)
            b.setFixedWidth(width)
            b.clicked.connect(fn)
            if text == "删除":
                self.btn_remove = b
                b.setEnabled(False)
            if text == "上级":
                self.btn_up = b
            ops.addWidget(b)
        ops.addStretch(1)
        lay.addLayout(ops)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["名称", "大小", "修改时间"])
        self.table.setShowGrid(False)
        self.table.verticalHeader().setDefaultSectionSize(24)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)

        from PySide6.QtWidgets import QHeaderView
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.table.doubleClicked.connect(self._on_double)
        self.table.itemSelectionChanged.connect(
            lambda: self.sel_changed.emit(bool(self.table.selectedItems())))
        lay.addWidget(self.table, 1)
        self.sel_changed.connect(self.btn_remove.setEnabled)

        self.reload()

    # ---- 目录 ----
    def chdir(self, path: str) -> None:
        path = path or self.cwd
        try:
            if not self.fs.is_dir(path):
                return
        except Exception as e:                  # noqa: BLE001
            QMessageBox.warning(self, "打不开", str(e))
            return
        self.cwd = path
        self.path_edit.setText(path)
        self.reload()
        self.dir_changed.emit(path)

    def reload(self) -> None:
        self.table.setRowCount(0)
        try:
            self.btn_up.setEnabled(self.fs.parent(self.cwd) != self.cwd)
        except Exception:                       # noqa: BLE001
            pass
        try:
            rows = self.fs.listdir(self.cwd)
        except Exception as e:                  # noqa: BLE001
            # 自动加载失败不能弹模态框（会卡住无人值守场景），只在标题栏提示
            self.table.setRowCount(0)
            self.path_edit.setToolTip("列目录失败：%s" % e)
            self.path_edit.setText("%s   ⚠ %s" % (self.cwd, str(e)[:60]))
            return
        self.table.setRowCount(len(rows))
        ic = QFileIconProvider()
        dir_icon = ic.icon(QFileIconProvider.Folder)
        file_icon = ic.icon(QFileIconProvider.File)
        for i, (name, size, mtime, is_dir) in enumerate(rows):
            item = QTableWidgetItem(name)
            item.setIcon(dir_icon if is_dir else file_icon)
            item.setData(Qt.UserRole, (name, is_dir))
            self.table.setItem(i, 0, item)
            self.table.setItem(i, 1, QTableWidgetItem("" if is_dir else human_size(size)))
            self.table.setItem(i, 2, QTableWidgetItem(human_time(mtime)))
        self.table.resizeColumnToContents(1)

    def _on_double(self, idx) -> None:
        data = self.table.item(idx.row(), 0).data(Qt.UserRole)
        if not data:
            return
        name, is_dir = data
        full = self.fs.join(self.cwd, name)
        if is_dir:
            self.chdir(full)
        else:
            self.activated.emit(full)

    def selected(self) -> List[Tuple[str, bool]]:
        out = []
        for idx in self.table.selectionModel().selectedRows():
            data = self.table.item(idx.row(), 0).data(Qt.UserRole)
            if data:
                out.append((self.fs.join(self.cwd, data[0]), data[1]))
        return out

    def _mkdir(self) -> None:
        from PySide6.QtWidgets import QInputDialog
        name, ok = QInputDialog.getText(self, "新建目录", "目录名：")
        if ok and name:
            try:
                self.fs.mkdir(self.fs.join(self.cwd, name))
                self.reload()
            except Exception as e:              # noqa: BLE001
                QMessageBox.warning(self, "失败", str(e))

    def _remove(self) -> None:
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


class FileBrowser(QWidget):
    """本地 ↔ 远端 双栏 + 上传/下载。"""

    def __init__(self, session, parent=None):
        super().__init__(parent)
        self.session = session
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 4, 4, 4)

        # 本地在上、远端在下：上传是"往下走"，下载是"往上走"，箭头必须跟排布一致
        self.btn_upload = QPushButton("上传  本地 → 远端")
        self.btn_upload.setToolTip("把上方「本地」里选中的文件传到下方「远端」当前目录")
        self.btn_upload.clicked.connect(self.upload)
        self.btn_download = QPushButton("下载  远端 → 本地")
        self.btn_download.setToolTip("把下方「远端」里选中的文件传到上方「本地」当前目录")
        self.btn_download.clicked.connect(self.download)
        self.hint = QLabel("选中文件后才能上传 / 下载")
        self.hint.setStyleSheet("color:#767676")

        bar = QWidget()
        ops = QHBoxLayout(bar)
        ops.setContentsMargins(0, 2, 0, 2)
        ops.addWidget(self.btn_upload)
        ops.addWidget(self.btn_download)
        ops.addSpacing(8)
        ops.addWidget(self.hint, 1)
        bar.setFixedHeight(40)

        # 面板本身不宽，左右并排会把"名称"列压没；上下叠放，每栏都占满宽度
        sp = QSplitter(Qt.Vertical)
        self.local = FileTable(LocalFS(), "本地")
        self.remote = FileTable(RemoteFS(session), "远端 %s" % session.profile.host)
        sp.addWidget(self.local)
        sp.addWidget(bar)          # 传输按钮夹在两栏之间，方向一眼看懂
        sp.addWidget(self.remote)
        sp.setChildrenCollapsible(False)
        sp.setCollapsible(1, False)
        sp.setStretchFactor(0, 1)
        sp.setStretchFactor(2, 1)
        sp.setSizes([300, 40, 300])
        lay.addWidget(sp, 1)
        for t in (self.local, self.remote):
            t.sel_changed.connect(self._update_transfer_state)
        self.btn_upload.setEnabled(False)
        self.btn_download.setEnabled(False)
        self.remote.activated.connect(lambda p: self.hint.setText("远端文件：%s" % p))

    def _update_transfer_state(self, _=None) -> None:
        up = bool(self.local.table.selectedItems())
        down = bool(self.remote.table.selectedItems())
        self.btn_upload.setEnabled(up)
        self.btn_download.setEnabled(down)
        self.hint.setText("" if (up or down) else "选中文件后才能上传 / 下载")

    # ---- 传输 ----
    def _run_transfer(self, title: str, fn) -> None:
        dlg = QProgressDialog(title, "取消", 0, 100, self)
        dlg.setWindowModality(Qt.WindowModal)
        dlg.setMinimumDuration(0)
        th = _Transfer(fn, self)

        def on_prog(done: int, total: int):
            pct = int(done * 100 / total) if total else 0
            dlg.setValue(pct)
            dlg.setLabelText("%s  %s / %s" % (title, human_size(done), human_size(total)))

        def on_fail(msg: str):
            dlg.close()
            QMessageBox.warning(self, "传输失败", msg)

        th.progressed.connect(on_prog)
        th.failed.connect(on_fail)
        th.finished.connect(lambda: (dlg.close(), self.local.reload(), self.remote.reload()))
        dlg.canceled.connect(th.terminate)
        th.start()
        self._th = th

    def upload(self) -> None:
        items = [p for p, d in self.local.selected() if not d]
        if not items:
            QMessageBox.information(self, "上传", "先在左侧选中文件")
            return
        target_dir = self.remote.cwd
        jobs = items

        def work(cb):
            for local in jobs:
                remote = posixpath_join(target_dir, os.path.basename(local))
                self.session.upload(local, remote, cb)

        self._run_transfer("上传到 %s" % target_dir, work)

    def download(self) -> None:
        items = [p for p, d in self.remote.selected() if not d]
        if not items:
            QMessageBox.information(self, "下载", "先在右侧选中文件")
            return
        target_dir = self.local.cwd
        jobs = items

        def work(cb):
            for remote in jobs:
                local = os.path.join(target_dir, posixpath.basename(remote))
                self.session.download(remote, local, cb)

        self._run_transfer("下载到 %s" % target_dir, work)


def posixpath_join(a: str, b: str) -> str:
    return posixpath.join(a, b)