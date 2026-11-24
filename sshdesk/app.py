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

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QDockWidget, QInputDialog, QLabel, QLineEdit,
                               QMessageBox, QStatusBar, QTreeWidgetItem, QVBoxLayout, QWidget)

from bricks_qt import Ctx, build, load_file
from bricks_qt import theme as _theme
from bricks_qt.app import BricksWindow
from bricks_qt.remotefiles import RemoteFiles
from bricks_qt.terminal import Terminal

from . import __version__
from .config import AppConfig, HostProfile, default_mono_font
from .dialogs import HostDialog, SessionManager
from .license import License
from .session import SSHError, SSHSession

UI_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ui")
ASSETS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "assets")


def _app_asset(name: str) -> str:
    """打包后资源在 sys._MEIPASS 下，源码运行在仓库 assets/ 下。"""
    cands = []
    meipass = getattr(sys, "_MEIPASS", "")
    if meipass:
        cands += [os.path.join(meipass, "assets", name), os.path.join(meipass, name)]
    cands.append(os.path.join(ASSETS, name))
    for p in cands:
        if p and os.path.exists(p):
            return p
    return ""
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
        fam = profile.font_family or cfg.font_family or default_mono_font()
        try:
            self.term.set_font_family(fam)
        except Exception:                   # noqa: BLE001
            pass
        self.on_state = None                # 状态文字回调，宿主接到状态栏/页签
        self.term.connected_changed.connect(self._on_conn_state)

    # ---- 通道状态 ----
    def _on_conn_state(self, alive: bool) -> None:
        """通道结束要说到界面上：否则状态栏一直写「已连接」，敲键全被 send() 静默丢掉。"""
        if alive:
            return
        try:
            self.term.feed("\r\n\x1b[31m[连接已断开 —— 点「重连」重新连接]\x1b[0m\r\n".encode("utf-8"))
        except Exception:                   # noqa: BLE001
            pass
        if callable(self.on_state):
            self.on_state("连接已断开（点「重连」重新连接）")

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
            note = getattr(self.session, "route_note", "")
            ka = getattr(self.session, "keepalive_note", "")
            return "已连接 %s（%s）%s%s" % (self.profile.display(),
                                          getattr(self.session, "host_key_status", ""),
                                          " · " + note if note else "",
                                          " · " + ka if ka else "")
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

    def dispose(self) -> None:
        """拆掉一整页（关标签/测试收尾都走这里）。

        不能把控件留给 Python 的循环垃圾回收：`term.connected_changed` 连着本对象，
        page ↔ term 形成引用环，只有 gc 能回收它；而 gc 可能在 Qt 派发事件的过程中跑，
        在 processEvents 里析构控件就会段错误（Linux/offscreen 实测 6/8 崩）。
        所以这里先断开信号打断环，再把控件交给事件循环 deleteLater 删除。
        """
        self.close_session()
        try:
            self.term.connected_changed.disconnect(self._on_conn_state)
        except (RuntimeError, TypeError):       # 已断开或控件已销毁
            pass
        self.on_state = None
        w = getattr(self, "widget", None)
        self.widget = None
        if w is not None:
            try:
                w.setParent(None)
                w.deleteLater()
            except RuntimeError:
                pass


class MainWindow(BricksWindow):
    """主窗口：bricks .ui + 宿主动作。"""

    def __init__(self, cfg: Optional[AppConfig] = None):
        self.cfg = cfg or AppConfig.load()
        ctx = Ctx(base_dir=UI_DIR)
        node = load_file(os.path.join(UI_DIR, "main.ui"))
        super().__init__(node, ctx, "sshdesk %s — SSH / SFTP 客户端" % __version__)
        self.ctx = ctx
        self.resize(1280, 820)

        self.tree = ctx.refs["sessions"]            # QTreeWidget
        self.tabs = ctx.refs["workspace"]           # QTabWidget
        self.status_label: QLabel = ctx.refs["status"]
        self.font_label: QLabel = ctx.refs["font_label"]
        self.search = ctx.refs["search"]
        self.pages_: List[SessionPage] = []
        self._trash: List[QWidget] = []             # 等事件循环删掉的旧页
        if hasattr(self.tree, "setHeaderHidden"):
            self.tree.setHeaderHidden(True)      # dock 标题已经写着「会话」，表头再写一遍是重复
        self._install_dock(ctx)

        # .ui 里的 script 名字 → Python 函数
        ctx.on("toggle_sessions", lambda *a: self.toggle_sessions())
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
        ctx.on("license_info", lambda *a: self.show_license())

        from PySide6.QtGui import QIcon
        icon = QIcon()
        for n in (16, 24, 32, 48, 64, 128, 256, 512, 1024):
            p = _app_asset("sshdesk-%dx%d.png" % (n, n)) or (
                _app_asset("sshdesk-512.png") if n == 512 else "")
            if p:
                icon.addFile(p)
        if icon.availableSizes():
            self.setWindowIcon(icon)
        elif _app_asset("sshdesk.png"):
            self.setWindowIcon(QIcon(_app_asset("sshdesk.png")))
        self.font_label.setText("字号 %d" % self.cfg.font_size)
        self._init_license()
        self.fill_sessions()

    # ---------------- 授权（license）----------------
    def _init_license(self) -> None:
        """启动即算一次授权状态：徽标常驻状态栏，异常才弹一次窗。"""
        self.license = License()
        st = self.license.state()
        self.license_label = QLabel()
        self.license_label.setToolTip("点工具栏「授权」可查看机器码 / 粘贴授权文件")
        self.statusBar().addPermanentWidget(self.license_label)
        self._refresh_license_badge()
        hb = (self.license.data or {}).get("heartbeat") or {}
        if st.ok and hb.get("url"):
            self.license.heartbeat_async()          # 后台线程，绝不阻塞启动
        if not st.ok and st.mode != "trial":
            QTimer.singleShot(0, lambda: self.show_license(warn=True))

    def _refresh_license_badge(self) -> None:
        st = self.license.state(refresh=True)
        self.license_label.setText("  " + st.label())
        color = "#0f766e" if st.mode == "licensed" else ("#a63a0a" if st.ok else "#b91c1c")
        self.license_label.setStyleSheet("color:%s;" % color)

    def show_license(self, warn: bool = False) -> None:
        st = self.license.state(refresh=True)
        box = QMessageBox(self)
        box.setWindowTitle("授权")
        box.setIcon(QMessageBox.Warning if (warn or not st.ok) else QMessageBox.Information)
        box.setText(st.label())
        info = "机器码：%s\n授权文件：%s" % (self.license.machine_code(), self.license.path)
        if st.reason:
            info += "\n说明：%s" % st.reason
        box.setInformativeText(info)
        paste = box.addButton("粘贴授权…", QMessageBox.ActionRole)
        copyb = box.addButton("复制机器码", QMessageBox.ActionRole)
        box.addButton("关闭", QMessageBox.RejectRole)
        box.exec()
        if box.clickedButton() is paste:
            self.paste_license()
        elif box.clickedButton() is copyb:
            QGuiApplication.clipboard().setText(self.license.machine_code())
            self.status_label.setText("机器码已复制：%s" % self.license.machine_code())

    def paste_license(self) -> None:
        text, ok = QInputDialog.getMultiLineText(
            self, "粘贴授权", "把服务商发来的 .lic 内容整段粘贴进来：", "")
        if not ok or not (text or "").strip():
            return
        try:
            st = self.license.install(text)
        except Exception as e:                          # noqa: BLE001
            QMessageBox.warning(self, "授权无效", str(e)[:400])
            return
        self._refresh_license_badge()
        self.status_label.setText(st.label())
        if st.ok:
            QMessageBox.information(self, "授权成功", st.label())
        else:
            QMessageBox.warning(self, "授权未生效", "%s\n%s" % (st.label(), st.reason))

    # ---------------- 左侧会话窗：可浮动 / 可关闭 / 可拖窄 ----------------
    def _install_dock(self, ctx: Ctx) -> None:
        """.ui 里的 left 只是个 VBox：装进 QDockWidget 才能拖出去浮动、关闭、拖窄。"""
        left: QWidget = ctx.refs["left"]
        left.setMinimumWidth(150)
        left.setMaximumWidth(16777215)      # .ui 里的 px 宽度是 min==max 硬锁，这里解掉
        dock = QDockWidget("会话", self)
        dock.setObjectName("sessions_dock")
        dock.setFeatures(QDockWidget.DockWidgetMovable
                         | QDockWidget.DockWidgetFloatable
                         | QDockWidget.DockWidgetClosable)
        dock.setAllowedAreas(Qt.LeftDockWidgetArea | Qt.RightDockWidgetArea)
        dock.setWidget(left)
        self.addDockWidget(Qt.LeftDockWidgetArea, dock)
        self.dock = dock
        self.resizeDocks([dock], [280], Qt.Horizontal)
        # 窄屏（iPhone）：会话列表默认收起，工具栏「会话」按钮随时拉出，把宽度全让给终端
        QTimer.singleShot(0, self._adapt_narrow)
        # 工具栏横跨整窗、状态条落到窗口底部；否则 dock 会顶到最左边，把工具栏挤到右边去
        bar = ctx.refs.get("toolbar")
        self._bar = bar
        if bar is not None:
            self.setMenuWidget(bar)
        sb = QStatusBar()
        sb.addWidget(ctx.refs["status"], 1)
        self.setStatusBar(sb)
        btn = ctx.refs.get("btn_sessions")
        if btn is not None:
            btn.setCheckable(True)
            btn.setChecked(True)
            dock.visibilityChanged.connect(lambda on: btn.setChecked(bool(on)))

    def toggle_sessions(self) -> None:
        self.dock.setVisible(not self.dock.isVisible())

    def _adapt_narrow(self) -> None:
        """窄屏把侧边会话栏收起：iPhone 上左右并排会把终端挤没。"""
        if self.width() >= 700:
            return
        if self.dock.isVisible():
            self.dock.hide()
        # 工具栏同样放不下：先砍掉字号调节/管理/重连这些次要按钮，保住首屏可用
        bar = getattr(self, "_bar", None)
        if bar is None:
            return
        drop = ("管理", "重连", "A-", "A+", "复位")
        for w in bar.findChildren(QWidget):
            txt = w.text() if hasattr(w, "text") else ""
            if txt and (txt in drop or txt.startswith("字号")):
                w.hide()

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
        allowed, why = self.license.allow("max_sessions", len(self.pages_))
        if not allowed:                                 # 试用/未授权的会话数上限
            self.status_label.setText(why)
            QMessageBox.information(
                self, "需要授权",
                "%s\n\n机器码：%s\n点工具栏「授权」可粘贴授权文件。"
                % (why, self.license.machine_code()))
            return
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
        page.on_state = lambda msg, p=page: self._page_state(p, msg)

        try:
            self.status_label.setText(page.connect())
        except SSHError as e:
            self.status_label.setText("连接失败：%s" % e)
            QMessageBox.warning(self, "连接失败", str(e))
            self.close_tab(self.tabs.indexOf(holder), drop_only=True)
            return
        page.term.setFocus()

    def _page_state(self, page: "SessionPage", msg: str) -> None:
        """把会话状态同时写到状态栏和页签标题上。"""
        self.status_label.setText(msg)
        holder = getattr(page, "holder", None)
        idx = self.tabs.indexOf(holder) if holder is not None else -1
        if idx < 0:
            return
        name = page.profile.name or page.profile.host
        self.tabs.setTabText(idx, name + ("（已断开）" if "断开" in msg else ""))

    def close_tab(self, idx: int, drop_only: bool = False) -> None:
        if idx is None or idx < 0 or idx >= self.tabs.count():
            return
        holder = self.tabs.widget(idx)
        page = next((p for p in self.pages_ if getattr(p, "holder", None) is holder), None)
        if page is not None:
            try:
                page.dispose()                  # 停通道 + 断信号 + 控件交给事件循环删
            except Exception:                   # noqa: BLE001
                pass
            self.pages_.remove(page)
        self.tabs.removeTab(idx)
        if holder is not None:
            self._trash.append(holder)          # 留住包装对象，等 Qt 真删完再放手
            holder.deleteLater()
            QTimer.singleShot(0, self._trash.clear)
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
            self._page_state(page, page.connect())
        except SSHError as e:
            self._page_state(page, "重连失败：%s" % e)

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

    if "--license" in argv:                     # 运维/客服查授权，不需要图形界面
        lic = License()
        st = lic.state(refresh=True)
        _emit("机器码：%s" % lic.machine_code())
        _emit("状态  ：%s [%s]" % (st.label(), st.mode))
        _emit("授权文件：%s%s" % (lic.path, "" if os.path.isfile(lic.path) else "（不存在）"))
        return 0 if st.ok else 1

    if "--activate" in argv:                    # 命令行装授权：sshdesk --activate x.lic
        i = argv.index("--activate")
        src = argv[i + 1] if i + 1 < len(argv) else ""
        if not src or not os.path.isfile(src):
            _emit("用法：sshdesk --activate <授权文件.lic>")
            return 2
        with open(src, encoding="utf-8") as fh:
            st = License().install(fh.read())
        _emit("状态：%s [%s]" % (st.label(), st.mode))
        return 0 if st.ok else 1

    app = QApplication.instance() or QApplication(sys.argv[:1])
    _apply_ui_font(app)
    win = MainWindow(cfg)

    if "--selftest" in argv:
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        app.processEvents()
        _emit("selftest ok: sshdesk %s，%d 个会话，窗口正常起来（bricks .ui）"
              % (__version__, len(cfg.hosts)))
        return 0

    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
