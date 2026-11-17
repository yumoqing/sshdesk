"""主窗口：会话列表 + 多标签终端 + 文件面板 + 字体缩放。"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDockWidget, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QSplitter, QTabWidget, QToolBar, QVBoxLayout,
                               QWidget, QDialog)

from .config import AppConfig, HostProfile
from .dialogs import HostDialog, SessionManager
from .files import FileBrowser
from .session import SSHError, SSHSession
from .term import TerminalView


class SessionTab(QWidget):
    """一个会话标签：左终端 + 右文件面板（可隐藏）。"""

    status = Signal(str)
    failed = Signal(str)

    def __init__(self, profile: HostProfile, cfg: AppConfig, parent=None):
        super().__init__(parent)
        self.profile = profile
        self.cfg = cfg
        self.session: Optional[SSHSession] = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.splitter = QSplitter(Qt.Horizontal)
        self.term = TerminalView(self, cfg.font_family, profile.font_size or cfg.font_size)
        self.splitter.addWidget(self.term)
        self.splitter.setStretchFactor(0, 3)
        self.browser: Optional[FileBrowser] = None
        lay.addWidget(self.splitter, 1)
        self.term.connected_changed.connect(self._on_conn_changed)

    # ---- 连接 ----
    def connect(self) -> bool:
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.session = SSHSession(self.profile).connect()
            chan = self.session.open_shell(self.term._screen.columns, self.term._screen.lines)
            self.term.attach_channel(chan)
            self.term._refresh_metrics()
            self.term._apply_size_to_chan()
            if self.cfg.show_file_panel:
                self.show_files(True)
            self.status.emit("已连接 %s（%s）" % (self.profile.display(),
                                                getattr(self.session, "host_key_status", "")))
            return True
        except SSHError as e:
            self.failed.emit(str(e))
            return False
        finally:
            QGuiApplication.restoreOverrideCursor()

    def show_files(self, on: bool) -> None:
        if on and self.browser is None and self.session is not None:
            try:
                self.browser = FileBrowser(self.session, self)
                self.splitter.addWidget(self.browser)
                self.splitter.setStretchFactor(1, 2)
                w = max(self.width(), 900)
                self.splitter.setSizes([int(w * 0.62), int(w * 0.38)])
            except Exception as e:                 # noqa: BLE001
                self.failed.emit("打开文件面板失败：%s" % e)
                return
        if self.browser is not None:
            self.browser.setVisible(on)

    def set_font_size(self, size: int) -> None:
        self.term.set_font_size(size)
        self.profile.font_size = size

    def close_session(self) -> None:
        self.term.detach()
        if self.session:
            self.session.close()
        self.session = None

    def _on_conn_changed(self, alive: bool) -> None:
        if not alive:
            self.status.emit("%s 已断开" % self.profile.display())


class MainWindow(QMainWindow):
    def __init__(self, cfg: Optional[AppConfig] = None):
        super().__init__()
        self.cfg = cfg or AppConfig.load()
        self.setWindowTitle("sshdesk — SSH / SFTP 客户端")
        self.resize(1280, 820)

        # ---- 左侧会话列表 ----
        dock = QDockWidget("会话", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤…")
        self.search.textChanged.connect(self._fill_sessions)
        bl.addWidget(self.search)
        self.sessions = QListWidget()
        self.sessions.itemDoubleClicked.connect(lambda _i: self.connect_current())
        self.sessions.setContextMenuPolicy(Qt.ActionsContextMenu)
        bl.addWidget(self.sessions, 1)
        row = QHBoxLayout()
        for text, fn in (("新建", self.new_session), ("管理", self.manage_sessions),
                         ("连接", self.connect_current)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        bl.addLayout(row)
        dock.setWidget(box)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # ---- 标签 ----
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.setCentralWidget(self.tabs)

        # ---- 工具栏 ----
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        self.addToolBar(tb)
        act_new = QAction("新建会话", self)
        act_new.triggered.connect(self.new_session)
        tb.addAction(act_new)
        act_manage = QAction("会话管理", self)
        act_manage.triggered.connect(self.manage_sessions)
        tb.addAction(act_manage)
        tb.addSeparator()
        act_font_up = QAction("A+", self)
        act_font_up.setShortcut(QKeySequence("Ctrl+="))
        act_font_up.triggered.connect(lambda: self.bump_font(+1))
        tb.addAction(act_font_up)
        act_font_dn = QAction("A-", self)
        act_font_dn.setShortcut(QKeySequence("Ctrl+-"))
        act_font_dn.triggered.connect(lambda: self.bump_font(-1))
        tb.addAction(act_font_dn)
        act_font_0 = QAction("A0", self)
        act_font_0.setShortcut(QKeySequence("Ctrl+0"))
        act_font_0.triggered.connect(lambda: self.set_font(self.cfg.font_size))
        tb.addAction(act_font_0)
        tb.addSeparator()
        self.act_files = QAction("文件面板", self, checkable=True)
        self.act_files.setChecked(self.cfg.show_file_panel)
        self.act_files.triggered.connect(self.toggle_files)
        tb.addAction(self.act_files)
        act_reconnect = QAction("重连", self)
        act_reconnect.triggered.connect(self.reconnect_current)
        tb.addAction(act_reconnect)
        self.font_label = QLabel(" 字号 %d " % self.cfg.font_size)
        tb.addWidget(self.font_label)

        self.statusBar().showMessage("就绪")
        self._fill_sessions()

    # ---- 会话列表 ----
    def _fill_sessions(self) -> None:
        kw = self.search.text().strip().lower()
        self.sessions.clear()
        for h in self.cfg.hosts:
            if kw and kw not in ("%s %s %s %s" % (h.name, h.host, h.user, h.jump_host)).lower():
                continue
            txt = "%s\n%s@%s:%s%s" % (h.name or h.host, h.user, h.host, h.port,
                                      ("  ↳ 跳板 %s" % h.jump_host) if h.jump_host else "")
            item = QListWidgetItem(txt)
            item.setData(Qt.UserRole, h)
            self.sessions.addItem(item)

    def _selected_host(self) -> Optional[HostProfile]:
        item = self.sessions.currentItem()
        return item.data(Qt.UserRole) if item else None

    # ---- 动作 ----
    def new_session(self) -> None:
        dlg = HostDialog(None, self)
        if dlg.exec() == QDialog.Accepted:
            h = dlg.result_profile()
            self.cfg.upsert(h)
            self.cfg.save()
            self._fill_sessions()
            self.open_tab(h)

    def manage_sessions(self) -> None:
        dlg = SessionManager(self.cfg, self)
        if dlg.exec() == QDialog.Accepted and dlg.chosen:
            self.open_tab(dlg.chosen)
        self._fill_sessions()

    def connect_current(self) -> None:
        h = self._selected_host()
        if h:
            self.open_tab(h)
        else:
            QMessageBox.information(self, "连接", "先在左侧选一个会话")

    def open_tab(self, profile: HostProfile) -> None:
        tab = SessionTab(profile, self.cfg, self)
        tab.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        tab.failed.connect(self._on_tab_failed)
        idx = self.tabs.addTab(tab, profile.display())
        self.tabs.setCurrentIndex(idx)
        ok = tab.connect()
        if not ok:
            self.tabs.removeTab(idx)
            tab.deleteLater()
            return
        tab.term.setFocus()

    def _on_tab_failed(self, msg: str) -> None:
        self.statusBar().showMessage(msg, 10000)
        QMessageBox.warning(self, "连接失败", msg)

    def close_tab(self, idx: int) -> None:
        w = self.tabs.widget(idx)
        if isinstance(w, SessionTab):
            w.close_session()
        self.tabs.removeTab(idx)
        if w:
            w.deleteLater()

    def reconnect_current(self) -> None:
        w = self.tabs.currentWidget()
        if not isinstance(w, SessionTab):
            return
        w.close_session()
        if not w.connect():
            self.statusBar().showMessage("重连失败", 8000)

    def toggle_files(self, on: bool) -> None:
        self.cfg.show_file_panel = on
        self.cfg.save()
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.show_files(on)

    # ---- 字体 ----
    def bump_font(self, delta: int) -> None:
        self.set_font(self.cfg.font_size + delta)

    def set_font(self, size: int) -> None:
        size = max(6, min(40, size))
        self.cfg.font_size = size
        self.cfg.save()
        self.font_label.setText(" 字号 %d " % size)
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.set_font_size(size)

    def closeEvent(self, e) -> None:                # noqa: N802
        for i in range(self.tabs.count()):
            w = self.tabs.widget(i)
            if isinstance(w, SessionTab):
                w.close_session()
        self.cfg.save()
        super().closeEvent(e)


def _apply_ui_font(app: QApplication) -> None:
    """界面字体：优先中文字体，终端仍用等宽字体。"""
    from PySide6.QtGui import QFont, QFontDatabase
    fams = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC"):
        if name in fams:
            app.setFont(QFont(name, 9))
            return


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv if argv is None else argv)
    cfg = AppConfig.load()
    app = QApplication(argv[:1])
    app.setApplicationName("sshdesk")
    app.setStyle("Fusion")
    _apply_ui_font(app)
    win = MainWindow(cfg)
    want = None
    for a in argv[1:]:
        if a in ("--list", "-l"):
            for h in cfg.hosts:
                print(h.display())
            return 0
        if not a.startswith("-"):
            want = cfg.by_name(a)
            if want is None:
                print("找不到会话：%s" % a, file=sys.stderr)
    if "--selftest" in argv:
        from PySide6.QtCore import QTimer
        win.show()
        QTimer.singleShot(1200, app.quit)
        rc = app.exec()
        print("selftest ok: %d 个会话，窗口正常起来" % len(cfg.hosts))
        return rc
    win.show()
    if want is not None:
        win.open_tab(want)
    return app.exec()