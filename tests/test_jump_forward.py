"""传送落地后进协议区域：先按住 W 跳两下（用户 2026-11-11），还不行再按老办法四处走；W 一定会松开。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import navigate as N  # noqa: E402


class _Job:
    def wait(self):
        return self


class _Ctrl:
    def __init__(self, fail_on_space=False):
        self.log, self.fail = [], fail_on_space

    def post_key_down(self, vk):
        if self.fail and vk == N.VK["space"]:
            raise RuntimeError("模拟出错")
        self.log.append(("down", vk))
        return _Job()

    def post_key_up(self, vk):
        self.log.append(("up", vk))
        return _Job()


def _nav(ctrl, scenes):
    n = N.Navigator.__new__(N.Navigator)  # 不连游戏
    n.controller = ctrl
    n.last_error = ""
    it = iter(scenes)
    n.scene = lambda: next(it)
    n.ocr_text = lambda roi: "工业"
    return n


def test_jump_keys():
    N.time.sleep = lambda s: None
    c = _Ctrl()
    _nav(c, []).jump_forward(1.2)
    W, SP = N.VK["w"], N.VK["space"]
    assert c.log[0] == ("down", W) and c.log[-1] == ("up", W)
    assert c.log.count(("down", SP)) == N.JUMPS == c.log.count(("up", SP))


def test_w_released_on_error():
    N.time.sleep = lambda s: None
    c = _Ctrl(fail_on_space=True)
    try:
        _nav(c, []).jump_forward(1.2)
    except RuntimeError:
        pass
    assert c.log[-1] == ("up", N.VK["w"])


def test_enter_overhead_jumps_first():
    """落地不在区域里（第一次看不是大世界提示）→ 先跳；跳完看到「进入俯瞰」→ 按 CapsLock。"""
    N.time.sleep = lambda s: None
    c = _Ctrl()
    # scene() 调用顺序：开头查是否已在俯瞰、原地、跳完、按 CapsLock 后
    n = _nav(c, ["Scene_Unknown", "Scene_Unknown", "Scene_World", "Scene_Overhead"])
    assert n.enter_overhead()
    downs = [vk for kind, vk in c.log if kind == "down"]
    assert downs[:2] == [N.VK["w"], N.VK["space"]] and downs[-1] == N.VK["capslock"]
    assert N.VK["a"] not in downs and N.VK["s"] not in downs  # 跳一下就进去了，没有四处走


if __name__ == "__main__":
    test_jump_keys()
    test_w_released_on_error()
    test_enter_overhead_jumps_first()
    print("3/3 通过")
