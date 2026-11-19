"""心跳/吊销服务（实现在 pylice 的 serve 子命令，本文件只补 sshdesk 的 app/公钥）。

    python tools/license_server.py --dir licenses --port 8787          # 老用法照旧
    python tools/license_server.py serve --dir licenses --port 8787 --revoke L2026...
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import pylice                                              # noqa: E402

from sshdesk.license import APP, PUBKEY_HEX                # noqa: E402


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0].startswith("-"):
        argv = ["serve"] + argv                            # 兼容"直接给 --dir/--port"
    return pylice.main(argv, app=APP, pubkey=PUBKEY_HEX)


if __name__ == "__main__":
    sys.exit(main())
