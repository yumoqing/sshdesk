"""终端控件：paramiko channel + pyte 解析 + 自绘网格，支持字体缩放与窗口 resize。"""

from __future__ import annotations

import threading
from typing import Optional

import pyte
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import (QColor, QFont, QFontMetricsF, QKeyEvent, QPainter, QPaintEvent,
                           QResizeEvent, QGuiApplication)
from PySide6.QtWidgets import QApplication, QWidget

DEFAULT_FG = QColor("#e6e6e6")
DEFAULT_BG = QColor("#1e1e1e")
CURSOR_BG = QColor("#00c896")

# pyte 的 16 色 → 接近 Windows Terminal 的配色
ANSI16 = ["#0c0c0c", "#c50f1f", "#13a10e", "#c19c00", "#0037da", "#881798", "#3a96dd", "#cccccc",
          "#767676", "#e74856", "#16c60c", "#f9f1a5", "#3b78ff", "#b4009e", "#61d6d6", "#f2f2f2"]

# pyte 的颜色名 → xterm 配色（暗/亮两套）
NAMED = {"black": "#0c0c0c", "red": "#c50f1f", "green": "#13a10e", "brown": "#c19c00",
         "blue": "#0037da", "magenta": "#881798", "cyan": "#3a96dd", "white": "#cccccc"}
NAMED_BRIGHT = {"black": "#767676", "red": "#e74856", "green": "#16c60c", "brown": "#f9f1a5",
                "blue": "#3b78ff", "magenta": "#b4009e", "cyan": "#61d6d6", "white": "#f2f2f2"}
HEXCHARS = set("0123456789abcdef")


def xterm256(n: int) -> str:
    """xterm 256 色下标 → #rrggbb。"""
    n = int(n) & 0xFF
    if n < 16:
        return (ANSI16 + ["#0c0c0c", "#e74856", "#16c60c", "#f9f1a5", "#3b78ff",
                          "#b4009e", "#61d6d6", "#f2f2f2"])[n]
    if n < 232:
        n -= 16
        lv = [0, 95, 135, 175, 215, 255]
        return "#%02x%02x%02x" % (lv[n // 36], lv[(n // 6) % 6], lv[n % 6])
    g = 8 + (n - 232) * 10
    return "#%02x%02x%02x" % (g, g, g)


def color_of(val, default: QColor, bold: bool = False) -> QColor:
    """把 pyte 的 cell.fg / cell.bg 翻成 QColor；任何认不出的值都退回默认色。"""
    try:
        if val is None:
            return default
        if isinstance(val, bool):
            return default
        if isinstance(val, int):
            return QColor(xterm256(val))
        if isinstance(val, (tuple, list)) and len(val) == 3:
            return QColor(int(val[0]) & 255, int(val[1]) & 255, int(val[2]) & 255)
        if not isinstance(val, str):
            return default
        v = val.strip().lower()
        if not v or v.startswith("default"):
            return default
        if v in NAMED:
            return QColor(NAMED_BRIGHT.get(v) if bold else NAMED[v])
        if len(v) == 6 and set(v) <= HEXCHARS:
            return QColor("#" + v)
        return default
    except Exception:                       # noqa: BLE001
        return default


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
        self._pad_x = 10.0             # 文字不要贴着边框
        self._pad_y = 8.0
        self._font = QFont(font_family, font_size)
        self._font.setStyleHint(QFont.Monospace)
        self._refresh_metrics()
        self._blink = True
        self._hist_bottom = 0          # 到底部时的 history.position
        self._sel_a = None             # 选择锚点 (row, col)
        self._sel_b = None             # 选择终点 (row, col)
        self._selecting = False
        self.setMouseTracking(True)
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
        aw = max(0.0, self.width() - 2 * self._pad_x)
        ah = max(0.0, self.height() - 2 * self._pad_y)
        cols = max(20, int(aw / self._cell_w)) if self.width() else 100
        rows = max(5, int(ah / self._cell_h)) if self.height() else 30
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
        if key == Qt.Key_C and (mods & Qt.ControlModifier):
            if self.copy_selection():                     # 有选中 → 复制
                return
            if mods & Qt.ShiftModifier:                   # 没选中时 Ctrl+Shift+C 复制整屏
                QGuiApplication.clipboard().setText(self.screen_text())
                return
        if key == Qt.Key_A and (mods & Qt.ControlModifier) and (mods & Qt.ShiftModifier):
            self.select_all()
            return
        if key == Qt.Key_Insert and (mods & Qt.ControlModifier):
            self.copy_selection()
            return
        if key == Qt.Key_Insert and (mods & Qt.ShiftModifier):
            self.paste()
            return
        if mods & Qt.ControlModifier and mods & Qt.ShiftModifier:
            if key == Qt.Key_C:
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

    # ---------------- 选择 / 复制 ----------------
    def _cell_at(self, pos) -> tuple:
        col = int((pos.x() - self._pad_x) // self._cell_w)
        row = int((pos.y() - self._pad_y) // self._cell_h)
        return (max(0, min(self._screen.lines - 1, row)),
                max(0, min(self._screen.columns - 1, col)))

    def _ordered(self):
        if self._sel_a is None or self._sel_b is None:
            return None
        a, b = self._sel_a, self._sel_b
        return (a, b) if (a[0], a[1]) <= (b[0], b[1]) else (b, a)

    def has_selection(self) -> bool:
        """单点点击不算选择。"""
        rng = self._ordered()
        return bool(rng and rng[0] != rng[1])

    def clear_selection(self) -> None:
        self._sel_a = self._sel_b = None
        self.update()

    def select_all(self) -> None:
        self._sel_a = (0, 0)
        self._sel_b = (self._screen.lines - 1, self._screen.columns - 1)
        self.update()

    def selection_text(self) -> str:
        rng = self._ordered()
        if not rng or not self.has_selection():
            return ""
        (r1, c1), (r2, c2) = rng
        buf = self._screen.buffer
        out = []
        for r in range(r1, r2 + 1):
            line = buf.get(r, {})
            lo = c1 if r == r1 else 0
            hi = c2 if r == r2 else self._screen.columns - 1
            out.append("".join((line.get(x).data if line.get(x) else " ")
                               for x in range(lo, hi + 1)).rstrip())
        while out and not out[-1]:
            out.pop()
        return "\n".join(out)

    def copy_selection(self) -> bool:
        txt = self.selection_text()
        if not txt:
            return False
        QGuiApplication.clipboard().setText(txt)
        return True

    def paste(self) -> None:
        self.send(QGuiApplication.clipboard().text().encode("utf-8"))

    def focusInEvent(self, e) -> None:                     # noqa: N802
        self.update()
        super().focusInEvent(e)

    def showEvent(self, e) -> None:                        # noqa: N802
        super().showEvent(e)
        self.setFocus(Qt.OtherFocusReason)                 # 切过来就能直接敲

    def mousePressEvent(self, e) -> None:                  # noqa: N802
        self.setFocus(Qt.MouseFocusReason)                 # 关键：点击即取焦点
        if e.button() == Qt.LeftButton:
            self._sel_a = self._cell_at(e.position())
            self._sel_b = self._sel_a
            self._selecting = True
            self.update()
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:                   # noqa: N802
        if not self._selecting and (e.buttons() & Qt.LeftButton) and self._sel_a is not None:
            self._selecting = True
        if self._selecting:
            self._sel_b = self._cell_at(e.position())
            self.update()
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:                # noqa: N802
        if e.button() == Qt.LeftButton and self._selecting:
            self._selecting = False
            if not self.has_selection():
                self.clear_selection()
        super().mouseReleaseEvent(e)

    def mouseDoubleClickEvent(self, e) -> None:            # noqa: N802
        """双击选中光标下的单词；点在空白处则选整行文字。"""
        if e.button() == Qt.LeftButton:
            row, col = self._cell_at(e.position())
            line = self._screen.buffer.get(row, {})
            text = "".join((line.get(x).data if line.get(x) else " ")
                           for x in range(self._screen.columns))
            lo = len(text) - len(text.lstrip())
            hi = len(text.rstrip()) - 1
            if hi < lo:                                    # 空行
                self._sel_a, self._sel_b = (row, 0), (row, 0)
            elif lo <= col <= hi and text[col] != " ":
                s = col
                while s > lo and text[s - 1] != " ":
                    s -= 1
                t2 = col
                while t2 < hi and text[t2 + 1] != " ":
                    t2 += 1
                self._sel_a, self._sel_b = (row, s), (row, t2)
            else:
                self._sel_a, self._sel_b = (row, lo), (row, hi)
            self.update()
        super().mouseDoubleClickEvent(e)

    def contextMenuEvent(self, e) -> None:                 # noqa: N802
        from PySide6.QtWidgets import QMenu
        m = QMenu(self)
        a_copy = m.addAction("复制")
        a_copy.setEnabled(self.has_selection())
        a_copy.triggered.connect(self.copy_selection)
        m.addAction("粘贴", self.paste)
        m.addSeparator()
        m.addAction("全选", self.select_all)
        m.addAction("回到底部", lambda: self.scroll_history(-999))
        m.exec(e.globalPos())

    # ---------------- 绘制 ----------------
    def screen_text(self) -> str:
        """整屏文字（不含未显示的历史）。"""
        return "\n".join(line.rstrip() for line in self._screen.display)

    def selected_text(self) -> str:
        """有选区返回选区，否则返回整屏（兼容旧调用）。"""
        return self.selection_text() or self.screen_text()

    def paintEvent(self, e: QPaintEvent) -> None:        # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), DEFAULT_BG)
        p.setFont(self._font)
        lines = self._screen.buffer
        cols, rows = self._screen.columns, self._screen.lines
        px, py = self._pad_x, self._pad_y
        font_plain = QFont(self._font)
        font_bold = QFont(self._font)
        font_bold.setBold(True)
        visible = min(rows, int((self.height() - 2 * py) / self._cell_h) + 1)
        for y in range(max(0, visible)):
            line = lines.get(y, {})
            x = 0
            while x < cols:
                cell = line.get(x)
                if cell is None:
                    x += 1
                    continue
                # 颜色一律走 color_of：pyte 给的是 'green' / 'ff0000' / int，
                # 之前直接拿字符串下标 ANSI16 会抛异常，整帧绘制中断（表现为只画到第一个彩色字符、光标也没了）
                try:
                    fg = color_of(cell.fg, DEFAULT_FG, bool(cell.bold))
                    bg = color_of(cell.bg, DEFAULT_BG)
                    if cell.reverse:
                        fg, bg = bg if bg != DEFAULT_BG else DEFAULT_FG, fg
                except Exception:                        # noqa: BLE001
                    fg, bg = DEFAULT_FG, DEFAULT_BG
                run = cell.data
                start = x
                x += 1
                while x < cols:
                    nxt = line.get(x)
                    if nxt is None or nxt.data == " ":
                        break
                    try:
                        nfg = color_of(nxt.fg, DEFAULT_FG, bool(nxt.bold))
                        nbg = color_of(nxt.bg, DEFAULT_BG)
                        if nxt.reverse:
                            nfg, nbg = nbg if nbg != DEFAULT_BG else DEFAULT_FG, nfg
                    except Exception:                    # noqa: BLE001
                        break
                    if nfg != fg or nbg != bg or nxt.bold != cell.bold:
                        break
                    run += nxt.data
                    x += 1
                rect = QRectF(px + start * self._cell_w, py + y * self._cell_h,
                              len(run) * self._cell_w, self._cell_h)
                if bg != DEFAULT_BG:
                    p.fillRect(rect, bg)
                p.setPen(fg)
                p.setFont(font_bold if cell.bold else font_plain)
                p.drawText(QPointF(px + start * self._cell_w,
                                   py + y * self._cell_h + self._cell_h * 0.78), run)
        p.setFont(self._font)

        # 选区高亮
        rng = self._ordered()
        if rng and self.has_selection():
            (r1, c1), (r2, c2) = rng
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(58, 110, 165, 210))
            for r in range(r1, r2 + 1):
                lo = c1 if r == r1 else 0
                hi = c2 if r == r2 else cols - 1
                p.drawRect(QRectF(px + lo * self._cell_w, py + r * self._cell_h,
                                  (hi - lo + 1) * self._cell_w, self._cell_h))
            p.setBrush(Qt.NoBrush)

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
        if self._blink and not getattr(self._screen.cursor, "hidden", False):
            cx, cy = self._screen.cursor.x, self._screen.cursor.y
            if not (0 <= cx < cols and 0 <= cy < rows):
                p.end()
                return
            p.fillRect(QRectF(px + cx * self._cell_w, py + cy * self._cell_h,
                              self._cell_w, self._cell_h), CURSOR_BG)
        p.end()