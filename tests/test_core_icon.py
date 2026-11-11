"""地图上找基地图标（2026-11-11 灰度群友应龙关传送失败）：
- 他的地图放大了一档，基地图标比模板大约 1.3 倍，原尺寸只匹配到 0.49；
- 原来先把界面区域涂黑再匹配，黑块边缘 (1206, 232) 假匹配到 0.6x，点到了他放的地图标记「标记1」。
样本在 tests/data/core_maps/：只涂黑了底部 UID 那一条（在找图标时不看的区域里）。
别用 game.mask_uid 涂：它左侧那块是给主菜单角色卡用的，黑块边缘会在地图上造出假匹配（108, 575）。"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import navigate as N  # noqa: E402
from paths import imread  # noqa: E402

DATA = ROOT / "tests" / "data" / "core_maps"
TPLS = [(p.name, imread(p)) for p in sorted((ROOT / "resource" / "badges").glob("core_icon*.png"))]


class _Nav:
    core_tpls = TPLS


def _cores(name, threshold=N.CORE_FALLBACK):
    img = cv2.imdecode(np.fromfile(str(DATA / name), np.uint8), 1)
    return N.Navigator.find_cores(_Nav(), img, threshold)


def test_top_candidate_is_base():
    cases = {  # 图 → 基地中心
        "yinglongguan_zoomed_tester.png": (774, 181),
        "jingyugu.png": (794, 188),
        "shoudun.png": (1015, 536),
        "wulingcheng_main.png": (562, 27),
    }
    for name, (bx, by) in cases.items():
        cs = _cores(name)
        assert cs, name
        s, x, y, tpl = cs[0]
        assert abs(x - bx) <= 15 and abs(y - by) <= 15, (name, cs[:3])
        others = [c for c in cs if abs(c[1] - bx) > 30 or abs(c[2] - by) > 30]
        assert not others or s - others[0][0] >= 0.05, (name, cs[:3])  # 留余量，别和别的图标咬得太近


def test_no_mask_edge_artifact():
    """界面区域（右上、右侧按钮）里不出候选，尤其是原来黑块边缘的 (1206, 232)。"""
    for name in ("yinglongguan_zoomed_tester.png", "no_base.png", "jingyugu.png"):
        assert not any(N.in_core_mask(x, y) or (abs(x - 1206) <= 10 and abs(y - 232) <= 10) for _, x, y, _ in _cores(name)), name


if __name__ == "__main__":
    test_top_candidate_is_base()
    test_no_mask_edge_artifact()
    print("2/2 通过")
