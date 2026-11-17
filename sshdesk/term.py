"""终端控件：paramiko channel + pyte 解析 + 自绘网格，支持字体缩放与窗口 resize。"""

from __future__ import annotations

import threading
from typing import Optional

import pyte
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPaintEvent, QResizeEvent
from PySide6.QtWidgets import QApplication, QWidget

DEFAULT_FG = QColor("#e6e6e6")
DEFAULT_BG = QColor("#1e1e1e")
CURSOR_BG = QColor("#00c896")

# pyte 的 16 色 → 接近 Windows Terminal 的配色
ANSI16 = ["#0c0c0c", "#c50f1f", "#13a10e", "#c19c00", "#0037da", "#881798", "#3a96dd", "#cccccc",
          "#767676", "#e74856", "#16c60c", "#f9f1a5", "#3b78ff", "#b4009e", "#61d6d6", "#f2f2f2"]

KEYS = {
    Qt.Key_Return: b"\r", Qt.Key_Enter: b"\r", Qt.Key_Backspace: b"\x7f", Qt.Key_Tab: b"\t",
    Qt.Key_Escape: b"\x1b", Qt.Key_Up: b"\x1b[A", Qt.Key_Down: b"\x1b[B",
    Qt.Key_Right: b"\x1b[C", Qt.Key_Left: b"\x1b[D",
    Qt.Key_Home: b"\x1b[H", Qt.Key_End: b"\x1b[F",
    Qt.Key_PageUp: b"\x1b[5~", Qt.Key_PageDown: b"\x1b[6~",
    Qt.Key_Insert: b"\x1b[2~", Qt.Key_Delete: b"\x1b[3~",
}
FKEYS = [Qt.Key_F1, Qt.Key_F2, Qt.Key_F3, Qt.Key_F4, Qt.Key_F5, Qt.Key_F6, Qt.Key_F7, Qt.Key_F8,
         Qt.Key_F9, Qt.Key_F10, Qt.Key_F11, Qt.Key_F12]


class TerminalView(QWidget):
    """一个 pyte 屏幕 + 自绘；字体大小变化只影响显示，col/row 会自动重算。"""

    connected_changed = Signal(bool)
    title_changed = Signal(str)

    def __init__(self, parent=None, font_family: str = "Consolas", font_size: int = 11):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAttribute(Qt.WA_OpaquePaintEvent, True)
        self._font_family = font_family
        self._font_size = font_size
        self._screen = pyte.HistoryScreen(100, 30, history=5000)   # 带 5000 行回滚历史
        self._stream = pyte.ByteStream(self._screen)
        self._chan = None
        self._reader: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._cell_w = 8.0
        self._cell_h = 16.0
        self._font = QFont(font_family, font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._refresh_metrics()
        self._blink = True
        self._hist_bottom = 0          # 到底部时的 history.position
        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_blink)
        self._timer.start(600)

    # ---------------- 字体 ----------------
    def font_size(self) -> int:
        return self._font_size

    def set_font_size(self, size: int) -> None:
        size = max(6, min(40, int(size)))
        if size == self._font_size:
            return
        self._font_size = size
        self._font = QFont(self._font_family, size)
        self._font.setStyleHint(QFont.Monospace)
        self._refresh_metrics()
        self._apply_size_to_chan()
        self.update()

    def _refresh_metrics(self) -> None:
        fm = QFontMetricsF(self._font)
        self._cell_w = max(4.0, fm.horizontalAdvance("M"))
        self._cell_h = max(6.0, fm.height())
        cols = max(20, int(self.width() / self._cell_w)) if self.width() else 100
        rows = max(5, int(self.height() / self._cell_h)) if self.height() else 30
        self._resize_screen(rows, cols)

    def _resize_screen(self, rows: int, cols: int) -> None:
        """pyte 缩行会清屏，这里先把可见内容存下来再重放（字号变化/窗口缩放都靠它）。"""
        if (rows, cols) == (self._screen.lines, self._screen.columns):
            return
        saved = [line.rstrip() for line in self._screen.display]
        self._screen.resize(rows, cols)
        if rows < len(saved):
            keep = [line for line in saved if line][-rows:]
            self._screen.reset()
            if keep:
                self._stream.feed("\r\n".join(keep).encode("utf-8"))

    def _apply_size_to_chan(self) -> None:
        if self._chan is not None:
            try:
                self._chan.resize_pty(width=self._screen.columns, height=self._screen.lines)
            except Exception:                       # noqa: BLE001
                pass

    # ---------------- 通道 ----------------
    def attach_channel(self, chan) -> None:
        self._chan = chan
        self._stop.clear()
        self._reader = threading.Thread(target=self._read_loop, args=(chan,), daemon=True)
        self._reader.start()

    def _read_loop(self, chan) -> None:
        while not self._stop.is_set():
            try:
                data = chan.recv(65536)
            except Exception:                       # noqa: BLE001
                break
            if not data:
                break
            self._stream.feed(data)
            QTimer.singleShot(0, self.update)       # 回主线程重绘
        QTimer.singleShot(0, lambda: self.connected_changed.emit(False))

    def detach(self) -> None:
        self._stop.set()
        if self._chan is not None:
            try:
                self._chan.close()
            except Exception:                       # noqa: BLE001
                pass
            self._chan = None

    def send(self, data: bytes) -> None:
        if self._chan is None:
            return
        try:
            self._chan.sendall(data)
        except Exception:                           # noqa: BLE001
            pass

    # ---------------- 事件 ----------------
    def _on_blink(self) -> None:
        self._blink = not self._blink
        if self._chan is not None:
            self.update()

    def resizeEvent(self, e: QResizeEvent) -> None:      # noqa: N802
        super().resizeEvent(e)
        self._refresh_metrics()
        self._apply_size_to_chan()
        self.update()

    # ---------------- 历史回滚 ----------------
    def scroll_history(self, pages: int) -> None:
        try:
            for _ in range(abs(pages)):
                if pages > 0:
                    self._screen.prev_page()
                else:
                    self._screen.next_page()
        except Exception:                        # noqa: BLE001
            pass
        self.update()

    def wheelEvent(self, e) -> None:             # noqa: N802
        self.scroll_history(1 if e.angleDelta().y() > 0 else -1)

    def keyPressEvent(self, e: QKeyEvent) -> None:       # noqa: N802
        mods, key = e.modifiers(), e.key()
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:                          # Ctrl+Shift+C 复制
                QApplication.clipboard().setText(self.selected_text())
                return
            if key == Qt.Key_V:                          # Ctrl+Shift+V 粘贴
                self.send(QApplication.clipboard().text().encode("utf-8"))
                return
        if key in (Qt.Key_PageUp, Qt.Key_PageDown) and (mods & Qt.ShiftModifier):
            self.scroll_history(1 if key == Qt.Key_PageUp else -1)
            return
        if key in KEYS:
            self.send(KEYS[key])
            return
        if key in FKEYS:
            n = FKEYS.index(key) + 1
            self.send((b"\x1b[%d~" % (11 + n)) if n > 4 else (b"\x1bO" + bytes([80 + n])))
            return
        text = e.text()
        if text:
            if mods & Qt.ControlModifier and len(text) == 1:
                c = text.upper()
                if "A" <= c <= "Z":
                    self.send(bytes([ord(c) - 64]))
                    return
            self.send(text.encode("utf-8"))

    # ---------------- 绘制 ----------------
    def selected_text(self) -> str:
        return "\n".join(line.rstrip() for line in self._screen.display)

    def paintEvent(self, e: QPaintEvent) -> None:        # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), DEFAULT_BG)
        p.setFont(self._font)
        lines = self._screen.buffer
        cols, rows = self._screen.columns, self._screen.lines
        default_fg = default_bg = None
        for y in range(min(rows, int(self.height() / self._cell_h) + 1)):
            line = lines.get(y, {})
            x = 0
            while x < cols:
                cell = line.get(x)
                if cell is None:
                    x += 1
                    continue
                fg = QColor(ANSI16[cell.fg]) if isinstance(cell.fg, str) and cell.fg.startswith("default") is False else DEFAULT_FG
                bg = DEFAULT_BG
                if cell.reverse:
                    fg, bg = DEFAULT_BG, fg
                # 合并同色连续字符，减少 drawText 调用
                run = cell.data
                start = x
                x += 1
                while x < cols:
                    nxt = line.get(x)
                    if nxt is None or nxt.data == " ":
                        break
                    nfg = QColor(ANSI16[nxt.fg]) if isinstance(nxt.fg, str) and not nxt.fg.startswith("default") else DEFAULT_FG
                    if nxt.reverse:
                        nfg = DEFAULT_BG
                    if nfg != fg or nxt.bold != cell.bold:
                        break
                    run += nxt.data
                    x += 1
                rect = QRectF(start * self._cell_w, y * self._cell_h,
                              len(run) * self._cell_w, self._cell_h)
                if bg != DEFAULT_BG:
                    p.fillRect(rect, bg)
                p.setPen(fg)
                f = QFont(self._font)
                f.setBold(bool(cell.bold))
                p.setFont(f)
                p.drawText(QPointF(start * self._cell_w, y * self._cell_h + self._cell_h * 0.78), run)
        # 回滚提示（pyte 的 history.position 是从历史顶部数的行号，到底部时最大）
        try:
            pos = self._screen.history.position
            self._hist_bottom = max(self._hist_bottom, pos)
            back = self._hist_bottom - pos
            if back > 0:
                msg = "↑ 已回看 %d 行 · Shift+PgDn 回到底部" % back
                w = min(self.width() - 8, 8 * len(msg))
                box = QRectF(self.width() - w - 8, 6, w, self._cell_h + 4)
                p.fillRect(box, QColor(0, 0, 0, 180))
                p.setPen(QColor("#f9f1a5"))
                p.drawText(box, Qt.AlignCenter, msg)
        except Exception:                        # noqa: BLE001
            pass
        # 光标
        if self._blink:
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
            p.fillRect(QRectF(cx * self._cell_w, cy * self._cell_h, self._cell_w, self._cell_h), CURSOR_BG)
        p.end()