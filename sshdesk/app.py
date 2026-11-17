"""sshdesk 主程序：界面完全由 bricks .ui 描述，bricks_qt 渲染成 Qt 原生窗口。

这份 app.py 只管"业务"——SSH/SFTP、配置、对话框；界面在 ui/main.ui 与 ui/session.ui 里：

    ui/main.ui      工具栏 + 会话树 + 工作区页签（TabPanel）
    ui/session.ui   一个会话的「终端 / 文件」两个页签（Terminal / RemoteFiles）

按钮点了什么，靠 Ctx 的宿主动作表：.ui 里 `binds[].actiontype=script` + `script=<名字>`，
桌面侧就到 ctx.on(名字) 注册的 Python 函数；同一份 .ui 在 web 侧跑的是同名 JS 函数。
"""

from __future__ import annotations

import os
import sys
from typing import List, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from bricks_qt import Ctx, build, load_file
from bricks_qt import theme as _theme
from bricks_qt.app import BricksWindow
from bricks_qt.remotefiles import RemoteFiles
from bricks_qt.terminal import Terminal

from .config import AppConfig, HostProfile
from .dialogs import HostDialog, SessionManager
from .session import SSHError, SSHSession

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
DEFAULT_FONT = 12


def _apply_ui_font(app: Optional[QApplication] = None) -> dict:
    """统一中文字体 + Fusion 风格（bricks_qt.theme.apply 的旧名，测试还在用）。"""
    return _theme.apply(app)


def _emit(msg: str) -> None:
    """冻结成窗口版 exe 时 stdout 是 None，这里兜住。"""
    try:
        if sys.stdout is None:
            return
        sys.stdout.write(str(msg) + "\n")
        sys.stdout.flush()
    except Exception:                       # noqa: BLE001
        pass


class SessionPage:
    """一个会话页：由 ui/session.ui 渲染出「终端 / 文件」，这里只负责连和不连。"""

    def __init__(self, profile: HostProfile, cfg: AppConfig, host_ctx: Ctx):
        self.profile = profile
        self.cfg = cfg
        node = load_file(os.path.join(UI_DIR, "session.ui"))
        sub = Ctx(base_dir=UI_DIR, asset_roots=host_ctx.asset_roots)
        sub.handlers = host_ctx.handlers          # 动作表共享，控件引用各自独立
        self.ctx = sub
        self.widget = build(node, sub)
        if self.widget is None:
            raise RuntimeError("session.ui 渲染失败")
        self.term: Terminal = sub.refs["term"]
        self.files: RemoteFiles = sub.refs["files"]
        self.pages = sub.refs["pages"]
        self.session: Optional[SSHSession] = None
        self.term.set_font_size(int(profile.font_size or cfg.font_size or DEFAULT_FONT))

    # ---- 兼容旧接口（测试/脚本用）----
    @property
    def browser(self) -> RemoteFiles:
        return self.files

    def show_files(self, on: bool) -> None:
        try:
            self.pages.setCurrentIndex(1 if on else 0)
        except Exception:                     # noqa: BLE001
            pass
        if not on:
            self.term.setFocus()

    def set_font_size(self, size: int) -> None:
        self.term.set_font_size(size)

    # ---- 连接 ----
    def ask_password(self) -> bool:
        pwd, ok = QInputDialog.getText(
            self.widget, "需要密码",
            "%s@%s 的登录密码：" % (self.profile.user, self.profile.host),
            QLineEdit.Password)
        if not ok or not pwd:
            return False
        self.profile.password = pwd
        return True

    def connect(self) -> str:
        """返回状态文字；失败抛 SSHError。"""
        if self.profile.auth == "password" and not self.profile.password:
            if not self.ask_password():
                raise SSHError("已取消：没有密码")
        QGuiApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.session = SSHSession(self.profile).connect()
            chan = self.session.open_shell(self.term._screen.columns, self.term._screen.lines)
            self.term.attach_channel(chan)
            self.term._refresh_metrics()
            self.term._apply_size_to_chan()
            self.files.attach(self.session)
            self.show_files(False)
            return "已连接 %s（%s）" % (self.profile.display(),
                                      getattr(self.session, "host_key_status", ""))
        finally:
            QGuiApplication.restoreOverrideCursor()

    def close_session(self) -> None:
        try:
            self.term.detach()
        except Exception:                      # noqa: BLE001
            pass
        if self.session is not None:
            try:
                self.session.close()
            except Exception:                  # noqa: BLE001
                pass
            self.session = None


class MainWindow(BricksWindow):
    """主窗口：bricks .ui + 宿主动作。"""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or AppConfig.load()
        ctx = Ctx(base_dir=UI_DIR)
        node = load_file(os.path.join(UI_DIR, "main.ui"))
        super().__init__(node, ctx, "sshdesk — SSH / SFTP 客户端（bricks）")
        self.ctx = ctx
        self.resize(1280, 820)

        self.tree = ctx.refs["sessions"]            # QTreeWidget
        self.tabs = ctx.refs["workspace"]           # QTabWidget
        self.status_label: QLabel = ctx.refs["status"]
        self.font_label: QLabel = ctx.refs["font_label"]
        self.search = ctx.refs["search"]
        self.pages_: List[SessionPage] = []
        if hasattr(self.tree, "setHeaderLabels"):
            self.tree.setHeaderLabels(["会话"])

        # .ui 里的 script 名字 → Python 函数
        ctx.on("new_session", lambda *a: self.new_session())
        ctx.on("manage_sessions", lambda *a: self.manage_sessions())
        ctx.on("connect_selected", lambda *a: self.connect_selected())
        ctx.on("reconnect", lambda *a: self.reconnect_current())
        ctx.on("font_up", lambda *a: self.bump_font(+1))
        ctx.on("font_down", lambda *a: self.bump_font(-1))
        ctx.on("font_reset", lambda *a: self.set_font(DEFAULT_FONT))
        ctx.on("filter_sessions", lambda n, e, text: self.fill_sessions(str(text or "")))
        ctx.on("session_activated", lambda n, e, label: self.connect_by_name(str(label or "")))
        ctx.on("close_tab", lambda n, e, idx: self.close_tab(int(idx)))

        self.font_label.setText("字号 %d" % self.cfg.font_size)
        self.fill_sessions()

    # ---------------- 会话列表 ----------------
    def fill_sessions(self, keyword: str = "") -> None:
        kw = (keyword or "").strip().lower()
        self.tree.clear()
        for h in self.cfg.hosts:
            blob = ("%s %s %s %s" % (h.name, h.user, h.host, h.jump_host or "")).lower()
            if kw and kw not in blob:
                continue
            it = QTreeWidgetItem([h.name])
            tip = "%s@%s:%s · %s" % (h.user, h.host, h.port,
                                     "密码" if h.auth == "password" else "密钥")
            if h.jump_host:
                tip += " · 跳板 %s" % h.jump_host
            it.setToolTip(0, tip)
            it.setData(0, Qt.UserRole, h.name)
            self.tree.addTopLevelItem(it)

    def _selected_host(self) -> Optional[HostProfile]:
        it = self.tree.currentItem()
        if it is None:
            return None
        return self.cfg.by_name(it.data(0, Qt.UserRole) or it.text(0))

    def connect_selected(self) -> None:
        h = self._selected_host()
        if h is None:
            self.status_label.setText("先在左侧选一个会话")
            return
        self.open_tab(h)

    def connect_by_name(self, name: str) -> None:
        h = self.cfg.by_name(name)
        if h is not None:
            self.open_tab(h)

    # ---------------- 标签 ----------------
    def open_tab(self, profile: HostProfile) -> None:
        try:
            page = SessionPage(profile, self.cfg, self.ctx)
        except Exception as e:                      # noqa: BLE001
            QMessageBox.warning(self, "渲染失败", str(e))
            return
        holder = QWidget()
        lay = QVBoxLayout(holder)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(page.widget)
        page.holder = holder
        self.pages_.append(page)
        self.tabs.addTab(holder, profile.name or profile.host)
        self.tabs.setCurrentWidget(holder)

        try:
            self.status_label.setText(page.connect())
        except SSHError as e:
            self.status_label.setText("连接失败：%s" % e)
            QMessageBox.warning(self, "连接失败", str(e))
            self.close_tab(self.tabs.indexOf(holder), drop_only=True)
            return
        page.term.setFocus()

    def close_tab(self, idx: int, drop_only: bool = False) -> None:
        if idx is None or idx < 0 or idx >= self.tabs.count():
            return
        holder = self.tabs.widget(idx)
        page = next((p for p in self.pages_ if getattr(p, "holder", None) is holder), None)
        if page is not None:
            try:
                page.close_session()
            except Exception:                       # noqa: BLE001
                pass
            self.pages_.remove(page)
        self.tabs.removeTab(idx)
        if not drop_only:
            self.status_label.setText("已关闭会话")

    # ---------------- 动作 ----------------
    def new_session(self) -> None:
        dlg = HostDialog(parent=self)
        if dlg.exec():
            h = dlg.result_profile()
            self.cfg.upsert(h)
            self.cfg.save()
            self.fill_sessions()
            self.open_tab(h)

    def manage_sessions(self) -> None:
        dlg = SessionManager(self.cfg, self)
        dlg.exec()
        self.cfg.save()
        self.fill_sessions()

    def reconnect_current(self) -> None:
        holder = self.tabs.currentWidget()
        page = next((p for p in self.pages_ if getattr(p, "holder", None) is holder), None)
        if page is None:
            self.status_label.setText("没有可重连的会话")
            return
        page.close_session()
        try:
            self.status_label.setText(page.connect())
        except SSHError as e:
            self.status_label.setText("重连失败：%s" % e)

    # ---------------- 字号 ----------------
    def bump_font(self, delta: int) -> None:
        self.set_font(self.cfg.font_size + delta)

    def set_font(self, size: int) -> None:
        size = max(6, min(40, int(size)))
        self.cfg.font_size = size
        self.cfg.save()
        self.font_label.setText("字号 %d" % size)
        for p in self.pages_:
            p.set_font_size(size)

    # ---------------- 生命周期 ----------------
    def closeEvent(self, e) -> None:                # noqa: N802
        for p in list(self.pages_):
            p.close_session()
        try:
            self.cfg.save()
        except Exception:                           # noqa: BLE001
            pass
        super().closeEvent(e)


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cfg = AppConfig.load()
    if "--list" in argv:
        for h in cfg.hosts:
            _emit("%-24s %s@%s:%s %s" % (h.name, h.user, h.host, h.port,
                                         "密码" if h.auth == "password" else "密钥"))
        return 0

    app = QApplication.instance() or QApplication(sys.argv[:1])
    _apply_ui_font(app)
    win = MainWindow(cfg)

    if "--selftest" in argv:
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        app.processEvents()
        _emit("selftest ok: %d 个会话，窗口正常起来（bricks .ui）" % len(cfg.hosts))
        return 0

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
