"""兼容层：终端控件已提升为 bricks 组件 `bricks_qt.terminal.Terminal`。

sshdesk 的界面现在由 ui/session.ui 描述（widgettype=Terminal），bricks_qt 负责渲染。
这里只留旧名字，方便老脚本/测试继续 import。
"""

from bricks_qt.terminal import Terminal, Terminal as TerminalView   # noqa: F401

__all__ = ["Terminal", "TerminalView"]
