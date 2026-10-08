#!/usr/bin/env python3
"""
生成仓库的 Social preview 卡片（1280x640 —— GitHub 要求的尺寸）。

为什么把它留在仓库里：这张图是要**上传到 GitHub 设置里**的，
不上传时它就是仓库里一个普通的 PNG。留着脚本，以后改字/换配色能重画，
而不是"这张图是谁怎么做的"。

依赖只有 Pillow（自带字体，不联网）：
    python3 tools/make-social-preview.py

上传位置：仓库 Settings -> General -> Social preview -> Edit -> Upload an image
"""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1280, 640
BG = (13, 17, 23)          # GitHub dark canvas
FG = (230, 237, 243)
MUTED = (139, 148, 158)
ACCENT = (42, 171, 238)    # Telegram blue
CHIP_BG = (22, 27, 34)
CHIP_BD = (48, 54, 61)

ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
ARIAL_B = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MENLO = "/System/Library/Fonts/Menlo.ttc"
M = 72                     # 页边距

OUT = Path(__file__).resolve().parent.parent / "assets" / "social-preview.png"


def main() -> int:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    def fnt(path, size):
        return ImageFont.truetype(path, size)

    def fit(text, path, size, max_w):
        """字号自适应：预览图会被缩得很小，宁可小一点也不能溢出。"""
        while size > 10:
            f = fnt(path, size)
            if d.textlength(text, font=f) <= max_w:
                return f
            size -= 1
        return fnt(path, 10)

    d.rectangle([0, 0, 8, H], fill=ACCENT)          # 左侧强调条

    y = 104
    d.text((M, y), "Telegram in  \u00b7  Apple apps out",
           font=fnt(ARIAL_B, 26), fill=ACCENT)
    y += 54

    title = "telegram-apple-reminders"
    ft = fit(title, ARIAL_B, 62, W - 2 * M)
    d.text((M, y), title, font=ft, fill=FG)
    y += int(ft.size * 1.30)

    sub = "Send one message to a Telegram bot \u2014 it lands in the right Apple app on your Mac."
    d.text((M, y), sub, font=fit(sub, ARIAL, 30, W - 2 * M), fill=FG)
    y += 54

    fc = fnt(ARIAL_B, 24)
    x = M
    for c in ("Reminders", "Calendar", "Notes"):
        tw = d.textlength(c, font=fc)
        d.rounded_rectangle([x, y, x + tw + 44, y + 50], radius=25,
                            fill=CHIP_BG, outline=CHIP_BD, width=2)
        d.text((x + 22, y + 10), c, font=fc, fill=FG)
        x += tw + 44 + 16
    y += 50 + 36

    note = "No database. No sync. Your Apple apps stay the single source of truth."
    d.text((M, y), note, font=fit(note, ARIAL, 26, W - 2 * M), fill=MUTED)
    y += 46

    d.text((M, y), "# memo    @ calendar    ! flag    (nothing) = to-do",
           font=fnt(MENLO, 22), fill=MUTED)

    d.text((M, H - 72), "Python + AppleScript + launchd   \u00b7   MIT   \u00b7   macOS",
           font=fnt(ARIAL, 22), fill=MUTED)
    repo = "github.com/Carlapple2025-gif/telegram-apple-reminders"
    fr = fnt(MENLO, 18)
    d.text((W - M - d.textlength(repo, font=fr), H - 70), repo, font=fr, fill=ACCENT)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    img.save(OUT, "PNG", optimize=True)
    print(f"✅ {OUT}  {img.size[0]}x{img.size[1]}  {OUT.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
