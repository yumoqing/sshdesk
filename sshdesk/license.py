"""sshdesk 的授权参数。实现全在独立模块 `pylice`（github.com/yumoqing/pylice）：

    pip install git+https://github.com/yumoqing/pylice

这一层只剩产品侧三件事：**app 名、内嵌公钥、试用口径 + 配置目录**。
换签发方只改 `PUBKEY_HEX`（同时要用新私钥重发所有授权）。

授权本身的不变量（签名覆盖全部字段、指纹留空位不参与比对、到期按 UTC、心跳打不通不降级、
到期只降级到 1 个会话）全在 pylice 里，用例也在那边；`tests/test_license.py` 只验这层接线。
"""

from __future__ import annotations

import os
import sys

from pylice import (                                    # noqa: F401  对外仍按老接口暴露
    TRIAL_DAYS, License as _License, State, check_license, fingerprint_match,
    load_private_key, machine_code, machine_components, machine_fingerprint,
    new_keypair, parse_machine_code, public_hex, verify_signature,
)
from pylice import issue as make_license                # noqa: F401

APP = "sshdesk"

# 签发方公钥（Ed25519，32 字节 hex）。私钥在 ~/.sshdesk-license/issuer_ed25519.key，绝不入仓库。
PUBKEY_HEX = "fedda75130baa32f336e0019450db44bc3482563c502b039b64eb9fe6bc8afd7"

TRIAL_LIMITS = {"max_sessions": 2}                      # 试用期最多 2 个会话
LICENSE_NAME = "license.lic"
STATE_NAME = "license_state.json"


class License(_License):
    """把 app / 公钥 / 配置目录钉死，调用方只写 License()。"""

    def __init__(self, path: str = "", state_path: str = "", pubkey: str = PUBKEY_HEX,
                 now=None, trial_days: int = TRIAL_DAYS, trial_limits=None):
        from .config import config_dir
        base = config_dir()
        super().__init__(
            app=APP, pubkey=pubkey,
            path=path or os.environ.get("SSHDESK_LICENSE_FILE")
            or os.path.join(base, LICENSE_NAME),
            state_path=state_path or os.path.join(base, STATE_NAME),
            now=now, trial_days=trial_days,
            trial_limits=TRIAL_LIMITS if trial_limits is None else trial_limits,
            config_dir=base,
        )


def _main(argv=None) -> int:
    """python -m sshdesk.license [license 文件]：打印本机机器码与授权状态。"""
    argv = list(sys.argv[1:] if argv is None else argv)
    lic = License(path=argv[0] if argv else "")
    st = lic.state(refresh=True)
    print("机器码 : %s" % lic.machine_code())
    print("授权文件: %s%s" % (lic.path, "" if os.path.isfile(lic.path) else "（不存在，试用中）"))
    print("状态   : %s  [%s]" % (st.label(), st.mode))
    if st.features:
        print("功能位 : %s" % ", ".join(sorted(st.features)))
    if st.limits:
        print("限额   : %s" % ", ".join("%s=%s" % (k, v) for k, v in sorted(st.limits.items())))
    return 0 if st.ok else 1


if __name__ == "__main__":
    sys.exit(_main())
