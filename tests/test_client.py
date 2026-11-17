"""端到端测试：本地起 fake SSH 服务器，验密码/密钥登录、跳板、SFTP 上传下载、shell 往返、GUI 多标签与字体缩放。

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


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sshdesk_test_")
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

    def t_jump():
        s = SSHSession(jump_prof).connect()
        try:
            rc, out, _ = s.exec("through-jump")
            assert "through-jump" in out, out
        finally:
            s.close()
        assert any(h.events["forward"].is_set() for h in jump_srv.handlers), "跳板没有被用到"

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

    check("密码登录 + exec", t_password)
    check("密钥登录（免密）+ exec", t_key)
    check("错误密码被拒", t_bad_password)
    check("跳板机 direct-tcpip", t_jump)
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
        tab = win.tabs.widget(0)
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
        assert tab.browser is not None, "文件面板没建起来"
        assert tab.browser.remote.cwd, "远端目录为空"

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