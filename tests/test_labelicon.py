"""标签图标识别（src/labelicon.py）用例：用 tests/data/label_icons/ 里的 6 张历史裁片（带 3 px 边、26 px 图标），
把裁片贴到一张假画面上、给出「文字框在图标右边 2 px」的 box，看 identify 认不认得对，以及首字不一致时是否拒绝。

用法：.venv\\Scripts\\python.exe tests\\test_labelicon.py
"""

import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import labelicon  # noqa: E402

DATA = ROOT / "tests" / "data" / "label_icons"


def place(crop: np.ndarray, ids: labelicon.LabelIcons):
    """把带边裁片贴进 200×200 的画面，返回 (画面, 对应的文字框)。裁片左上 = (50-shift, 50-shift)，图标右边缘 = 50+icon，文字框 x = 图标右边缘 + 2。"""
    img = np.zeros((200, 200, 3), np.uint8)
    img[:] = (40, 60, 30)
    y0, x0 = 50 - ids.shift, 50 - ids.shift
    img[y0:y0 + crop.shape[0], x0:x0 + crop.shape[1]] = crop
    box = [50 + ids.icon + 2, 50 + ids.icon // 2 - 10, 20, 20]
    return img, box


def main() -> int:
    ids = labelicon.LabelIcons()
    assert ids.available(), "resource/icons/labels 里没有模板（先跑 src/tools/icon_harvest.py --install）"
    fails = 0
    for f in sorted(DATA.glob("*.png")):
        name = f.stem
        crop = cv2.imdecode(np.fromfile(str(f), np.uint8), cv2.IMREAD_COLOR)
        img, box = place(crop, ids)
        r = ids.identify(img, box, first_char=name[0])
        good = r.ok and r.name == name
        print(f"  {'✓' if good else '✗'} {name}: {r.name} {r.score:.2f} 领先 {r.margin:.2f} {r.reason}")
        fails += not good
        wrong = ids.identify(img, box, first_char="封" if name[0] != "封" else "固")
        assert not wrong.ok and "首字" in wrong.reason, wrong
    blank = np.zeros((200, 200, 3), np.uint8)
    r = ids.identify(blank, [80, 50, 20, 20])
    assert not r.ok and "字形" in r.reason, r
    print(f"  ✓ 首字不一致拒绝、空白处拒绝")
    print(f"{6 - fails}/6 认对")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
