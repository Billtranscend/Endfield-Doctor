"""校准：俯瞰画面里设备旁的产物图标相对状态标记在什么位置，能不能直接认出产物。

  python src\\tools\\calib_product_icon.py debug\\after_close.png 369,283,idle,配件机 113,302,blocked,灌装机
每个参数：标记 x, y, 类型, 设备名。会把标记周围裁大图存到 debug/calib_<x>_<y>.png，
并在几个候选偏移处用 panel_recipe 的图标匹配列出该设备候选产物的差异。
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel_recipe as pr  # noqa: E402

OFFSETS = {
    "idle": [(14, -14), (16, -16), (12, -12), (18, -18)],
    "blocked": [(0, 78), (0, 75), (0, 82), (-2, 78), (2, 78)],
}
ICON_R = 20  # 产物图标圆形的半径（720p）


def candidates(machine: str) -> list[str]:
    outs = []
    for r in pr.RECIPES:
        if r["machine_name"] == machine:
            for o in r["outputs"]:
                if o not in outs:
                    outs.append(o)
    return outs


def main(argv):
    img = cv2.imread(argv[0])
    for spec in argv[1:]:
        x, y, kind, machine = spec.split(",")
        x, y = int(x), int(y)
        big = img[max(0, y - 120) : y + 120, max(0, x - 120) : x + 120]
        cv2.imwrite(str(pr.ROOT / "debug" / f"calib_{x}_{y}.png"), cv2.resize(big, None, fx=3, fy=3, interpolation=cv2.INTER_NEAREST))
        print(f"== {machine} 标记 ({x},{y}) {kind}")
        for dx, dy in OFFSETS[kind]:
            cx, cy = x + dx, y + dy
            tile = img[cy - ICON_R : cy + ICON_R, cx - ICON_R : cx + ICON_R]
            if tile.shape[0] != 2 * ICON_R or tile.shape[1] != 2 * ICON_R:
                continue
            scored = []
            for item in candidates(machine):
                ic = pr.icon(pr.icon_ids(item)[0])
                if ic is None:
                    continue
                best = float("inf")
                for size in (22, 26, 30, 34):
                    s = size / max(ic.shape[:2])
                    w, h = max(4, round(ic.shape[1] * s)), max(4, round(ic.shape[0] * s))
                    if w > 2 * ICON_R or h > 2 * ICON_R:
                        continue
                    small = cv2.resize(ic, (w, h), interpolation=cv2.INTER_AREA)
                    mask = (small[:, :, 3] > 200).astype(np.float32)
                    mask3 = np.repeat(mask[:, :, None], 3, axis=2)
                    if mask.sum() < 20:
                        continue
                    res = cv2.matchTemplate(tile.astype(np.float32), small[:, :, :3].astype(np.float32), cv2.TM_SQDIFF, mask=mask3)
                    best = min(best, float(np.sqrt(max(res.min(), 0) / mask3.sum())))
                scored.append((best, pr.label(item)))
            scored.sort()
            print(f"  偏移 ({dx:+d},{dy:+d}) → " + ", ".join(f"{n} {d:.0f}" for d, n in scored[:4]))


if __name__ == "__main__":
    main(sys.argv[1:])
