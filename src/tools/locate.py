"""给报告里的设备出「定位图」：从扫描时存的每屏截图里裁出设备周围一块，框住设备本体，标上名字和坐标。

用户看不懂坐标（2026-11-04：「种植机 (763,837) 你给我坐标我也不知道在哪啊」），报告里的每台问题设备都要配这种图。

用法：.venv\\Scripts\\python.exe src\\tools\\locate.py <patrol.json> x,y[,名字] [x,y[,名字] ...] [--small] [--out 文件.png]
  patrol.json 里有 "shots" 就用它；没有就取 debug/shots 下时间 ≤ 扫描时间的最近一个目录。
  --small：小设备（空闲标记画在产物图标左下、标签在上方）的框；默认按大设备（标记在设备顶部、标签在下方）。
  多台设备时每台一张拼成一行。
"""

import json
import sys
from pathlib import Path

import cv2


def _imread(path):
    """cv2.imread 不支持中文路径（Windows）：目录名带分区名时读不到。"""
    return cv2.imdecode(np.fromfile(str(path), dtype=np.uint8), cv2.IMREAD_COLOR)
import numpy as np
from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paths import ROOT  # noqa: E402,F401
SHOTS = ROOT / "debug" / "shots"
FONT = next((p for p in [Path(r"C:\Windows\Fonts\msyh.ttc"), Path(r"C:\Windows\Fonts\simhei.ttf")] if p.exists()), None)
CROP_W, CROP_H = 360, 320  # 裁多大一块（720p 像素）
SAFE = (80, 100, 1200, 560)  # 屏幕里底部建造栏以上、左右留边的区域


def shots_dir(patrol: dict) -> Path | None:
    if patrol.get("shots"):
        p = ROOT / patrol["shots"]
        if p.exists():
            return p
    stamp = patrol.get("time") or ""
    cands = sorted(d for d in SHOTS.iterdir() if d.is_dir() and d.name <= stamp)
    return cands[-1] if cands else None


def best_shot(shots: Path, wx: int, wy: int) -> tuple[Path, int, int] | None:
    """能看到这个坐标、且它离屏幕中心最近的那张截图，返回（文件, 屏幕 x, 屏幕 y）。"""
    best = None
    for f in shots.glob("*.png"):
        try:
            cx, cy = (int(v) for v in f.stem.split("_"))
        except ValueError:
            continue
        sx, sy = wx - cx, wy - cy
        if not (SAFE[0] <= sx <= SAFE[2] and SAFE[1] <= sy <= SAFE[3]):
            continue
        d = abs(sx - 640) + abs(sy - 330)
        if best is None or d < best[0]:
            best = (d, f, sx, sy)
    return best[1:] if best else None


def locate_one(shots: Path, wx: int, wy: int, name: str, small: bool, shot: str | None = None, sx: int | None = None, sy: int | None = None) -> Image.Image | None:
    """shot/sx/sy：扫描时记下的「哪张图、图里哪个位置」，有就直接用（准）；没有才按坐标反查哪张图能看到。"""
    if shot and sx is not None and sy is not None and (shots / shot).exists():
        hit = (shots / shot, int(sx), int(sy))
    else:
        hit = best_shot(shots, wx, wy)
    if hit is None:
        return None
    f, sx, sy = hit
    img = _imread(str(f))
    if img is None:
        return None
    h, w = img.shape[:2]
    # 设备本体的框：大设备标记在顶部，本体在标记下方；小设备标记在产物图标左下，本体在标记上方
    box = (sx - 72, sy - 15, sx + 72, sy + 105) if not small else (sx - 60, sy - 95, sx + 60, sy + 35)
    x0 = int(np.clip(sx - CROP_W // 2, 0, w - CROP_W))
    y0 = int(np.clip(sy - CROP_H // 2 + 30, 0, h - CROP_H))
    crop = img[y0 : y0 + CROP_H, x0 : x0 + CROP_W]
    im = Image.fromarray(cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)).resize((CROP_W * 2, CROP_H * 2), Image.LANCZOS)
    draw = ImageDraw.Draw(im)
    bx0, by0, bx1, by1 = ((box[0] - x0) * 2, (box[1] - y0) * 2, (box[2] - x0) * 2, (box[3] - y0) * 2)
    draw.rectangle((bx0, by0, bx1, by1), outline=(255, 60, 60), width=5)
    font = ImageFont.truetype(str(FONT), 26) if FONT else ImageFont.load_default()
    text = f"{name} ({wx}, {wy})"
    tw = draw.textlength(text, font=font)
    tx, ty = max(4, min(bx0, im.width - tw - 8)), max(4, by0 - 36)
    draw.rectangle((tx - 4, ty - 2, tx + tw + 4, ty + 30), fill=(0, 0, 0))
    draw.text((tx, ty), text, fill=(255, 220, 80), font=font)
    return im


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    patrol = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    small = "--small" in argv
    out = Path(argv[argv.index("--out") + 1]) if "--out" in argv else ROOT / "debug" / "where.png"
    shots = shots_dir(patrol)
    if shots is None:
        print("找不到截图目录")
        return 1
    devices = {(d["world_x"], d["world_y"]): d for d in patrol["devices"]}
    panels = []
    for item in argv[1:]:
        if item.startswith("--") or item == str(out):
            continue
        parts = item.split(",")
        if len(parts) < 2 or not parts[0].lstrip("-").isdigit():
            continue
        wx, wy = int(parts[0]), int(parts[1])
        name = parts[2] if len(parts) > 2 else (devices.get((wx, wy), {}).get("name") or "设备")
        im = locate_one(shots, wx, wy, name, small)
        if im is None:
            print(f"({wx}, {wy}) 不在任何一屏截图里（{shots.name}）")
            continue
        panels.append(im)
    if not panels:
        return 1
    save_sheet(panels, out)
    print(f"已保存 {out}（截图目录 {shots.name}）")
    return 0


def save_sheet(panels: list[Image.Image], out: Path, per_row: int = 3) -> None:
    """几张定位图拼成一张（每行 per_row 张）。"""
    rows = [panels[i : i + per_row] for i in range(0, len(panels), per_row)]
    w = max(sum(p.width for p in r) + 10 * (len(r) - 1) for r in rows)
    h = sum(max(p.height for p in r) for r in rows) + 10 * (len(rows) - 1)
    sheet = Image.new("RGB", (w, h), (20, 20, 20))
    y = 0
    for r in rows:
        x = 0
        for p in r:
            sheet.paste(p, (x, y))
            x += p.width + 10
        y += max(p.height for p in r) + 10
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(out)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
