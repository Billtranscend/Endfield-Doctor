"""任务计划第 6 项：管子颜色定流体（壤晶废液深青、沉积酸黄色）。

- 族名和族里的具体流体能并网，和别的流体不能；并完有具体名字就去掉族名。
- 深青阈值：2026-11-10 近景实测的壤晶废液管像素算深青，清水管、压在标签底下的水管（H 99–101 暗青）不算。
- 黄色管默认不自动定（和传送带同色相）。
"""
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import pipenet as PN  # noqa: E402
import near_patrol as NP  # noqa: E402

TEAL = next(k for k in PN.FLUID_FAMILIES if "深青" in k)
YELLOW = next(k for k in PN.FLUID_FAMILIES if "黄色" in k)


def test_families():
    assert PN.fluids_compatible({TEAL}, {"壤晶废液"}) and PN.fluids_compatible({TEAL}, {"惰性壤晶废液"})
    assert not PN.fluids_compatible({TEAL}, {"清水"}) and not PN.fluids_compatible({TEAL}, {YELLOW})
    assert PN.fluids_compatible({YELLOW}, {"沉积酸"}) and not PN.fluids_compatible({"污水"}, {"清水"})
    assert PN.fluids_compatible(set(), {"清水"})
    assert PN.fluids_merge({TEAL}, {"壤晶废液"}) == {"壤晶废液"}
    assert PN.fluids_merge({TEAL}, set()) == {TEAL}
    assert PN.carries({TEAL}, "壤晶废液") and not PN.carries({TEAL}, "清水") and PN.carries(set(), "清水")
    # 疑似污水（颜色像污水、平移半格能看到蓝管）：不挡和清水并网，并网后让位；单独时仍按污水算
    assert PN.fluids_compatible({"疑似污水"}, {"清水"}) and PN.fluids_merge({"疑似污水"}, {"清水"}) == {"清水"}
    assert not PN.carries({"疑似污水"}, "清水") and PN.carries({"疑似污水"}, "污水")
    assert not PN.fluids_compatible({"污水"}, {"清水"})


def _hsv(rows):
    return np.array([rows], np.uint8)


def test_teal_threshold():
    genuine = _hsv([(93, 140, 70), (95, 149, 72), (90, 92, 80), (92, 127, 78)])     # 武陵城提纯机旁壤晶废液管
    water = _hsv([(100, 178, 140), (101, 191, 124)])                                  # 清水管
    under_label = _hsv([(100, 139, 83), (100, 149, 82), (100, 128, 80)])              # 压在「仓库取货口」标签底下的水管
    assert NP.teal_color(genuine).all()
    assert not NP.teal_color(water).any()
    assert not NP.teal_color(under_label).any()
    assert NP.USE_YELLOW is False
    # 输入端进流检查：壤晶废液口按深青掩码量外面那段管子（原来报「没有颜色参数」）；沉积酸仍没有
    img = np.zeros((40, 40, 3), np.uint8)
    img[:] = cv2.cvtColor(np.array([[[93, 140, 70]]], np.uint8), cv2.COLOR_HSV2BGR)[0, 0]
    assert NP.fluid_pipe_mask(img, "壤晶废液").all() and NP.fluid_pipe_mask(img, "沉积酸") is None


if __name__ == "__main__":
    test_families()
    test_teal_threshold()
    print("2/2 通过")
