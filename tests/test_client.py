"""端到端测试：本地起 fake SSH 服务器，验密码/密钥登录、跳板、SFTP 上传下载、shell 往返、GUI 多标签与字体缩放。

大文件断点续传的用例单独在 tests/test_resume.py（那套会真搬几 MB、反复开关进度框，
混在长流程里容易踩到与本功能无关的 PySide 事件队列老问题）。

用法：python tests/test_client.py
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import paramiko                                    # noqa: E402

import fake_sshd                                   # noqa: E402
from sshdesk.config import AppConfig, HostProfile   # noqa: E402
from sshdesk.session import SSHError, SSHSession    # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    t0 = time.time()
    try:
        fn()
        PASS.append((name, time.time() - t0))
        print("  ✅ %-28s %.2fs" % (name, time.time() - t0))
    except Exception as e:                          # noqa: BLE001
        FAIL.append((name, str(e)))
        print("  ❌ %-28s %s" % (name, str(e)[:200]))
        traceback.print_exc(limit=2)


def drop_widget(widget) -> None:
    """把测试里造的控件交给事件循环删掉，别留给 gc。

    page.term 的 connected_changed 连着宿主对象，会形成只有 gc 能回收的引用环；
    gc 可能在 Qt 派发事件途中跑，在 processEvents 里析构控件就是段错误（实测 6/8 崩）。
    """
    if widget is None:
        return
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    try:
        widget.close()
        widget.setParent(None)
        widget.deleteLater()
    except RuntimeError:                            # 控件已被父控件带走
        return
    if app is not None:
        app.processEvents()


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sshdesk_test_")
    os.environ["SSHDESK_CONFIG_DIR"] = os.path.join(tmp, "cfg")
    sftp_root = os.path.join(tmp, "sftp")
    os.makedirs(sftp_root, exist_ok=True)

    # 测试用密钥
    key = paramiko.RSAKey.generate(2048)
    key_path = os.path.join(tmp, "id_test")
    key.write_private_key_file(key_path)
    pub = key

    target = fake_sshd.start(2201, root=sftp_root, authorized=[pub])
    jump_srv = fake_sshd.start(2203, root=sftp_root, authorized=[pub], allow_forward=True)

    base = dict(host="127.0.0.1", port=2201, user="test")
    pwd_prof = HostProfile(auth="password", password="test123", **base)
    key_prof = HostProfile(auth="key", key_path=key_path, **base)
    jump_prof = HostProfile(auth="key", key_path=key_path, jump_host="127.0.0.1",
                            jump_port=2203, jump_user="test", jump_auth="key",
                            jump_key_path=key_path, **base)

    print("== 会话能力 ==")

    def t_password():
        s = SSHSession(pwd_prof).connect()
        try:
            rc, out, err = s.exec("hello-password")
            assert rc == 0, "rc=%s err=%s" % (rc, err)
            assert "hello-password" in out, out
        finally:
            s.close()

    def t_key():
        s = SSHSession(key_prof).connect()
        try:
            rc, out, _ = s.exec("hello-key")
            assert "hello-key" in out, out
        finally:
            s.close()

    def t_bad_password():
        bad = HostProfile(auth="password", password="nope", **base)
        try:
            SSHSession(bad).connect()
        except SSHError as e:
            assert "认证失败" in str(e), str(e)
            return
        raise AssertionError("错误密码竟然连上了")

    def t_probe():
        """直连探测本身：本机监听端口=可达，没人监听=不可达，且必须有超时上限。"""
        from sshdesk.session import _probe_direct
        assert _probe_direct("127.0.0.1", 2201, 2.0) is True, "本机监听端口却探测成不可达"
        assert _probe_direct("127.0.0.1", 2299, 1.0) is False, "没人监听的端口却探测成可达"
        t0 = time.time()
        _probe_direct("10.255.255.1", 22, 0.6)          # 黑洞地址：必须按时返回（值不关心）
        assert time.time() - t0 < 3.0, "探测没有超时上限：%.1fs" % (time.time() - t0)

    def t_jump_skip():
        """**目标直连可达时不该用跳板机**（本地 127.0.0.1:2201 必然可达）。"""
        s = SSHSession(jump_prof).connect()
        try:
            assert s.via_jump is False, "直连可达却还走了跳板机"
            assert "已跳过跳板机" in s.route_note, s.route_note
            rc, out, _ = s.exec("direct-no-jump")
            assert "direct-no-jump" in out, out
        finally:
            s.close()
        assert not any(h.events["forward"].is_set() for h in jump_srv.handlers), \
            "直连可达却仍然开了 direct-tcpip 通道"

    def t_jump():
        """探测不到直连时才走跳板：把探测钉成不可达（本地目标永远是可达的，没法真造）。"""
        import sshdesk.session as session_mod
        old = session_mod._probe_direct
        session_mod._probe_direct = lambda *a, **kw: False
        try:
            s = SSHSession(jump_prof).connect()
        finally:
            session_mod._probe_direct = old
        try:
            assert s.via_jump is True, "没走跳板机"
            rc, out, _ = s.exec("through-jump")
            assert "through-jump" in out, out
        finally:
            s.close()
        assert any(h.events["forward"].is_set() for h in jump_srv.handlers), "跳板没有被用到"

    def t_jump_fallback():
        """探测说可达、SSH 却连不上（端口是别的服务）→ 必须退回跳板机再试，不能直接报错。"""
        import socket as _socket
        import threading

        srv = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        srv.setsockopt(_socket.SOL_SOCKET, _socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 2205))
        srv.listen(32)

        def _accept_and_close():
            while True:
                try:
                    c, _ = srv.accept()
                except OSError:
                    return
                c.close()

        threading.Thread(target=_accept_and_close, daemon=True).start()
        prof = HostProfile(auth="key", key_path=key_path, host="127.0.0.1", port=2205,
                           user="test", jump_host="127.0.0.1", jump_port=2203,
                           jump_user="test", jump_auth="key", jump_key_path=key_path)
        try:
            SSHSession(prof).connect()
        except SSHError as e:
            assert "经跳板机也失败" in str(e), str(e)[:160]
        else:
            raise AssertionError("直连和跳板都没连上却成功返回了")
        finally:
            srv.close()

    def t_sftp_roundtrip():
        s = SSHSession(key_prof).connect()
        try:
            local = os.path.join(tmp, "up.txt")
            with open(local, "w", encoding="utf-8") as fh:
                fh.write("sshdesk 传输测试\n" * 500)
            sent = s.upload(local, "/up.txt")
            assert sent == os.path.getsize(local), sent
            back = os.path.join(tmp, "down.txt")
            got = s.download("/up.txt", back)
            assert got == sent, (got, sent)
            assert sha256(local) == sha256(back), "下载内容与上传不一致"
            names = [n for n, _sz, _mt, _d in s.listdir("/")]
            assert "up.txt" in names, names
            s.mkdir("/sub")
            assert "sub" in [n for n, _sz, _mt, _d in s.listdir("/")]
            # 远端文件确实落在 root 里
            assert os.path.isfile(os.path.join(sftp_root, "up.txt")), "服务端没有该文件"
        finally:
            s.close()

    def t_shell():
        s = SSHSession(key_prof).connect()
        try:
            chan = s.open_shell(90, 28)
            chan.sendall(b"hello-terminal\r")
            buf = b""
            for _ in range(30):
                time.sleep(0.1)
                try:
                    data = chan.recv(65536)
                except Exception:                   # noqa: BLE001
                    data = b""
                if data:
                    buf += data
                if b"<hello-terminal>" in buf:
                    return
            raise AssertionError("shell 没回显：%r" % buf[:120])
        finally:
            s.close()

    def t_hostkey():
        from sshdesk.config import known_hosts_path
        from sshdesk.session import SSHSession, SSHError
        kh = known_hosts_path()
        if os.path.exists(kh):
            os.remove(kh)
        strict = HostProfile(auth="key", key_path=key_path, hostkey="strict", **base)
        try:
            SSHSession(strict).connect()
        except SSHError as e:
            assert "未知主机密钥" in str(e), str(e)[:120]
        else:
            raise AssertionError("严格模式不该放行未知主机")
        auto = HostProfile(auth="key", key_path=key_path, hostkey="auto", **base)
        s1 = SSHSession(auto).connect()
        assert "新增信任" in s1.host_key_status, s1.host_key_status
        s1.close()
        assert os.path.isfile(kh), "known_hosts 没写出来"
        s2 = SSHSession(auto).connect()
        assert "校验通过" in s2.host_key_status, s2.host_key_status
        s2.close()
        # 篡改 → 必须拒绝
        lines = [l for l in open(kh, encoding="utf-8").read().splitlines() if l.strip()]
        parts = lines[0].split()
        parts[2] = "AAAAB3NzaC1yc2EAAAADAQABAAAAgQCtampered0000000000000000"
        open(kh, "w", encoding="utf-8").write(" ".join(parts) + "\n")
        try:
            SSHSession(auto).connect()
        except SSHError as e:
            assert "主机密钥变了" in str(e), str(e)[:120]
        else:
            raise AssertionError("密钥被篡改还放行了")
        open(kh, "w", encoding="utf-8").write("\n".join(lines) + "\n")

    check("主机密钥 strict/TOFU/篡改检测", t_hostkey)

    def t_scrollback():
        from sshdesk.term import TerminalView
        from PySide6.QtWidgets import QApplication
        from PySide6.QtCore import QSize
        app = QApplication.instance() or QApplication(sys.argv[:1])
        tv = TerminalView(None, "Consolas", 10)
        tv.resize(QSize(600, 300))
        tv.show()
        app.processEvents()
        for i in range(200):
            tv._stream.feed(("line-%03d\r\n" % i).encode())
        app.processEvents()
        tail = tv.selected_text()
        assert "line-199" in tail, "最新内容不在当前页"
        tv.scroll_history(3)          # 往上翻 3 屏
        app.processEvents()
        back = tv.selected_text()
        assert "line-199" not in back, "回滚后不该还显示最新行"
        assert any("line-%03d" % i in back for i in range(120, 190)), back[:120]
        tv.scroll_history(-99)        # 回到底部
        app.processEvents()
        assert "line-199" in tv.selected_text(), "回到底部失败"
        tv.close()

    check("终端滚动历史回看", t_scrollback)

    check("密码登录 + exec", t_password)
    check("密钥登录（免密）+ exec", t_key)
    check("错误密码被拒", t_bad_password)
    check("直连探测（可达/不可达/限时）", t_probe)
    check("直连可达则不用跳板机", t_jump_skip)
    check("直连不可达才用跳板机", t_jump)
    check("直连失败自动退回跳板机", t_jump_fallback)
    check("SFTP 上传/下载/列表/建目录", t_sftp_roundtrip)
    check("交互 shell 往返", t_shell)

    # ---------- GUI ----------
    print("== GUI（offscreen）==")

    def t_gui():
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtGui import QImage
        from PySide6.QtWidgets import QApplication

        from sshdesk.app import MainWindow
        from sshdesk.config import AppConfig

        app = QApplication.instance() or QApplication(sys.argv[:1])
        app.setStyle("Fusion")
        cfg = AppConfig(hosts=[key_prof], font_size=11)
        win = MainWindow(cfg)
        win.resize(QSize(1240, 800))
        win.setAttribute(Qt.WA_DontShowOnScreen, True)
        win.show()
        app.processEvents()

        win.open_tab(key_prof)
        for _ in range(40):
            app.processEvents()
            time.sleep(0.05)
        assert win.tabs.count() == 1, "标签数=%d" % win.tabs.count()
        tab = win.pages_[0]
        win.open_tab(key_prof)
        app.processEvents()
        assert win.tabs.count() == 2, "多标签失败：%d" % win.tabs.count()

        # 字体缩放：字号变大 → 单元格高度变大
        before = tab.term._cell_h
        win.set_font(18)
        app.processEvents()
        after = tab.term._cell_h
        assert after > before, "字号放大后 cell 高度没变（%s → %s）" % (before, after)
        win.set_font(11)
        app.processEvents()

        # 文件面板
        tab.show_files(True)
        app.processEvents()
        assert tab.files is not None, "文件面板没建起来"
        assert tab.files.cwd, "远端目录为空"
        assert tab.files.table.rowCount() >= 1, "远端目录没有条目"
        names = [tab.files.table.item(r, 0).text() for r in range(tab.files.table.rowCount())]
        assert names, "远端目录为空"
        assert tab.files.cwd == "/" and ".." not in names, \
            "根目录不该有 ..：%r" % names[:3]

        # 终端内容断言（窗口截图对自绘控件不可靠，另存一张终端单独的图作证据）
        screen_text = tab.term.selected_text()
        assert "fake-ssh ready" in screen_text, "终端没收到服务端输出：%r" % screen_text[:80]
        tab.term.repaint()
        app.processEvents()
        os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
        tab.term.grab().save(os.path.join(ROOT, "out", "terminal.png"))
        win.repaint()
        app.processEvents()
        win.grab().save(os.path.join(ROOT, "out", "sshdesk.png"))
        img = QImage(os.path.join(ROOT, "out", "sshdesk.png"))
        assert not img.isNull(), "截图失败"
        win.close()
        for i in reversed(range(win.tabs.count())):
            win.close_tab(i)
        app.processEvents()
        drop_widget(win)

    # ---------- 终端彩色输出必须真的画出来（回归：pyte 的颜色是字符串/十六进制/int，
    # 早期直接拿字符串下标 ANSI16 会抛异常 → paintEvent 中断，只剩第一个彩色字符之前的内容）----------
    def t_color():
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtWidgets import QApplication
        from sshdesk.term import TerminalView
        app = QApplication.instance() or QApplication(sys.argv[:1])
        tv = TerminalView(font_family="Consolas", font_size=12)
        tv.resize(QSize(800, 300))
        tv.setAttribute(Qt.WA_DontShowOnScreen, True)
        tv.show()
        tv._refresh_metrics()
        tv._stream.feed(b"(py3) \x1b]0;t\x07\x1b[01;32mymq@home\x1b[00m:"
                        b"\x1b[01;34m~\x1b[00m$ \x1b[38;5;208mORANGE\x1b[0m "
                        b"\x1b[38;2;255;0;0mRED\x1b[0m\r\n")
        app.processEvents()
        txt = tv.screen_text()
        assert "ymq@home:~$" in txt, "彩色提示符没进屏幕缓冲区：%r" % txt[:80]
        assert "ORANGE" in txt and "RED" in txt, "256 色/真彩文本丢了：%r" % txt[:80]
        img = tv.grab().toImage()          # 触发 paintEvent：颜色解析炸就会在这里抛
        assert not img.isNull(), "终端截图失败"
        # 抗锯齿会把纯色打散，按色相判定而不是精确 hex
        green = orange = red = 0
        for y in range(0, min(img.height(), 40)):
            for x in range(0, img.width()):
                c = img.pixelColor(x, y)
                r, g, b = c.red(), c.green(), c.blue()
                if g > 140 and r < 90 and b < 90:
                    green += 1
                if r > 220 and 100 < g < 190 and b < 60:
                    orange += 1
                if r > 190 and g < 70 and b < 70:
                    red += 1
        assert green, "绿色提示符没画出来"
        assert orange, "256 色调色板没画出来"
        assert red, "真彩没画出来"
        drop_widget(tv)

    check("终端彩色输出渲染", t_color)

    # ---------- 目录交互：.. / 双击进入 ----------
    def t_nav():
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        prof = key_prof
        sess = SSHSession(prof).connect()
        from sshdesk.files import FileBrowser
        fb = FileBrowser(sess)
        fb.resize(QSize(700, 500))
        app.processEvents()
        names = [fb.table.item(r, 0).text() for r in range(fb.table.rowCount())]
        assert "sub" in names, "远端根目录里没有 sub：%r" % names[:6]
        root = fb.cwd
        # 双击 sub 进入
        row = names.index("sub")
        fb._on_double(fb.table.model().index(row, 0))
        app.processEvents()
        assert fb.cwd.rstrip("/").endswith("sub"), "双击没进目录：%s" % fb.cwd
        names2 = [fb.table.item(r, 0).text() for r in range(fb.table.rowCount())]
        assert names2[0] == "..", "子目录里没有 ..：%r" % names2[:3]
        # 双击 .. 回上级
        fb._on_double(fb.table.model().index(0, 0))
        app.processEvents()
        assert fb.cwd == root, ".. 没回上级：%s != %s" % (fb.cwd, root)
        sess.close()
        drop_widget(fb)

    check("目录 .. / 双击进入", t_nav)

    # ---------- 拖入上传 ----------
    def t_drop_upload():
        from PySide6.QtCore import QMimeData, QSize, QUrl, Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        sess = SSHSession(key_prof).connect()
        from sshdesk.files import FileBrowser
        fb = FileBrowser(sess)
        fb.resize(QSize(700, 500))
        fb.chdir("/")
        app.processEvents()
        local = os.path.join(tmp, "拖入测试.txt")
        with open(local, "w", encoding="utf-8") as fh:
            fh.write("dropped 内容 123")
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(local)])

        class _Ev:
            def __init__(self, m):
                self._m = m

            def mimeData(self):
                return self._m

            def setDropAction(self, a):
                pass

            def accept(self):
                pass

        fb.table.dropEvent(_Ev(mime))
        for _ in range(60):
            app.processEvents()
            time.sleep(0.05)
            if os.path.basename(local) in [r[0] for r in sess.listdir("/")]:
                break
        names = [r[0] for r in sess.listdir("/")]
        assert "拖入测试.txt" in names, "拖入上传没落地：%r" % names
        assert sess.stat_size("/拖入测试.txt") == len("dropped 内容 123".encode()), "上传内容长度不对"

        # 拖出：下到临时目录
        got = fb._fetch_to_temp(["拖入测试.txt"])
        assert got and os.path.isfile(got[0]), "拖出下载失败：%r" % got
        assert open(got[0], encoding="utf-8").read() == "dropped 内容 123", "拖出内容不对"
        sess.remove("/拖入测试.txt")
        sess.close()
        drop_widget(fb)

    check("拖拽上传 / 拖出下载", t_drop_upload)

    # ---------- 中文输入（走输入法提交）----------
    def t_ime():
        from PySide6.QtCore import QSize, Qt
        from PySide6.QtGui import QInputMethodEvent
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        prof = key_prof
        sess = SSHSession(prof).connect()
        from sshdesk.term import TerminalView
        tv = TerminalView(font_family="Consolas", font_size=12)
        tv.resize(QSize(700, 300))
        tv.setAttribute(Qt.WA_DontShowOnScreen, True)
        tv.show()
        tv._refresh_metrics()
        chan = sess.open_shell(tv._screen.columns, tv._screen.lines)
        tv.attach_channel(chan)
        for _ in range(20):
            app.processEvents()
            time.sleep(0.03)
        ev = QInputMethodEvent()
        ev.setCommitString("中文输入测试")
        tv.inputMethodEvent(ev)
        app.processEvents()
        from PySide6.QtGui import QKeyEvent
        tv.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Return, Qt.NoModifier))
        deadline = time.time() + 6
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.03)
            if "中文输入测试" in tv.screen_text():
                break
        txt = tv.screen_text()
        assert "中文输入测试" in txt, "中文没发出去/没回显：%r" % txt[-80:]
        tv.detach()
        sess.close()
        drop_widget(tv)

    check("终端中文输入", t_ime)

    # ---------- 掉线必须说出来（原来通道结束是静默的，敲键全被丢掉）----------
    def t_disconnect():
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        from bricks_qt import Ctx
        from sshdesk.app import UI_DIR, SessionPage
        from sshdesk.config import AppConfig
        page = SessionPage(key_prof, AppConfig(), Ctx(base_dir=UI_DIR))
        said = []
        page.on_state = said.append
        page.connect()
        assert page.session is not None
        page.session._chan.close()                       # 服务端关通道 = 掉线
        deadline = time.time() + 6
        while time.time() < deadline and not said:
            app.processEvents()
            time.sleep(0.03)
        assert said, "通道结束后没有通知宿主（状态栏会一直写「已连接」，敲键被静默丢弃）"
        assert "断开" in said[0], "提示文案里没有「断开」：%r" % (said[0],)
        assert "断开" in page.term.screen_text(), "终端里没画出断线提示：%r" % page.term.screen_text()[-60:]
        page.dispose()
        drop_widget(page.widget)

    check("掉线提示", t_disconnect)

    # ---------- 空闲不得误判断链：recv() 只有真 EOF 才许返回空 ----------
    def t_idle_no_false_eof():
        import queue as _q
        from sshdesk.session import ShellChannel

        class _Q:
            """没有数据时**必须**让调用方一直等；带 timeout 就让上层拿到空串 = 误判断线。"""

            def get(self, *a, **kw):
                if kw:
                    raise _q.Empty
                raise RuntimeError("blocked-forever")

        ch = object.__new__(ShellChannel)
        ch._q = _Q()
        try:
            got = ch.recv(10)
        except RuntimeError as e:
            assert "blocked-forever" in str(e), e
        else:
            raise AssertionError(
                "recv() 空闲就返回了 %r —— 上层把它当 EOF，会误报「连接已断开」（老 bug：get(timeout=600)）" % (got,))

    check("空闲不误判断链", t_idle_no_false_eof)

    # ---------- 拖一整个目录进去：文件要落地，且终端通道不许被判掉线 ----------
    def t_drop_dir_alive():
        from PySide6.QtCore import QMimeData, QSize, QUrl, Qt
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance() or QApplication(sys.argv[:1])
        sess = SSHSession(key_prof).connect()
        from sshdesk.term import TerminalView
        from sshdesk.files import FileBrowser
        tv = TerminalView(font_family="Consolas", font_size=12)
        tv.resize(QSize(700, 300))
        tv.setAttribute(Qt.WA_DontShowOnScreen, True)
        tv.show()
        tv._refresh_metrics()
        chan = sess.open_shell(tv._screen.columns, tv._screen.lines)
        tv.attach_channel(chan)
        dead = []
        tv.connected_changed.connect(lambda ok: dead.append(ok))
        fb = FileBrowser(sess)
        fb.resize(QSize(700, 500))
        fb.chdir("/")
        app.processEvents()

        src = os.path.join(tmp, "拖入目录")
        os.makedirs(os.path.join(src, "sub", "deep"), exist_ok=True)
        for rel in ("a.txt", os.path.join("sub", "b.txt"), os.path.join("sub", "deep", "c.bin")):
            p = os.path.join(src, rel)
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "wb") as fh:
                fh.write(b"x" * 1024)
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(src)])

        class _Ev:
            def __init__(self, m):
                self._m = m

            def mimeData(self):
                return self._m

            def setDropAction(self, a):
                pass

            def accept(self):
                pass

        fb.table.dropEvent(_Ev(mime))
        want = {"a.txt", "b.txt", "c.bin"}
        deadline = time.time() + 25
        got = set()
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.05)
            try:
                got = {n for n in (r[0] for r in sess.listdir("/拖入目录/sub/deep"))}
                got |= {r[0] for r in sess.listdir("/拖入目录")}
                got |= {r[0] for r in sess.listdir("/拖入目录/sub")}
            except Exception:                            # noqa: BLE001
                got = set()
            if want <= got:
                break
        assert want <= got, "目录没完整上传：%r" % (sorted(got),)
        assert not [d for d in dead if d is False], "拖目录后终端被判掉线：%r" % (dead,)
        sess.remove("/拖入目录/sub/deep/c.bin")
        sess.remove("/拖入目录/sub/b.txt")
        sess.remove("/拖入目录/a.txt")
        sess.rmdir("/拖入目录/sub/deep")
        sess.rmdir("/拖入目录/sub")
        sess.rmdir("/拖入目录")
        tv.detach()
        sess.close()
        drop_widget(fb)
        drop_widget(tv)

    check("拖目录上传不断线", t_drop_dir_alive)

    # ---------- 真实按键路由：焦点在终端上、键真的走事件链送到远端 ----------
    def t_keyroute():
        from PySide6.QtCore import Qt
        from PySide6.QtTest import QTest
        from PySide6.QtWidgets import QApplication, QVBoxLayout, QWidget
        app = QApplication.instance() or QApplication(sys.argv[:1])
        from bricks_qt import Ctx
        from sshdesk.app import UI_DIR, SessionPage
        from sshdesk.config import AppConfig
        page = SessionPage(key_prof, AppConfig(), Ctx(base_dir=UI_DIR))
        win = QWidget()
        lay = QVBoxLayout(win)
        lay.addWidget(page.widget)
        win.resize(900, 520)
        win.show()
        app.processEvents()
        page.connect()
        for _ in range(20):
            app.processEvents()
            time.sleep(0.03)
        fw = QApplication.focusWidget()
        assert fw is page.term, "连上以后焦点不在终端上（键会被别的控件吃掉）：%r" % (fw,)
        QTest.keyClicks(page.term, "abc123")
        QTest.keyClick(page.term, Qt.Key_Return)
        deadline = time.time() + 6
        while time.time() < deadline:
            app.processEvents()
            time.sleep(0.03)
            if "abc123" in page.term.screen_text():
                break
        assert "abc123" in page.term.screen_text(), \
            "按键没走到远端/没回显：%r" % page.term.screen_text()[-60:]
        page.dispose()
        drop_widget(page.widget)
        drop_widget(win)

    check("真实按键路由", t_keyroute)

    def t_keepalive():
        """保活参数按会话生效：keepalive_max=0 必须是"永不因保活失败断开"。

        appPublic.SSHNode 建连时写死 60s/3 次，某些服务器不回应 keepalive 请求，
        表现就是空闲几分钟自己掉线。这里验证 SSHdesk 建连后确实覆盖了这两个值。
        """
        never = 1 << 30

        def applied(prof):
            s = SSHSession(prof).connect()
            try:
                c = s.node.conn
                assert (hasattr(c, "update_keepalive_options")
                        or hasattr(c, "set_keepalive")), \
                    "asyncssh 版本太老（无 update_keepalive_options / set_keepalive）"
                return c._keepalive_interval, c._keepalive_count_max, s.keepalive_note
            finally:
                s.close()

        iv, cnt, note = applied(HostProfile(auth="key", key_path=key_path,
                                            keepalive=90, keepalive_max=0, **base))
        assert iv == 90, "保活间隔没生效：%s" % iv
        assert cnt >= never, "keepalive_max=0 应等于永不超时，实际 %s" % cnt
        assert "永不超时" in note, note

        iv, cnt, note = applied(HostProfile(auth="key", key_path=key_path,
                                            keepalive=45, keepalive_max=7, **base))
        assert (iv, cnt) == (45, 7), "自定义保活没生效：%s/%s" % (iv, cnt)

        iv, cnt, note = applied(HostProfile(auth="key", key_path=key_path,
                                            keepalive=0, keepalive_max=0, **base))
        assert iv == 0, "关闭保活没生效：%s" % iv
        assert "关闭" in note, note

    check("保活可配 + 永不超时", t_keepalive)

    check("多标签 + 字体缩放 + 文件面板", t_gui)

    # ---------- 收尾 ----------
    for th in (target, jump_srv):
        th.stop()

    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for name, err in FAIL:
        print("  ❌ %s: %s" % (name, err[:200]))
    if not FAIL:
        print("截图：%s" % os.path.join(ROOT, "out", "sshdesk.png"))
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())