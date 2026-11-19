"""签发工具（实现在 pylice，本文件只钉 sshdesk 的 app 名/公钥/默认私钥路径）。

    python tools/license_tool.py keygen          # 生成签发密钥（私钥在仓库外）
    python tools/license_tool.py machine         # 本机机器码
    python tools/license_tool.py issue --org 某某公司 --days 365 \
        --features sftp,resume --max-sessions 5 --machine S1-... --out 某某公司.lic
    python tools/license_tool.py verify 某某公司.lic
    python tools/license_tool.py inspect 某某公司.lic
    python tools/license_tool.py serve --dir licenses --port 8787
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pylice                                              # noqa: E402

from sshdesk.license import APP, PUBKEY_HEX                # noqa: E402

DEFAULT_KEY = os.path.join(os.path.expanduser("~"), ".sshdesk-license", "issuer_ed25519.key")


def main(argv=None) -> int:
    return pylice.main(list(sys.argv[1:] if argv is None else argv),
                       app=APP, pubkey=PUBKEY_HEX, key=DEFAULT_KEY)


if __name__ == "__main__":
    sys.exit(main())
