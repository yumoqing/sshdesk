"""兼容层：远端文件面板已提升为 bricks 组件 `bricks_qt.remotefiles.RemoteFiles`。

界面由 ui/session.ui 描述（widgettype=RemoteFiles）。这里保留旧名字。
"""

from bricks_qt.remotefiles import (RemoteFS, RemoteFiles, human_size,   # noqa: F401
                                   human_time)

FileBrowser = RemoteFiles

__all__ = ["RemoteFiles", "FileBrowser", "RemoteFS", "human_size", "human_time"]
