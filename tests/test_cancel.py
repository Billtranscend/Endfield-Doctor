"""点了取消后下一次给游戏发输入/截图就停（2026-11-11 群友：只巡武陵城一个分区，点取消没反应——以前只在分区之间查取消）。"""
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app")]

import api  # noqa: E402
import game  # noqa: E402


class _Past(Exception):
    """guard_size 被调到了 = 已经过了取消检查。"""


def test_guard_blocks_input_but_not_key_release():
    fake = game.GuardedController.__new__(game.GuardedController)
    fake._hwnd = 1
    fake._handle, fake._own = None, False  # 没真连游戏：免得析构时报错
    orig = game.guard_size

    def past(hwnd):
        raise _Past

    game.guard_size = past
    try:
        game.clear_stop()
        for name in ("post_click", "post_key_down", "post_screencap", "post_key_up"):
            try:
                getattr(game.GuardedController, name)(fake, 0, 0)
            except _Past:
                pass
        game.request_stop()
        for name in ("post_click", "post_swipe", "post_click_key", "post_key_down", "post_screencap", "post_scroll"):
            if not hasattr(game.Win32Controller, name):
                continue
            try:
                getattr(game.GuardedController, name)(fake, 0, 0)
                raise AssertionError(f"{name} 取消后还在发")
            except game.Cancelled:
                pass
        try:
            game.GuardedController.post_key_up(fake, 0x57)  # 松 W 不拦，否则角色一直往前走
            raise AssertionError("post_key_up 应当照常执行")
        except _Past:
            pass
        try:
            game.move_cursor(1, 640, 360)
            raise AssertionError("move_cursor 取消后还在移")
        except game.Cancelled:
            pass
        # Cancelled 不能被扫描里的 except Exception 重试吞掉
        assert not issubclass(game.Cancelled, Exception)
    finally:
        game.guard_size = orig
        game.clear_stop()


def test_job_cancel_mid_area():
    """模拟深度巡检扫一个分区的中途：一直在发截图，点取消后几秒内任务变成「已取消」，不是等扫完。"""
    tmp = Path(tempfile.mkdtemp())
    old_debug, api.DEBUG = api.DEBUG, tmp  # 任务日志写到临时目录
    a = api.Api()
    steps = {"n": 0}

    def long_scan(job):
        for _ in range(10000):  # 不取消要跑很久
            game.check_stop()
            steps["n"] += 1
            time.sleep(0.01)
        return "不该跑到这"

    assert a._start("patrol_test", long_scan)["ok"]
    time.sleep(0.2)
    a.cancel()
    t0 = time.time()
    while a._job.state == "running" and time.time() - t0 < 3:
        time.sleep(0.02)
    assert a._job.state == "cancelled", a._job.state
    assert a._job.report_id is None and steps["n"] < 1000
    a._thread.join(3)
    game.check_stop()  # 任务结束后取消标记已清：界面读游戏状态不受影响
    a.cancel()         # 没任务在跑时点取消不设标记
    game.check_stop()
    # 下一个任务开始时清掉取消标记
    assert a._start("next", lambda job: (game.check_stop(), "ok")[1])["ok"]
    a._thread.join(3)
    assert a._job.state == "done", (a._job.state, a._job.error)
    logs = sorted(tmp.glob("app_log_*.log"))
    assert len(logs) == 2, logs
    text = "\n".join(p.read_text(encoding="utf-8") for p in logs)
    assert "已取消" in text and "结束：cancelled" in text and "结束：done" in text, text
    api.DEBUG = old_debug
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_guard_blocks_input_but_not_key_release()
    test_job_cancel_mid_area()
    print("2/2 通过")
