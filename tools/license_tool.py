"""签发工具：生成签发密钥、给客户出 license、核验 license。

只用标准库 + cryptography（客户端已依赖）。**私钥文件绝不入仓库**：
默认落在 ~/.sshdesk-license/issuer_ed25519.key（权限 600），丢了就换一对新的公钥重发所有授权。

    python tools/license_tool.py keygen                      # 生成签发密钥 → 打印公钥
    python tools/license_tool.py machine                     # 本机机器码（发给客户对照/自测）
    python tools/license_tool.py issue --org 某某公司 --days 365 \
        --features sftp,resume --max-sessions 5 \
        --machine S1-xxxx-xxxx-xxxx-xx --heartbeat http://license.example.com/hb \
        --out 某某公司.lic
    python tools/license_tool.py verify 某某公司.lic          # 在本机核验（签名+指纹+到期+宽限）
    python tools/license_tool.py inspect 某某公司.lic         # 只看内容与签名，不校验本机
"""

from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from sshdesk import license as L                            # noqa: E402

DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".sshdesk-license", "issuer_ed25519.key")


def _key_path(p: str = "") -> str:
    return p or os.environ.get("SSHDESK_ISSUER_KEY") or DEFAULT_KEY


def cmd_keygen(a) -> int:
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    path = _key_path(a.key)
    if os.path.exists(path) and not a.force:
        print("已存在：%s（要换就用 --force，换完所有旧授权都失效）" % path)
        print("公钥：%s" % L.public_hex(L.load_private_key(path)))
        return 0
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    priv = Ed25519PrivateKey.generate()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(priv.private_bytes_raw().hex())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    pub = L.public_hex(priv)
    print("私钥已写入：%s（备份好，丢了无法补发）" % path)
    print("公钥（贴进 sshdesk/license.py 的 PUBKEY_HEX）：")
    print(pub)
    return 0


def cmd_machine(a) -> int:
    print("机器码：%s" % L.machine_code())
    comps = L.machine_components()
    for k, v in comps.items():
        print("  %-4s %s" % (k, (v[:8] + "…" + v[-4:]) if len(v) > 12 else (v or "（取不到）")))
    return 0


def cmd_issue(a) -> int:
    priv = L.load_private_key(_key_path(a.key))
    machine = {}
    if a.machine:
        try:
            machine = L.parse_machine_code(a.machine)
        except ValueError as e:
            print("机器码不对：%s" % e)
            return 2
    limits = {}
    if a.max_sessions:
        limits["max_sessions"] = int(a.max_sessions)
    hb = {}
    if a.heartbeat:
        hb = {"url": a.heartbeat, "interval_days": int(a.interval),
              "grace_days": int(a.grace)}
    lic = L.make_license(priv, org=a.org, days=a.days, expire=a.expire or "",
                         features=[s.strip() for s in (a.features or "").split(",") if s.strip()],
                         limits=limits, machine=machine, heartbeat=hb,
                         license_id=a.id or "")
    text = json.dumps(lic, ensure_ascii=False, indent=1)
    out = a.out or "%s.lic" % (a.org or lic["id"])
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(text)
    print("已签发：%s" % os.path.abspath(out))
    print("  授权号 %s / %s / 到期 %s / 功能位 %s" % (
        lic["id"], lic["org"], lic["expire"], ",".join(lic["features"]) or "-"))
    print("  绑定机器：%s" % ("是" if machine else "否（不绑机器，任一机器可用）"))
    return 0


def _read(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def cmd_inspect(a) -> int:
    d = _read(a.file)
    ok = L.verify_signature(d, d.get("sig", ""), a.pubkey or L.PUBKEY_HEX)
    print("签名：%s" % ("✅ 通过" if ok else "❌ 失败（不是这把公钥签的，或内容被改过）"))
    for k in ("id", "org", "app", "issued_at", "expire", "features", "limits", "heartbeat"):
        print("  %-9s %s" % (k, d.get(k)))
    m = d.get("machine") or {}
    print("  machine   %s" % (", ".join("%s=%s" % (k, (v or "-")[:8]) for k, v in sorted(m.items()))
                              if m else "（不绑机器）"))
    return 0 if ok else 1


def cmd_verify(a) -> int:
    lic = L.License(path=a.file, state_path=a.state or os.path.join(os.path.dirname(
        os.path.abspath(a.file)), "license_state.json"),
        pubkey=a.pubkey or L.PUBKEY_HEX)
    st = lic.state(refresh=True)
    print("机器码：%s" % lic.machine_code())
    print("状态  ：%s  [%s]" % (st.label(), st.mode))
    print("说明  ：%s" % (st.reason or "-"))
    print("功能位：%s" % (", ".join(sorted(st.features)) or "-"))
    print("限额  ：%s" % (", ".join("%s=%s" % (k, v) for k, v in sorted(st.limits.items())) or "-"))
    return 0 if st.ok else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="sshdesk 授权签发工具")
    sub = p.add_subparsers(dest="cmd", required=True)

    k = sub.add_parser("keygen", help="生成签发密钥对（私钥在仓库外）")
    k.add_argument("--key", default="")
    k.add_argument("--force", action="store_true")
    k.set_defaults(fn=cmd_keygen)

    m = sub.add_parser("machine", help="打印本机机器码")
    m.set_defaults(fn=cmd_machine)

    i = sub.add_parser("issue", help="签发 license")
    i.add_argument("--org", default="")
    i.add_argument("--days", type=int, default=365)
    i.add_argument("--expire", default="", help="YYYY-MM-DD，优先于 --days")
    i.add_argument("--features", default="sftp,resume")
    i.add_argument("--max-sessions", dest="max_sessions", type=int, default=0)
    i.add_argument("--machine", default="", help="客户发来的机器码（不传=不绑机器）")
    i.add_argument("--heartbeat", default="", help="心跳地址，如 http://x/hb")
    i.add_argument("--interval", type=int, default=7)
    i.add_argument("--grace", type=int, default=15)
    i.add_argument("--id", default="")
    i.add_argument("--key", default="")
    i.add_argument("--out", default="")
    i.set_defaults(fn=cmd_issue)

    v = sub.add_parser("verify", help="本机核验 license")
    v.add_argument("file")
    v.add_argument("--state", default="")
    v.add_argument("--pubkey", default="")
    v.set_defaults(fn=cmd_verify)

    s = sub.add_parser("inspect", help="只看内容与签名")
    s.add_argument("file")
    s.add_argument("--pubkey", default="")
    s.set_defaults(fn=cmd_inspect)

    a = p.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())