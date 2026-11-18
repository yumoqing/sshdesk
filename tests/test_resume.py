"""断点续传专项测试：本地起 fake SSH 服务器，只验大文件传输 / 续传 / 跳过 / 取消。

用法：python tests/test_resume.py

单独成一个文件（而不是塞进 test_client.py）：这套用例会真的搬几 MB 数据、反复开关进度框，
混在长流程里跑时 PySide 的事件队列容易踩到与本功能无关的老问题（原始 HEAD 的实现也能复现），
分开跑最稳，也便于 CI 单独定位。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests"))

import paramiko                                    # noqa: E402

import fake_sshd                                   # noqa: E402
from sshdesk.config import HostProfile             # noqa: E402
from sshdesk.session import TransferCancelled, SSHSession   # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    t0 = time.time()
    try:
        fn()
        PASS.append(name)
        print("  ✅ %-34s %.2fs" % (name, time.time() - t0))
    except Exception as e:                          # noqa: BLE001
        FAIL.append((name, str(e)))
        print("  ❌ %-34s %s" % (name, str(e)[:200]))
        traceback.print_exc(limit=2)


def sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sshdesk_resume_")
    os.environ["SSHDESK_CONFIG_DIR"] = os.path.join(tmp, "cfg")
    sftp_root = os.path.join(tmp, "sftp")
    os.makedirs(sftp_root, exist_ok=True)

    key = paramiko.RSAKey.generate(2048)
    key_path = os.path.join(tmp, "id_test")
    key.write_private_key_file(key_path)
    srv = fake_sshd.start(2211, root=sftp_root, authorized=[key])
    prof = HostProfile(host="127.0.0.1", port=2211, user="test", auth="key", key_path=key_path)

    print("== 会话层 ==")

    def t_session_resume():
        s = SSHSession(prof).connect()
        try:
            data = os.urandom(4 * 1024 * 1024)          # 4MB：跨很多个 256K 块
            src = os.path.join(tmp, "big.bin")
            with open(src, "wb") as fh:
                fh.write(data)

            # 1) 传一小段就取消：远端留下"半个文件"，这正是断点的来源
            stop = threading.Event()

            def prog(done, total):
                if done >= 1024 * 1024:
                    stop.set()

            try:
                s.upload(src, "/big.bin", prog, stop=stop)
            except TransferCancelled:
                pass
            else:
                raise AssertionError("置了 stop 之后 upload 没有中止")
            part = s.stat_size("/big.bin")
            assert 0 < part < len(data), part

            # 2) 再传一次：必须从 part 续上，不是从 0 重来
            sent = s.upload(src, "/big.bin", lambda d, t: None)
            assert s.transfer_info["mode"] == "resume", s.transfer_info
            assert s.transfer_info["offset"] == part, s.transfer_info
            assert sent == len(data) - part, (sent, part)
            assert sha256(os.path.join(sftp_root, "big.bin")) == sha256(src), "续传后内容不一致"

            # 3) 已经完整：跳过，一个字节都不传
            sent2 = s.upload(src, "/big.bin")
            assert sent2 == 0 and s.transfer_info["mode"] == "skip", s.transfer_info

            # 4) 下载方向：本地先有 1MB，必须从 1MB 续
            back = os.path.join(tmp, "big.back")
            with open(back, "wb") as fh:
                fh.write(data[:1024 * 1024])
            got = s.download("/big.bin", back)
            assert s.transfer_info["mode"] == "resume", s.transfer_info
            assert got == len(data) - 1024 * 1024, (got, len(data))
            assert sha256(back) == sha256(src), "续传下载内容不一致"

            # 5) 本地已完整：跳过
            got2 = s.download("/big.bin", back)
            assert got2 == 0 and s.transfer_info["mode"] == "skip", s.transfer_info

            # 6) 本地比远端长（脏尾巴）：整传并截掉多余部分
            with open(back, "ab") as fh:
                fh.write(b"junk" * 1000)
            s.download("/big.bin", back)
            assert s.transfer_info["mode"] == "full", s.transfer_info
            assert sha256(back) == sha256(src), "本地脏尾巴没被截掉"
        finally:
            s.close()

    check("大文件上传/下载续传 · 跳过 · 截断", t_session_resume)

    print("== 文件面板 ==")

    from PySide6.QtWidgets import QApplication       # noqa: E402
    app = QApplication.instance() or QApplication(sys.argv[:1])
    from sshdesk.files import FileBrowser            # noqa: E402

    def t_panel_resume():
        sess = SSHSession(prof).connect()
        fb = FileBrowser(sess)
        fb.chdir("/")
        app.processEvents()
        assert fb._resumable, "文件面板没识别到可续传的 session"

        data = os.urandom(2 * 1024 * 1024)
        d1 = os.path.join(tmp, "half")
        d2 = os.path.join(tmp, "full")
        os.makedirs(d1, exist_ok=True)
        os.makedirs(d2, exist_ok=True)
        half = os.path.join(d1, "panel.bin")            # 先只传前 1MB
        full = os.path.join(d2, "panel.bin")            # 再传完整的 2MB
        with open(half, "wb") as fh:
            fh.write(data[:1024 * 1024])
        with open(full, "wb") as fh:
            fh.write(data)

        def run_upload(path: str) -> str:
            fb.upload_paths([path])
            th = fb._th
            deadline = time.time() + 60
            while th.is_running() and time.time() < deadline:
                app.processEvents()
                time.sleep(0.02)
            th.wait(10)
            for _ in range(10):
                app.processEvents()
                time.sleep(0.01)
            return fb.hint.text()

        run_upload(half)
        assert sess.stat_size("/panel.bin") == 1024 * 1024, sess.stat_size("/panel.bin")
        hint = run_upload(full)
        assert sess.stat_size("/panel.bin") == len(data), "没有续传到完整长度"
        assert sha256(os.path.join(sftp_root, "panel.bin")) == sha256(full), "续传内容不一致"
        assert "续传" in hint, "界面没提示续传：%r" % hint
        hint2 = run_upload(full)
        assert "跳过" in hint2, "已完整的文件没被跳过：%r" % hint2

        # 拖出下载也走断点：临时目录里先放半截，再拖同一个文件
        drag_dir = os.path.join(tempfile.gettempdir(), "sshdesk_drag")
        os.makedirs(drag_dir, exist_ok=True)
        staged = os.path.join(drag_dir, "panel.bin")
        with open(staged, "wb") as fh:
            fh.write(data[:1024 * 1024])
        got = fb._fetch_to_temp(["panel.bin"])
        assert got and os.path.isfile(got[0]), "拖出下载失败：%r" % got
        assert sha256(got[0]) == sha256(full), "拖出下载没续传/内容不一致"
        os.remove(staged)

        sess.remove("/panel.bin")
        sess.close()
        fb.deleteLater()
        for _ in range(10):
            app.processEvents()
            time.sleep(0.01)

    check("面板续传 / 跳过 / 拖出续传", t_panel_resume)

    srv.stop()
    print()
    print("通过 %d / %d" % (len(PASS), len(PASS) + len(FAIL)))
    for name, err in FAIL:
        print("  ❌ %s: %s" % (name, err[:200]))
    if not FAIL:
        shutil.rmtree(tmp, ignore_errors=True)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())