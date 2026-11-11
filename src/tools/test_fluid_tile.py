"""调试：看配方栏产物格子里的液滴被认成什么，并把格子放大保存。"""

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import fluid_icon  # noqa: E402
import panel_recipe as pr  # noqa: E402

name = sys.argv[1] if len(sys.argv) > 1 else "panel_filler"
img = cv2.imread(str(pr.ROOT / "debug" / f"{name}.png"))
tiles = pr.find_tiles(img)
print("格子：", tiles)
x, y, w, h = tiles[-1]
tile = img[y : y + h, x : x + w]
cv2.imwrite(str(pr.ROOT / "debug" / f"{name}_tile_x6.png"), cv2.resize(tile, (w * 6, h * 6), interpolation=cv2.INTER_NEAREST))
liquids = {p.stem for p in fluid_icon.ICON_DIR.glob("item_liquid_*.png")}
for m in fluid_icon.classify(tile, liquids)[:5]:
    print(f"  {m.fluid:<32} 差异 {m.score:7.1f}  反差 {m.contrast:6.1f}")
print("detect →", fluid_icon.detect(tile, liquids)[0])
