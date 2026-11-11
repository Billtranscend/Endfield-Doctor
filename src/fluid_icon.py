"""识别灌装瓶 / 耐压罐图标里叠加的液体或气体小图标。

简报里同名的「蓝铁瓶」「赤铜耐压罐」只能靠瓶身中间的液滴区分。
做法：把 AKEData 的液体/气体图标缩到几种尺寸，带透明遮罩做彩色模板匹配，取差异最小的那个；
再看匹配位置里外的颜色反差，反差太小说明那里只是瓶身，判为空瓶。
"""

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
from paths import imread as _imread  # cv2.imread/imwrite 不认中文路径
import numpy as np

from paths import ROOT

ICON_DIR = ROOT / "resource" / "icons" / "fluid"
# 720p 简报里液滴的高度大约在这个范围（像素）
DROP_HEIGHTS = range(14, 27, 2)
# 判为「有液体」要同时满足下面两条（2026-11-04 用武陵简报实测校准）：
# 有液体的反差 ≥ 47、差异 ≤ 380；空瓶反差 ≤ 30，或差异 ≥ 438（匹配到的是背景）
MIN_CONTRAST = 35.0
MAX_SCORE = 420.0


@dataclass
class Match:
    fluid: str
    score: float  # 遮罩内平均像素差，越小越像
    contrast: float  # 液滴内外平均颜色的距离


@lru_cache(maxsize=4)
def _templates(heights: tuple = tuple(DROP_HEIGHTS)):
    out = []
    for f in sorted(ICON_DIR.glob("*.png")):
        im = _imread(str(f), cv2.IMREAD_UNCHANGED)
        ys, xs = np.where(im[:, :, 3] > 30)
        im = im[ys.min() : ys.max() + 1, xs.min() : xs.max() + 1]
        for h in heights:
            w = max(4, round(im.shape[1] * h / im.shape[0]))
            small = cv2.resize(im, (w, h), interpolation=cv2.INTER_AREA)
            bgr = small[:, :, :3].astype(np.float32)
            mask = (small[:, :, 3] > 200).astype(np.uint8)
            mask3 = np.repeat(mask[:, :, None], 3, axis=2).astype(np.float32)
            out.append((f.stem, bgr, mask, mask3, float(mask.sum())))
    return out


def _contrast(img: np.ndarray, loc, mask: np.ndarray) -> float:
    x, y = loc
    h, w = mask.shape
    pad = 3
    y0, x0 = max(0, y - pad), max(0, x - pad)
    y1, x1 = min(img.shape[0], y + h + pad), min(img.shape[1], x + w + pad)
    big = np.zeros((y1 - y0, x1 - x0), np.uint8)
    big[y - y0 : y - y0 + h, x - x0 : x - x0 + w] = mask
    ring = cv2.dilate(big, np.ones((5, 5), np.uint8)) - big
    region = img[y0:y1, x0:x1]
    if big.sum() == 0 or ring.sum() == 0:
        return 0.0
    inside = region[big > 0].mean(axis=0)
    outside = region[ring > 0].mean(axis=0)
    return float(np.linalg.norm(inside - outside))


def classify(icon_bgr: np.ndarray, candidates: set[str] | None = None, heights=None) -> list[Match]:
    """返回按 score 从小到大排好的匹配；candidates 用来限定液体或气体；heights 指定液滴像素高度范围
    （简报图标 56 像素用默认 14–26；俯瞰里 40 像素的产物图标用 8–16）。"""
    img = icon_bgr.astype(np.float32)
    best: dict[str, Match] = {}
    for fid, bgr, mask, mask3, area in _templates(tuple(heights) if heights else tuple(DROP_HEIGHTS)):
        if candidates and fid not in candidates:
            continue
        if bgr.shape[0] > img.shape[0] or bgr.shape[1] > img.shape[1]:
            continue
        res = cv2.matchTemplate(img, bgr, cv2.TM_SQDIFF, mask=mask3)
        min_val, _, min_loc, _ = cv2.minMaxLoc(res)
        score = float(min_val) / (area * 3)
        if fid not in best or score < best[fid].score:
            best[fid] = Match(fid, score, _contrast(img, min_loc, mask))
    return sorted(best.values(), key=lambda m: m.score)


def detect(
    icon_bgr: np.ndarray, candidates: set[str] | None = None, heights=None, max_score: float = MAX_SCORE
) -> tuple[str | None, Match | None]:
    """返回 (液体 id 或 None 表示空瓶, 最佳匹配)。

    先按反差把「只是瓶身」的候选去掉，再在剩下的里取差异最小的；俯瞰里 40 像素的小图差异普遍偏高，
    调用方可以把 max_score 放宽（patrol 用 600）。
    """
    ranked = classify(icon_bgr, candidates, heights)
    if not ranked:
        return None, None
    with_drop = [m for m in ranked if m.contrast >= MIN_CONTRAST and m.score <= max_score]
    if with_drop:
        return with_drop[0].fluid, with_drop[0]
    return None, ranked[0]
