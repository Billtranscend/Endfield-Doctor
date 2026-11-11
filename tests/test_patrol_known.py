"""第二遍不重复点开第一遍已经点开过的设备（2026-11-11 群友：「详细巡检会频繁查看堵塞或未工作的设备，即使已经看过很多遍」）。
11-10 景玉谷 12 次点开里 2 次是这种重复：第一遍点开了没读出标签的种植机、上下两台分不清的封装机，第二遍清空列表后又点了一次。"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import badges  # noqa: E402
import patrol  # noqa: E402
from patrol import Device, Patrol  # noqa: E402


def _patrol(badge_list):
    p = Patrol.__new__(Patrol)  # 不连游戏
    p.stats = {"shot_n": 0, "shot_t": 0.0, "ocr_n": 0, "ocr_t": 0.0}
    p.shot_dir, p.cached_labels, p.trust_nominal, p.cached_feats = None, None, False, None
    p.cam, p.devices, p.open_all, p.only_names, p.known = (0, 0), [], True, None, []
    p.last_env = p.last_foreign = None
    p.last_panel, p.last_warn, p.last_flow = "", "", ""
    p.opened = []
    p.stable_badges = lambda *a, **k: ([(b, 5) for b in badge_list], np.zeros((720, 1280, 3), np.uint8))
    p.device_labels = lambda: []
    p.guess_name = lambda b, labels: "种植机"
    p.idle_on_top = p.idle_above = p.idle_logistics_below = lambda *a: None
    p.ensure_overhead = lambda: None
    p.desc = lambda r: ""

    def open_device(b, labels, check_badge=True):
        p.opened.append((b.x, b.y))
        return ("种植机", "阻塞", None)

    p.open_device = open_device
    return p


def test_second_pass_skips_known():
    b1 = badges.Badge("blocked", 1101, 643, 0.9)   # 第一遍点开过的种植机
    b2 = badges.Badge("blocked", 400, 300, 0.9)    # 第一遍没点开过的
    p = _patrol([b1, b2])
    p.known = [Device("种植机", "阻塞", "阻塞", 1100, 641, recipe="锦草", panel="001_种植机.jpg")]
    found = p.scan_view_fast()
    assert p.opened == [(400, 300)], p.opened                      # 只点了没见过的那台
    reused = next(d for d in found if d.world_x == 1101)
    assert reused.name == "种植机" and reused.panel == "001_种植机.jpg" and reused.recipe == "锦草"
    assert len(found) == 2


def test_badge_changed_reopens():
    """同位置但标记变了（第一遍阻塞、这遍空闲）：状态变了，要重新点开看。"""
    p = _patrol([badges.Badge("idle", 1101, 643, 0.9)])
    p.known = [Device("种植机", "阻塞", "阻塞", 1100, 641)]
    p.scan_view_fast()
    assert p.opened == [(1101, 643)]


if __name__ == "__main__":
    test_second_pass_skips_known()
    test_badge_changed_reopens()
    print("2/2 通过")
