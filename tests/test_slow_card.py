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
    test_fit_720p()
    print("4/4 通过")
