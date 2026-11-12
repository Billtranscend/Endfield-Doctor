"""读蓝图：认页签、认「蓝图预览 / 编辑蓝图」页、认编辑页里误点设备弹出的「可选图标」小窗。

2026-11-12 群友（蓝图作者）的蓝图在「我的蓝图」里：那里 (1232,155) 是「预览&编辑」，点开的是「编辑蓝图」页，
0.10.3 / 0.10.4 报「没进到蓝图预览」。游戏内核对后适配：画布同一套，设备一览往下 50 px、一屏 4 行，点到设备弹「可选图标」小窗。

样本 tests/data/bpa_tabs/：shared_top / mine_top = F1 列表页最上面 60 行（共享蓝图 / 我的蓝图 选中，实拍）；
edit = 编辑蓝图页；edit_dev = 编辑蓝图页点到天有洪炉弹出小窗（底部 UID 一行涂掉了）。OCR 用假的，只测按图判断的部分。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

from bpa import game_ops as G  # noqa: E402
from paths import imread  # noqa: E402

DATA = ROOT / "tests" / "data" / "bpa_tabs"
BLANK = np.full((720, 1280, 3), 220, np.uint8)


def screen(top: np.ndarray) -> np.ndarray:
    img = np.full((720, 1280, 3), 200, np.uint8)
    img[: top.shape[0]] = top
    return img


def game(text: str = "") -> G.Game:
    """不连游戏的 Game：只用按图判断的方法；OCR 一律返回 text。"""
    g = G.Game.__new__(G.Game)
    g.text = lambda img, roi: text
    g.log = lambda *a: None
    return g


def swapped(a: tuple[int, int], b: tuple[int, int]) -> np.ndarray:
    """把共享蓝图样本里两个页签块对调（合成「系统蓝图」选中的画面）。"""
    top = imread(DATA / "shared_top.png").copy()
    x, y = top[:, a[0]:a[1]].copy(), top[:, b[0]:b[1]].copy()
    top[:, a[0]:a[1]], top[:, b[0]:b[1]] = y, x
    return top


def test_shared_and_my_tabs_pass():
    g = game()
    assert g.check_tab(screen(imread(DATA / "shared_top.png"))) == "共享蓝图"
    assert g.check_tab(screen(imread(DATA / "mine_top.png"))) == "我的蓝图"  # 实拍


def test_system_tab_stops_before_any_click():
    g = game()
    img = screen(swapped((228, 415), (420, 607)))
    assert g.active_tab(img) == "系统蓝图"
    try:
        g.check_tab(img)
    except G.SafetyStop as e:
        assert "系统蓝图" in str(e)
    else:
        raise AssertionError("在「系统蓝图」页签应该停下")


def test_unknown_tab_does_not_block():
    """页签认不出（没有深色的）时不拦，交给后面的标题核对。"""
    g = game()
    assert g.active_tab(BLANK) is None and g.check_tab(BLANK) is None


def test_viewer_mode_by_title():
    assert game("//蓝图预览").viewer_mode(BLANK) == "preview"
    assert game("//编辑蓝图").viewer_mode(BLANK) == "edit"
    g = game("蓝图一览")
    assert g.viewer_mode(BLANK) is None and not g.on_preview(BLANK)


def test_icon_popup_needs_dark_panel_and_title():
    popup, plain = imread(DATA / "edit_dev.png"), imread(DATA / "edit.png")
    assert game("天有洪炉可选图标").icon_popup(popup)
    assert not game("天有洪炉可选图标").icon_popup(plain)  # 没有实心深色面板：不去读字
    assert not game("别的字").icon_popup(popup)  # 有深色块但标题不是「可选图标」：不当小窗（不按 Esc）


def test_list_box_per_mode():
    """编辑蓝图页的设备一览比预览页低 50 px，底部被「返回 / 保存」挡住：一屏 4 整行；预览页 6 行。"""
    def rows(mode):
        top0, lo, hi = G.LIST_BOX[mode]
        return [r for r in range(10) if lo <= top0 + 70 * r and top0 + 70 * r + 62 <= hi]
    assert rows("preview") == [0, 1, 2, 3, 4, 5] and rows("edit") == [0, 1, 2, 3]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
