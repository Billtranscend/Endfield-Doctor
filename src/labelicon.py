"""标签图标识别：俯瞰画面里每个设备标签左边有一个白色类型图标（约 24 px，最远缩放），按设备类型固定。
用它和标签首字一起确认「这台是什么」，比只看截断成首字的 OCR 文字可靠得多。

模板由 tools/icon_harvest.py 从历史巡检（面板确认过名字的设备）自动裁出、对齐、平均而来，装在 resource/icons/labels/。
2026-11-07：16 类、383 个样本留一法 96.9%；弱类：扩容反应池 45%（标签位置特殊，裁窗常偏）、三种暗管口互相混（物流件，不影响关键设备）。
仓库取货口 / 仓库存取线基段 图标完全一样，并成「仓库口」。

用法：
    ids = LabelIcons()
    r = ids.identify(image, text_box, first_char="固")  # text_box = OCR 的标签文字框 [x, y, w, h]
    r.name, r.score, r.margin, r.ok
判定：best ≥ ACCEPT 且领先第二名 ≥ MARGIN 才算认出；给了首字时还要求类名首字一致（不一致 → ok=False，name 仍给出供参考）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from paths import ROOT

LABELS_DIR = ROOT / "resource" / "icons" / "labels"
ACCEPT = 0.60  # 最佳相关至少这么高（留一法里本类最低 0.42–0.9，他类最高 0.44–0.92；暗管口之间不靠这个分）
MARGIN = 0.10  # 领先第二名


@dataclass
class IconMatch:
    name: str | None
    score: float
    second: str | None
    margin: float
    ok: bool
    reason: str = ""


class LabelIcons:
    def __init__(self, path: Path = LABELS_DIR):
        self.templates: dict[str, np.ndarray] = {}
        self.icon, self.shift, self.same = 26, 3, {}
        idx = path / "index.json"
        if not idx.exists():
            return
        meta = json.loads(idx.read_text(encoding="utf-8"))
        self.icon, self.shift, self.same = meta.get("icon", 26), meta.get("shift", 3), meta.get("same", {})
        for name in meta.get("classes", {}):
            f = path / f"{name}.png"
            if f.exists():
                tpl = cv2.imdecode(np.fromfile(str(f), np.uint8), cv2.IMREAD_GRAYSCALE)
                if tpl is not None and tpl.shape == (self.icon, self.icon):
                    self.templates[name] = tpl

    def available(self) -> bool:
        return bool(self.templates)

    def canonical(self, name: str) -> str:
        return self.same.get(name, name)

    def crop(self, image: np.ndarray, box) -> np.ndarray | None:
        """标签文字框左边的图标，带 shift 像素边（和 icon_harvest.crop_icon 同一规则）。"""
        x, y, w, h = box
        cy = y + h // 2
        x1 = x - 2
        x0 = x1 - self.icon
        y0 = cy - self.icon // 2
        pad = self.shift
        if x0 - pad < 0 or y0 - pad < 0 or y0 + self.icon + pad > image.shape[0] or x1 + pad > image.shape[1]:
            return None
        return image[y0 - pad:y0 + self.icon + pad, x0 - pad:x1 + pad]

    @staticmethod
    def binarize(crop: np.ndarray) -> np.ndarray:
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        return ((hsv[..., 2] > 175) & (hsv[..., 1] < 70)).astype(np.uint8) * 255

    def scores(self, crop: np.ndarray) -> list[tuple[str, float]]:
        b = self.binarize(crop).astype(np.float32)
        out = []
        for name, tpl in self.templates.items():
            res = cv2.matchTemplate(b, tpl.astype(np.float32), cv2.TM_CCOEFF_NORMED)
            out.append((name, float(res.max()) if res.size else -1.0))
        out.sort(key=lambda kv: -kv[1])
        return out

    def identify(self, image: np.ndarray, box, first_char: str | None = None) -> IconMatch:
        crop = self.crop(image, box)
        if crop is None or not self.templates:
            return IconMatch(None, 0.0, None, 0.0, False, "裁不到图标" if crop is None else "没有模板")
        core = self.binarize(crop)[self.shift:-self.shift, self.shift:-self.shift]
        if (core > 0).sum() < 60:
            return IconMatch(None, 0.0, None, 0.0, False, "图标处没有白色字形")
        sc = self.scores(crop)
        name, score = sc[0]
        second, s2 = sc[1] if len(sc) > 1 else (None, -1.0)
        margin = score - s2
        if score < ACCEPT:
            return IconMatch(name, score, second, margin, False, f"相关 {score:.2f} 低于 {ACCEPT}")
        if margin < MARGIN:
            return IconMatch(name, score, second, margin, False, f"和 {second} 只差 {margin:.2f}")
        if first_char and not name.startswith(first_char) and not (name == "仓库口" and first_char == "仓"):
            return IconMatch(name, score, second, margin, False, f"图标像 {name}，但标签首字是「{first_char}」")
        return IconMatch(name, score, second, margin, True)
