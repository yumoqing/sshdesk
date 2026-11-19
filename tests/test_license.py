"""license 模块用例：纯逻辑，不连 GUI、不连外网（心跳用本地 http 服务）。

用法：python tests/test_license.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshdesk import license as L                            # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    t0 = time.time()
    try:
        fn()
        PASS.append(name)
        print("  ✅ %-34s %.2fs" % (name, time.time() - t0))
    except Exception as e:                                  # noqa: BLE001
        FAIL.append((name, str(e)))
        print("  ❌ %-34s %s" % (name, str(e)[:200]))
        traceback.print_exc()


class Clock:
    """可注入的时间源：测到期/宽限不靠 sleep。"""

    def __init__(self, t=1_800_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, days: float) -> None:
        self.t += days * 86400


def new_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    return priv, L.public_hex(priv)


class Env:
    """一个临时目录 + 一对签发密钥 + 时钟，每个用例独立。"""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="sshdesk_lic_")
        self.priv, self.pub = new_key()
        self.clock = Clock()
        self.path = os.path.join(self.dir, "license.lic")
        self.state = os.path.join(self.dir, "state.json")

    def lic(self, **kw) -> L.License:
        kw.setdefault("path", self.path)
        kw.setdefault("state_path", self.state)
        kw.setdefault("pubkey", self.pub)
        kw.setdefault("now", self.clock)
        return L.License(**kw)

    def issue(self, **kw) -> dict:
        kw.setdefault("issued_at", self.clock.t)
        kw.setdefault("org", "测试客户")
        return L.make_license(self.priv, **kw)

    def write(self, payload: dict) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=1)

    def cleanup(self):
        shutil.rmtree(self.dir, ignore_errors=True)


def with_env(fn):
    def wrap():
        env = Env()
        try:
            fn(env)
        finally:
            env.cleanup()
    return wrap


def _serve_heartbeat(answer=b'{"ok": true}'):
    hits = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            hits.append(json.loads(self.rfile.read(n) or b"{}"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(answer)

        def log_message(self, format, *args):               # noqa: A002  静音
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, "http://127.0.0.1:%d/hb" % srv.server_address[1], hits


# --------------------------------------------------------------------------- #
def t_machine_code(env):
    code = L.machine_code()
    fp = L.parse_machine_code(code)
    cur = L.machine_fingerprint()
    assert fp == cur, (fp, cur)
    assert L.fingerprint_match(fp, cur) == (True, 3), L.fingerprint_match(fp, cur)
    try:
        L.parse_machine_code(code[:-2] + ("00" if not code.endswith("00") else "11"))
    except ValueError:
        return
    raise AssertionError("校验位错了却没报错")


def t_roundtrip(env):
    env.write(env.issue(days=365, features=["sftp", "resume"],
                        limits={"max_sessions": 5}, machine=L.machine_fingerprint()))
    lic = env.lic()
    st = lic.state()
    assert st.mode == "licensed" and st.ok, (st.mode, st.reason)
    assert st.limits["max_sessions"] == 5, st.limits
    assert lic.has("sftp") and not lic.has("jump"), st.features
    assert lic.allow("max_sessions", 4)[0] and not lic.allow("max_sessions", 5)[0]
    assert 360 <= (st.days_left or 0) <= 366, st.days_left


def t_other_machine(env):
    env.write(env.issue(days=30, machine={"sys": "a" * 16, "mac": "b" * 16, "disk": "c" * 16}))
    st = env.lic().state()
    assert st.mode == "machine_mismatch" and not st.ok, (st.mode, st.reason)


def t_two_of_three_is_not_enough(env):
    """三项缺一（license 里 disk 空）→ 剩两项都得中；这是"换盘要重签"的边界。"""
    cur = L.machine_fingerprint()
    part = {"sys": cur["sys"], "mac": cur["mac"], "disk": ""}
    env.write(env.issue(days=30, machine=part))
    st = env.lic().state()
    assert st.ok, st.reason
    wrong = dict(part, mac="f" * 16)
    env.write(env.issue(days=30, machine=wrong))
    assert env.lic().state().mode == "machine_mismatch"


def t_expired(env):
    env.write(env.issue(expire=time.strftime("%Y-%m-%d", time.gmtime(env.clock.t - 86400 * 2)),
                        machine=L.machine_fingerprint()))
    st = env.lic().state()
    assert st.mode == "expired" and not st.ok, (st.mode, st.reason)
    assert st.limits["max_sessions"] == 1, st.limits


def t_expire_is_utc(env):
    """到期日按 UTC 当天结束算，不能因为本地时区提前一天锁死。"""
    day = time.strftime("%Y-%m-%d", time.gmtime(env.clock.t))
    env.write(env.issue(expire=day, machine=L.machine_fingerprint()))
    assert env.lic().state().ok, "当天应可用"
    env.clock.t += 86400 + 60
    assert env.lic().state().mode == "expired"


def t_tampered(env):
    d = env.issue(days=365, machine=L.machine_fingerprint())
    d["expire"] = "2099-12-31"
    env.write(d)
    st = env.lic().state()
    assert st.mode == "invalid" and not st.ok, (st.mode, st.reason)


def t_verify_sig_catches(env):
    d = env.issue(days=10)
    assert L.verify_signature(d, d["sig"], env.pub)
    d["limits"] = {"max_sessions": 999}
    assert not L.verify_signature(d, d["sig"], env.pub)


def t_trial(env):
    lic = env.lic(trial_days=10)
    st = lic.state()
    assert st.mode == "trial" and st.days_left == 10, (st.mode, st.days_left)
    assert not lic.allow("max_sessions", 2)[0], "试用 2 个会话上限没生效"
    assert lic.allow("max_sessions", 1)[0]
    env.clock.advance(11)
    st = lic.state(refresh=True)
    assert st.mode == "expired" and st.limits["max_sessions"] == 1, (st.mode, st.limits)


def t_clock_rollback(env):
    env.write(env.issue(days=365, machine=L.machine_fingerprint()))
    env.clock.advance(10)
    assert env.lic().state().ok                       # 记住 max_seen
    env.clock.t -= 3 * 86400                          # 手改系统时间往前拨
    st = env.lic().state(refresh=True)
    assert st.mode == "clock_tampered" and not st.ok, (st.mode, st.reason)


def t_grace_flow(env):
    srv, url, hits = _serve_heartbeat()
    try:
        env.write(env.issue(days=365, machine=L.machine_fingerprint(),
                            heartbeat={"url": url, "interval_days": 7, "grace_days": 15}))
        lic = env.lic()
        assert lic.state().mode == "licensed"

        env.clock.advance(8)                           # 超过心跳间隔但仍在宽限
        st = env.lic().state(refresh=True)
        assert st.mode == "grace" and st.ok, (st.mode, st.reason)
        assert st.offline_days_left == 7, st.offline_days_left

        r = env.lic().heartbeat()                      # 联网成功 → 回到 licensed
        assert r["ok"] and hits and hits[-1]["app"] == "sshdesk", (r, hits)
        assert env.lic().state(refresh=True).mode == "licensed"

        env.clock.advance(16)                          # 宽限用完
        st = env.lic().state(refresh=True)
        assert st.mode == "grace_expired" and not st.ok, (st.mode, st.reason)
        assert st.limits["max_sessions"] == 1
    finally:
        srv.shutdown()


def t_heartbeat_down(env):
    env.write(env.issue(days=365, machine=L.machine_fingerprint(),
                        heartbeat={"url": "http://127.0.0.1:9/hb", "grace_days": 15}))
    lic = env.lic()
    r = lic.heartbeat(timeout=1)
    assert not r["ok"], r
    assert lic.state().ok, "心跳失败不该当场降级（由宽限期决定）"


def t_unbound_license(env):
    env.write(env.issue(days=30))
    assert env.lic().state().mode == "licensed", "不绑机器的授权在本机也该通过"


def t_install(env):
    lic = env.lic()
    assert lic.state().mode == "trial"
    st = lic.install(json.dumps(env.issue(days=100, org="某某科技有限公司",
                                          machine=L.machine_fingerprint())))
    assert st.mode == "licensed" and st.org == "某某科技有限公司", (st.mode, st.org)
    assert os.path.isfile(env.path)


def main() -> int:
    print("== license 模块 ==")
    check("机器码 roundtrip + 校验位", with_env(t_machine_code))
    check("签发→安装→验签 全链路", with_env(t_roundtrip))
    check("别的机器上用不了", with_env(t_other_machine))
    check("三项缺一 = 剩两项必须全中", with_env(t_two_of_three_is_not_enough))
    check("到期即失效（限 1 会话）", with_env(t_expired))
    check("到期按 UTC 算", with_env(t_expire_is_utc))
    check("改过的 license 验签失败", with_env(t_tampered))
    check("签名覆盖全部字段", with_env(t_verify_sig_catches))
    check("试用期与试用上限", with_env(t_trial))
    check("系统时间回拨", with_env(t_clock_rollback))
    check("心跳宽限全流程", with_env(t_grace_flow))
    check("心跳打不通不降级", with_env(t_heartbeat_down))
    check("不绑机器的授权", with_env(t_unbound_license))
    check("粘贴激活 install()", with_env(t_install))

    print("\n%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for n, e in FAIL:
        print("  ❌ %s: %s" % (n, e))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())