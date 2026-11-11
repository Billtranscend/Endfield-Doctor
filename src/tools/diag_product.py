"""诊断：俯瞰截图里某个标记旁的产物图标，列出容器底图分数和各候选产物的差异。

  python src\\tools\\diag_product.py debug\\after_close.png 113,440,blocked,灌装机 231,421,blocked,灌装机
"""

import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import panel_recipe as pr  # noqa: E402
import patrol  # noqa: E402

SIZES = (22, 26, 30, 34)


def main(argv):
    img = cv2.imread(argv[0])
    active = patrol.load_active_items()
    for spec in argv[1:]:
        x, y, kind, machine = spec.split(",")
        x, y = int(x), int(y)
        dx, dy = (14, -14) if kind == "idle" else (0, 78)
        r = patrol.PRODUCT_ICON_R
        tile = img[y + dy - r : y + dy + r, x + dx - r : x + dx + r]
        cv2.imwrite(str(pr.ROOT / "debug" / f"prod_{x}_{y}.png"), cv2.resize(tile, (200, 200), interpolation=cv2.INTER_NEAREST))
        outputs = sorted({o for rc in pr.RECIPES if rc["machine_name"] == machine for o in rc["outputs"]})
        outputs = [o for o in outputs if o in active] or outputs
        print(f"== {machine} ({x},{y}) 候选 {len(outputs)}")
        bases = {}
        for o in outputs:
            if pr.is_filled_container(o):
                b = next((b for p, b in pr.BASE_CONTAINER.items() if o.startswith(p)), None)
                if b:
                    bases.setdefault(b, []).append(o)
        for b in bases:
            print(f"  底图 {pr.label(b)}: {pr._icon_diff_free(tile, pr.ITEM_ICONS.get(b, b), sizes=SIZES):.1f}")
        scored = []
        for o in outputs:
            comp = pr.composite_icon(o) if pr.is_filled_container(o) else None
            scored.append((pr._icon_diff_free(tile, pr.icon_ids(o)[0], sizes=SIZES, ic=comp), o))
        for d, o in sorted(scored)[:8]:
            print(f"  {d:6.1f}  {pr.label(o)}")


if __name__ == "__main__":
    main(sys.argv[1:])
