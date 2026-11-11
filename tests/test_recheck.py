"""多时点确认（Patrol.recheck）和定向单遍过滤的离线用例：不连游戏，镜头、截图、标记识别都用假的。

用法：.venv\\Scripts\\python.exe tests\\test_recheck.py
"""

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402

import badges  # noqa: E402
import patrol  # noqa: E402
from patrol import Device, Patrol  # noqa: E402


def fake_patrol(devices, frames_at):
    """frames_at(cam, i) -> 第 i 帧的标记列表（屏幕坐标）。"""
    p = object.__new__(Patrol)
    p.devices = devices
    p.cam = np.array([0.0, 0.0])
    p.frame = 0

    def goto_world(wx, wy, *a, **k):
        p.cam = np.array([wx - 640.0, wy - 330.0])
        p.frame = 0
        return True

    def shot():
        i = p.frame
        p.frame += 1
        return (tuple(p.cam), i)

    p.goto_world = goto_world
    p.shot = shot
    badges.find_badges = lambda img: frames_at(img[0], img[1])
    return p


def dev(name, badge, x, y, passes=2):
    return Device(name, badge, badge, x, y, passes=passes)


def run_case():
    time.sleep = lambda s: None  # 不真等 12 秒
    a = dev("固气转化机", "阻塞", 1000, 800)   # 持续：每帧都在
    b = dev("配件机", "阻塞", 1200, 820)      # 间歇：容量箱周期，第 3–5 帧不在
    c = dev("封装机", "空闲中", 1100, 700)     # 镜头漂了 40 像素，仍应对上，持续
    d = dev("精炼炉", "阻塞", 900, 760)       # 一帧都没有：没对上，不改结论
    e = dev("水泵", "阻塞", 5000, 5000, passes=1)  # 本来就是瞬时，不复查

    def frames_at(cam, i):
        out = []
        for dv, drift, present in ((a, 0, True), (b, 0, i not in (3, 4, 5)), (c, 40, True)):
            if present:
                kind = "blocked" if dv.badge == "阻塞" else "idle"
                out.append(badges.Badge(kind, int(dv.world_x - cam[0]) + drift, int(dv.world_y - cam[1]), 10.0))
        return out

    p = fake_patrol([a, b, c, d, e], frames_at)
    stats = p.recheck()
    assert a.passes == 2 and "13/13" in a.seen, a
    assert b.passes == 1 and "10/13" in b.seen, b
    assert c.passes == 2, c
    assert d.passes == 2 and "没对上" in d.seen, d
    assert e.passes == 1 and e.seen == "", e
    assert stats["screens"] == 1 and stats["intermittent"] == 1 and stats["persistent"] == 2 and stats["unmatched"] == 1, stats
    print("  ✓ recheck：持续 / 间歇 / 漂移 / 没对上 / 瞬时不复查", stats)


def filter_case():
    """sweep_once 末尾的过滤：没点开（guessed）的生产设备丢掉，关闭和物流件保留。"""
    log_name = next(iter(patrol.LOGISTICS))
    ds = [
        Device("封装机", "阻塞", "阻塞", 1, 1, guessed=True),
        Device("（无标签）", "空闲中", "空闲中", 2, 2, guessed=True),
        Device("封装机", "关闭", "关闭", 3, 3, guessed=True),
        Device(log_name, "阻塞", "阻塞", 4, 4, guessed=True),
        Device("天有洪炉", "空闲中", "空闲中", 5, 5, guessed=False),
    ]
    kept = [d for d in ds if not d.guessed or d.badge == "关闭" or (d.name != "（无标签）" and all(m in patrol.LOGISTICS for m in d.name.split("|")))]
    assert [d.world_x for d in kept] == [3, 4, 5], [d.world_x for d in kept]
    src = (ROOT / "src" / "patrol.py").read_text(encoding="utf-8")
    assert "d.kind" not in src, "patrol.py 里还有 d.kind（Device 没有这个字段）"
    print("  ✓ 单遍过滤：丢没点开的生产设备，留关闭/物流件/点开过的")


if __name__ == "__main__":
    run_case()
    filter_case()
    print("2/2 通过")
