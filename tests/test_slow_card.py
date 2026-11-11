"""慢电脑（2026-11-12 群友）：
- 点基地图标后卡片弹得慢，1.5 秒时标题和按钮都读空，以前直接判「不是基地」；现在读空就再等（最多 CARD_WAIT 秒）。
- 1366×768 屏 MaaFw 截图是 1281×720，和 1280×720 的参考图 phaseCorrelate 报尺寸不一致；差几个像素的统一缩成 1280×720。"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import game  # noqa: E402
import navigate as N  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _card(appear_at):
    clock = _Clock()
    orig = N.time
    N.time = clock
    try:
        n = N.Navigator.__new__(N.Navigator)

        def ocr(roi):
            if clock.t < appear_at:
                return ""
            return "天王坪援建点" if roi == N.CARD_TITLE_ROI else "详情 传送"

        n.ocr_text = ocr
        return n.card_is_core(), clock.t
    finally:
        N.time = orig


def test_slow_card_waits():
    (ok, desc), t = _card(2.0)
    assert ok and t <= N.CARD_WAIT + 0.5, (desc, t)


def test_no_card_gives_up():
    (ok, desc), t = _card(99)
    assert not ok and t <= N.CARD_WAIT + 0.5, (desc, t)


def test_fast_card_no_wait():
    (ok, _), t = _card(0)
    assert ok and t == 0


def test_overlapping_icon_menu():
    """群友 H 景玉谷：基地图标旁边摆了息壤供电桩，点下去弹出「选择目标」菜单（据点 / 息壤供电桩），不是卡片。
    卡片读空时点菜单里的「据点」那一行，再读卡片。"""
    clock = _Clock()
    orig = N.time
    N.time = clock
    state = {"menu": False, "card": False, "clicks": []}
    try:
        n = N.Navigator.__new__(N.Navigator)
        n.shot = lambda: None
        n.find_cores = lambda image, threshold=None, tpls=None: [(0.92, 794, 188, "core_icon.png")]
        n.key = lambda k, *a: state.update(menu=False) if k == "esc" else None

        def click(x, y):
            state["clicks"].append((x, y))
            if (x, y) == (794, 188):
                state["menu"] = True
            elif state["menu"] and (x, y) == (945, 455):
                state["menu"], state["card"] = False, True

        n.click = click

        def ocr(roi):
            if not state["card"]:
                return ""
            return "天王坪援建点" if roi == N.CARD_TITLE_ROI else "详情 传送"

        n.ocr_text = ocr
        n.ocr_boxes = lambda roi: ([([925, 445, 40, 20], "据点"), ([925, 487, 90, 20], "息壤供电桩")]
                                   if state["menu"] else [])
        assert n.click_core({}) is True
        assert state["clicks"] == [(794, 188), (945, 455)], state["clicks"]
    finally:
        N.time = orig


def test_fit_720p():
    for w in (1281, 1279, 1290):
        out = game.fit_720p(np.zeros((720, w, 3), np.uint8))
        assert out.shape == (720, 1280, 3), w
    same = np.zeros((720, 1280, 3), np.uint8)
    assert game.fit_720p(same) is same
    wide = np.zeros((720, 1720, 3), np.uint8)  # 带鱼屏这类比例本来就过不了 16:9 检查：不硬缩
    assert game.fit_720p(wide) is wide


if __name__ == "__main__":
    test_slow_card_waits()
    test_no_card_gives_up()
    test_fast_card_no_wait()
    test_overlapping_icon_menu()
    test_fit_720p()
    print("5/5 通过")
