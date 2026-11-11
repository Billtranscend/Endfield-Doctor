"""P0：连接游戏窗口、截图、判断当前是不是简报界面。

只截图和识别，不发送任何鼠标键盘输入。
用法：.venv\\Scripts\\python.exe src\\p0_connect.py
"""

import sys

from maa.resource import Resource
from maa.tasker import Tasker

from game import ROOT, connect, find_game_window, init_toolkit, save_screenshot


def main() -> int:
    init_toolkit()

    window = find_game_window()
    if window is None:
        print("没找到游戏窗口（类名 UnityWndClass、标题含 Endfield）。请先打开游戏。")
        return 1
    print(f"找到游戏窗口：hwnd={window.hwnd} 标题={window.window_name!r}")

    controller = connect(window)
    if controller is None:
        print("连接游戏窗口失败。")
        return 1

    shot = save_screenshot(controller, "p0_screencap")
    h, w = controller.cached_image.shape[:2]
    ratio_ok = abs(w / h - 16 / 9) < 0.01
    print(f"截图尺寸：{w}x{h}（MaaFramework 已缩放），16:9：{'是' if ratio_ok else '否'}")
    print(f"调试截图（UID 已涂黑）：{shot}")

    resource = Resource()
    if not resource.post_bundle(str(ROOT / "resource")).wait().succeeded:
        print("加载 resource 失败。")
        return 1

    tasker = Tasker()
    tasker.bind(resource, controller)
    if not tasker.inited:
        print("Tasker 初始化失败。")
        return 1

    detail = tasker.post_task("P0_DetectScene").wait().get()
    hit = [node.name for node in detail.nodes] if detail else []
    names = {"Scene_Brief": "简报", "Scene_Overhead": "俯瞰", "Scene_World": "大世界"}
    scene = next((names[n] for n in hit if n in names), "未识别")
    print(f"命中节点：{hit}")
    print(f"当前界面：{scene}")
    return 0 if scene != "未识别" else 2


if __name__ == "__main__":
    sys.exit(main())
