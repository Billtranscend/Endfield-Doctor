"""诊断：把配方栏的标准格子放大画出来，叠上「正确物品」图标的最佳匹配框，并列出候选差异。

  python src\\tools\\diag_tiles.py panel_zz 0 item_copper_nugget  panel_furnace 0 item_copper_ore ...
每三个参数一组：面板截图名、格子序号（原料格在前）、期望的物品 id（可省略写 -）。
输出 debug/diag_tiles.png。
"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel_recipe as pr  # noqa: E402

MACHINE = {"panel_filler": "灌装机", "panel_filler3": "灌装机", "panel_zz": "配件机", "panel_shaper": "塑形机", "panel_furnace": "精炼炉"}


def main(argv):
    groups = [argv[i : i + 3] for i in range(0, len(argv), 3)]
    panels = []
    for name, idx, want in groups:
        idx = int(idx)
        img = cv2.imread(str(pr.ROOT / "debug" / f"{name}.png"))
        ins, outs = pr.canonical_tiles(img)
        x, y, w, h = (ins + outs)[idx]
        tile = img[y : y + h, x : x + w]
        big = cv2.resize(tile, (w * 5, h * 5), interpolation=cv2.INTER_NEAREST)
        machine = MACHINE[name]
        items = {i for r in pr.RECIPES if r["machine_name"] == machine for i in (*r["inputs"], *r["outputs"])}
        items = {i for i in items if not pr.is_filled_container(i)}
        ranked = sorted((pr.item_diff(tile, i), i) for i in items)
        print(f"== {name} 格子 {idx} @({x},{y})")
        print("  前几名：", ", ".join(f"{pr.label(i)} {d:.1f}" for d, i in ranked[:4]))
        if want != "-":
            d, box = pr._icon_best(tile, pr.icon_ids(want)[0])
            rank = [i for _, i in ranked].index(want) + 1 if want in items else "-"
            print(f"  期望 {pr.label(want)}：差异 {d:.1f}，排名 {rank}，位置 {box}")
            if box:
                bx, by, bw, bh = box
                cv2.rectangle(big, (bx * 5, by * 5), ((bx + bw) * 5, (by + bh) * 5), (0, 0, 255), 1)
        cv2.circle(big, (pr.ICON_CENTER[0] * 5, pr.ICON_CENTER[1] * 5), 3, (255, 0, 0), -1)
        panels.append(big)
    hmax = max(p.shape[0] for p in panels)
    panels = [cv2.copyMakeBorder(p, 0, hmax - p.shape[0], 0, 10, cv2.BORDER_CONSTANT, value=(255, 255, 255)) for p in panels]
    cv2.imwrite(str(pr.ROOT / "debug" / "diag_tiles.png"), np.hstack(panels))


if __name__ == "__main__":
    main(sys.argv[1:])
