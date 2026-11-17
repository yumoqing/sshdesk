"""PyInstaller 入口（用绝对 import，不能直接冻结包里的 __main__.py）。"""

import sys

from sshdesk.app import main

if __name__ == "__main__":
    sys.exit(main())
