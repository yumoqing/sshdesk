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


class TransferCancelled(SSHError):
    """用户取消传输。已经写下去的部分保留，下次同一个目标会从断点继续。"""


#: SFTP 块大小与流水线深度。大文件全靠 pipelining（同时挂 16 个 WRITE/READ 在途）才跑得满带宽；
#: 块太小（32K 默认）在高延迟链路上会退化成一问一答。
SFTP_CHUNK = 256 * 1024
SFTP_MAX_REQUESTS = 16
#: 单块读写的等待上限。传输本身不设总超时（大文件本来就慢，180s 一定不够），
#: 只判断"卡住了"：单块超过这个时间没有任何进展才算链路断开。
SFTP_STALL = 120.0


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

    def send_nowait(self, data: bytes) -> None:
        """不阻塞调用线程的写：排到 asyncio 循环里就返回（终端敲键走这条）。"""
        async def _w():
            try:
                self.proc.stdin.write(data)
                await self.proc.stdin.drain()
            except Exception:                    # noqa: BLE001
                pass
        try:
            asyncio.run_coroutine_threadsafe(_w(), self.loop.loop)
        except Exception:                        # noqa: BLE001
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


#: 直连探测的超时（秒）。只做 TCP 握手，局域网/公网都够；探测不到就照旧走跳板。
DIRECT_PROBE_TIMEOUT = 1.5


def _probe_direct(host: str, port: int, timeout: float = DIRECT_PROBE_TIMEOUT) -> bool:
    """目标能不能**直连**：只做一次 TCP 握手，连不上（拒绝/超时/解析失败）都算不可达。

    这是决定"要不要跳板机"的唯一依据，所以必须便宜且绝不抛异常：
    - 整个探测放进子线程并 join 限时，DNS 卡住也不会把界面拖住；
    - 探测线程是 daemon，超时后自己烂在后台，不影响主流程。
    """
    import socket

    def _try() -> bool:
        try:
            with socket.create_connection((host, int(port or 22)), timeout):
                return True
        except (OSError, ValueError):
            return False

    box: dict = {}
    t = threading.Thread(target=lambda: box.update(ok=_try()), daemon=True)
    t.start()
    t.join(timeout)
    return bool(box.get("ok"))


class SSHSession:
    def __init__(self, profile: HostProfile):
        self.profile = profile
        self.loop = _Loop.shared()
        self.node = None
        self._chan: Optional[ShellChannel] = None
        self._sftp = None
        #: 上一次传输的决策，给界面显示"续传/跳过"用
        #: {"path", "mode": "full"|"resume"|"skip", "offset", "total", "sent"}
        self.transfer_info: dict = {}
        #: 上一次连接实际走的路径：True=经跳板机，False=直连
        self.via_jump: bool = False
        #: 给界面看的一句话：跳没跳、为什么（空 = 没配跳板机）
        self.route_note: str = ""

    # ---------- 连接 ----------
    def _open(self, jumpers: List[dict]) -> None:
        """按给定的跳板链建连（`jumpers=[]` 就是直连）。"""
        p = self.profile
        kw = dict(host=p.host, username=p.user, port=p.port or 22, jumpers=jumpers)
        if p.auth == "password" and p.password:
            kw["password"] = p.password
        else:
            kw["client_keys"] = [os.path.expanduser(p.key_path or "~/.ssh/id_rsa")]
            if p.key_passphrase:
                kw["passphrase"] = p.key_passphrase
        # SSHNode.__init__ 会 print 整个配置（含密码），这里静音，避免落到日志
        with contextlib.redirect_stdout(io.StringIO()):
            self.node = SSHNode(**kw)
        self.loop.run(self.node.connect(), timeout=30)

    def connect(self) -> "SSHSession":
        p = self.profile
        jumpers = _jumpers(p)
        self.via_jump = bool(jumpers)
        self.route_note = ""
        if jumpers and _probe_direct(p.host, p.port or 22):
            # 目标自己就连得上 → 跳板机是多余的一跳（多一个故障点 + 多一次握手），直接连
            jumpers = []
            self.via_jump = False
            self.route_note = "直连可达，已跳过跳板机 %s" % p.jump_host
        try:
            self._open(jumpers)
        except SSHError as e:
            msg = str(e)
            if "Permission denied" in msg or "authentication" in msg.lower():
                raise SSHError("认证失败：%s" % msg[:160])
            if not jumpers and p.jump_host:
                # 探测说可达、真连却失败（握手被墙/端口是别的服务）→ 退回跳板机再试一次
                try:
                    self._open(_jumpers(p))
                except SSHError as e2:
                    raise SSHError("直连失败（%s），经跳板机也失败（%s）"
                                   % (msg[:100], str(e2)[:100]))
                self.via_jump = True
                self.route_note = "直连失败，已改走跳板机 %s" % p.jump_host
            else:
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

    def stat_info(self, path: str) -> Tuple[int, float]:
        """(size, mtime)。文件不存在时抛异常。"""
        a = self.loop.run(self._sftp_client().stat(path), timeout=30)
        return int(a.size or 0), float(a.mtime or 0)

    def _stat_or_none(self, path: str):
        try:
            return self.stat_info(path)
        except Exception:                        # noqa: BLE001
            return None

    @staticmethod
    def _plan(total: int, have: Optional[int], offset: Optional[int]):
        """决定 "跳过 / 续传 / 整个重传"，返回 (offset, mode)。"""
        if offset is None:
            if have is not None and have == total:
                return total, "skip"
            if have is not None and 0 < have < total:
                return have, "resume"
            return 0, "full"
        offset = max(0, min(int(offset), total))
        if total and offset == total:
            return offset, "skip"
        return offset, ("resume" if offset else "full")

    def upload(self, local: str, remote: str,
               progress: Optional[Callable[[int, int], None]] = None,
               offset: Optional[int] = None,
               stop: Optional[object] = None) -> int:
        """上传本地文件；返回**本次实际传的字节数**。

        - `offset=None`：自己判断断点（远端更短 → 从远端长度续传；一样长 → 跳过）
        - `offset=n`：按调用方算好的断点传（界面已经探过一遍，省一次来回）
        - `stop`：`threading.Event`，置位后下一块之前中止并抛 `TransferCancelled`，
          已经写下去的部分留在远端，下次同一个目标自动续传
        """
        sftp = self._sftp_client()
        total = os.path.getsize(local)
        info = self._stat_or_none(remote) if offset is None else None
        offset, mode = self._plan(total, info[0] if info else None, offset)
        self.transfer_info = {"path": remote, "mode": mode, "offset": offset,
                              "total": total, "sent": 0}
        if mode == "skip":
            if progress:
                progress(total, total)
            return 0

        async def _put():
            sent = 0
            pos = offset
            # 续传必须 r+b：wb 会在打开时把已传的部分截掉
            async with sftp.open(remote, "r+b" if offset else "wb",
                                 block_size=SFTP_CHUNK,
                                 max_requests=SFTP_MAX_REQUESTS) as fh:
                with open(local, "rb") as src:
                    src.seek(offset)
                    if progress:
                        progress(pos, total)
                    while True:
                        if stop is not None and stop.is_set():
                            raise TransferCancelled("上传已取消：%s" % remote)
                        data = src.read(SFTP_CHUNK)
                        if not data:
                            break
                        try:
                            await asyncio.wait_for(fh.write(data, pos), SFTP_STALL)
                        except asyncio.TimeoutError:
                            raise SSHError("传输停滞：%d 秒没有进展（%s）" % (SFTP_STALL, remote))
                        pos += len(data)
                        sent += len(data)
                        if progress:
                            progress(pos, total)
            return sent

        sent = self.loop.run(_put(), timeout=None)
        self.transfer_info["sent"] = sent
        return sent

    def download(self, remote: str, local: str,
                 progress: Optional[Callable[[int, int], None]] = None,
                 offset: Optional[int] = None,
                 stop: Optional[object] = None) -> int:
        """下载远端文件；返回本次实际传的字节数。断点/取消语义同 upload（看本地已有多大）。"""
        sftp = self._sftp_client()
        total = self.stat_size(remote)
        have = os.path.getsize(local) if os.path.isfile(local) else None
        offset, mode = self._plan(total, have, offset)
        self.transfer_info = {"path": remote, "mode": mode, "offset": offset,
                              "total": total, "sent": 0}
        if mode == "skip":
            if progress:
                progress(total, total)
            return 0
        os.makedirs(os.path.dirname(os.path.abspath(local)) or ".", exist_ok=True)

        async def _get():
            done = 0
            pos = offset
            async with sftp.open(remote, "rb",
                                 block_size=SFTP_CHUNK,
                                 max_requests=SFTP_MAX_REQUESTS) as fh:
                with open(local, "r+b" if offset else "wb") as dst:
                    dst.seek(offset)
                    dst.truncate(offset)     # 去掉比远端多的残留尾巴
                    if progress:
                        progress(pos, total)
                    while True:
                        if stop is not None and stop.is_set():
                            raise TransferCancelled("下载已取消：%s" % remote)
                        try:
                            data = await asyncio.wait_for(fh.read(SFTP_CHUNK, pos), SFTP_STALL)
                        except asyncio.TimeoutError:
                            raise SSHError("传输停滞：%d 秒没有进展（%s）" % (SFTP_STALL, remote))
                        if not data:
                            break
                        dst.write(data)
                        pos += len(data)
                        done += len(data)
                        if progress:
                            progress(pos, total)
            return done

        sent = self.loop.run(_get(), timeout=None)
        self.transfer_info["sent"] = sent
        return sent
