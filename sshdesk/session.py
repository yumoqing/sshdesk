"""SSH 会话层：**复用 appPublic.sshx 模块**（SSHNode + asyncssh），不再自己撸 paramiko。

对外是同步接口，内部跑一个常驻 asyncio 循环线程：

    s = SSHSession(profile).connect()
    rc, out, err = s.exec("uname -a")
    chan = s.open_shell(100, 30)      # 给 TerminalView 用：recv / sendall / resize_pty
    s.upload(local, "/tmp/x", progress)
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import os
import queue
import threading
from typing import Callable, List, Optional, Tuple

from .config import HostProfile, known_hosts_path

def _import_sshx():
    """优先用已安装/已在 PYTHONPATH 的 appPublic；找不到就试几个常见仓库位置。"""
    import sys
    try:
        from appPublic.sshx import SSHNode
        return SSHNode
    except ImportError:
        pass
    cands = [os.environ.get("SSHDESK_APPPUBLIC", ""),
             os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "..", "apppublic"),
             r"D:\work\repos\apppublic"]
    for c in cands:
        if c and os.path.isdir(os.path.join(c, "appPublic")):
            if c not in sys.path:
                sys.path.insert(0, c)
            try:
                from appPublic.sshx import SSHNode
                return SSHNode
            except ImportError:
                continue
    raise ImportError("需要 appPublic 模块（sshx）：把仓库根加进 PYTHONPATH 或设置 SSHDESK_APPPUBLIC")


SSHNode = _import_sshx()


class SSHError(Exception):
    pass


class _Loop:
    """常驻 asyncio 循环（后台线程），同步接口通过它调度协程。"""

    _shared: Optional["_Loop"] = None

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, daemon=True).start()

    @classmethod
    def shared(cls) -> "_Loop":
        if cls._shared is None:
            cls._shared = cls()
        return cls._shared

    def run(self, coro, timeout: Optional[float] = 120.0):
        fut = asyncio.run_coroutine_threadsafe(coro, self.loop)
        try:
            return fut.result(timeout)
        except TimeoutError as e:
            raise SSHError("操作超时：%s" % e)
        except SSHError:
            raise
        except Exception as e:                    # noqa: BLE001
            raise SSHError(str(e)[:200])


class ShellChannel:
    """把 asyncssh 的 process 包成 pyte 终端控件要的样子。"""

    def __init__(self, proc, loop: _Loop):
        self.proc = proc
        self.loop = loop
        self._q: "queue.Queue[bytes]" = queue.Queue()
        asyncio.run_coroutine_threadsafe(self._pump(), loop.loop)

    async def _pump(self) -> None:
        try:
            while True:
                data = await self.proc.stdout.read(4096)
                if not data:
                    break
                self._q.put(data if isinstance(data, bytes) else data.encode())
        except Exception:                         # noqa: BLE001
            pass
        finally:
            self._q.put(b"")

    # ---- 终端控件接口 ----
    def recv(self, n: int = 65536) -> bytes:
        try:
            return self._q.get(timeout=600)
        except queue.Empty:
            return b""

    def sendall(self, data: bytes) -> None:
        async def _w():
            self.proc.stdin.write(data)
            await self.proc.stdin.drain()
        try:
            self.loop.run(_w(), timeout=15)
        except Exception:                         # noqa: BLE001
            pass

    def resize_pty(self, width: int, height: int) -> None:
        try:
            self.proc.change_terminal_size(width, height)
        except Exception:                         # noqa: BLE001
            pass

    def close(self) -> None:
        try:
            self.proc.close()
        except Exception:                         # noqa: BLE001
            pass


def _jumpers(p: HostProfile) -> List[dict]:
    if not p.jump_host:
        return []
    d = {"host": p.jump_host, "port": p.jump_port or 22, "username": p.jump_user or "root"}
    if p.jump_auth == "password" and p.jump_password:
        d["password"] = p.jump_password
    else:
        d["client_keys"] = [os.path.expanduser(p.jump_key_path or "~/.ssh/id_rsa")]
    return [d]


class SSHSession:
    def __init__(self, profile: HostProfile):
        self.profile = profile
        self.loop = _Loop.shared()
        self.node = None
        self._chan: Optional[ShellChannel] = None
        self._sftp = None

    # ---------- 连接 ----------
    def connect(self) -> "SSHSession":
        p = self.profile
        kw = dict(host=p.host, username=p.user, port=p.port or 22, jumpers=_jumpers(p))
        if p.auth == "password" and p.password:
            kw["password"] = p.password
        else:
            kw["client_keys"] = [os.path.expanduser(p.key_path or "~/.ssh/id_rsa")]
            if p.key_passphrase:
                kw["passphrase"] = p.key_passphrase
        # SSHNode.__init__ 会 print 整个配置（含密码），这里静音，避免落到日志
        with contextlib.redirect_stdout(io.StringIO()):
            self.node = SSHNode(**kw)
        try:
            self.loop.run(self.node.connect(), timeout=30)
        except SSHError as e:
            if "Permission denied" in str(e) or "authentication" in str(e).lower():
                raise SSHError("认证失败：%s" % str(e)[:160])
            raise
        try:
            self.host_key_status = self._check_host_key()
        except SSHError:
            self.close()
            raise
        except Exception as e:                    # noqa: BLE001
            msg = str(e)
            if "Permission denied" in msg or "authentication" in msg.lower():
                raise SSHError("认证失败：%s" % msg[:160])
            raise SSHError("连接 %s:%s 失败：%s" % (p.host, p.port or 22, msg[:200]))
        return self

    # ---------- 主机密钥 ----------
    def _host_entry(self) -> str:
        p = self.profile
        port = p.port or 22
        return p.host if port == 22 else "[%s]:%d" % (p.host, port)

    def _fetch_host_key(self):
        """返回 (keytype, base64)；连上后才有。"""
        key = self.node.conn.get_server_host_key()
        pub = key.export_public_key("openssh")
        pub = pub.decode() if isinstance(pub, bytes) else pub
        parts = pub.split()
        return parts[0], parts[1]

    def _check_host_key(self) -> str:
        """auto：首连记录、之后比对；strict：不在文件里直接拒绝。返回 key type。"""
        entry = self._host_entry()
        ktype, b64 = self._fetch_host_key()
        path = known_hosts_path()
        lines = []
        if os.path.isfile(path):
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = [l.rstrip("\n") for l in fh if l.strip()]
        known = {}
        for line in lines:
            parts = line.split()
            if len(parts) >= 3:
                known[(parts[0], parts[1])] = parts[2]
        old = known.get((entry, ktype))
        mode = (self.profile.hostkey or "auto").lower()
        if old is None:
            if mode == "strict":
                raise SSHError(
                    "未知主机密钥（%s %s，指纹见下），严格模式下拒绝连接：\n%s" %
                    (entry, ktype, self.fingerprint_hint(entry, ktype, b64)))
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write("%s %s %s\n" % (entry, ktype, b64))
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            return "新增信任：%s %s（已写入 known_hosts）" % (entry, ktype)
        if old != b64:
            raise SSHError(
                "主机密钥变了！可能被中间人劫持：\n  期望 %s...\n  实际 %s...\n"
                "确认无误后删掉 %s 里对应行再连。" % (old[:24], b64[:24], path))
        return "主机密钥校验通过：%s %s" % (entry, ktype)

    @staticmethod
    def fingerprint_hint(entry: str, ktype: str, b64: str) -> str:
        import base64
        import hashlib
        raw = base64.b64decode(b64)
        digest = base64.b64encode(hashlib.sha256(raw).digest()).decode().rstrip("=")
        return "  %s %s SHA256:%s" % (entry, ktype, digest)

    def close(self) -> None:
        try:
            if self._chan:
                self._chan.close()
        except Exception:                         # noqa: BLE001
            pass
        try:
            if self.node and self.node.conn:
                self.node.conn.close()
        except Exception:                         # noqa: BLE001
            pass
        self._chan = self._sftp = None
        self.node = None

    @property
    def connected(self) -> bool:
        try:
            return bool(self.node and self.node.conn and not self.node.conn.is_closed())
        except Exception:                         # noqa: BLE001
            return False

    # ---------- 命令 ----------
    def exec(self, command: str, timeout: float = 40.0) -> Tuple[int, str, str]:
        if not self.node or not self.node.conn:
            raise SSHError("未连接")

        async def _run():
            return await self.node.conn.run(command, check=False)

        res = self.loop.run(_run(), timeout=timeout)
        out = res.stdout if isinstance(res.stdout, str) else (res.stdout or b"").decode("utf-8", "replace")
        err = res.stderr if isinstance(res.stderr, str) else (res.stderr or b"").decode("utf-8", "replace")
        return res.exit_status, out, err

    # ---------- 交互 shell ----------
    def open_shell(self, cols: int = 100, rows: int = 30,
                   term: str = "xterm-256color") -> ShellChannel:
        if not self.node or not self.node.conn:
            raise SSHError("未连接")

        async def _open():
            return await self.node.conn.create_process(
                term_type=term, term_size=(rows, cols), encoding=None)

        proc = self.loop.run(_open(), timeout=30)
        self._chan = ShellChannel(proc, self.loop)
        return self._chan

    # ---------- 文件 ----------
    def _sftp_client(self):
        if self._sftp is None:
            async def _start():
                return await self.node.conn.start_sftp_client()
            self._sftp = self.loop.run(_start(), timeout=30)
        return self._sftp

    def listdir(self, path: str) -> List[Tuple[str, int, float, bool]]:
        sftp = self._sftp_client()

        async def _ls():
            out = []
            for a in await sftp.readdir(path):
                name = a.filename or ""
                if name in (".", ".."):
                    continue
                perms = a.attrs.permissions if a.attrs else None
                is_dir = bool(perms is not None and (perms & 0o040000) == 0o040000)
                out.append((name,
                            int(a.attrs.size) if a.attrs and a.attrs.size else 0,
                            float(a.attrs.mtime) if a.attrs and a.attrs.mtime else 0.0,
                            is_dir))
            return out

        return self.loop.run(_ls(), timeout=90)

    def realpath(self, path: str = ".") -> str:
        try:
            return self.loop.run(self._sftp_client().realpath(path), timeout=20) or "/"
        except Exception:                         # noqa: BLE001
            return "/"

    def stat_size(self, path: str) -> int:
        a = self.loop.run(self._sftp_client().stat(path), timeout=30)
        return int(a.size or 0)

    def is_dir(self, path: str) -> bool:
        try:
            a = self.loop.run(self._sftp_client().stat(path), timeout=30)
            return bool(a.permissions is not None and (a.permissions & 0o040000) == 0o040000)
        except Exception:                         # noqa: BLE001
            return False

    def mkdir(self, path: str) -> None:
        self.loop.run(self._sftp_client().mkdir(path), timeout=30)

    def rmdir(self, path: str) -> None:
        self.loop.run(self._sftp_client().rmdir(path), timeout=30)

    def remove(self, path: str) -> None:
        self.loop.run(self._sftp_client().remove(path), timeout=30)

    def upload(self, local: str, remote: str,
               progress: Optional[Callable[[int, int], None]] = None) -> int:
        sftp = self._sftp_client()
        total = os.path.getsize(local)
        chunk = 256 * 1024

        async def _put():
            sent = 0
            async with sftp.open(remote, "wb") as fh:
                with open(local, "rb") as src:
                    while True:
                        data = src.read(chunk)
                        if not data:
                            break
                        await fh.write(data)
                        sent += len(data)
                        if progress:
                            progress(sent, total)
            return sent

        return self.loop.run(_put(), timeout=180)

    def download(self, remote: str, local: str,
                 progress: Optional[Callable[[int, int], None]] = None) -> int:
        sftp = self._sftp_client()
        total = self.stat_size(remote)
        chunk = 256 * 1024
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)

        async def _get():
            done = 0
            async with sftp.open(remote, "rb") as fh:
                with open(local, "wb") as dst:
                    while True:
                        data = await fh.read(chunk)
                        if not data:
                            break
                        dst.write(data)
                        done += len(data)
                        if progress:
                            progress(done, total)
            return done

        return self.loop.run(_get(), timeout=180)