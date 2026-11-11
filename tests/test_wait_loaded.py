"""传送后等加载：还在加载画面（左上角「明日方舟」）就接着等，最多 60 秒（2026-11-11 群友 H：24 秒还在加载，被判传送失败）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import navigate as N  # noqa: E402


class _Clock:
    def __init__(self):
        self.t = 0.0

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += s


def _run(texts_by_time):
    """texts_by_time(t) → 这时左上角 OCR 读到的字。返回（结果, 用了几秒）。"""
    clock = _Clock()
    orig = N.time
    N.time = clock
    try:
        n = N.Navigator.__new__(N.Navigator)
        n.ocr_text = lambda roi: texts_by_time(clock.t)
        ok = n.wait_loaded()
        return ok, clock.t
    finally:
        N.time = orig


def test_slow_loading_waits():
    ok, t = _run(lambda t: "明日方" if t < 40 else "工业")
    assert ok and 40 <= t <= 45, t


def test_loading_capped():
    ok, t = _run(lambda t: "日方")
    assert not ok and t <= N.LOAD_MAX + 5, t


def test_no_loading_screen_gives_up_as_before():
    ok, t = _run(lambda t: "")
    assert not ok and t <= N.LOAD_WAIT + N.LOAD_EXTRA + 2, t


def test_fast_load():
    ok, t = _run(lambda t: "探索")
    assert ok and t <= N.LOAD_WAIT + 1


if __name__ == "__main__":
    test_slow_loading_waits()
    test_loading_capped()
    test_no_loading_screen_gives_up_as_before()
    test_fast_load()
    print("4/4 通过")
