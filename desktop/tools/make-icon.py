#!/usr/bin/env python3
"""生成桌面壳图标：clay 底 + 白气泡。产出 desktop/build/icon.png 和 icon.ico。

跑法（仓库根，任何装了 Pillow 的 Python）：
    python desktop/tools/make-icon.py
"""
import os
import sys

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(os.path.dirname(HERE), "build")
CLAY = (217, 119, 87, 255)
WHITE = (255, 255, 255, 255)
S = 512


def draw_icon(size=S):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    k = size / 512.0

    # 圆角方底
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=int(112 * k), fill=CLAY)
    # 气泡主体
    d.rounded_rectangle([int(96 * k), int(120 * k), int(416 * k), int(348 * k)],
                        radius=int(62 * k), fill=WHITE)
    # 左下小尾巴
    d.polygon([(int(148 * k), int(320 * k)), (int(148 * k), int(420 * k)),
               (int(240 * k), int(342 * k))], fill=WHITE)
    # 三个点
    for x in (186, 256, 326):
        d.ellipse([int((x - 21) * k), int(213 * k), int((x + 21) * k), int(255 * k)], fill=CLAY)
    return img


def main():
    os.makedirs(OUT, exist_ok=True)
    img = draw_icon(S)
    png = os.path.join(OUT, "icon.png")
    ico = os.path.join(OUT, "icon.ico")
    resample = getattr(Image, "Resampling", Image).LANCZOS
    img.resize((512, 512), resample).save(png)
    img.save(ico, sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    print("wrote", png, os.path.getsize(png), "bytes")
    print("wrote", ico, os.path.getsize(ico), "bytes")


if __name__ == "__main__":
    sys.exit(main())
