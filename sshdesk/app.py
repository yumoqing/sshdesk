"""主窗口：会话列表 + 多标签终端 + 文件面板 + 字体缩放。"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAction, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QDockWidget, QHBoxLayout, QLabel, QLineEdit, QSizePolicy,
                               QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
                               QPushButton, QSplitter, QStackedWidget, QTabWidget, QToolBar,
                               QVBoxLayout, QWidget, QDialog)

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
        self.splitter.setChildrenCollapsible(False)
        self.term.setMinimumWidth(320)
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
                self.browser.setMinimumWidth(420)
                # 等布局算完再设比例，否则此刻 width() 还是 0，终端会被压成一条
                from PySide6.QtCore import QTimer
                QTimer.singleShot(0, self._apply_split)
            except Exception as e:                 # noqa: BLE001
                self.failed.emit("打开文件面板失败：%s" % e)
                return
        if self.browser is not None:
            self.browser.setVisible(on)

    def _apply_split(self) -> None:
        w = max(self.width(), 1000)
        self.splitter.setSizes([int(w * 0.58), int(w * 0.42)])

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


class SessionRow(QWidget):
    """会话列表的一行：名称（粗）+ 地址（灰）+ 跳板徽标，比两行裸文本好扫。"""

    def __init__(self, h: HostProfile, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self._sel = False
        self.setStyleSheet("background: transparent")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(8, 6, 8, 6)
        lay.setSpacing(2)
        name = QLabel(h.name or "%s@%s" % (h.user, h.host))
        f = name.font()
        f.setBold(True)
        name.setFont(f)
        name.setStyleSheet("color:#1f1f1f")
        lay.addWidget(name)
        addr = QLabel("%s@%s:%s%s%s" % (h.user, h.host, h.port,
                                        "  ·  密钥" if h.auth == "key" else "  ·  密码",
                                        "  ·  跳板 %s" % h.jump_host if h.jump_host else ""))
        addr.setStyleSheet("color:#767676; font-size:11px")
        addr.setToolTip("%s@%s:%s" % (h.user, h.host, h.port))
        lay.addWidget(addr)

    def set_selected(self, on: bool) -> None:
        if on == self._sel:
            return
        self._sel = on
        self.setStyleSheet("background: %s; border-radius: 6px" % ("#dbe8ff" if on else "transparent"))


class EmptyState(QWidget):
    """没有打开会话时的引导页（原来是一大片空白）。"""

    new_requested = Signal()
    import_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setAlignment(Qt.AlignCenter)
        lay.setSpacing(10)
        title = QLabel("还没有打开的会话")
        tf = title.font()
        tf.setPointSize(tf.pointSize() + 6)
        tf.setBold(True)
        title.setFont(tf)
        title.setAlignment(Qt.AlignCenter)
        tip = QLabel("双击左侧会话直接连接，或点「新建会话」配置一台服务器。\n"
                     "支持密钥免密 / 密码登录，跳板机在会话设置里填。")
        tip.setAlignment(Qt.AlignCenter)
        tip.setStyleSheet("color:#767676")
        lay.addWidget(title)
        lay.addWidget(tip)
        row = QHBoxLayout()
        row.setAlignment(Qt.AlignCenter)
        b_new = QPushButton("＋  新建会话")
        b_new.setStyleSheet("QPushButton{background:#1677ff;color:#fff;border:0;border-radius:4px;"
                            "padding:8px 20px;font-weight:600}"
                            "QPushButton:hover{background:#4096ff}")
        b_new.clicked.connect(self.new_requested.emit)
        b_imp = QPushButton("从 ~/.ssh/config 导入")
        b_imp.clicked.connect(self.import_requested.emit)
        row.addWidget(b_new)
        row.addWidget(b_imp)
        holder = QWidget()
        holder.setLayout(row)
        lay.addWidget(holder)


class MainWindow(QMainWindow):
    def __init__(self, cfg: Optional[AppConfig] = None):
        super().__init__()
        self.cfg = cfg or AppConfig.load()
        self.setWindowTitle("sshdesk — SSH / SFTP 客户端")
        self.resize(1280, 820)

        # ---- 左侧会话列表 ----
        dock = QDockWidget("会话", self)
        dock.setFeatures(QDockWidget.DockWidgetMovable)   # 不允许浮动，标题栏就不出那个小图标
        box = QWidget()
        bl = QVBoxLayout(box)
        bl.setContentsMargins(4, 4, 4, 4)
        self.search = QLineEdit()
        self.search.setPlaceholderText("过滤…")
        self.search.textChanged.connect(self._fill_sessions)
        bl.addWidget(self.search)
        self.sessions = QListWidget()
        self.sessions.setStyleSheet(
            "QListWidget::item{padding:2px;border-radius:6px}"
            "QListWidget::item:hover{background:#eef4ff}"
            "QListWidget::item:selected{background:#dbe8ff}")
        self.sessions.setSpacing(2)
        self.sessions.itemDoubleClicked.connect(lambda _i: self.connect_current())
        self.sessions.setContextMenuPolicy(Qt.ActionsContextMenu)
        bl.addWidget(self.sessions, 1)
        row = QHBoxLayout()
        for text, fn, attr in (("＋ 新建", self.new_session, "btn_new"),
                               ("管理", self.manage_sessions, "btn_manage"),
                               ("连接", self.connect_current, "btn_connect")):
            b = QPushButton(text)
            b.clicked.connect(fn)
            setattr(self, attr, b)
            row.addWidget(b)
        bl.addLayout(row)
        dock.setWidget(box)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)

        # ---- 标签（无会话时显示引导页）----
        self.tabs = QTabWidget()
        self.tabs.setTabsClosable(True)
        self.tabs.setMovable(True)
        self.tabs.tabCloseRequested.connect(self.close_tab)
        self.empty = EmptyState()
        self.empty.new_requested.connect(self.new_session)
        self.empty.import_requested.connect(self.import_ssh_config)
        self.stack = QStackedWidget()
        self.stack.addWidget(self.empty)
        self.stack.addWidget(self.tabs)
        self.setCentralWidget(self.stack)

        # ---- 工具栏 ----
        tb = QToolBar("主工具栏")
        tb.setMovable(False)
        tb.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.addToolBar(tb)

        act_new = QAction("＋ 新建会话", self)
        act_new.setToolTip("新建一个服务器会话（Ctrl+N）")
        act_new.setShortcut(QKeySequence("Ctrl+N"))
        act_new.triggered.connect(self.new_session)
        tb.addAction(act_new)

        tb.addSeparator()
        self.act_files = QAction("文件面板", self, checkable=True)
        self.act_files.setToolTip("显示 / 隐藏右侧 SFTP 文件面板")
        self.act_files.setChecked(self.cfg.show_file_panel)
        self.act_files.triggered.connect(self.toggle_files)
        tb.addAction(self.act_files)

        self.act_reconnect = QAction("重连", self)
        act_reconnect = self.act_reconnect
        act_reconnect.setToolTip("断开当前标签并重新连接")
        act_reconnect.triggered.connect(self.reconnect_current)
        tb.addAction(act_reconnect)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)

        self.font_label = QLabel("字号 %d  " % self.cfg.font_size)
        self.font_label.setToolTip("终端字号（Ctrl+= 放大 / Ctrl+- 缩小 / Ctrl+0 复位）")
        tb.addWidget(self.font_label)
        act_font_dn = QAction("A－", self)
        act_font_dn.setToolTip("缩小字号 (Ctrl+-)")
        act_font_dn.setShortcut(QKeySequence("Ctrl+-"))
        act_font_dn.triggered.connect(lambda: self.bump_font(-1))
        tb.addAction(act_font_dn)
        act_font_up = QAction("A＋", self)
        act_font_up.setToolTip("放大字号 (Ctrl+=)")
        act_font_up.setShortcut(QKeySequence("Ctrl+="))
        act_font_up.triggered.connect(lambda: self.bump_font(+1))
        tb.addAction(act_font_up)
        act_font_0 = QAction("复位", self)
        act_font_0.setToolTip("字号恢复默认 (Ctrl+0)")
        act_font_0.setShortcut(QKeySequence("Ctrl+0"))
        act_font_0.triggered.connect(lambda: self.set_font(11))
        tb.addAction(act_font_0)

        self.sessions.currentItemChanged.connect(self._on_row_changed)
        self.sessions.itemSelectionChanged.connect(
            lambda: self.btn_connect.setEnabled(bool(self.sessions.selectedItems())))
        self.statusBar().setContentsMargins(8, 0, 8, 0)
        self.statusBar().showMessage("就绪")
        self._sync_empty_state()
        self._fill_sessions()

    # ---- 会话列表 ----
    def _fill_sessions(self) -> None:
        kw = self.search.text().strip().lower()
        self.sessions.clear()
        for h in self.cfg.hosts:
            if kw and kw not in ("%s %s %s %s" % (h.name, h.host, h.user, h.jump_host)).lower():
                continue
            item = QListWidgetItem()
            item.setData(Qt.UserRole, h)
            item.setSizeHint(SessionRow(h).sizeHint())
            self.sessions.addItem(item)
            self.sessions.setItemWidget(item, SessionRow(h))

    def _on_row_changed(self, cur, prev) -> None:
        for it in (prev, cur):
            if it is None:
                continue
            w = self.sessions.itemWidget(it)
            if isinstance(w, SessionRow):
                w.set_selected(it is cur)

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

    def import_ssh_config(self) -> None:
        dlg = SessionManager(self.cfg, self)
        dlg._import_ssh()
        self._fill_sessions()

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

    def _sync_empty_state(self) -> None:
        has_tabs = self.tabs.count() > 0
        self.stack.setCurrentWidget(self.tabs if has_tabs else self.empty)
        # 没有会话时，文件面板/重连 无用，禁用掉
        for act in (getattr(self, "act_files", None), getattr(self, "act_reconnect", None)):
            if act is not None:
                act.setEnabled(has_tabs)
        if hasattr(self, "btn_connect"):
            self.btn_connect.setEnabled(bool(self.sessions.selectedItems()))

    def open_tab(self, profile: HostProfile) -> None:
        tab = SessionTab(profile, self.cfg, self)
        tab.status.connect(lambda m: self.statusBar().showMessage(m, 8000))
        tab.failed.connect(self._on_tab_failed)
        idx = self.tabs.addTab(tab, profile.display())
        self.tabs.setCurrentIndex(idx)
        self._sync_empty_state()
        ok = tab.connect()
        if not ok:
            self.tabs.removeTab(idx)
            tab.deleteLater()
            return
        tab.term.setFocus()
        for i in range(self.sessions.count()):
            it = self.sessions.item(i)
            if it.data(Qt.UserRole) is profile:
                self.sessions.setCurrentItem(it)
                break

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
        self._sync_empty_state()

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


def _utf8_console() -> None:
    """Windows 控制台默认 cp1252，打印中文会 UnicodeEncodeError；窗口版就没有 stdout。"""
    for stream in (sys.stdout, sys.stderr):
        if stream is None:
            continue
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _emit(msg: str) -> None:
    try:
        if sys.stdout is not None:
            print(msg)
    except Exception:                              # noqa: BLE001
        pass


def _apply_ui_font(app: QApplication) -> None:
    """界面字体：优先中文字体，终端仍用等宽字体。"""
    from PySide6.QtGui import QFont, QFontDatabase
    fams = set(QFontDatabase.families())
    for name in ("Microsoft YaHei UI", "Microsoft YaHei", "Noto Sans CJK SC", "PingFang SC"):
        if name in fams:
            app.setFont(QFont(name, 9))
            return


def main(argv: Optional[List[str]] = None) -> int:
    _utf8_console()
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
                _emit(h.display())
            return 0
        if not a.startswith("-"):
            want = cfg.by_name(a)
            if want is None:
                _emit("找不到会话：%s" % a)
    if "--selftest" in argv:
        from PySide6.QtCore import QTimer
        win.show()
        QTimer.singleShot(1200, app.quit)
        rc = app.exec()
        _emit("selftest ok: %d 个会话，窗口正常起来" % len(cfg.hosts))
        return rc
    win.show()
    if want is not None:
        win.open_tab(want)
    return app.exec()