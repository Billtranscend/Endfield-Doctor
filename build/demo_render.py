"""录演示视频（第二步：加字幕、加速、压成 H.264 mp4）。

读 demo_record.py 的 raw.mp4 + events.json：每段按 speed 抽帧，底部加半透明字幕条，前后各加一张片头/片尾，
用 imageio-ffmpeg 自带的 ffmpeg 压成 libx264 yuv420p（微信 / QQ 直接能放）。
用法：python build\\demo_render.py <录制目录> [--out <mp4 路径>] [--fps 20]
"""

import argparse
import json
import sys
from pathlib import Path

import cv2
import imageio_ffmpeg
import numpy as np
from PIL import Image, ImageDraw, ImageFont

FONT = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
ACCENT = (255, 212, 0)


def wrap(text: str, n: int) -> list[str]:
    return [text[i : i + n] for i in range(0, len(text), n)] or [""]


def caption(frame: np.ndarray, text: str, speed: int, font: ImageFont.FreeTypeFont, small: ImageFont.FreeTypeFont) -> np.ndarray:
    if not text:
        return frame
    im = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA")
    w, h = im.size
    lines = wrap(text, 40)
    lh = font.size + 10
    band_h = lh * len(lines) + 28
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    d.rectangle([0, h - band_h, w, h], fill=(10, 12, 14, 215))
    d.rectangle([0, h - band_h, w, h - band_h + 3], fill=ACCENT + (255,))
    for i, line in enumerate(lines):
        d.text((28, h - band_h + 14 + i * lh), line, font=font, fill=(240, 240, 240, 255))
    if speed > 1:
        tag = f"×{speed} 加速"
        tw = d.textlength(tag, font=small)
        d.rectangle([w - tw - 40, 18, w - 16, 18 + small.size + 14], fill=(0, 0, 0, 170), outline=ACCENT + (255,))
        d.text((w - tw - 28, 24), tag, font=small, fill=ACCENT + (255,))
    im = Image.alpha_composite(im, overlay).convert("RGB")
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def card(size: tuple[int, int], title: str, sub: str, font_big: ImageFont.FreeTypeFont, font: ImageFont.FreeTypeFont) -> np.ndarray:
    w, h = size
    im = Image.new("RGB", size, (15, 17, 19))
    d = ImageDraw.Draw(im)
    for x in range(0, w, 20):  # 斜纹条
        d.line([(x, h // 2 + 60), (x + 10, h // 2 + 60)], fill=ACCENT, width=4)
    tw = d.textlength(title, font=font_big)
    d.text(((w - tw) / 2, h / 2 - 90), title, font=font_big, fill=(240, 240, 240))
    sw = d.textlength(sub, font=font)
    d.text(((w - sw) / 2, h / 2 + 90), sub, font=font, fill=(160, 170, 180))
    return cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("folder")
    ap.add_argument("--out", default=None)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--title", default="终末地基建诊断")
    ap.add_argument("--sub", default="两分钟看懂怎么用 · 建议开声音？不用，看字幕就行")
    ap.add_argument("--end-title", default="就这些")
    ap.add_argument("--end-sub", default="遇到报错：把「报告」页里那一份 json 发到群里")
    a = ap.parse_args(argv)
    folder = Path(a.folder)
    meta = json.loads((folder / "events.json").read_text(encoding="utf-8"))
    events = meta["events"]
    src_fps = meta["fps"]
    cap = cv2.VideoCapture(str(folder / "raw.mp4"))
    w, h = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)), int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    w2, h2 = w - w % 2, h - h % 2  # yuv420p 要偶数
    font = ImageFont.truetype(FONT, 26)
    small = ImageFont.truetype(FONT, 20)
    big = ImageFont.truetype(FONT_BOLD if Path(FONT_BOLD).exists() else FONT, 54)
    out = Path(a.out or folder / "demo.mp4")
    writer = imageio_ffmpeg.write_frames(
        str(out), (w2, h2), fps=a.fps, codec="libx264", pix_fmt_out="yuv420p", quality=7, macro_block_size=1, ffmpeg_log_level="error"
    )
    writer.send(None)

    def emit(frame: np.ndarray, seconds: float):
        rgb = cv2.cvtColor(frame[:h2, :w2], cv2.COLOR_BGR2RGB)
        for _ in range(int(round(seconds * a.fps))):
            writer.send(np.ascontiguousarray(rgb))

    emit(card((w2, h2), a.title, a.sub, big, font), 3.0)
    # events.json 里的 intro：录屏前的步骤卡片（解压、管理员运行这类录不到的），[[标题, 说明, 秒数], …]
    for t_, s_, sec in meta.get("intro", []):
        emit(card((w2, h2), t_, s_, big, font), float(sec))
    # 逐帧：按时间找所在段，抽帧 + 字幕；输出按 a.fps 重采样（源 src_fps）
    idx, kept, acc = 0, 0, 0.0
    step = src_fps / a.fps  # 每输出一帧消耗多少源帧（不加速时）
    seg_i = -1
    speed, text = 1, ""
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        t = idx / src_fps
        while seg_i + 1 < len(events) and events[seg_i + 1]["t"] <= t:
            seg_i += 1
            speed, text = events[seg_i]["speed"], events[seg_i]["text"]
        acc += 1.0 / (step * speed)
        if acc >= 1.0:
            acc -= 1.0
            rgb = cv2.cvtColor(caption(frame, text, speed, font, small)[:h2, :w2], cv2.COLOR_BGR2RGB)
            writer.send(np.ascontiguousarray(rgb))
            kept += 1
        idx += 1
    emit(card((w2, h2), a.end_title, a.end_sub, big, font), 3.0)
    writer.close()
    print(f"源 {idx} 帧 → 输出 {kept} 帧（{kept / a.fps:.0f} 秒）+ 片头片尾 6 秒：{out}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
