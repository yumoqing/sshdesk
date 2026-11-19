"""最小授权服务器（演示/自用）：校验签名 + 机器指纹 + 到期，回应心跳。

客户端 `License.heartbeat()` 每 interval_days 打一次；打不通不降级（由宽限期决定），
所以这台服务不要求 7×24，返回 200 即可。

    python tools/license_server.py --dir ./licenses --port 8787
    # 客户端授权里写 "heartbeat": {"url": "http://<host>:8787/hb", ...}

真实运营至少要补：TLS、按 license_id 的吊销表、请求日志落库、速率限制。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshdesk import license as L                            # noqa: E402


def load_licenses(directory: str) -> dict:
    """把目录下所有 .lic 读进内存，键 = license id。"""
    out = {}
    for name in sorted(os.listdir(directory or ".")):
        if not name.endswith(".lic"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(d, dict) and d.get("id"):
            out[str(d["id"])] = d
    return out


def check(d: "dict | None", machine: dict, revoked: set) -> tuple:
    if not d:
        return False, "未签发的授权号"
    if str(d.get("id")) in revoked:
        return False, "授权已吊销"
    if not L.verify_signature(d, d.get("sig", "")):
        return False, "签名校验失败"
    exp = L._parse_expire(str(d.get("expire") or ""))
    if exp is None:
        return False, "到期日无效"
    if time.time() > exp:
        return False, "授权已到期"
    fp = d.get("machine") or {}
    if fp:
        ok, hit = L.fingerprint_match(fp, machine or {})
        if not ok:
            return False, "机器指纹不匹配（命中 %d 项）" % hit
    return True, "ok"


def serve(directory: str, port: int, host: str, revoked: set, quiet: bool = False):
    cache_holder = {"t": 0.0, "lic": {}}

    class H(BaseHTTPRequestHandler):
        def _json(self, code: int, obj: dict) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):                                  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json(400, {"ok": False, "error": "请求不是 JSON"})
            if not quiet:
                print("[hb] %s %s" % (self.address_string(), req.get("license_id")))
            if cache_holder["t"] < time.time() - 30:        # 30 秒重扫目录，避免每次读盘
                cache_holder["lic"] = load_licenses(directory)
                cache_holder["t"] = time.time()
            d = cache_holder["lic"].get(str(req.get("license_id") or ""))
            ok, why = check(d, req.get("machine") or {}, revoked)
            if not ok:
                print("[hb] 拒绝 %s：%s" % (req.get("license_id"), why))
                return self._json(403, {"ok": False, "error": why})
            return self._json(200, {"ok": True, "expire": d.get("expire"),
                                    "server_time": int(time.time())})

        def do_GET(self):                                   # noqa: N802
            self._json(200, {"ok": True, "service": "sshdesk-license", "licenses":
                             len(load_licenses(directory))})

        def log_message(self, format, *args):               # noqa: A002
            pass

    srv = ThreadingHTTPServer((host, port), H)
    print("授权服务启动：http://%s:%d/hb（授权目录 %s，%d 份在位）"
          % (host, srv.server_address[1], os.path.abspath(directory),
             len(load_licenses(directory))))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="sshdesk 授权心跳服务（演示）")
    p.add_argument("--dir", default="licenses", help="存放 .lic 的目录")
    p.add_argument("--port", type=int, default=8787)
    p.add_argument("--host", default="0.0.0.0")
    p.add_argument("--revoke", default="", help="吊销的授权号，逗号分隔")
    p.add_argument("--quiet", action="store_true")
    a = p.parse_args(argv)
    os.makedirs(a.dir, exist_ok=True)
    return serve(a.dir, a.port, a.host,
                 {s.strip() for s in a.revoke.split(",") if s.strip()}, a.quiet)


if __name__ == "__main__":
    sys.exit(main())