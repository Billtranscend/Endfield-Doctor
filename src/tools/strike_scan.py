"""罢工图标（橙色圆环 + 感叹号 + 循环箭头，用户 2026-11-07 截图）：从历史每屏截图里找出全部出现位置，统计它旁边是什么设备。

颜色：HSV 色相 8–22、饱和 >180、亮度 >200 的橙色像素；图标直径约 24 px（最远缩放）。用连通域找橙色块，再按「环 + 中心感叹号」
的形状过滤：块的外接框 20–30 px 见方、橙色占外接框面积 35%–70%（实心的橙色产物图标/传送带箭头不满足）。
找到后用 OCR 缓存（debug/icon_harvest/ocr_cache.json）里离它最近的标签首字说明旁边是什么。

用法：.venv\\Scripts\\python.exe src\\tools\\strike_scan.py [--install]
  --install  把样本平均成模板存到 resource/badges/strike.png（给 badges.py 加「罢工」标记用）
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
DEBUG = ROOT / "debug"
UI_MASKS = [(0, 0, 340, 260), (520, 10, 240, 70), (1060, 0, 220, 720), (0, 545, 1280, 175)]  # 小地图、顶部、右菜单、底部建造栏


def orange_mask(img: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    m = ((hsv[..., 0] >= 8) & (hsv[..., 0] <= 22) & (hsv[..., 1] > 180) & (hsv[..., 2] > 200)).astype(np.uint8)
    for x, y, w, h in UI_MASKS:
        m[y:y + h, x:x + w] = 0
    return m


SEEDS = [("20261105-220413/711_355.png", 240, 422), ("20261105-220413/711_355.png", 338, 482), ("20261105-220413/711_355.png", 597, 475)]
R = 14  # 模板半径：图标直径约 24
THRESH = 0.72


def seed_template() -> np.ndarray:
    """三个人工确认过的样本平均（对照图 scratchpad/orange2.png）。只在橙色掩膜上匹配，背景不参与。"""
    acc = None
    for rel, x, y in SEEDS:
        img = cv2.imdecode(np.fromfile(str(DEBUG / "shots" / rel), np.uint8), cv2.IMREAD_COLOR)
        m = orange_mask(img)[y - R:y + R, x - R:x + R].astype(np.float32) * 255
        acc = m if acc is None else acc + m
    return (acc / len(SEEDS)).astype(np.uint8)


TEMPLATE = None


def find_strikes(img: np.ndarray) -> list[tuple[int, int, int, int]]:
    """橙色掩膜上做归一化相关模板匹配，≥THRESH 算命中，近邻去重。返回 [(cx, cy, 分数×100, 橙色像素数)]。"""
    global TEMPLATE
    if TEMPLATE is None:
        TEMPLATE = seed_template()
    m = orange_mask(img).astype(np.float32) * 255
    res = cv2.matchTemplate(m, TEMPLATE.astype(np.float32), cv2.TM_CCOEFF_NORMED)
    out = []
    ys, xs = np.where(res >= THRESH)
    for y, x in sorted(zip(ys, xs), key=lambda p: -res[p[0], p[1]]):
        cx, cy = int(x + R), int(y + R)
        if any(abs(cx - ox) <= 12 and abs(cy - oy) <= 12 for ox, oy, _, _ in out):
            continue
        out.append((cx, cy, int(res[y, x] * 100), int(m[cy - R:cy + R, cx - R:cx + R].sum() / 255)))
    return out


def main(argv: list[str]) -> int:
    cache_path = DEBUG / "icon_harvest" / "ocr_cache.json"
    cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
    shots = sorted(p for d in (DEBUG / "shots").iterdir() if d.is_dir() for p in d.glob("*.png"))
    hits = []
    samples = []
    for p in shots:
        img = cv2.imdecode(np.fromfile(str(p), np.uint8), cv2.IMREAD_COLOR)
        if img is None:
            continue
        for cx, cy, size, area in find_strikes(img):
            boxes = cache.get(str(p), [])
            near = sorted(
                ((abs(b[0] + b[2] // 2 - cx) + abs(b[1] + b[3] // 2 - cy), re.sub(r"[.。·…]+$", "", t)[:1], b[0] + b[2] // 2 - cx, b[1] + b[3] // 2 - cy)
                 for b, t, s in boxes if re.match(r"^[一-鿿]", t)),
            )[:2]
            hits.append({"shot": str(p.relative_to(ROOT)), "x": int(cx), "y": int(cy), "size": int(size), "near": [(int(d), t, int(dx), int(dy)) for d, t, dx, dy in near if d <= 120]})
            if 0 <= cy - 14 and cy + 14 <= img.shape[0] and 0 <= cx - 14 and cx + 14 <= img.shape[1]:
                samples.append(img[cy - 14:cy + 14, cx - 14:cx + 14])
    print(f"{len(shots)} 张截图，罢工图标 {len(hits)} 处")
    by_day = Counter(h["shot"].split("\\")[2][:8] if "\\" in h["shot"] else h["shot"].split("/")[2][:8] for h in hits)
    print("按日期：", dict(sorted(by_day.items())))
    near_char = Counter(h["near"][0][1] for h in hits if h["near"])
    print("最近标签首字：", near_char.most_common(10))
    offs = Counter((round(h["near"][0][2] / 10) * 10, round(h["near"][0][3] / 10) * 10) for h in hits if h["near"])
    print("最近标签相对位置（10px 桶）：", offs.most_common(6))
    for h in hits[:12]:
        print("  ", h)
    out = DEBUG / "icon_harvest" / "strike_hits.json"
    out.write_text(json.dumps(hits, ensure_ascii=False, indent=1), encoding="utf-8")
    if samples:
        sheet = np.hstack([cv2.resize(s, (112, 112), interpolation=cv2.INTER_NEAREST) for s in samples[:10]])
        cv2.imencode(".png", sheet)[1].tofile(str(DEBUG / "icon_harvest" / "strike_samples.png"))
        if "--install" in argv:
            dest = ROOT / "resource" / "badges" / "strike.png"
            cv2.imencode(".png", samples[0])[1].tofile(str(dest))  # 存一张原图样本（badges.py 按自己的方式取色/做遮罩）
            print("模板已存", dest, "样本", len(samples))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
