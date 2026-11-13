"""读蓝图：拼接比例、尺寸自检、带红标的暗管出口（2026-11-13 issue #1，一位蓝图作者用自己的 8 张蓝图做蓝图分析）。

四个互不相干的毛病：
  A. 「武陵城右下」41×44：拼图 1149×1528（应为 1425×1529），中间错位一道；暗管 9 个里 7 个点不出连接线，拼到基地锚点 7/13。
     根因是滑块 → 画面的比例取了帧间实测值的中位数，蓝图里有重复结构时实测值是假匹配 → vision.scroll_scale。
  B. 「应龙关下」设备一览有 2 个暗管出口，拼图上一个没认到：图标下面带红标、没有放大镜角标的那种没有模板。
     作者确认这是「外接的」暗管（对面的口不在这张蓝图里）→ 模板「单_出口_未连接」，认出来带 open 标记，读蓝图时不去点。
  C. 「武陵左上」26×48、「应龙关小组件」13×31：比画布窄，拼图宽度就是画布宽 926，尺寸自检按 904 / 452 核对打了 ✗ → vision.size_ok。
  D. 「应龙关上」49×32：有一帧滚动条那一行在滑块外面多了 2 个深色像素，滑块 (140, 619) 被读成 (27, 619)，
     那一帧贴偏 213 px，拼图宽 1723（应为 1703）→ vision.thumbs 取最长的一段深色（longest_run）。


第二轮（他升到 0.10.7 把 8 张重读了一遍：尺寸 8 张全过，「武陵城左下」37×47 还有两处）：
  E. 外接的暗管入口、外接的多口暗管入口没有模板，被「单_出口_未连接」以 0.90 / 0.85 分认成外接出口：总数 8 个对，
     类型分错，暗管数量 ✗ → 模板「单_入口_未连接」「多_入口_未连接」。
  F. 对角的一对暗管（出口 (63,64)、入口 (1089,1562)，全长 1816）只配上单向：从出口点出去，线的最后一段落在空白画布上，
     渐隐后读到 (973,1397) 就停了，差 202，超出出口这边的余量 70 → vision.pick_partner 原来的窗口里没有暗管时按距离放宽。

样本 tests/data/bpa_pipes/：open_exit_left / open_exit_right = 他「应龙关下」拼图上那两个红标出口；
paired_exit / paired_entry = 他「武陵城右下」上有配对的出口、入口（带放大镜）；
open_entry / open_multi_entry = 他「武陵城左下」上外接的入口、外接的多口入口。原始预览帧 25 MB，不进仓库；
A 的数字（滑块长度、四步的实测比例）取自那 8 帧的离线重放，F 的端点取自他的连接线截图的离线重放。
"""
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import vision as V  # noqa: E402

DATA = ROOT / "tests" / "data" / "bpa_pipes"


def blank(w: int, h: int) -> np.ndarray:
    return np.zeros((h, w, 3), np.uint8)


# ---------------------------------------------------------------- A. 拼接比例

def test_alias_samples_do_not_pull_the_scale():
    """「武陵城右下」：四步横移，滑块都动 315 px，票最多的位移两步是 ±496（真）、两步是 ±50（假匹配）。
    滑块长 572.5 → 按长度算 -900/572.5 = -1.572。假的两个样本被筛掉，比例取真的那两个。"""
    samples = [-496 / 315, 496 / -315, 50 / 315, -50 / -315]
    s = V.scroll_scale(samples, 572.5, V.VIEW_EFF[0])
    assert abs(s - (-1.575)) < 0.01, s
    assert abs(s * 315 - (-496)) < 2  # 先验位移回到真值附近，ORB 在 ±15 px 里能找到票


def test_scale_falls_back_to_thumb_length():
    """实测样本不够（竖向三步票数都不到 300）或全是假的：直接按滑块长度算。竖向 -581/227 = -2.559，真值约 -2.53。"""
    assert abs(V.scroll_scale([], 227.0, V.VIEW_EFF[1]) - (-2.559)) < 0.01
    assert abs(V.scroll_scale([0.16, 0.16], 572.5, V.VIEW_EFF[0]) - (-1.572)) < 0.01


def test_consistent_samples_still_win():
    """实测样本和按长度算的差不多时仍用实测的中位数（更准）；没有滑块时照旧只看实测值。"""
    assert V.scroll_scale([-1.60, -1.62, -1.61], 572.5, V.VIEW_EFF[0]) == -1.61
    assert V.scroll_scale([-1.2, -1.3, -1.25], None, V.VIEW_EFF[0]) == -1.25
    assert V.scroll_scale([], None, V.VIEW_EFF[0]) == -1.0


# ---------------------------------------------------------------- D. 读滑块

def frame_with_thumbs(h_runs, v_runs) -> np.ndarray:
    """造一帧 720p 画面：滚动条那一行（y=679）、那一列（x=944）按给的区间涂深色，其余浅色。"""
    im = np.full((720, 1280, 3), 230, np.uint8)
    for a, b in h_runs:
        im[679, a:b + 1] = 40
    for a, b in v_runs:
        im[a:b + 1, 944] = 40
    return im


def test_thumb_ignores_stray_dark_pixels():
    """「应龙关上」第 04 帧：x=27–28 有 2 个深色像素，滑块在 (140, 619)。要读成滑块本身，不是 (27, 619)。"""
    im = frame_with_thumbs([(27, 28), (140, 619)], [(244, 557)])
    assert V.thumbs(im) == ((140, 619), (244, 557))
    im = frame_with_thumbs([(42, 521)], [(70, 72), (352, 665)])  # 竖向同理
    assert V.thumbs(im) == ((42, 521), (352, 665))


def test_thumb_absent_when_only_specks():
    """反例：这个方向不用滚（没有滑块），只有零星深色像素：读成没有滑块，不能把零星像素当滑块。"""
    im = frame_with_thumbs([(27, 28), (300, 305)], [])
    assert V.thumbs(im) == (None, None)


# ---------------------------------------------------------------- C. 尺寸自检

def test_narrow_blueprint_checked_against_canvas():
    """比画布窄：拼图宽度最小就是画布宽 926，高度照常核对。"""
    ok, msg = V.size_ok(blank(926, 1665), (26, 48))  # 武陵左上：应为 904×1668
    assert ok and "按画布核对" in msg, msg
    ok, msg = V.size_ok(blank(926, 1085), (13, 31))  # 应龙关小组件：应为 452×1077
    assert ok, msg


def test_narrow_blueprint_with_wrong_height_still_fails():
    """反例：比画布窄，但高度差了 300 px：还是不过。"""
    assert not V.size_ok(blank(926, 1365), (26, 48))[0]


def test_normal_blueprint_size_check_unchanged():
    assert V.size_ok(blank(1421, 1528), (41, 44))[0]
    ok, msg = V.size_ok(blank(1149, 1528), (41, 44))  # 修之前「武陵城右下」的拼图
    assert not ok and "1425×1529" in msg, msg


def test_tiny_blueprint_is_not_checkable():
    """两个方向都比画布小：游戏里放大显示，比例不是 34.75。不过，并说明原因（不能因为「按画布核对」就放过去）。"""
    ok, msg = V.size_ok(blank(926, 608), (14, 11))
    assert not ok and "放大显示" in msg, msg


# ---------------------------------------------------------------- B. 带红标的暗管出口

def test_open_exit_is_recognised_and_flagged():
    for name in ("open_exit_left.png", "open_exit_right.png"):
        found = V.detect(V.rd(DATA / name))
        assert len(found) == 1 and found[0]["name"] == "暗管出口" and found[0].get("open") is True, (name, found)
        assert found[0]["score"] > 0.9


def test_open_entries_are_recognised_as_entries():
    """「武陵城左下」：外接的入口、外接的多口入口。0.10.7 都认成了「暗管出口」（外接）。"""
    for name, want in (("open_entry.png", "暗管入口"), ("open_multi_entry.png", "多口暗管入口")):
        found = V.detect(V.rd(DATA / name))
        assert len(found) == 1 and found[0]["name"] == want and found[0].get("open") is True, (name, found)
        assert found[0]["score"] > 0.9


def test_open_exits_stay_exits():
    """反例：加了外接入口的模板以后，外接出口还是出口（两种样子只差图标里三角的朝向）。"""
    for name in ("open_exit_left.png", "open_exit_right.png"):
        found = V.detect(V.rd(DATA / name))
        assert [f["name"] for f in found] == ["暗管出口"], (name, found)


def test_paired_pipes_are_not_flagged():
    """反例：有配对的出口、入口（带放大镜）不带 open 标记，名字也不变。"""
    for name, want in (("paired_exit.png", "暗管出口"), ("paired_entry.png", "暗管入口")):
        found = V.detect(V.rd(DATA / name))
        assert len(found) == 1 and found[0]["name"] == want and "open" not in found[0], (name, found)


def test_open_pipes_count_but_are_not_clicked():
    """数量自检把没有配对的也算上（应龙关下：设备一览 2 个出口）；读蓝图挑要点的暗管时把它们去掉。"""
    found = [{"name": "暗管出口", "p": [63, 200], "score": 0.96, "open": True},
             {"name": "暗管出口", "p": [1496, 63], "score": 1.0, "open": True},
             {"name": "息壤供电桩", "p": [218, 80], "score": 0.9}]
    pipes = [f for f in found if f["name"] in V.PIPE_NAMES]
    ok, got, exp = V.pipe_count_check(pipes, {"暗管出口": 2})
    assert ok and got["暗管出口"] == 2
    assert [a for a in pipes if not a.get("open")] == []  # project.read_blueprints 里的筛法


# ---------------------------------------------------------------- F. 连接线远端渐隐

LEFT_BOTTOM = [{"name": "暗管出口", "p": [336, 643]}, {"name": "暗管入口", "p": [1089, 1562]}, {"name": "暗管入口", "p": [1225, 95]},
               {"name": "暗管出口", "p": [1189, 1565]}, {"name": "暗管入口", "p": [65, 1255]}, {"name": "暗管出口", "p": [63, 64]}]


def pick(pipes, i, end):
    others = [(j, c) for j, c in enumerate(pipes) if j != i]
    r = V.pick_partner(pipes[i]["p"], end, pipes[i]["name"], [c for _, c in others])
    return None if r is None else others[r[1]][0]


def test_faded_line_from_exit_still_finds_partner():
    """「武陵城左下」对角那一对：从出口 (63,64) 点出去，线读到 (973,1397) 就淡得读不到了，离入口 (1089,1562) 还差 202。"""
    assert pick(LEFT_BOTTOM, 5, (973, 1397)) == 1
    assert pick(LEFT_BOTTOM, 1, (180, 235)) == 5  # 反方向（从入口点出去）原来就配得上，照旧


def test_exit_prefers_partner_within_normal_reach():
    """反例：同一条直线上有两个入口，线读到头的那个（差 55）就是伙伴，不能因为放宽了就取更远的那个。"""
    pipes = [{"name": "暗管出口", "p": [0, 500]}, {"name": "暗管入口", "p": [1500, 500]}, {"name": "暗管入口", "p": [1700, 500]}]
    assert pick(pipes, 0, (1445, 500)) == 1


def test_line_far_too_short_matches_nothing():
    """反例：线只读到一半（中间被挡、没跟上），不能硬配：放宽的余量只有距离的 16% + 20。"""
    assert pick(LEFT_BOTTOM, 5, (600, 848)) is None
    pipes = [{"name": "暗管出口", "p": [0, 500]}, {"name": "暗管出口", "p": [1500, 500]}]
    assert pick(pipes, 0, (1300, 500)) is None  # 类型相同的不配


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
