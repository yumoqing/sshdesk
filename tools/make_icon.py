"""生成 sshdesk 图标（三平台共用一套图形，按平台导出 ico / icns / png）。

设计：深海军蓝圆角方块 + 薄荷色 chevron `>` + 青色光标条 —— 一句话说清"终端"，
配色（#3ddc84 / #5ad1e6 on #0f1b2d）跟常见的"纯黑+亮绿"终端图标区分开。

小尺寸单独"光学加粗"：16/32px 直接按小尺寸重绘（笔画更粗、去掉高光和光晕），
不然从 1024 缩下去 16px 只剩一团色块。

用法：
    python tools/make_icon.py
    iconutil -c icns assets/sshdesk.iconset -o assets/sshdesk.icns    # macOS 上补 icns
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSETS = os.path.join(ROOT, "assets")

TOP = (15, 27, 45)
BOTTOM = (20, 51, 80)
MINT = (61, 220, 132)
CYAN = (90, 209, 230)

ICO_SIZES = [16, 24, 32, 48, 64, 128, 256]


def _gradient(size: int, top, bottom, radius: int) -> Image.Image:
    grad = Image.new("RGB", (1, size))
    px = grad.load()
    for y in range(size):
        t = y / max(1, size - 1)
        px[0, y] = tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
    img = grad.resize((size, size), Image.BILINEAR).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _stroke(d, points, width: int, color) -> None:
    r = max(1, width // 2)
    d.line(points, fill=color, width=width, joint="curve")
    for (x, y) in (points[0], points[-1]):
        d.ellipse([x - r, y - r, x + r, y + r], fill=color)


def render_16() -> Image.Image:
    """16px 手绘像素版：逐行摆像素，保证 `>` 连续、无孤立点、光标条是实心块。

    直接用 line(width=2) 画斜线在 16px 下会留下断口和毛刺（实测右上角出现孤立像素），
    所以改成显式行表：上半 `>` 下行、下半上行，各 2px 粗，交点对齐。"""
    img = _gradient(16, TOP, BOTTOM, 3)
    d = ImageDraw.Draw(img)
    mint, cyan = MINT + (255,), CYAN + (255,)

    # 上半臂：从 (3,3) 走到 (8,8)；下半臂镜像回 (3,13)
    rows_upper = [(3, 3), (4, 4), (5, 5), (6, 6), (7, 7), (8, 8)]
    rows_lower = [(8, 8), (7, 9), (6, 10), (5, 11), (4, 12), (3, 13)]
    seen = set()
    for x, y in rows_upper + rows_lower:
        for dx in (0, 1):                       # 2px 粗
            px = x + dx
            if 1 <= px <= 14 and 1 <= y <= 14 and (px, y) not in seen:
                d.point((px, y), fill=mint)
                seen.add((px, y))

    # 光标条：x 10..13, y 10..11（与 `>` 下臂同高，留 1px 空隙）
    d.rectangle([10, 10, 13, 11], fill=cyan)
    return img


def render(size: int) -> Image.Image:
    small = size <= 64
    radius = int(size * (0.20 if small else 0.225))
    img = _gradient(size, TOP, BOTTOM, radius)
    d = ImageDraw.Draw(img)

    if not small:
        glow = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(glow).ellipse([-size * 0.25, -size * 0.55, size * 0.95, size * 0.35],
                                     fill=(90, 209, 230, 26))
        img = Image.alpha_composite(img, glow)
        d = ImageDraw.Draw(img)

    if size == 16:
        return render_16()
    if size <= 24:                              # 24px：只留最必要的两个形状
        x0, xm, x1 = 0.150, 0.520, 0.150
        y0, ym, y1 = 0.230, 0.490, 0.750
        w = int(size * 0.175)
        bar = (0.630, 0.600, 0.845, 0.745)      # 小块方头光标，别做成小点
    elif small:
        x0, xm, x1 = 0.235, 0.545, 0.235
        y0, ym, y1 = 0.275, 0.49, 0.705
        w = int(size * 0.145)
        bar = (0.600, 0.585, 0.835, 0.705)
    else:
        x0, xm, x1 = 0.300, 0.565, 0.300
        y0, ym, y1 = 0.315, 0.500, 0.685
        w = int(size * 0.088)
        bar = (0.600, 0.565, 0.845, 0.685)      # 加长加粗，跟上 chevron 的体量

    chev = [(int(size * x0), int(size * y0)), (int(size * xm), int(size * ym)),
            (int(size * x1), int(size * y1))]
    _stroke(d, chev, w, MINT)
    if not small:
        _stroke(d, [(x, y - max(2, w // 7)) for x, y in chev], max(2, w // 6),
                (MINT[0] + 40, MINT[1] + 20, MINT[2] + 30))

    bx0, by0, bx1, by1 = (int(size * v) for v in bar)
    d.rounded_rectangle([bx0, by0, bx1, by1], radius=(by1 - by0) // 2, fill=CYAN)
    return img


def main() -> int:
    os.makedirs(ASSETS, exist_ok=True)

    render(1024).save(os.path.join(ASSETS, "sshdesk.png"))
    render(512).save(os.path.join(ASSETS, "sshdesk-512.png"))

    render(16).save(os.path.join(ASSETS, "sshdesk-16.png"))
    frames = [render(n) for n in ICO_SIZES]
    frames[-1].save(os.path.join(ASSETS, "sshdesk.ico"), format="ICO",
                    sizes=[(n, n) for n in ICO_SIZES])

    iconset = os.path.join(ASSETS, "sshdesk.iconset")
    if os.path.isdir(iconset):
        shutil.rmtree(iconset)
    os.makedirs(iconset)
    for base in (16, 32, 128, 256, 512):
        render(base).save(os.path.join(iconset, "icon_%dx%d.png" % (base, base)))
        render(base * 2).save(os.path.join(iconset, "icon_%dx%d@2x.png" % (base, base)))

    sizes = [256, 128, 64, 32, 16]
    pad = 16
    strip = Image.new("RGBA", (sum(sizes) + pad * (len(sizes) + 1), 256 + pad * 2),
                      (245, 246, 248, 255))
    x = pad
    for n in sizes:
        im = render(n)
        strip.paste(im, (x + (256 - n) // 2, pad + (256 - n) // 2), im)
        x += n + pad
    strip.save(os.path.join(ASSETS, "icon_preview.png"))

    icns = os.path.join(ASSETS, "sshdesk.icns")
    if sys.platform == "darwin" and shutil.which("iconutil"):
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", icns], check=False)
        print("icns:", os.path.exists(icns))
    else:
        print("非 macOS：iconset 已生成，到 Mac 上跑 "
              "iconutil -c icns assets/sshdesk.iconset -o assets/sshdesk.icns")

    for name in ("sshdesk.png", "sshdesk-512.png", "sshdesk.ico", "icon_preview.png"):
        p = os.path.join(ASSETS, name)
        print("%-18s %8d bytes" % (name, os.path.getsize(p)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
