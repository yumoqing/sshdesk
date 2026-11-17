"""测试用 SSH 服务器（paramiko 实现）：密码/公钥认证、exec、交互 shell、SFTP、direct-tcpip 转发。

只用于测试，不追求协议完整：
  * shell 是一个极简 REPL：收到一行就回 "<行内容>" 加提示符，用来验证终端输入输出闭环
  * SFTP 以 root 目录为根（chroot 风格），真实读写本地文件，用来验证上传/下载
  * direct-tcpip 会真的去连目标，用来验证跳板机通路
"""

from __future__ import annotations

import os
import posixpath
import socket
import threading
import time
from typing import List, Optional, Tuple

import logging

import paramiko

logging.getLogger("paramiko").setLevel(logging.CRITICAL)   # 客户端粗暴断开时不刷 traceback

DEBUG = bool(os.environ.get("FAKE_SSHD_DEBUG"))


def dbg(*a):
    if DEBUG:
        print("[fake-sshd]", *a, flush=True)


HOST_KEY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_fake_host_key")


def host_key() -> paramiko.RSAKey:
    if os.path.isfile(HOST_KEY_PATH):
        try:
            return paramiko.RSAKey(filename=HOST_KEY_PATH)
        except paramiko.SSHException:
            pass
    key = paramiko.RSAKey.generate(2048)
    key.write_private_key_file(HOST_KEY_PATH)
    return key


class _SFTPHandle(paramiko.SFTPHandle):
    def stat(self):
        try:
            return paramiko.SFTPAttributes.from_stat(os.fstat(self.readfile.fileno()))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def chattr(self, attr):
        return paramiko.SFTP_OP_UNSUPPORTED


class _SFTPIface(paramiko.SFTPServerInterface):
    ROOT = "/"

    def _r(self, path: str) -> str:
        p = posixpath.normpath("/" + path.lstrip("/"))
        return os.path.join(self.ROOT, p.lstrip("/"))

    def list_folder(self, path):
        try:
            out = []
            for name in os.listdir(self._r(path)):
                a = paramiko.SFTPAttributes.from_stat(os.stat(os.path.join(self._r(path), name)))
                a.filename = name
                out.append(a)
            return out
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def stat(self, path):
        try:
            return paramiko.SFTPAttributes.from_stat(os.stat(self._r(path)))
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    lstat = stat

    def open(self, path, flags, attr):
        p = self._r(path)
        try:
            binary_flag = getattr(os, "O_BINARY", 0)
            mode = attr.st_mode if attr and attr.st_mode else 0o666
            fd = os.open(p, flags | binary_flag, mode)
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)
        f = os.fdopen(fd, "wb" if flags & (os.O_WRONLY | os.O_RDWR) else "rb")
        h = _SFTPHandle(flags)
        h.filename = p
        h.readfile = f
        h.writefile = f if "w" in getattr(f, "mode", "") else None
        return h

    def remove(self, path):
        try:
            os.remove(self._r(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rename(self, old, new):
        try:
            os.replace(self._r(old), self._r(new))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def mkdir(self, path, attr):
        try:
            os.mkdir(self._r(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def rmdir(self, path):
        try:
            os.rmdir(self._r(path))
            return paramiko.SFTP_OK
        except OSError as e:
            return paramiko.SFTPServer.convert_errno(e.errno)

    def canonicalize(self, path):
        return posixpath.normpath("/" + path.lstrip("/"))

    def chattr(self, path, attr):
        return paramiko.SFTP_OK


class FakeSSHServer(paramiko.ServerInterface):
    def __init__(self, user: str = "test", password: str = "test123",
                 authorized: Optional[List[paramiko.PKey]] = None,
                 root: str = ".", allow_forward: bool = True):
        self.user, self.password = user, password
        self.authorized = authorized or []
        self.root = root
        self.allow_forward = allow_forward
        self.events = {"shell": threading.Event(), "exec": threading.Event(),
                       "subsystem": threading.Event(), "forward": threading.Event()}
        self.command: Optional[str] = None
        self.dest_queue: List[Tuple[str, int]] = []
        self.requests: dict = {}            # chanid -> ("exec"|"shell"|"subsystem", payload)
        self.subsystem: Optional[str] = None
        self.log: List[str] = []

    # ---- 认证 ----
    def check_auth_password(self, username, password):
        ok = (username == self.user and password == self.password)
        self.log.append("auth-password %s %s" % (username, "ok" if ok else "fail"))
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def check_auth_publickey(self, username, key):
        for k in self.authorized:
            if k == key:
                self.log.append("auth-pubkey %s ok" % username)
                return paramiko.AUTH_SUCCESSFUL
        self.log.append("auth-pubkey %s fail" % username)
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username):
        return "password,publickey"

    # ---- 通道 ----
    def check_channel_request(self, kind, chanid):
        return paramiko.OPEN_SUCCEEDED

    def check_channel_direct_tcpip_request(self, chanid, origin, destination):
        if not self.allow_forward:
            return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED
        self.dest_queue.append(destination)     # 给后面的转发通道用
        return paramiko.OPEN_SUCCEEDED

    def check_channel_pty_request(self, *a, **kw):
        return True

    # asyncssh 客户端会先发 env（LANG/LC_*）再 exec；不认这个请求它会直接关通道
    def check_channel_env_request(self, channel, name, value):
        return True

    def check_channel_shell_request(self, channel):
        self.requests[channel.get_id()] = ("shell", None)
        self.events["shell"].set()
        return True

    def check_channel_exec_request(self, channel, command):
        cmd = command.decode() if isinstance(command, bytes) else command
        self.command = cmd
        self.requests[channel.get_id()] = ("exec", cmd)
        self.events["exec"].set()
        return True

    def check_channel_subsystem_request(self, channel, name):
        self.subsystem = name
        self.requests[channel.get_id()] = ("subsystem", name)
        self.events["subsystem"].set()
        if name != "sftp":
            return False
        # paramiko 不会自动启子系统处理器，得自己 new 一个 SFTPServer 线程
        _SFTPIface.ROOT = self.root
        handler = paramiko.SFTPServer(channel, name, self, _SFTPIface)
        handler.start()
        self.sftp_handler = handler
        return True


def _bridge(a: socket.socket, b):
    """两个 socket 对拷，任意一方关闭就收尾。"""
    try:
        while True:
            data = a.recv(32768)
            if not data:
                break
            b.sendall(data)
    except OSError:
        pass
    finally:
        for s in (a, b):
            try:
                s.close()
            except OSError:
                pass


class ServerThread(threading.Thread):
    """监听端口，每个连接起一个 handler 线程。"""

    def __init__(self, port: int, root: str = ".", user: str = "test",
                 password: str = "test123", authorized: Optional[List[paramiko.PKey]] = None,
                 allow_forward: bool = True):
        super().__init__(daemon=True)
        self.port = port
        self.root = root
        self.user, self.password = user, password
        self.authorized = authorized or []
        self.allow_forward = allow_forward
        self.key = host_key()
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", port))
        self.sock.listen(20)
        self._stop = threading.Event()
        self.handlers: List[FakeSSHServer] = []

    # ---- 生命周期 ----
    def run(self) -> None:
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                client, addr = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle, args=(client, addr), daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass

    # ---- 每个连接 ----
    def _handle(self, client: socket.socket, addr) -> None:
        transport = paramiko.Transport(client)
        transport.add_server_key(self.key)
        # SFTP 子系统必须在 start_server 之前注册，handler 才会在该通道上生效
        _SFTPIface.ROOT = self.root
        transport.set_subsystem_handler("sftp", paramiko.SFTPServer, _SFTPIface)
        server = FakeSSHServer(self.user, self.password, self.authorized, self.root,
                               self.allow_forward)
        self.handlers.append(server)
        try:
            transport.start_server(server=server)
        except Exception:                      # noqa: BLE001
            transport.close()
            return
        dbg("handler loop start active=%s" % transport.is_active())
        while transport.is_active():
            try:
                chan = transport.accept(1)
            except Exception as e:              # noqa: BLE001  socket.timeout 等
                dbg("accept exc %s: %s" % (type(e).__name__, str(e)[:60]))
                continue
            dbg("accept ->", chan)
            if chan is None:
                time.sleep(0.05)
                continue
            threading.Thread(target=self._serve_channel, args=(chan, server, transport),
                             daemon=True).start()

    def _serve_channel(self, chan, server: FakeSSHServer, transport) -> None:
        try:
            dbg("serve_channel active=%s forward_queue=%d" % (chan.active, len(server.dest_queue)))
            # 跳板转发：真去连目标再对拷
            if server.dest_queue:     # paramiko 没有 get_kind，靠 direct-tcpip 请求登记的队列判断
                dest = server.dest_queue.pop(0)
                if not dest:
                    chan.close()
                    return
                server.events["forward"].set()
                remote = socket.create_connection((dest[0], dest[1]), timeout=10)
                threading.Thread(target=_bridge, args=(chan, remote), daemon=True).start()
                _bridge(remote, chan)
                return

            # 通道刚打开时请求还没到：等本通道自己的请求（按 chanid 区分）
            kind, payload = None, None
            for _ in range(100):
                kind, payload = server.requests.get(chan.get_id(), (None, None))
                if kind:
                    break
                time.sleep(0.05)
            dbg("chan %s -> %s" % (chan.get_id(), kind))

            if kind == "subsystem":
                while transport.is_active() and chan.active:
                    time.sleep(0.1)
                return

            if kind == "exec":
                chan.sendall(b"exec-ok: " + (payload or "").encode() + b"\r\n")
                chan.send_exit_status(0)
                chan.close()
                return

            if kind == "shell":
                server.events["shell"].clear()
                chan.sendall(b"fake-ssh ready\r\n$ ")
                buf = b""
                while True:
                    try:
                        data = chan.recv(4096)
                    except Exception:           # noqa: BLE001
                        break
                    if not data:
                        break
                    buf += data
                    while b"\r" in buf or b"\n" in buf:
                        sep = b"\r" if b"\r" in buf else b"\n"
                        line, buf = buf.split(sep, 1)
                        chan.sendall(b"<" + line + b">\r\n$ ")
                chan.close()
                return

            time.sleep(0.1)
            chan.close()
        except Exception:                       # noqa: BLE001
            try:
                chan.close()
            except Exception:                   # noqa: BLE001
                pass


def start(port: int, root: str = ".", **kw) -> ServerThread:
    th = ServerThread(port, root, **kw)
    th.start()
    # 等端口真的能连上
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return th
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("测试 SSH 服务器没起来：%d" % port)