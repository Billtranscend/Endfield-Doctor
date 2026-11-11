"""近景定向巡检（src/near_patrol.py）的纯函数：视点规划、按底账定位、标签身份核对、判读。不碰游戏。"""
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "tools"))

import near_patrol as NP  # noqa: E402


def _ents(pts, name="固气转化机"):
    return [{"name": name, "q": np.array(p, float), "far": tuple(p)} for p in pts]


def test_plan_views_covers_all_targets_inside_safe():
    rng = np.random.default_rng(2)
    targets = _ents(rng.uniform(0, 2500, size=(23, 2)))
    views = NP.plan_views(targets)
    seen = [id(e) for v in views for e in v["targets"]]
    assert sorted(seen) == sorted(id(e) for e in targets) and len(seen) == len(set(seen))
    for v in views:
        for e in v["targets"]:
            u = e["q"] - v["cam"]
            assert NP.SAFE[0] <= u[0] <= NP.SAFE[1] and NP.SAFE[2] <= u[1] <= NP.SAFE[3], (u, v["cam"])


def test_localize_recovers_camera_offset_on_periodic_layout():
    # 一列间距 200 的同名设备（周期排列）：估计偏了 (37, −58)，要找回真位置，而不是错一个周期
    every = _ents([(800, 300 + 200 * k) for k in range(8)])
    true_cam = np.array([400.0, 200.0])
    labels = [(e["name"], e["q"] - true_cam, None) for e in every if 80 <= (e["q"] - true_cam)[1] <= 540]
    cam, nv, nl = NP.localize(labels, every, true_cam + [37, -58])
    assert np.abs(cam - true_cam).max() <= 1.0 and nv == nl >= 2, (cam, nv, nl)


def test_identify_reads_merged_and_misread_labels():
    name = "固气转化机"
    h = 24
    icon = np.array([500.0, 300.0])
    x0 = icon[0] + NP.ICON_OFF[name] * h  # 文字起点
    # 单独一个标签，OCR 把最后一个字读错
    assert NP.identify([([int(x0), 288, 120, h], "固气转化杌")], name, icon)[0]
    # 前面连着别的标签（「暗管入口」+ 图标 + 本标签）
    cw = 0.82 * h
    left = x0 - 4 * cw - 1.3 * h
    assert NP.identify([([int(left), 288, 260, h], "暗管入口固气转化机")], name, icon)[0]
    # 位置对不上（隔了 80 px）不算
    assert not NP.identify([([int(x0 + 80), 288, 120, h], "固气转化机")], name, icon)[0]


def test_judge_near_rules():
    pb = {"normal": [{"product": "赫铜块", "note": "优先级低"}],
          "device_designs": [{"area": "应龙关", "name": "液气转化机", "kinds": ["idle", "strike"], "note": "半速混气设计"},
                             {"area": "景玉谷", "name": "封装机", "x": 436, "y": 1258, "kinds": ["idle"], "note": "单线供料"}]}

    def row(name, counts, area="应龙关", far=(100, 100), product=None, id_ok=True, n=12):
        return {"area": area, "name": name, "far": list(far), "frames": n, "counts": counts, "product": product, "id_ok": id_ok}

    assert NP.judge_near(row("气体散布机", {"strike": 1}), pb)[0] == "问题"
    assert NP.judge_near(row("固气转化机", {"strike": 12}, product="赫铜块"), pb)[0] == "设计内"
    assert NP.judge_near(row("液气转化机", {"strike": 2}), pb)[0] == "设计内"
    assert NP.judge_near(row("固气转化机", {"strike": 12}, product="灼铜块"), pb)[0] == "问题"
    assert NP.judge_near(row("固气转化机", {"strike": 3}, product="灼铜块"), pb)[0] == "待确认"
    assert NP.judge_near(row("固气转化机", {}, id_ok=False), pb)[0] == "待确认"
    assert NP.judge_near(row("固气转化机", {"blocked": 12}), pb)[0] == "观察"
    assert NP.judge_near(row("固气转化机", {"blocked": 5}), pb)[0] == "正常"
    assert NP.judge_near(row("封装机", {"idle": 10}, area="景玉谷", far=(437, 1261)), pb)[0] == "设计内"
    assert NP.judge_near(row("封装机", {"idle": 10}, area="景玉谷", far=(733, 768)), pb)[0] == "观察"  # 坐标不对的同类不算
    assert NP.judge_near(row("固气转化机", {}), pb)[0] == "正常"


def _scene(gap=False, glow=False, seed=0):
    """合成一块 520×400 的地面：一根横管 + 一根竖管。gap = 横管断开 30 px；glow = 竖管变成发白（流光，管子还在）。"""
    import cv2
    rng = np.random.default_rng(seed)
    img = (rng.normal(60, 6, (400, 520, 3))).clip(0, 255).astype(np.uint8)
    img[..., 1] = np.clip(img[..., 1].astype(int) + 25, 0, 255)  # 偏绿的草地
    pipe = (230, 200, 120)  # BGR 浅蓝
    cv2.rectangle(img, (60, 250), (400, 266), pipe, -1)
    cv2.rectangle(img, (420, 60), (436, 380), (235, 235, 225) if glow else pipe, -1)
    for x in range(70, 400, 40):  # 管道边缘的暗线
        cv2.line(img, (x, 250), (x, 266), (150, 130, 80), 1)
    if gap:
        img[246:271, 100:132] = (rng.normal(60, 6, (25, 32, 3))).clip(0, 255).astype(np.uint8)
    return img


def _cur(img):
    return {"m": NP.struct_masks(img), "valid": np.ones(img.shape[:2], np.uint8), "badge": np.zeros(img.shape[:2], np.uint8), "img": img}


def test_struct_compare_finds_cut_pipe_not_glow():
    base = _cur(_scene())
    assert NP.compare_struct(_cur(_scene(seed=1)), base)["boxes"] == []          # 没变
    r = NP.compare_struct(_cur(_scene(gap=True, seed=2)), base)                   # 横管断了一段
    assert len(r["boxes"]) == 1 and 95 <= r["boxes"][0][0] <= 110, r
    assert NP.compare_struct(_cur(_scene(glow=True, seed=3)), base)["boxes"] == []  # 竖管只是发白


def test_dedupe_struct_keeps_gap_on_blocked_device():
    rows = [{"name": "水泵", "far": [1437, 443], "frames": 13, "counts": {"blocked": 13}, "id_ok": True, "area": "景玉谷",
             "struct": {"lost_px": 351, "boxes": [[75, 265, 26, 16]], "at": [[1846.8, 598.5]]}},
            {"name": "水泵", "far": [1437, 541], "frames": 13, "counts": {"blocked": 5}, "id_ok": True, "area": "景玉谷",
             "struct": {"lost_px": 351, "boxes": [[75, 96, 26, 16]], "at": [[1846.8, 598.7]]}}]
    NP.dedupe_struct(rows)
    assert "struct_dup_of" not in rows[0] and rows[1]["struct_dup_of"].startswith("水泵 (1437, 443)")
    assert NP.judge_near(rows[0], {})[0] == "问题" and NP.judge_near(rows[1], {})[0] == "正常"


def test_dedupe_struct_gap_goes_to_device_whose_pipe_it_is():
    # 两台都持续阻塞（2026-11-08 实跑），断口那根管子通到 (1437,443) 旁 30 px、离 (1437,541) 99 px
    rows = [{"name": "水泵", "far": [1437, 541], "q": [2018.8, 694.7], "frames": 13, "counts": {"blocked": 12}, "id_ok": True,
             "area": "景玉谷", "struct": {"lost_px": 284, "boxes": [[75, 96, 19, 16]], "at": [[1845.1, 600.4]], "pipe_to_dev": [98.8]}},
            {"name": "水泵", "far": [1437, 443], "q": [2018.8, 525.5], "frames": 13, "counts": {"blocked": 13}, "id_ok": True,
             "area": "景玉谷", "struct": {"lost_px": 284, "boxes": [[75, 265, 19, 16]], "at": [[1844.5, 598.9]], "pipe_to_dev": [30.1]}}]
    NP.dedupe_struct(rows)
    assert rows[0].get("struct_dup_of", "").startswith("水泵 (1437, 443)") and "struct_dup_of" not in rows[1]


def test_water_per_min_from_recipe():
    from bpa import pipenet as PN
    assert PN.water_per_min("碳块×1 + 清水×1 → 息壤×1（2秒）") == 30
    assert PN.water_per_min("清水×1 + 芽针种子×1 → 芽针×2（2秒）") == 30
    assert PN.water_per_min("致密源石粉末×15 + 息壤×5 → 低容武陵电池×1（10秒）") == 0


def test_jingyugu_water_networks_and_pump_gates():
    """实测数据（有就跑）：景玉谷 6 个清水输入端 4 张网全是零余量、12 台用水设备全被覆盖；5 台水泵各进自己的输入端。
    （2026-11-08 用户断开水泵 (1437,443) 的出水管对照确认：它进输入端 3，和输入端 2 一起供 3 台洪炉 + 1 台种植机）"""
    import json
    from bpa import pipenet as PN
    root = Path(__file__).resolve().parent.parent
    bd = root / "debug/bpa/实测-皇帝产线/base/景玉谷"
    led_p = root / "data/blueprints/1.6皇帝产线/ledger/景玉谷.json"
    if not (bd / "composite.png").exists() or not led_p.exists():
        return
    led = json.loads(led_p.read_text(encoding="utf-8"))
    recs = json.loads((led_p.parent / "景玉谷_recipes.json").read_text(encoding="utf-8"))["devices"]
    nets, users = {}, set()
    for no in range(1, 7):
        r = PN.network(bd, led, recs, no)
        nets[tuple(g["no"] for g in r["same_gates"])] = (r["supply"], r["demand"])
        users |= {(u["name"], *u["far"]) for u in r["users"]}
    assert nets == {(1,): (60, 60), (2, 3): (120, 120), (4,): (60, 60), (5, 6): (120, 120)}, nets
    assert len(users) == 12
    targets, _, _, F2N = NP.load_targets("1.6皇帝产线", "景玉谷", ["水泵"])
    if not NP.baseline_dir("1.6皇帝产线", "景玉谷").exists():
        return
    got = {tuple(t["far"]): NP.pump_gate("1.6皇帝产线", "景玉谷", t, F2N)[0] for t in targets}
    assert got == {(1313, 241): 1, (1357, 345): 2, (1437, 443): 3, (1437, 541): 4, (1436, 640): 5}, got


def test_area_check_trust():
    """实测数据（有就跑）：只用蓝图拼合图时，景玉谷、应龙关管网闭合可用；武陵城没闭合 → 不用（用上近景图见 test_area_check_near）。"""
    import json
    from bpa import pipenet as PN
    root = Path(__file__).resolve().parent.parent
    for area, want in (("景玉谷", True), ("应龙关", True), ("武陵城", False)):
        bd = root / f"debug/bpa/实测-皇帝产线/base/{area}"
        lp = root / f"data/blueprints/1.6皇帝产线/ledger/{area}.json"
        if not (bd / "composite.png").exists() or not lp.exists():
            return
        r = PN.area_check(bd, json.loads(lp.read_text(encoding="utf-8")),
                          json.loads((lp.parent / f"{area}_recipes.json").read_text(encoding="utf-8"))["devices"])
        assert r["trusted"] == want, (area, r["coverage"], r["problems"])
    assert PN.water_per_min("池产物：液化息壤(50)/重息壤(0)/液化重息壤(0)", "清水", "扩容反应池") == 30


def test_area_check_near():
    """实测数据（有就跑）：用上游戏近景图（断口核实 + 污水管按深紫色分开 + 产物图标推配方）后三个据点都闭合。
    2026-11-08 用户：赤铜块配方带污水，清水只算蓝色管；武陵城那串断口游戏里是连着的。"""
    import json
    from bpa import pipenet as PN
    import near_patrol as NP
    root = Path(__file__).resolve().parent.parent
    for area in ("景玉谷", "应龙关", "武陵城"):
        bd = NP.bpa_base_dir("1.6皇帝产线", area)
        lp = root / f"data/blueprints/1.6皇帝产线/ledger/{area}.json"
        if bd is None or not lp.exists():
            return
        led = json.loads(lp.read_text(encoding="utf-8"))
        if not (root / led["scan"]["dir"] / "scan.json").exists():
            return
        NP.register_near_checks("1.6皇帝产线", area)
        recs = json.loads((lp.parent / f"{area}_recipes.json").read_text(encoding="utf-8"))["devices"]
        r = PN.area_check(bd, led, recs)
        assert r["trusted"], (area, r["coverage"], r["problems"])
        if area == "武陵城":
            # 2026-11-10：扩容反应池 (1489,1055) 的池内链用清水，算成用水设备（25 台）。它左下口的进水管在近景图里投到了并排的
            # 污水管上，原来被定成污水、网闭合 24/25；改成「平移半格内能看到蓝管就只算疑似污水」后 25/25（游戏里核实是蓝色水管）
            assert r["coverage"] == [25, 25], r["coverage"]
            assert [n["supply"] for n in r["nets"]] == [840.0], [(n["supply"], n["demand"]) for n in r["nets"]]
            n13 = PN.network(bd, led, recs, 13)
            assert any(u["name"] == "扩容反应池" and u["far"] == [1489, 1055] for u in n13["users"]), [u["far"] for u in n13["users"]]
            # 顶上暗管 (821,65) 那条是精炼炉排污水去废水处理机的路，不是清水
            n = PN.network(bd, led, recs, None, start_tunnel=(821, 65))
            assert n and "污水" in n.get("note", ""), n
            # 没读到配方的精炼炉：按产物图标，(1870,699) 是蓝铁块（不用水），(1060,502) 是赤铜块
            t = PN.recipe_table(bd, led, recs)
            assert "蓝铁" in t[("精炼炉", 1870, 699)][0] and "赤铜" in t[("精炼炉", 1060, 502)][0]


def test_gate_watch():
    """实测数据（有就跑）：流体输入端当水泵看（用户 2026-11-08：清水口当水泵；应龙关气液类型多，也要看）。
    拼合图换算 + 固定偏移后按图标圆环对准；外侧那段管子（按流体颜色）判「连着」。沉积酸/壤晶废液没有颜色参数。
    景玉谷 21:27/21:31 断管时 3 号口判断开、管道像素少 ≥25%，断前（20:34）和接回后（21:37）连着。"""
    import json
    import cv2
    import near_patrol as NP
    from bpa import nearmap as NM
    from offline_ocr import imread
    root = Path(__file__).resolve().parent.parent
    valid = NP._valid_full()
    want = {"武陵城": 19, "景玉谷": 7, "应龙关": 6}
    for area, n in want.items():
        lp = root / f"data/blueprints/1.6皇帝产线/ledger/{area}.json"
        if not lp.exists() or NP.bpa_base_dir("1.6皇帝产线", area) is None:
            return
        sd = root / json.loads(lp.read_text(encoding="utf-8"))["scan"]["dir"]
        if not (sd / "scan.json").exists():
            return
        F2N = NP.calib(sd)
        gts = NP.gate_targets("1.6皇帝产线", area, F2N, None)
        assert len(gts) == n, (area, len(gts))
        scr, _ = NM.screens(sd)
        for e in gts:
            f, cam = min(scr, key=lambda fc: np.abs(e["icon_q"] - fc[1] - np.array([670, 330])).sum())
            u = e["icon_q"] - cam
            img = imread(f)
            sn = NP.snap_ring(cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3), u)
            assert sn is not None and np.abs(sn - u).max() <= 12, (area, e["name"], f.name)
            m = NP.fluid_pipe_mask(img, e["fluid"])
            if e["fluid"] == "沉积酸":
                assert m is None  # 金黄和传送带同色相，不量
                continue
            if e["fluid"] == "壤晶废液":
                # 2026-11-10 起按深青掩码量；武陵城 19 号口外面是草地（废液从旁边暗管走），量出来是没接管子
                assert m is not None
                g = NP.gate_measure(m, img, valid, e["out"], (int(round(sn[0])), int(round(sn[1]))))
                assert not g["connected"], (area, e["name"], g)
                continue
            g = NP.gate_measure(m, img, valid, e["out"], (int(round(sn[0])), int(round(sn[1]))))
            assert g["connected"], (area, e["name"], f.name, g)
    # 真断管：景玉谷 3 号口
    got = {}
    for stamp in ("20261108-203422", "20261108-212711", "20261108-213105", "20261108-213737"):
        pp = root / f"debug/patrol_{stamp}.json"
        if not pp.exists():
            return
        pj = json.loads(pp.read_text(encoding="utf-8"))
        F2N = NP.calib(root / pj["near"]["ledger_scan"])
        e = next(t for t in NP.gate_targets("1.6皇帝产线", "景玉谷", F2N) if t["gate_no"] == 3)
        for v in pj["near"]["views"]:
            f = root / pj["shots"] / f"v{v['view']:02d}_f00.png"
            u = e["icon_q"] - np.array(v["cam"])
            if not f.exists() or not (280 <= u[0] <= 1040 and 100 <= u[1] <= 520):
                continue
            img = imread(f)
            sn = NP.snap_ring(cv2.medianBlur(cv2.cvtColor(img, cv2.COLOR_BGR2GRAY), 3), u)
            got[stamp] = NP.gate_measure(NP.fluid_pipe_mask(img, "清水"), img, valid, e["out"], (int(round(sn[0])), int(round(sn[1]))))
    base = got["20261108-203422"]
    assert base["connected"] and got["20261108-213737"]["connected"], got
    for st in ("20261108-212711", "20261108-213105"):
        row = {"gate_no": 3, "id_ok": True, "fluid": "清水", "rate": 60, "gate": {"now": got[st], "base": base, "baseline": "ok", "color": True}}
        assert NP.judge_gate(row)[0] == "问题", (st, got[st], base)
    # 判断：基线连着、这次断了且少很多 → 问题；断了但没少 → 待确认；暗很多（流变细）→ 观察；基线就没接出去的管子 → 观察；没颜色参数 → 观察
    row = {"gate_no": 3, "id_ok": True, "fluid": "清水", "rate": 60}
    ok = {"connected": True, "px": 900, "v": 160, "s": 120}
    J = lambda now, b=ok, **k: NP.judge_gate({**row, "gate": {"now": now, "base": b, "baseline": "ok", "color": True, **k}})[0]  # noqa: E731
    assert J({**ok, "connected": False, "px": 500}) == "问题"
    assert J({**ok, "connected": False, "px": 850}) == "待确认"
    assert J({**ok, "v": 90}) == "观察"  # 流变细（流量小），不是没水
    assert J(ok) == "正常"
    assert J({**ok, "connected": False}, {**ok, "connected": False}) == "观察"
    assert NP.judge_gate({**row, "gate": {"color": False}})[0] == "观察"

def test_struct_thinning():
    """管道有容量：流量小时水流变细、两边透明（用户 2026-11-08 截图）。掩码里管子两侧少一条，但横跨方向旁边还有水流 → 不算少了。"""
    H, W = 400, 520
    rng = np.random.RandomState(0)
    bg = (np.full((H, W, 3), 90) + rng.randint(0, 60, (H, W, 3))).astype(np.uint8)
    v, b = np.ones((H, W), np.uint8), np.zeros((H, W), np.uint8)

    def pipe(r0, r1, horiz=True):
        m = np.zeros((H, W, 2), np.uint8)
        if horiz:
            m[r0:r1, 100:420, 0] = 1
        else:
            m[60:360, r0:r1, 0] = 1
        im = bg.copy(); im[m[..., 0] > 0] = (200, 140, 30)
        return {"m": m, "valid": v, "badge": b, "img": im}

    for horiz in (True, False):
        base, thin = pipe(193, 207, horiz), pipe(197, 203, horiz)
        assert NP.compare_struct(thin, base)["lost_px"] == 0, horiz
        keep = NP.STRUCT_THIN
        try:
            NP.STRUCT_THIN = 2.0  # 关掉这条：变细会被当成少了（说明测试有效）
            assert NP.compare_struct(thin, base)["lost_px"] > 0, horiz
        finally:
            NP.STRUCT_THIN = keep


def test_author_gate_rate():
    """作者按编号给单个输入端设流速（2026-11-09：标记 3 的清水准入口 120/分）：load_inputs 优先用作者填的；
    景玉谷输入端 2、3 同一张网，3 号改 120 后这张网供给 60+120=180（原来 120）。清水口没填流速按 60。"""
    import json
    import tempfile
    from bpa import pipenet as PN
    import near_patrol as NP
    with tempfile.TemporaryDirectory() as t:
        bd = Path(t) / "base" / "景玉谷"
        bd.mkdir(parents=True)
        (bd / "inputs_auto.json").write_text(json.dumps([{"no": 1, "item": "清水", "rate": 60, "p": [0, 0]}]), encoding="utf-8")
        assert PN.load_inputs(bd)[0]["rate"] == 60
        marks = [{"no": 1, "item": "清水", "rate": None, "p": [0, 0]}, {"no": 3, "item": "清水", "rate": 120, "p": [5, 5]}]
        (Path(t) / "inputs.json").write_text(json.dumps({"景玉谷": {"marks": marks}}), encoding="utf-8")
        got = {m["no"]: m["rate"] for m in PN.load_inputs(bd)}
        assert got == {1: 60, 3: 120}, got
    root = Path(__file__).resolve().parent.parent
    bd = NP.bpa_base_dir("1.6皇帝产线", "景玉谷")
    lp = root / "data/blueprints/1.6皇帝产线/ledger/景玉谷.json"
    if bd is None or not lp.exists():
        return
    led = json.loads(lp.read_text(encoding="utf-8"))
    recs = json.loads((lp.parent / "景玉谷_recipes.json").read_text(encoding="utf-8"))["devices"]
    inputs = json.loads((bd / "inputs_auto.json").read_text(encoding="utf-8"))
    assert PN.network(bd, led, recs, 3, inputs)["supply"] == 120
    inputs = [dict(m, rate=120) if m["no"] == 3 else m for m in inputs]
    r = PN.network(bd, led, recs, 3, inputs)
    assert r["supply"] == 180 and r["supply_after"] == 60, (r["supply"], r["supply_after"])


if __name__ == "__main__":
    tests = [v for k, v in dict(globals()).items() if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
