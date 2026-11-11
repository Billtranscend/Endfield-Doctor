"""标记归属（guess_name / click_points）的离线用例：标签列表取自真实截图的 OCR 结果，不需要游戏。

用法：.venv\\Scripts\\python.exe tests\\test_guess_name.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import badges  # noqa: E402
import patrol  # noqa: E402

CASES = [
    # 2026-11-05 应龙关第 3 行 (0,713) 现场 OCR：两个空闲 zz 各在一个暗管入口标签上方 38/43 像素，
    # 上方是生产中的种植机（532,387）/精炼炉（453,450，OCR 读成「相」）。zz 必须归暗管，不能归种植机。
    {
        "labels": [(394, 348, "采"), (434, 507, "暗"), (453, 450, "相"), (463, 318, "息"), (493, 506, "暗"),
                   (532, 387, "种"), (552, 486, "暗"), (616, 516, "息")],
        "badge": badges.Badge("idle", 485, 468, 20.0),
        "name": "暗管入口|暗管出口",
        "first_click": (493, 510),
    },
    {
        "labels": [(434, 507, "暗"), (453, 450, "精"), (493, 506, "暗"), (532, 387, "种"), (552, 486, "暗")],
        "badge": badges.Badge("idle", 545, 443, 20.0),
        "name": "暗管入口|暗管出口",
        "first_click": (552, 490),
    },
    # 小设备布局：标签在 zz 上方 45 像素，下方没有物流件标签 → 还是归上面那台
    {
        "labels": [(1583, 1190, "固"), (1454, 1188, "仓")],
        "badge": badges.Badge("idle", 1583, 1235, 20.0),
        "name": "固气转化机",
        "first_click": (1583, 1190),
    },
    # 大设备顶部布局：zz 下方 38 像素是生产设备标签 → 归下面的大设备（idle_on_top），物流件规则不抢
    {
        "labels": [(600, 300, "封"), (600, 250, "暗")],
        "badge": badges.Badge("idle", 600, 262, 20.0),
        "name": "封装机",
        "first_click": (600, 308),
    },
]


def main() -> int:
    p = patrol.Patrol.__new__(patrol.Patrol)  # 不连游戏：guess_name / click_points 不用控制器
    fails = 0
    for i, c in enumerate(CASES):
        name = p.guess_name(c["badge"], c["labels"])
        pts = p.click_points(c["badge"], c["labels"])
        ok = name == c["name"] and pts[0] == c["first_click"]
        print(f"[{i}] {'PASS' if ok else 'FAIL'} name={name!r} (期望 {c['name']!r}) first_click={pts[0]} (期望 {c['first_click']})")
        fails += not ok
    print(f"{len(CASES) - fails}/{len(CASES)} 通过")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
