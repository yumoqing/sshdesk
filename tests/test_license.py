"""sshdesk 授权接线用例：只验这层把 app/公钥/目录钉对了。

授权机制本身（Ed25519、指纹、到期、宽限、吊销）的 21 项矩阵在 pylice 仓库里
（python tests/test_pylice.py），这里不重复，也不依赖签发私钥。

用法：python tests/test_license.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshdesk import license as L                            # noqa: E402

PASS, FAIL = [], []


def check(name, fn):
    t0 = time.time()
    try:
        fn()
        PASS.append(name)
        print("  ✅ %-38s %.2fs" % (name, time.time() - t0))
    except Exception as e:                                  # noqa: BLE001
        FAIL.append((name, str(e)))
        print("  ❌ %-38s %s" % (name, str(e)[:200]))
        traceback.print_exc()


class Env:
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="sshdesk_lic_")
        self.seed, pub = L.new_keypair()
        self.pub = pub.hex()
        self.clock = time.time
        self.path = os.path.join(self.dir, "license.lic")
        self.state = os.path.join(self.dir, "state.json")

    def lic(self, **kw) -> L.License:
        kw.setdefault("path", self.path)
        kw.setdefault("state_path", self.state)
        kw.setdefault("pubkey", self.pub)
        return L.License(**kw)

    def cleanup(self):
        import shutil
        shutil.rmtree(self.dir, ignore_errors=True)


def with_env(fn):
    def wrap():
        env = Env()
        try:
            fn(env)
        finally:
            env.cleanup()
    return wrap


def t_pubkey_shape():
    assert len(L.PUBKEY_HEX) == 64, "PUBKEY_HEX 必须是 32 字节 hex"
    bytes.fromhex(L.PUBKEY_HEX)
    assert L.APP == "sshdesk"


def t_trial_cap(env):
    """没授权文件 → 试用，且上限就是 sshdesk 定死的 2 个会话。"""
    lic = env.lic()
    st = lic.state()
    assert st.mode == "trial" and st.ok, (st.mode, st.reason)
    assert st.limits["max_sessions"] == 2, st.limits
    assert not lic.allow("max_sessions", 2)[0]
    assert lic.allow("max_sessions", 1)[0]


def t_install_license(env):
    lic = env.lic()
    payload = L.make_license(env.seed, app="sshdesk", org="演示客户",
                             features=["sftp"], limits={"max_sessions": 5},
                             machine=L.machine_fingerprint())
    st = lic.install(json.dumps(payload, ensure_ascii=False))
    assert st.mode == "licensed" and st.org == "演示客户", (st.mode, st.org, st.reason)
    assert lic.allow("max_sessions", 4)[0] and not lic.allow("max_sessions", 5)[0]
    assert lic.has("sftp") and not lic.has("jump")


def t_wrong_product(env):
    """别的产品的授权（app 不同）不认。"""
    payload = L.make_license(env.seed, app="other-app", machine=L.machine_fingerprint())
    st = env.lic().install(json.dumps(payload, ensure_ascii=False))
    assert st.mode == "invalid" and "产品" in st.reason, (st.mode, st.reason)


def t_wrong_pubkey(env):
    """签发私钥与内嵌公钥不是一对 → 验签失败。"""
    other_seed, _ = L.new_keypair()
    payload = L.make_license(other_seed, app="sshdesk", machine=L.machine_fingerprint())
    st = env.lic().install(json.dumps(payload, ensure_ascii=False))
    assert st.mode == "invalid" and "签名" in st.reason, (st.mode, st.reason)


def t_default_paths(env):
    """默认落点走 config_dir（SSHDESK_CONFIG_DIR 可改），文件名固定。"""
    os.environ["SSHDESK_CONFIG_DIR"] = env.dir
    try:
        lic = L.License()
        assert os.path.dirname(lic.path) == os.path.abspath(env.dir), lic.path
        assert lic.path.endswith("license.lic") and lic.state_path.endswith("license_state.json")
    finally:
        os.environ.pop("SSHDESK_CONFIG_DIR", None)


def main() -> int:
    print("== sshdesk 授权接线 ==")
    check("app 名与内嵌公钥", t_pubkey_shape)
    check("试用上限 2 个会话", with_env(t_trial_cap))
    check("装授权后放开到 5 个", with_env(t_install_license))
    check("别的产品的授权不认", with_env(t_wrong_product))
    check("换签发密钥后验签失败", with_env(t_wrong_pubkey))
    check("配置目录与文件名", with_env(t_default_paths))

    print("\n%d 通过 / %d 失败" % (len(PASS), len(FAIL)))
    for n, e in FAIL:
        print("  ❌ %s: %s" % (n, e))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
