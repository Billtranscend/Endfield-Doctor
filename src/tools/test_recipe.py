"""调试：用保存的详情面板截图测试配方识别。

  python src\\tools\\test_recipe.py                 # 跑全部用例
  python src\\tools\\test_recipe.py panel_x 灌装机 1  # 列出第 1 个格子对该设备所有候选物品的差异
"""

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel_recipe as pr  # noqa: E402

CASES = [
    ("panel_filler", "灌装机"),
    ("panel_filler3", "灌装机"),
    ("panel_zz", "配件机"),
    ("panel_shaper", "塑形机"),
    ("panel_furnace", "精炼炉"),
    ("panel_oven", "天有洪炉"),
]


def main(argv):
    if len(argv) == 3 and argv[2] == "--recipes":
        name, machine = argv[0], argv[1]
        img = cv2.imread(str(pr.ROOT / "debug" / f"{name}.png"))
        ins, outs = pr.canonical_tiles(img)
        in_c = [img[y : y + h, x : x + w] for x, y, w, h in ins]
        out_c = [img[y : y + h, x : x + w] for x, y, w, h in outs]
        rows = []
        for r in pr.RECIPES:
            if r["machine_name"] != machine:
                continue
            s_in = pr._assign(in_c, list(r["inputs"]))
            s_out = pr._assign(out_c, list(r["outputs"])[: len(outs)])
            if s_in is None or s_out is None:
                continue
            counted = s_in[2] + s_out[2]
            avg = (s_in[0] + s_out[0]) / counted if counted else float("inf")
            rows.append((avg, r["id"], (s_in, s_out), pr.describe(pr.Match(r, 0))))
        for avg, rid, parts, desc in sorted(rows)[:8]:
            print(f"  {avg:6.1f} in={parts[0]} out={parts[1]} {desc}  [{rid}]")
        return
    if len(argv) == 3:
        name, machine, idx = argv[0], argv[1], int(argv[2])
        img = cv2.imread(str(pr.ROOT / "debug" / f"{name}.png"))
        ins, outs = pr.canonical_tiles(img)
        tiles = ins + outs
        print("连通块：", pr.find_tiles(img), "标准格：", tiles)
        x, y, w, h = tiles[idx]
        tile = img[y : y + h, x : x + w]
        items = {i for r in pr.RECIPES if r["machine_name"] == machine for i in (*r["inputs"], *r["outputs"])}
        scored = sorted((pr.item_diff(tile, i), i) for i in items)
        for d, i in scored[:12]:
            print(f"  {d:7.1f}  {pr.label(i)}  [{i}]")
        return
    for name, machine in CASES:
        img = cv2.imread(str(pr.ROOT / "debug" / f"{name}.png"))
        if img is None:
            continue
        m = pr.identify_recipe(img, machine)
        print(f"{name} {machine} -> {pr.describe(m)}  差异 {m.score if m else '-'}")


if __name__ == "__main__":
    main(sys.argv[1:])
