"""调试用：向游戏窗口发送按键、点击、滚轮，或截一张 UID 已涂黑的图。

坐标都按 1280x720（MaaFramework 缩放后的截图坐标）。
发送输入需要本程序以管理员身份运行（游戏是管理员进程）。

用法：
  python src\\tools\\game_input.py shot [名字]
  python src\\tools\\game_input.py key capslock|esc|f1|m|w|a|s|d|0x14
  python src\\tools\\game_input.py hold w 0.8
  python src\\tools\\game_input.py click 640 360
  python src\\tools\\game_input.py press 640 360        # 按下-停顿-松开，用于重叠目标
  python src\\tools\\game_input.py scroll 640 360 -120  # 负数缩小，正数放大
  python src\\tools\\game_input.py drag 640 200 640 500   # 拖动（地图平移）
  python src\\tools\\game_input.py caps                 # 查看系统大写锁定状态
"""

import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from game import connect, find_game_window, init_toolkit, save_screenshot, wheel  # noqa: E402

VK = {
    "capslock": 0x14,
    "esc": 0x1B,
    "enter": 0x0D,
    "f1": 0x70,
    "f10": 0x79,
    "m": 0x4D,
    "e": 0x45,
    "q": 0x51,
    "z": 0x5A,
    "x": 0x58,
    "w": 0x57,
    "a": 0x41,
    "s": 0x53,
    "d": 0x44,
}


def vk_code(name: str) -> int:
    return int(name, 16) if name.lower().startswith("0x") else VK[name.lower()]


def caps_on() -> bool:
    # GetKeyState 读的是本线程的键盘状态，先同步一次系统状态再读
    ctypes.windll.user32.GetKeyboardState((ctypes.c_ubyte * 256)())
    return bool(ctypes.windll.user32.GetKeyState(0x14) & 1)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 1
    cmd, args = argv[0], argv[1:]

    if cmd == "caps":
        print(f"大写锁定：{'开' if caps_on() else '关'}")
        return 0

    init_toolkit()
    window = find_game_window()
    if window is None:
        print("没找到游戏窗口。")
        return 1
    controller = connect(window)
    if controller is None:
        print("连接游戏窗口失败。")
        return 1

    if cmd == "shot":
        print(save_screenshot(controller, args[0] if args else "shot"))
        return 0
    if cmd == "key":
        # post_click_key 按下松开间隔太短，CapsLock 偶尔不生效，所以手动停 50 毫秒
        code = vk_code(args[0])
        controller.post_key_down(code).wait()
        time.sleep(0.05)
        ok = controller.post_key_up(code).wait().succeeded
        time.sleep(0.2)
    elif cmd == "hold":
        code, seconds = vk_code(args[0]), float(args[1])
        controller.post_key_down(code).wait()
        time.sleep(seconds)
        ok = controller.post_key_up(code).wait().succeeded
    elif cmd == "click":
        ok = controller.post_click(int(args[0]), int(args[1])).wait().succeeded
    elif cmd == "press":
        x, y = int(args[0]), int(args[1])
        controller.post_touch_down(x, y).wait()
        time.sleep(0.15)
        ok = controller.post_touch_up().wait().succeeded
    elif cmd == "drag":
        # drag x1 y1 x2 y2 [毫秒]：按住左键从 (x1,y1) 拖到 (x2,y2)，用来拖地图
        x1, y1, x2, y2 = (int(a) for a in args[:4])
        ms = int(args[4]) if len(args) > 4 else 600
        ok = controller.post_swipe(x1, y1, x2, y2, ms).wait().succeeded
    elif cmd == "scroll":
        wheel(window.hwnd, int(args[0]), int(args[1]), int(args[2]))
        ok = True
    else:
        print(f"未知命令：{cmd}")
        return 1

    print(f"{cmd} {' '.join(args)} → {'成功' if ok else '失败'}；大写锁定：{'开' if caps_on() else '关'}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
