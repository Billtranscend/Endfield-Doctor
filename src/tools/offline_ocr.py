"""离线 OCR：不连游戏，对保存下来的截图跑 MaaFW 的 OCR（和巡检用同一套模型、同一个 roi 约定）。

pip 里的 MaaFW 没带 MaaDbgControlUnit，所以用 CustomController 把一张图当成「屏幕」喂给 Tasker。
用途：从历史每屏截图里裁标签图标做模板、离线测标签识别率、复核巡检结论。

    from tools.offline_ocr import OfflineOCR
    ocr = OfflineOCR()
    boxes = ocr.boxes(image_bgr, [340, 80, 720, 465])   # [(box[x,y,w,h], text, score)]
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from maa.controller import CustomController  # noqa: E402
from maa.resource import Resource  # noqa: E402
from maa.tasker import Tasker  # noqa: E402

from game import ROOT, init_toolkit  # noqa: E402


class ImageController(CustomController):
    """screencap 永远返回当前设的那张图；所有输入都当成功。"""

    def __init__(self):
        super().__init__()
        self.image = np.zeros((720, 1280, 3), np.uint8)

    def connect(self) -> bool:
        return True

    def request_uuid(self) -> str:
        return "offline-image"

    def start_app(self, intent: str) -> bool:
        return True

    def stop_app(self, intent: str) -> bool:
        return True

    def screencap(self) -> np.ndarray:
        return self.image

    def click(self, x, y) -> bool:
        return True

    def swipe(self, x1, y1, x2, y2, duration) -> bool:
        return True

    def touch_down(self, contact, x, y, pressure) -> bool:
        return True

    def touch_move(self, contact, x, y, pressure) -> bool:
        return True

    def touch_up(self, contact) -> bool:
        return True

    def click_key(self, keycode) -> bool:
        return True

    def input_text(self, text) -> bool:
        return True

    def key_down(self, keycode) -> bool:
        return True

    def key_up(self, keycode) -> bool:
        return True

    def scroll(self, x, y, dx, dy) -> bool:
        return True

    def relative_move(self, dx, dy) -> bool:
        return True

    def shell(self, cmd, timeout) -> str:
        return ""

    def inactive(self) -> bool:
        return True

    def get_info(self) -> str:
        return "{}"


class OfflineOCR:
    def __init__(self):
        init_toolkit()
        self.ctrl = ImageController()
        if not self.ctrl.post_connection().wait().succeeded:
            raise RuntimeError("离线控制器连接失败")
        res = Resource()
        res.post_bundle(str(ROOT / "resource")).wait()
        self.tasker = Tasker()
        self.tasker.bind(res, self.ctrl)
        self.n = 0
        self.seconds = 0.0

    def boxes(self, image: np.ndarray, roi: list[int], only_rec: bool = False) -> list[tuple[list[int], str, float]]:
        self.ctrl.image = image
        self.ctrl.post_screencap().wait()  # 先让控制器缓存这张图：不先截一次，Tasker 第一次 OCR 拿到的是空图（2026-11-07 实测 0 结果）
        node = {"Offline_OCR": {"recognition": "OCR", "roi": roi, "only_rec": only_rec, "action": "DoNothing", "timeout": 1, "rate_limit": 0}}
        t = time.perf_counter()
        detail = self.tasker.post_task("Offline_OCR", node).wait().get()
        self.n += 1
        self.seconds += time.perf_counter() - t
        if not detail or not detail.nodes:
            print(f"  OCR 没有结果节点（detail={detail!r}）")
            return []
        reco = detail.nodes[0].recognition
        if reco is None:  # 整块没有文字（草地、边界外）
            return []
        return [(list(r.box), r.text.strip(), float(r.score)) for r in reco.all_results]


    def close(self):
        """只给独立脚本用（程序里用 shared()，不要调这个）。进程退出时 MaaFW 的自定义控制器线程会卡住不退（2026-11-07 实测脚本结束后挂起），用完直接硬退出。"""
        import os
        sys.stdout.flush()
        os._exit(0)


_SHARED: OfflineOCR | None = None


def shared() -> OfflineOCR:
    """进程内共用的实例，用完不关。程序（基建护士）里必须用这个：close() 会硬退出整个进程，
    在程序里调用等于把程序关掉（2026-11-08 扫底账实测：识别完进程直接退出，底账没存）。程序退出时由 app/main.py 硬退出。"""
    global _SHARED
    if _SHARED is None:
        _SHARED = OfflineOCR()
    return _SHARED


def started() -> bool:
    return _SHARED is not None


def imread(path: str | Path) -> np.ndarray:
    """cv2.imread 遇到中文路径返回 None，用 imdecode。"""
    return cv2.imdecode(np.fromfile(str(path), np.uint8), cv2.IMREAD_COLOR)


if __name__ == "__main__":  # python src\tools\offline_ocr.py <截图> [x y w h]
    img = imread(sys.argv[1])
    roi = [int(v) for v in sys.argv[2:6]] if len(sys.argv) >= 6 else [340, 80, 720, 465]
    o = OfflineOCR()
    for box, text, score in sorted(o.boxes(img, roi), key=lambda r: (r[0][1], r[0][0])):
        print(box, f"{score:.2f}", text)
    print(f"{o.n} 次 OCR，{o.seconds:.2f} 秒")
    o.close()
