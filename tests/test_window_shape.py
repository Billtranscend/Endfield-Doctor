"""画面比例检查：不是 16:9 就不操作游戏，按屏幕给出具体改法；运行中窗口尺寸变了就停（2026-11-11 灰度群友窗口模式 2560×1368）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import game  # noqa: E402
from game import aspect_problem, recommend  # noqa: E402


def test_aspect():
    for w, h in [(2560, 1440), (1920, 1080), (1280, 720), (3840, 2160), (1366, 768)]:
        assert aspect_problem(w, h) is None, (w, h)
    for w, h in [(2560, 1368), (1920, 1017), (1920, 1200), (3440, 1440), (1280, 800)]:
        msg = aspect_problem(w, h)
        assert msg and f"{w}×{h}" in msg and "16:9" in msg, (w, h)
    assert aspect_problem(0, 0) is None  # 最小化时判断不了，不拦


def test_recommend():
    assert recommend(1920, 1080) == (True, (1600, 900))
    assert recommend(2560, 1440) == (True, (1920, 1080))
    assert recommend(3840, 2160) == (True, (2560, 1440))
    assert recommend(1920, 1200) == (False, (1600, 900))   # 16:10 屏：全屏比例不对，只能窗口
    assert recommend(3440, 1440) == (False, (1920, 1080))  # 带鱼屏
    msg = aspect_problem(2560, 1368, (2560, 1440))          # 群友的情况
    assert "你的屏幕是 2560×1440" in msg and "全屏" in msg and "1920×1080" in msg
    msg = aspect_problem(1920, 1200, (1920, 1200))
    assert "全屏/无边框的画面比例也不对" in msg and "1600×900" in msg


def test_guard_size():
    sizes = {"now": (2560, 1440)}
    orig = game.client_size, game.monitor_of
    game.client_size = lambda hwnd: sizes["now"]
    game.monitor_of = lambda hwnd: (2560, 1440, "DISPLAY1")
    try:
        game.reset_sizes()
        game.guard_size(1)                      # 第一次：查 16:9 后记下
        sizes["now"] = (0, 0)
        game.guard_size(1)                      # 最小化不算变
        sizes["now"] = (1920, 1080)
        try:
            game.guard_size(1)
            raise AssertionError("运行中尺寸变了应当停")
        except game.WindowShapeError as e:
            assert "2560×1440" in str(e) and "1920×1080" in str(e)
        game.reset_sizes()                      # 新任务：按现在的尺寸重记
        game.guard_size(1)
        sizes["now"] = (2560, 1368)
        game.reset_sizes()
        try:
            game.guard_size(1)                  # 重记时也查 16:9
            raise AssertionError("不是 16:9 应当停")
        except game.WindowShapeError as e:
            assert "2560×1368" in str(e)
    finally:
        game.client_size, game.monitor_of = orig
        game.reset_sizes()


if __name__ == "__main__":
    test_aspect()
    test_recommend()
    test_guard_size()
    print("3/3 通过")
