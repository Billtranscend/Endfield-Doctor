"""蓝图分析模式的离线单元测试（不需要游戏、不需要截图数据）。
运行：.venv\\Scripts\\python.exe -m pytest tests/test_bpa.py -q
"""

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "tools"))

from bpa import vision as V  # noqa: E402
from paths import imwrite  # noqa: E402


def test_link_status():
    # 0↔1 双向；2→3 单向；4 未配；5→1（1 已和 0 双向）= 矛盾
    part = {0: 1, 1: 0, 2: 3, 5: 1}
    st = V.link_status(6, part)
    assert st[0] == st[1] == "双向"
    assert st[2] == "单向" and st[3] == "单向"
    assert st[4] == "未配"
    assert st[5] == "矛盾" or st[5] == "单向"


def test_pick_partner_skips_middle_pipe():
    # 三个暗管共线：线从 0 出发穿过中间的 1（同为入口，类型不配）到 2（出口）。端点在 2 前 50 px（被高亮框挡住）。
    cands = [{"name": "暗管入口", "p": [400, 400]}, {"name": "暗管出口", "p": [800, 400]}]
    r = V.pick_partner([100, 400], [750, 400], "暗管入口", cands)
    assert r is not None and r[1] == 1
    # 中间是出口时，取更远的那个（线没在中间停下）
    cands2 = [{"name": "暗管出口", "p": [400, 400]}, {"name": "暗管出口", "p": [800, 400]}]
    r2 = V.pick_partner([100, 400], [760, 400], "暗管入口", cands2)
    assert r2[1] == 1


def test_fit_blueprint_recovers_rotation_and_shift():
    rng = np.random.default_rng(0)
    pts = [{"name": n, "p": [float(x), float(y)]} for n, x, y in
           zip(["息壤供电桩"] * 6 + ["暗管入口", "暗管出口"], rng.uniform(50, 1500, 8), rng.uniform(50, 1500, 8))]
    rot, t = 90, np.array([1500.0, 330.0])
    ledger = [{"name": a["name"], "x": float(f[0]), "y": float(f[1])}
              for a in pts for f in [t + V.K * np.array(V.Rp(rot, *a["p"])) + rng.normal(0, 3, 2)]]
    ledger += [{"name": "息壤供电桩", "x": float(x), "y": float(y)} for x, y in rng.uniform(300, 1900, (10, 2))]  # 别的蓝图的干扰
    r = V.fit_blueprint(pts, ledger)
    assert r["rot"] == rot
    assert np.allclose(r["t"], t, atol=6)
    assert r["inliers"] == len(pts) and r["second"] < r["inliers"] - 2


def test_size_ok():
    mos = np.zeros((int(50 * V.CELL), int(33 * V.CELL), 3), np.uint8)
    assert V.size_ok(mos, (33, 50))[0]
    assert not V.size_ok(mos, (33, 45))[0]


def test_scroll_delta_finds_shift():
    from bpa import game_ops as G
    rng = np.random.default_rng(1)
    big = (rng.uniform(0, 255, (1200, 1280, 3))).astype(np.uint8)
    big = np.asarray(__import__("cv2").GaussianBlur(big, (7, 7), 0))
    prev, nxt = big[0:720], big[64:784]  # 内容上移 64 px
    assert G._scroll_delta(prev, nxt) == 64
    assert G._scroll_delta(prev, prev) == 0


def test_clean_digits_drops_short_strokes():
    from bpa import game_ops as G
    img = np.zeros((720, 1280, 3), np.uint8)
    t = (1005, 253)
    x, y, w, h = G._digits_roi(t)
    img[y + 2:y + 8, x + 10:x + 12] = 255   # 6 px 高的亮边：应去掉
    img[y + 4:y + 15, x + 30:x + 32] = 255  # 11 px 高的「1」：应保留
    m = G._clean_digits(img, t)[y:y + h, x:x + w, 0] > 0
    assert m[:, 10:12].sum() == 0 and m[:, 30:32].sum() == 22


def test_pick_partner_prefers_on_line_over_farther_offline():
    # 应龙关2号 实测：出口 3 的线画到入口 1 再过一点；更远的入口 10 偏离 14 px 也落在窗口里，不该取它
    s, p1, p10 = np.array([951, 1326.0]), np.array([1499, 1221.0]), np.array([1601, 1187.0])
    d = (p1 - s) / np.linalg.norm(p1 - s)
    end = s + d * (np.linalg.norm(p1 - s) + 60)
    cands = [{"name": "暗管入口", "p": list(p1)}, {"name": "暗管入口", "p": list(p10)}]
    r = V.pick_partner(list(s), list(end), "暗管出口", cands)
    assert r is not None and r[1] == 0


def test_pick_partner_inlet_line_fades_before_outlet():
    # 武陵城2号 实测：入口的线在出口端渐隐，端点比出口早停 137 px，仍应配上
    r = V.pick_partner([1567, 1632], [463, 1207], "暗管入口", [{"name": "暗管出口", "p": [336, 1156]}])
    assert r is not None and r[1] == 0
    # 反方向（出口点出去）不放宽：伙伴在端点外 137 px 不配
    assert V.pick_partner([336, 1156], [1440, 1581], "暗管出口", [{"name": "暗管入口", "p": [1567, 1632]}]) is None


def test_ray_end_skips_highlight_box_gap():
    # 高亮框边（离中心 ~50 px）后隔 150 px 才是线：端点应在线的末端
    im = np.full((720, 1280, 3), 230, np.uint8)
    cx, cy = 300, 600
    import cv2
    cv2.rectangle(im, (cx - 50, cy - 50), (cx + 50, cy + 50), (250, 230, 180), 2)
    cv2.line(im, (cx, cy - 210), (cx, cy - 500), (250, 230, 180), 2)
    end, d = V.ray_end(im, cx, cy)
    assert end is not None and abs(end[1] - (cy - 500)) < 6


def test_logistics_detect_on_synthetic_canvas():
    # 把模板（各转一个角度）贴到浅灰底上：应按类型、位置认出来，空白处不出误报
    import cv2
    from bpa import logistics as LG
    bg = np.full((400, 600, 3), 228, np.uint8)
    cv2.line(bg, (0, 200), (600, 200), (215, 215, 215), 1)
    want = []
    for i, f in enumerate(sorted(LG.LG_DIR.glob("*_1.png"))):
        t = np.rot90(V.rd(f), i % 4)
        x, y = 60 + (i % 4) * 140, 70 + (i // 4) * 140
        bg[y - 17:y + 18, x - 17:x + 18] = t
        want.append((f.stem.rsplit("_", 1)[0], x, y))
    got = LG.detect(bg)
    for k, x, y in want:
        hit = [o for o in got if abs(o["p"][0] - x) <= 2 and abs(o["p"][1] - y) <= 2]
        assert hit and hit[0]["type"] == k, (k, x, y, hit)
    assert len(got) == len(want)


def test_decide_kinds_by_icon_state():
    from bpa import logistics as LG
    gs = [{"icon_group": 0, "kind_local": "物品", "belt_touch": True},   # 气体图标压在传送带上 → 管道，且底下可能叠物品准入口
          {"icon_group": 0, "kind_local": "管道", "belt_touch": False},
          {"icon_group": 1, "kind_local": "管道", "belt_touch": False},  # 固体图标 → 物品，不管局部线索
          {"icon_group": 2, "kind_local": "管道"}, {"icon_group": 2, "kind_local": "物品"}, {"icon_group": 2, "kind_local": "管道"}]
    LG.decide_kinds(gs, {0: {"state": "气"}, 1: {"state": "固"}, 2: {"state": None}})
    assert gs[0]["kind"] == "管道" and gs[0]["maybe_item_below"] and not gs[1]["maybe_item_below"]
    assert gs[2]["kind"] == "物品" and gs[2]["kind_how"] == "图标是固体"
    assert all(g["kind"] == "管道" and g["kind_how"] == "推断" and g["kind_conflict"] for g in gs[3:])


def test_item_library_roundtrip(tmp_path=None):
    import tempfile
    from pathlib import Path
    from bpa import logistics as LG
    d = Path(tmp_path or tempfile.mkdtemp())
    rng = np.random.default_rng(3)
    a = np.asarray(__import__("cv2").GaussianBlur(rng.integers(0, 255, (23, 23, 3)).astype(np.uint8), (5, 5), 0))
    b = 255 - a
    LG.save_item(a, "液", "", d)
    assert LG.lookup_item(a, d)[0] == "液"
    assert LG.lookup_item(b, d)[0] in (None, "液", "气", "固")  # 全局库里可能有，但项目库不该把 b 认成 a
    LG.forget_item(a, d)
    assert not list(d.glob("*.png"))


def test_presence_check():
    from bpa import logistics as LG
    items = [{"type": "管道分流器"}, {"type": "准入口"}]
    assert LG.presence_check(items, {"管道分流器": None, "管道准入口": None}) == []
    assert len(LG.presence_check(items, {"管道准入口": None, "物流桥": None})) == 2


def test_inputs_from_gates_rules():
    from bpa import inputs as I
    canvas = np.zeros((700, 700, 3), np.uint8)
    lg = [{"type": "准入口", "p": [25, 100], "icon_group": 0},          # 贴左边、两旁空 → 输入，液体预填 60
          {"type": "准入口", "p": [25, 300], "icon_group": 1},          # 贴左边，两旁（上下）接分流器 → 不算
          {"type": "管道分流器", "p": [25, 265]}, {"type": "管道分流器", "p": [25, 335]},
          {"type": "准入口", "p": [60, 500], "icon_group": 1},          # 离边 1.7 格 → 不算
          {"type": "准入口", "p": [675, 400], "icon_group": 1},         # 贴右边，朝里接暗管出口 → 往外排，不算
          {"type": "准入口", "p": [400, 675], "icon_group": 1},         # 贴下边，朝里接多口暗管入口 → 输入，接口 = 多口暗管
          {"type": "准入口", "p": [434, 675], "icon_group": 1}]         # 旁边是另一个准入口（一排口）→ 不影响
    pipes = [{"name": "暗管出口", "p": [610, 400]}, {"name": "多口暗管入口", "p": [400, 610]}]
    groups = {0: {"item": "清水", "state": "液"}, 1: {"item": "息壤气", "state": "气"}}
    marks, drop = I.from_gates(canvas, lg, groups, pipes)
    got = {tuple(m["p"]): m for m in marks}
    assert set(got) == {(25, 100), (400, 675), (434, 675)}
    assert got[(25, 100)]["rate"] == 60 and got[(25, 100)]["via"] == "水泵" and got[(25, 100)]["item"] == "清水"
    assert got[(400, 675)]["via"] == "多口暗管" and got[(400, 675)]["rate"] is None
    whys = {tuple(d["p"]): d["why"] for d in drop}
    assert "分流器" in whys[(25, 300)] and "没贴着" in whys[(60, 500)] and "出口" in whys[(675, 400)]


def test_save_ledger_boundary_and_duplicates():
    import json
    import tempfile
    from pathlib import Path
    from bpa import ledger_scan as LS
    d = Path(tempfile.mkdtemp()) / "ledger_scan_X_20260101-000000"
    d.mkdir(parents=True)
    (d / "scan.json").write_text(json.dumps({"scale": 1.7, "C": [640, 330], "screens": []}), encoding="utf-8")
    near = [{"name": "精炼炉", "x": 500, "y": 500, "seen": 5},
            {"name": "暗管出口", "x": 900, "y": 1905, "seen": 4}, {"name": "暗管出口", "x": 898, "y": 1906, "seen": 2},  # 压线、孤立（两条是同一台的重复读数）
            {"name": "水泵", "x": 2200, "y": 500, "seen": 3}]  # 线外
    p = LS.save_ledger("", "X", near, d, [350, 1910, 345, 1910], dest=d.parent / "X.json", log=lambda *_: None)
    body = json.loads(p.read_text(encoding="utf-8"))
    assert [e["name"] for e in body["devices"]] == ["精炼炉"]
    assert len(body["on_line"]) == 2 and body["outside"][0]["name"] == "水泵"
    p2 = LS.save_ledger("", "X", near, d, None, dest=d.parent / "X2.json", log=lambda *_: None)
    assert len(json.loads(p2.read_text(encoding="utf-8"))["devices"]) == 4  # 不给规划线：全收


def _world(h=2000, w=3000, seed=3):
    """有纹理、不周期的合成「地图」：给块匹配用。"""
    import cv2
    rng = np.random.default_rng(seed)
    g = cv2.GaussianBlur(rng.integers(0, 255, (h, w)).astype(np.uint8), (0, 0), 3)
    img = cv2.merge([g, cv2.equalizeHist(g), 255 - g])
    for _ in range(400):
        x, y = int(rng.integers(0, w - 60)), int(rng.integers(0, h - 60))
        cv2.rectangle(img, (x, y), (x + int(rng.integers(10, 60)), y + int(rng.integers(10, 60))), tuple(int(v) for v in rng.integers(0, 255, 3)), -1)
    return img


def test_block_shift_measures_camera_step():
    from bpa import ledger_scan as LS
    wd = _world()
    a = wd[100:820, 100:1380]
    b = wd[100 + 37:820 + 37, 100 + 390:1380 + 390]  # 镜头右移 390、下移 37
    m = LS.block_shift(a, b)
    assert m and abs(m[0] - 390) <= 1 and abs(m[1] - 37) <= 1
    assert LS.block_shift(a, np.zeros_like(a)) is None  # 没纹理：测不出，不瞎给


def test_refine_positions_fixes_wrong_steps():
    """扫描时一步记错（43 记成… 实为 248）、一步测不出按中位补错 10 px：离线复核按相邻 + 上下行重叠解回来。"""
    import json, tempfile, cv2
    from bpa import ledger_scan as LS
    wd = _world(2400, 2600)
    true = [(0, 0), (390, 0), (780, 0), (780, 248), (390, 248), (0, 248), (0, 496), (390, 496), (780, 496)]
    rec_cam = [(0, 0), (390, 0), (780, 0), (780, 258), (390, 258), (0, 258), (0, 301), (390, 301), (780, 301)]
    d = Path(tempfile.mkdtemp()); (d / "near").mkdir()
    screens = []
    for i, ((tx, ty), c) in enumerate(zip(true, rec_cam)):
        imwrite(str(d / "near" / f"{i:03d}.png"), wd[200 + ty:920 + ty, 200 + tx:1480 + tx])
        screens.append({"file": f"{i:03d}.png", "cam_near": list(map(float, c)), "row": [0, 0, 0, 1, 1, 1, 2, 2, 2][i]})
    (d / "scan.json").write_text(json.dumps({"screens": screens}), encoding="utf-8")
    rec = LS.refine_positions(d, log=lambda *_: None)
    got = np.array([x["cam_near"] for x in rec["screens"]])
    assert np.abs(got - np.array(true, float)).max() <= 1.5, got.round()
    assert rec["refined"] and rec["screens"][3]["cam_near_raw"] == [780.0, 258.0]


def test_rotated_shot_maps_back_to_base_screen():
    """Ctrl+R 补拍（内容绕屏幕中心顺时针转 90°×k）：rot_offset 量出的平移 + _derot_pt 能把补拍图上的点换回原屏同一点。"""
    import cv2
    from bpa import ledger_scan as LS
    wd = _world(1600, 1600, seed=5)
    sq = wd[100:1380, 100:1380]  # 1280×1280，中心 = 屏幕中心
    base = sq[280:1000, :]
    codes = {1: cv2.ROTATE_90_CLOCKWISE, 2: cv2.ROTATE_180, 3: cv2.ROTATE_90_COUNTERCLOCKWISE}
    for k, code in codes.items():
        rot = cv2.rotate(sq, code)[280:1000, :]
        T, nb = LS.rot_offset(base, rot, k)
        assert nb >= 2 and np.abs(T - LS.rot_theory_T(k)).max() <= 1.5, (k, T, nb)
        # 原屏 (700, 300) 这一点在补拍图上的位置：相对中心转 k 次顺时针 90°
        px, py = 700 - 640, 300 - 360
        for _ in range(k):
            px, py = -py, px
        q = (640 + px, 360 + py)
        back = np.array(LS._derot_pt(q, k), float) + T
        assert np.abs(back - [700, 300]).max() <= 1.5, (k, back)


def _placement_world(n=6, seed=1):
    """合成一张蓝图预览：n 台同类设备（深色外框 + 中心深色图标圆），按 rot 0 摆在基地 t=(100,50) 处。"""
    import cv2
    rng = np.random.default_rng(seed)
    m = np.full((720, 760, 3), 232, np.uint8)
    for x in range(0, 760, 35):
        cv2.line(m, (x, 0), (x, 719), (222, 222, 222), 1)
    centers = []
    for k in range(n):
        cx, cy = 120 + (k % 3) * 220, 160 + (k // 3) * 300
        cv2.rectangle(m, (cx - 50, cy - 50), (cx + 50, cy + 50), (60, 60, 60), 3)
        for j in range(4):
            cv2.rectangle(m, (cx - 46, cy - 40 + 22 * j), (cx - 36, cy - 26 + 22 * j), (120, 120, 120), 2)
        cv2.circle(m, (cx, cy), 16, (40, 40, 40), -1)  # 产物图标：深色圆盘 + 彩色描边（预览里的样子）
        cv2.circle(m, (cx, cy), 17, (220, 170, 40), 2)
        cv2.circle(m, (cx, cy), 6, (0, 120, 220), -1)
        centers.append((cx, cy))
    t = (100.0, 50.0)
    tf = {"K": V.K, "F0": list(t), "bps": {"bp": {"rot": 0, "t": list(t), "offset": [0, 0]}}}
    off = (10.0, -1.0)  # 底账（标签图标中心）相对设备中心的偏移
    devs = [{"name": "粉碎机", "x": t[0] + V.K * cx + off[0] + rng.normal(0, 0.8), "y": t[1] + V.K * cy + off[1] + rng.normal(0, 0.8)}
            for cx, cy in centers]
    bps = {"bp": {"mosaic": m, "devices": {"粉碎机": n, "传送带": None}, "anchors": [], "logistics": []}}
    return bps, tf, devs, m


def test_placement_all_present_missing_and_extra():
    from bpa import placement as PL
    bps, tf, devs, canvas = _placement_world()
    r = PL.check_base(bps, tf, {"devices": devs}, canvas)
    assert r["summary"] == "全部到位", r["summary"]
    assert r["bps"]["bp"]["marks_matched"] == 6
    # 少摆一台：报缺 1 台粉碎机，最像的候选就在它该在的位置
    gone = devs[4]
    r = PL.check_base(bps, tf, {"devices": devs[:4] + devs[5:]}, canvas)
    miss = r["bps"]["bp"]["missing"]
    assert [(m["name"], m["count"]) for m in miss] == [("粉碎机", 1)], miss
    c = miss[0]["candidates"][0]["far"]
    assert abs(c[0] - gone["x"]) <= 6 and abs(c[1] - gone["y"]) <= 6, (c, gone)
    # 蓝图范围里多一台蓝图外的设备：报出来，但不算「多出（蓝图里有的类型）」
    extra = dict(devs[0], name="反应池", x=devs[0]["x"] + 3)
    r = PL.check_base(bps, tf, {"devices": devs + [extra]}, canvas)
    ex = r["bps"]["bp"]["extra"]
    assert [(e["name"], e["in_blueprint"]) for e in ex] == [("反应池", False)], ex
    assert "蓝图外" in r["summary"]


def test_placement_drops_generic_port_duplicates():
    from bpa import placement as PL
    bps, tf, devs, canvas = _placement_world()
    bps["bp"]["devices"]["仓库取货口"] = 1
    port = {"name": "仓库取货口", "x": devs[0]["x"] - 22, "y": devs[0]["y"] + 10}  # 落在第一台设备的外框里（有墨迹）
    dup = {"name": "仓库口", "x": port["x"] + 12, "y": port["y"] + 2}
    r = PL.check_base(bps, tf, {"devices": devs + [port, dup]}, canvas)
    assert r["ledger_dups_dropped"] == 1
    assert not r["bps"]["bp"]["diff"].get("仓库取货口") and not r["bps"]["bp"]["notes"], r["bps"]["bp"]
    # 只剩分不清的「仓库口」（具体口被删）：它补上缺口，不报缺
    r = PL.check_base(bps, tf, {"devices": devs + [dup]}, canvas)
    assert not r["bps"]["bp"]["diff"], r["bps"]["bp"]["diff"]
