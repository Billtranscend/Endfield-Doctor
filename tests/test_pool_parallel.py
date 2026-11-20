"""池子并行 / 配方未知 / 作者选池子配方（审计第 3 项，2026-11-19）：在临时目录里造小据点，直接调 design.analyze。
不读任何本机实测数据。

小据点：一台反应池 (300,300)，输入端送清水 / 芽针粉末 / 锦草粉末进池子、赤铜瓶进两台灌装机；池子用管子直连两台灌装机
（一台灌芽针溶液、一台灌锦草溶液）。另放一台散布机（配方库里 0 条配方）。

这一批（第 2 步）：池子配方表里没有这台时，
- 出一行「配方未知」，rate 是数字 0.0，不再整台消失；summary 单独数，不算成没料；
- 下游灌装机的依据开头点名这台池子，写 blocked_by_unknown；数字不变；
- importer.expectations：作者把这行确认成「对」也不进 device_designs（不压巡检），也不进 unconfirmed；
- 配方库里 0 条配方的机器（散布机）照旧不出行。

第 3 步（池子并行，后半个文件 _scene 那一批）：两条以上互不相干配方的池子几条同时跑（官方），每条按「下游实际吃得下多少」
限速（推断：下游堵 → 池内产物攒满 → 那条停）。合成场景来自原型验证和两位核对者造的反例（T1–T12、R1、R2、S9、S11），
每条写了手算的期望和依据。都是 2 秒一份的配方：一条满速 = 每种原料 / 产物 30 每分钟（×2 的 60）。

第 4 步（文件最后一批）：作者在「池子配方」待确认卡上勾配方（fixes.json 的 pool_recipes）：几条互不相干的并行、首尾相接的并成
一条净配方、[] = 这台没在用、放不下的不用；没回答的池子出卡片和候选组合（不自动套用）。
"""
import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import importer  # noqa: E402
from bpa import project as P  # noqa: E402
from bpa import vision as V  # noqa: E402

POOL = (300, 300)
FILL = [(560, 220), (560, 380)]
SPRAY = (300, 520)  # 散布机：没有任何连线
GATES = [  # (编号, 画布点, 物品, 流速, 层, 去第几台设备)
    (1, (80, 300), "清水", 60, "pipe", 0),
    (2, (80, 240), "芽针粉末", 30, "belt", 0),
    (3, (80, 360), "锦草粉末", 30, "belt", 0),
    (4, (560, 60), "赤铜瓶", 30, "belt", 1),
    (5, (560, 540), "赤铜瓶", 30, "belt", 2),
]
FILL_RECIPES = [
    {"name": "灌装机", "x": FILL[0][0], "y": FILL[0][1], "recipe": "赤铜瓶×1 + 芽针溶液×1 → 赤铜瓶(芽针溶液)×1（2秒）"},
    {"name": "灌装机", "x": FILL[1][0], "y": FILL[1][1], "recipe": "赤铜瓶×1 + 锦草溶液×1 → 赤铜瓶(锦草溶液)×1（2秒）"},
]


def _build(td: Path, pool_recipe: str | None):
    """→ (据点目录, 底账, 配方表)。远景坐标 = 画布坐标（transform K=1）；输入端的 mark 和准入口同一点。"""
    proj = td / "项目"
    bd = proj / "base" / "测试"
    bd.mkdir(parents=True)
    devs = [{"name": "反应池", "x": POOL[0], "y": POOL[1]}] + [{"name": "灌装机", "x": x, "y": y} for x, y in FILL] + \
        [{"name": "散布机", "x": SPRAY[0], "y": SPRAY[1]}]
    pos = [POOL] + FILL
    nodes, links, marks = [], [], []
    for k, (no, p, item, rate, layer, to) in enumerate(GATES):
        kind = "管道" if layer == "pipe" else "物品"
        nodes.append({"id": k, "type": "准入口", "p": list(p), "c": list(p), "kind": kind})
        links.append({"layer": layer, "a": k, "a_side": "E", "end": "device", "device": {"name": devs[to]["name"], "c": list(pos[to])},
                      "path": [list(p), list(pos[to])], "dir": "a->b", "basis": ["合成"]})
        marks.append({"no": no, "p": list(p), "item": item, "rate": rate, "kind": kind})
    for j in (1, 2):  # 池子 → 灌装机：管子，设备直连
        links.append({"layer": "pipe", "free": True, "a": None, "end": "device", "a_dev": {"name": "反应池", "c": list(POOL)},
                      "device": {"name": "灌装机", "c": list(pos[j])}, "path": [list(POOL), list(pos[j])], "dir": "a->b", "basis": ["合成"]})
    (bd / "flow.json").write_text(json.dumps({"nodes": nodes, "links": links, "issues": [], "structures": []}, ensure_ascii=False),
                                  encoding="utf-8")
    (bd / "transform.json").write_text(json.dumps({"K": 1.0, "F0": [0, 0], "bps": {}}), encoding="utf-8")
    V.wr(bd / "composite.png", np.full((600, 700, 3), 232, np.uint8))
    (proj / "inputs.json").write_text(json.dumps({"测试": {"marks": marks}}, ensure_ascii=False), encoding="utf-8")
    rec = [dict(r) for r in FILL_RECIPES]
    if pool_recipe is not None:
        rec.insert(0, {"name": "反应池", "x": POOL[0], "y": POOL[1], "recipe": pool_recipe})
    return bd, {"devices": devs}, rec


_CACHE = {}


def _no_recipe():
    """池子配方表里没有这台（别人的武陵城 7 台池子就是这样）。算一次，几条测试共用。"""
    if "res" not in _CACHE:
        with tempfile.TemporaryDirectory() as td:
            bd, led, rec = _build(Path(td), None)
            _CACHE["res"] = D.analyze(bd, led, rec)
    return _CACHE["res"]


def _row(res, name, xy):
    return next((r for r in res["devices"] if r["device"] == name and (r["x"], r["y"]) == xy), None)


def test_unknown_pool_gets_a_row():
    res = _no_recipe()
    r = _row(res, "反应池", POOL)
    assert r is not None, [(d["device"], d["x"], d["y"]) for d in res["devices"]]
    assert r["state"] == "配方未知" and r["recipe_unknown"] is True and r["recipe"] == "", r
    assert isinstance(r["rate"], float) and r["rate"] == 0.0, r["rate"]  # 界面、summary 按数字读，不能是 None
    assert r["need"] == {} and r["got"] == {} and r["limit_by"] is None, r
    assert "芽针溶液" in r["can_make"] and "锦草溶液" in r["can_make"], r["can_make"]
    assert r["feeds"] == 3 and r["outs"] == 2, (r["feeds"], r["outs"])
    assert r["basis"][0].startswith("配方未知："), r["basis"]
    assert _row(res, "散布机", SPRAY) is None  # 配方库里 0 条配方的机器照旧不出行


def test_summary_counts_unknown_separately():
    res = _no_recipe()
    st = D.summary(res)["states"]
    assert st["配方未知"] == 1 and st["没料"] == 2 and st["没在用"] == 0, st  # 两台灌装机没料；池子不算进没料
    assert D._state_key("配方未知") == "配方未知" and D._state_key("没在用（作者说）") == "没在用"
    assert D._state_key("没接上（物流图里没有进料连接）") == "没接上" and D._state_key("什么都不是") == "没料"  # 兜底不变


def test_downstream_names_the_unknown_pool():
    """灌装机缺溶液：上游是配方未知、能做这种溶液的池子 → 依据开头点名，数字不变。"""
    res = _no_recipe()
    for (x, y), item in zip(FILL, ("芽针溶液", "锦草溶液")):
        r = _row(res, "灌装机", (x, y))
        assert r["state"] == "没料" and r["rate"] == 0.0 and r["got"][item] == 0.0, r
        assert r["blocked_by_unknown"] == [f"反应池@{POOL[0]},{POOL[1]}"], r.get("blocked_by_unknown")
        assert r["basis"][0].startswith("上游配方未知：反应池(300,300)") and item in r["basis"][0], r["basis"][0]


def test_name_at_most_three():
    """上游配方未知的设备超过 3 台：只点 3 台，写「等 n 台」；blocked_by_unknown 全列。直接造图，不跑 analyze。"""
    devices = [{"name": "灌装机", "x": 0, "y": 0}] + [{"name": "反应池", "x": 10 * k, "y": 10} for k in range(1, 6)]
    canvas = [(d["x"], d["y"]) for d in devices]

    class G:
        inn = defaultdict(list)
    G.inn[("d", 0)] = [(("n", 7), {})]  # 灌装机 ← 汇流器 7 ← 5 台池子
    G.inn[("n", 7)] = [(("d", k), {}) for k in range(1, 6)]
    rows = [{"device": "灌装机", "x": 0, "y": 0, "need": {"赤铜瓶": 30.0, "芽针溶液": 30.0}, "got": {"赤铜瓶": 30.0, "芽针溶液": 0.0},
             "rate": 0.0, "basis": ["输入端4号 赤铜瓶 30/分 → 30"]}]
    D._name_unknown_upstream(rows, G, devices, canvas, set(range(1, 6)), set())
    r = rows[0]
    assert len(r["blocked_by_unknown"]) == 5, r
    assert r["basis"][0].count("反应池(") == 3 and "等 5 台" in r["basis"][0], r["basis"][0]
    assert r["basis"][1] == "输入端4号 赤铜瓶 30/分 → 30"
    # 缺的料这种机器做不出来（赤铜瓶）：不点名
    rows = [{"device": "灌装机", "x": 0, "y": 0, "need": {"赤铜瓶": 30.0}, "got": {"赤铜瓶": 0.0}, "rate": 0.0, "basis": []}]
    D._name_unknown_upstream(rows, G, devices, canvas, set(range(1, 6)), set())
    assert "blocked_by_unknown" not in rows[0] and rows[0]["basis"] == [], rows[0]


def test_known_pool_unchanged():
    """池子配方表里有两种产物：照旧出一行（不是配方未知），下游不点名。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec = _build(Path(td), "池产物：芽针溶液(50)/锦草溶液(50)")
        res = D.analyze(bd, led, rec)
    r = _row(res, "反应池", POOL)
    assert r["state"] != "配方未知" and "recipe_unknown" not in r and r["rate"] > 0.99, r
    assert D.summary(res)["states"]["配方未知"] == 0
    assert all("blocked_by_unknown" not in d for d in res["devices"])


def test_importer_skips_unknown_rows():
    """作者把配方未知的行确认成「对」：不进 device_designs（rate 0.0 不是算出来的，不能压巡检），也不进 unconfirmed。"""
    res = _no_recipe()
    r = _row(res, "反应池", POOL)
    ex = importer.expectations(res, {"devices": {P.device_key(r): {"verdict": "ok"}}}, "测试")
    assert not any(x["name"] == "反应池" for x in ex["device_designs"] + ex["unconfirmed"]), ex
    ex = importer.expectations(res, {}, "测试")
    assert not any(x["name"] == "反应池" for x in ex["unconfirmed"]) and len(ex["unconfirmed"]) == 2, ex["unconfirmed"]


# ------------------------------------------------------------------ 第 3 步：池子并行（合成场景）
POOL2 = "池产物：芽针溶液(50)/锦草溶液(50)"
R = {
    "芽针瓶": "赤铜瓶×1 + 芽针溶液×1 → 赤铜瓶(芽针溶液)×1（2秒）",
    "锦草瓶": "赤铜瓶×1 + 锦草溶液×1 → 赤铜瓶(锦草溶液)×1（2秒）",
    "蓝铁锦草": "蓝铁瓶×1 + 锦草溶液×1 → 蓝铁瓶(锦草溶液)×1（2秒）",
    "液化瓶": "赤铜瓶×1 + 液化息壤×1 → 赤铜瓶(液化息壤)×1（2秒）",
    "惰废瓶": "赤铜瓶×1 + 惰性壤晶废液×1 → 赤铜瓶(惰性壤晶废液)×1（2秒）",
    "废液瓶": "赤铜瓶×1 + 壤晶废液×1 → 赤铜瓶(壤晶废液)×1（2秒）",
    "污水瓶": "赤铜瓶×1 + 污水×1 → 赤铜瓶(污水)×1（2秒）",
    "清水瓶": "赤铜瓶×1 + 清水×1 → 赤铜瓶(清水)×1（2秒）",
    "芽针种": "清水×1 + 芽针种子×1 → 芽针×2（2秒）",
    "锦草种": "清水×1 + 锦草种子×1 → 锦草×2（2秒）",
    "采芽针": "芽针×1 → 芽针种子×1（2秒）",
    "碎芽针": "芽针×1 → 芽针粉末×2（2秒）",
    "双溶液瓶": "芽针溶液×1 + 锦草溶液×1 + 赤铜瓶×1 → 测试品×1（2秒）",  # 合成配方：两种溶液都吃
    "双溶液": "芽针溶液×1 + 锦草溶液×1 → 测试品×1（2秒）",
    "一芽两锦": "芽针溶液×1 + 锦草溶液×2 → 测试品×1（2秒）",            # 合成：锦草溶液要 60/分
    "锦草芽针": "锦草溶液×1 + 芽针×4 → 测试品×1（2秒）",                # 合成：芽针要 120/分
}


def _scene(td: Path, sc: dict):
    """合成小据点（远景坐标 = 画布坐标；输入端的 mark 和准入口同一点；设备之间的连线用 free + a_dev）→ (据点目录, 底账, 配方表, 准入口限速)。
    sc：devices [(名字, 坐标, 配方 | (配方, 交替配方) | {"recipe", "inferred"} | None)]、nodes {键: (类型, 坐标)}、
    gates [(编号, 坐标, 物品, 流速, 去向)]（输入端）、limits {准入口节点键: 限速}、links [(a, b, 层[, 只走的物品])]。"""
    proj = td / "项目"
    bd = proj / "base" / "测试"
    bd.mkdir(parents=True)
    devs, recs = [], []
    for name, p, rc in sc["devices"]:
        devs.append({"name": name, "x": p[0], "y": p[1]})
        if isinstance(rc, tuple):
            recs.append({"name": name, "x": p[0], "y": p[1], "recipe": rc[0], "alt": rc[1], "note": "合成交替"})
        elif isinstance(rc, dict):
            recs.append(dict(rc, name=name, x=p[0], y=p[1]))
        elif rc:
            recs.append({"name": name, "x": p[0], "y": p[1], "recipe": rc})
    nodes, links, marks, nid = [], [], [], {}
    for key, (tp, p) in sc.get("nodes", {}).items():
        nid[key] = len(nodes)
        nodes.append({"id": len(nodes), "type": tp, "p": list(p), "c": list(p), "kind": "管道" if tp.startswith("管道") or tp == "准入口" else "物品"})
    glinks = []
    for no, p, item, rate, dst in sc["gates"]:
        layer = "pipe" if D._is_fluid(item) else "belt"
        nid[f"g{no}"] = len(nodes)
        nodes.append({"id": len(nodes), "type": "准入口", "p": list(p), "c": list(p), "kind": "管道" if layer == "pipe" else "物品"})
        marks.append({"no": no, "p": list(p), "item": item, "rate": rate, "kind": "管道" if layer == "pipe" else "物品"})
        glinks.append((f"g{no}", dst, layer))

    def pos(k):
        return list(sc["devices"][k][1]) if isinstance(k, int) else list(nodes[nid[k]]["p"])
    for a, b, layer, *its in glinks + list(sc["links"]):
        lk = {"layer": layer, "path": [pos(a), pos(b)], "basis": ["合成"]}
        if its:
            lk["items"] = its[0]
        if isinstance(a, int) and isinstance(b, int):
            lk.update(free=True, a=None, end="device", a_dev={"name": devs[a]["name"], "c": pos(a)},
                      device={"name": devs[b]["name"], "c": pos(b)}, dir="a->b")
        elif isinstance(b, int):
            lk.update(a=nid[a], a_side="E", end="device", device={"name": devs[b]["name"], "c": pos(b)}, dir="a->b")
        elif isinstance(a, int):
            lk.update(a=nid[b], a_side="W", end="device", device={"name": devs[a]["name"], "c": pos(a)}, dir="b->a")
        else:
            lk.update(a=nid[a], a_side="E", b=nid[b], end="node", dir="a->b")
        links.append(lk)
    (bd / "flow.json").write_text(json.dumps({"nodes": nodes, "links": links, "issues": [], "structures": []}, ensure_ascii=False),
                                  encoding="utf-8")
    (bd / "transform.json").write_text(json.dumps({"K": 1.0, "F0": [0, 0], "bps": {}}), encoding="utf-8")
    V.wr(bd / "composite.png", np.full((1300, 1300, 3), 232, np.uint8))
    (proj / "inputs.json").write_text(json.dumps({"测试": {"marks": marks}}, ensure_ascii=False), encoding="utf-8")
    gl = {"rows": [{"p": list(nodes[nid[k]]["p"]), "limit_on": True, "limit": v} for k, v in sc.get("limits", {}).items()]}
    return bd, {"devices": devs}, recs, gl


def _shuffled(sc: dict) -> dict:
    """同一个场景，设备、节点、输入端、连线的顺序全倒过来（结果不能变）。"""
    n = len(sc["devices"])
    mp = {k: n - 1 - k for k in range(n)}
    return dict(sc, devices=list(reversed(sc["devices"])), nodes=dict(reversed(list(sc.get("nodes", {}).items()))),
                gates=[(no, p, it, r, mp[d] if isinstance(d, int) else d) for no, p, it, r, d in reversed(sc["gates"])],
                links=[tuple([mp[a] if isinstance(a, int) else a, mp[b] if isinstance(b, int) else b] + list(rest))
                       for a, b, *rest in reversed(sc["links"])])


_SC_CACHE = {}


def _an(name: str, sc: dict) -> dict:
    """跑 analyze 主遍和「能送到多少」那一遍 → {"main", "avail", 坐标 "x,y": 行}。同一场景只算一次。"""
    if name not in _SC_CACHE:
        with tempfile.TemporaryDirectory() as td:
            bd, led, rec, gl = _scene(Path(td), sc)
            main = D.analyze(bd, led, rec, gl)
            avail = D.analyze(bd, led, rec, gl, rate_limited_intake=False)
        rows = {f"{r['x']},{r['y']}": r for r in main["devices"]}
        _SC_CACHE[name] = dict(rows, main=main, avail={f"{r['x']},{r['y']}": r for r in avail["devices"]})
    return _SC_CACHE[name]


def _rates(res, key):
    """并行池子 → [每条开工率]；别的设备 → 开工率。"""
    r = res[key]
    return [c["rate"] for c in r["channels"]] if r.get("channels") else r["rate"]


def _check(name, sc, want):
    res = _an(name, sc)
    for key, w in want.items():
        got = _rates(res, key)
        if isinstance(w, list):
            assert isinstance(got, list) and len(got) == len(w) and all(abs(a - b) <= 0.006 for a, b in zip(got, w)), (name, key, got, w)
        else:
            assert not isinstance(got, list) and abs(got - w) <= 0.006, (name, key, got, w)
    for kind in ("main",):
        it = res[kind]["iters"]
        assert not it["hit_cap"] and it["outer"] <= D.OUTER_MAX, (name, it)  # 内层没跑满、外层收敛了
    return res


def _loop(water, backup):
    """池子和种植机 ↔ 采种机互供的环共用一个清水输入端（S1 分流器分到池子和种植机），池子下游两台灌装机各只有 15 赤铜瓶。"""
    sc = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 400), R["锦草瓶"]),
                    ("种植机", (300, 600), R["芽针种"]), ("采种机", (500, 700), R["采芽针"]), ("粉碎机", (500, 560), R["碎芽针"]),
                    ("仓库存货口", (700, 560), None), ("仓库存货口", (700, 640), None)],
        "nodes": {"S1": ("管道分流器", (150, 450))},
        "gates": [(1, (40, 450), "清水", water, "S1"), (2, (150, 250), "芽针粉末", 30, 0), (3, (150, 350), "锦草粉末", 30, 0),
                  (4, (600, 80), "赤铜瓶", 15, 1), (5, (800, 400), "赤铜瓶", 15, 2)],
        "links": [("S1", 0, "pipe"), (0, 1, "pipe"), (0, 2, "pipe"),
                  (3, 4, "belt"), (4, 3, "belt"), (3, 5, "belt"), (5, 6, "belt"), (5, 7, "belt")],
    }
    if backup:  # 种植机那一路经过 1 进 1 出的汇流器：备路，主路（池子）吃饱了才分给它
        sc["nodes"]["M"] = ("管道汇流器", (150, 600))
        sc["links"] += [("S1", "M", "pipe"), ("M", 3, "pipe")]
    else:
        sc["links"] += [("S1", 3, "pipe")]
    return sc


def test_pool_and_plant_loop_share_water():
    """棘轮：池子两条溶液的下游都只吃得下 15（各 15 赤铜瓶）→ 两条各限 50%，只要 30 清水；省下的水给种植机环。
    简单并行（不按下游限速）时池子先占满 60，环被压低以后自己就是一个自洽解，回不来（核对者 T1b、T1c 里环是 0）。
    - 平分（T1a）：清水 50 在分流器上水位平分，池子要 30、种植机要 30 → 各 25：池子两条各 25/60 = 0.417，种植机 25/30 = 0.833；
    - 种植机在备路、清水 50（T1b）：池子先拿 30，剩 20 给种植机 → 0.667（采种机、粉碎机跟着 0.667）；
    - 种植机在备路、清水 60（T1c）：池子 30、种植机 30 → 环满速。"""
    _check("T1a", _loop(50, False), {"300,300": [0.417, 0.417], "300,600": 0.833, "500,700": 0.833, "500,560": 0.833,
                                    "600,200": 0.417, "600,400": 0.417})
    res = _check("T1b", _loop(50, True), {"300,300": [0.5, 0.5], "300,600": 0.667, "500,700": 0.667, "500,560": 0.667,
                                          "600,200": 0.5, "600,400": 0.5})
    pool = res["300,300"]
    assert pool["state"] == "间歇" and pool["limit_by"] is None, pool  # 出料受限，不是缺原料
    assert pool["basis"][0].startswith("出料受限：") and "灌装机(600,200) 缺赤铜瓶（能送到 15/30）" in pool["basis"][0], pool["basis"][0]
    assert pool["basis"][1].startswith("并行配方（官方") and "按出料上限只要 30" in pool["basis"][1], pool["basis"][1]
    assert not any(b.startswith("交替配方：") for b in pool["basis"]), pool["basis"]
    assert [c["demand_cap"] for c in pool["channels"]] == [0.5, 0.5] and pool["need_capped"]["清水"] == 30, pool
    assert [c["src"] for c in pool["channels"]] == ["面板", "面板"], pool["channels"]
    assert res["main"]["iters"]["outer"] >= 1
    _check("T1c", _loop(60, True), {"300,300": [0.5, 0.5], "300,600": 1.0, "500,700": 1.0, "500,560": 1.0})
    _check("T1b·乱序", _shuffled(_loop(50, True)), {"300,300": [0.5, 0.5], "300,600": 0.667, "500,700": 0.667, "500,560": 0.667})


def test_two_products_one_downstream():
    """两条配方的产物进同一台下游 D（D 同时吃芽针溶液、锦草溶液）。
    - D 只有 6 赤铜瓶（T2）：D 最多 0.2 → 两条各限 0.2，池子只要 12 清水；和它共用 45 清水的锦草种植机拿满 30；
    - D 不缺瓶、池子锦草粉末只有 15（T2b）：锦草那条 0.5；D 的另一种原料锦草溶液是池子自己的产物，按那条没被共用清水压之前的量算
      → D 吃得下 0.5 → 芽针那条也限 0.5（不白占清水）。"""
    t2 = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (650, 300), R["双溶液瓶"]),
                    ("种植机", (300, 650), R["锦草种"]), ("仓库存货口", (550, 620), None), ("仓库存货口", (550, 700), None)],
        "nodes": {"S1": ("管道分流器", (150, 450))},
        "gates": [(1, (40, 450), "清水", 45, "S1"), (2, (150, 230), "芽针粉末", 30, 0), (3, (150, 370), "锦草粉末", 30, 0),
                  (4, (650, 120), "赤铜瓶", 6, 1), (5, (100, 750), "锦草种子", 30, 2)],
        "links": [("S1", 0, "pipe"), ("S1", 2, "pipe"), (0, 1, "pipe"), (2, 3, "belt"), (2, 4, "belt")],
    }
    _check("T2", t2, {"300,300": [0.2, 0.2], "650,300": 0.2, "300,650": 1.0})
    _check("T2·乱序", _shuffled(t2), {"300,300": [0.2, 0.2], "650,300": 0.2, "300,650": 1.0})
    t2b = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (650, 300), R["双溶液"])],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 15, 0)],
        "links": [(0, 1, "pipe")],
    }
    _check("T2b", t2b, {"300,300": [0.5, 0.5], "650,300": 0.5})


def test_two_pools_feed_one_downstream():
    """两台并行池子各供 D 一种溶液（P1 的芽针溶液、P2 的锦草溶液），D 只有 15 赤铜瓶 → 只吃得下各 15；
    另一种溶液各去一台 30 瓶的灌装机。两台池子共用 90 清水：P1 = (0.5, 1)、P2 = (1, 0.5)，各要 45 清水，正好够。"""
    t3 = {
        "devices": [("反应池", (300, 200), POOL2), ("反应池", (300, 650), POOL2), ("灌装机", (700, 420), R["双溶液瓶"]),
                    ("灌装机", (700, 100), R["锦草瓶"]), ("灌装机", (700, 800), R["芽针瓶"])],
        "nodes": {"S1": ("管道分流器", (100, 420))},
        "gates": [(1, (30, 420), "清水", 90, "S1"), (2, (180, 120), "芽针粉末", 30, 0), (3, (180, 280), "锦草粉末", 30, 0),
                  (5, (180, 570), "芽针粉末", 30, 1), (6, (180, 730), "锦草粉末", 30, 1),
                  (7, (900, 100), "赤铜瓶", 30, 3), (8, (900, 800), "赤铜瓶", 30, 4), (9, (900, 420), "赤铜瓶", 15, 2)],
        "links": [("S1", 0, "pipe"), ("S1", 1, "pipe"),
                  (0, 2, "pipe", ["芽针溶液"]), (0, 3, "pipe", ["锦草溶液"]), (1, 2, "pipe", ["锦草溶液"]), (1, 4, "pipe", ["芽针溶液"])],
    }
    want = {"300,200": [0.5, 1.0], "300,650": [1.0, 0.5], "700,420": 0.5, "700,100": 1.0, "700,800": 1.0}
    _check("T3", t3, want)
    _check("T3·乱序", _shuffled(t3), want)


def test_pool_into_pool():
    """池子一条配方的产物进另一台并行池子：P1（液化息壤 / 芽针溶液）的液化息壤 → P2（污水 + 液化息壤 → 两种废液 / 锦草溶液）。
    P2 的壤晶废液那台灌装机只有 15 瓶 → P2 废液那条 0.5 → 只要 15 液化息壤 → P1 液化息壤那条 0.5（上限往上游传）。
    清水 75：P1 要 15 + 30、P2 锦草那条 30，正好够。"""
    t4 = {
        "devices": [("反应池", (300, 300), "池产物：液化息壤(50)/芽针溶液(50)"),
                    ("扩容反应池", (600, 300), "池产物：惰性壤晶废液(50)/壤晶废液(50)/锦草溶液(50)"),
                    ("灌装机", (900, 150), R["惰废瓶"]), ("灌装机", (900, 300), R["废液瓶"]), ("灌装机", (900, 450), R["锦草瓶"]),
                    ("灌装机", (300, 650), R["芽针瓶"])],
        "nodes": {"S1": ("管道分流器", (100, 480))},
        "gates": [(1, (30, 480), "清水", 75, "S1"), (2, (150, 200), "息壤", 30, 0), (3, (150, 400), "芽针粉末", 30, 0),
                  (4, (600, 80), "污水", 30, 1), (6, (600, 560), "锦草粉末", 30, 1),
                  (7, (1000, 150), "赤铜瓶", 30, 2), (8, (1000, 300), "赤铜瓶", 15, 3), (9, (1000, 450), "赤铜瓶", 30, 4),
                  (10, (150, 700), "赤铜瓶", 30, 5)],
        "links": [("S1", 0, "pipe"), ("S1", 1, "pipe"),
                  (0, 1, "pipe", ["液化息壤"]), (0, 5, "pipe", ["芽针溶液"]),
                  (1, 2, "pipe", ["惰性壤晶废液"]), (1, 3, "pipe", ["壤晶废液"]), (1, 4, "pipe", ["锦草溶液"])],
    }
    want = {"300,300": [0.5, 1.0], "600,300": [0.5, 1.0], "900,150": 0.5, "900,300": 0.5, "900,450": 1.0, "300,650": 1.0}
    _check("T4", t4, want)
    _check("T4·乱序", _shuffled(t4), want)


def test_three_pools_in_series():
    """池子串联三层：P1 液化息壤 → P2 壤晶废液 → P3（蓝铁粉末 + 壤晶废液 → 污水 + 壤晶）的污水 → 灌装机（只有 7.5 瓶）。
    灌装机吃 7.5 → P3 那条 0.25 → 要 15 壤晶废液 → P2 废液那条 0.5 → 要 15 液化息壤 → P1 那条 0.5。另一条各 1.0。
    上限一层层单调往上传：阻尼只在变化方向反了时才取中点，这里不阻尼，4 轮精确收敛（每轮取平均会拖成 8 轮 0.508）。"""
    t12 = {
        "devices": [("反应池", (200, 300), "池产物：液化息壤(50)/芽针溶液(50)"),
                    ("扩容反应池", (500, 300), "池产物：惰性壤晶废液(50)/壤晶废液(50)/锦草溶液(50)"),
                    ("扩容反应池", (800, 300), "池产物：污水(50)/壤晶(50)/芽针溶液(50)"),
                    ("灌装机", (1050, 200), R["污水瓶"]), ("仓库存货口", (1050, 420), None),
                    ("灌装机", (500, 650), R["惰废瓶"]), ("灌装机", (200, 650), R["芽针瓶"]), ("灌装机", (500, 80), R["锦草瓶"]),
                    ("灌装机", (800, 650), R["芽针瓶"])],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (100, 200), "息壤", 30, 0), (3, (100, 420), "芽针粉末", 30, 0),
                  (4, (380, 200), "污水", 30, 1), (5, (380, 420), "清水", 30, 1), (6, (420, 470), "锦草粉末", 30, 1),
                  (7, (700, 200), "蓝铁粉末", 30, 2), (8, (700, 420), "清水", 30, 2), (9, (700, 470), "芽针粉末", 30, 2),
                  (10, (1150, 200), "赤铜瓶", 7.5, 3), (11, (600, 750), "赤铜瓶", 30, 5), (12, (100, 750), "赤铜瓶", 30, 6),
                  (13, (600, 30), "赤铜瓶", 30, 7), (14, (900, 750), "赤铜瓶", 30, 8)],
        "links": [(0, 1, "pipe", ["液化息壤"]), (0, 6, "pipe", ["芽针溶液"]),
                  (1, 2, "pipe", ["壤晶废液"]), (1, 5, "pipe", ["惰性壤晶废液"]), (1, 7, "pipe", ["锦草溶液"]),
                  (2, 3, "pipe", ["污水"]), (2, 4, "belt", ["壤晶"]), (2, 8, "pipe", ["芽针溶液"])],
    }
    res = _check("T12", t12, {"200,300": [0.5, 1.0], "500,300": [0.5, 1.0], "800,300": [0.25, 1.0], "1050,200": 0.25})
    assert res["main"]["iters"]["outer"] <= 5, res["main"]["iters"]


def test_reverse_coupling():
    """反向耦合（上限越松、下游越吃不下）要收敛到自洽解。
    - 池内（R1）：下游吃 芽针溶液×1 + 锦草溶液×2，清水 40。两条一起被清水压，下游的另一种原料又是池子自己的产物：
      按那条没被清水比例压之前的量算才不会自己压自己。最大自洽解 t1 = a、t2 = 2a，清水 30a + 60a = 40 → a = 0.444、t2 = 0.889；
    - 共用清水（R2）：清水 40 分给池子和种植机 Q，Q 的芽针是下游 D 的另一种原料（要 120）。池子锦草那条越松，Q 分到的水越少、
      D 越吃不下锦草溶液。阻尼（方向反了取中点）收敛到解析解 0.3889；芽针粉末只有 5，芽针那条 1/6。"""
    r1 = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (650, 300), R["一芽两锦"])],
        "gates": [(1, (40, 300), "清水", 40, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0)],
        "links": [(0, 1, "pipe")],
    }
    _check("R1", r1, {"300,300": [0.444, 0.889], "650,300": 0.444})
    _check("R1·乱序", _shuffled(r1), {"300,300": [0.444, 0.889], "650,300": 0.444})
    r2 = {
        "devices": [("反应池", (300, 300), POOL2), ("种植机", (300, 600), R["芽针种"]), ("灌装机", (700, 450), R["锦草芽针"]),
                    ("灌装机", (700, 150), R["芽针瓶"])],
        "nodes": {"S1": ("分流器", (150, 450)), "B1": ("分流器", (500, 520)), "B2": ("分流器", (500, 620))},
        "gates": [(1, (40, 450), "清水", 40, "S1"), (2, (150, 200), "芽针粉末", 5, 0), (3, (150, 360), "锦草粉末", 30, 0),
                  (4, (150, 650), "芽针种子", 30, 1), (5, (900, 150), "赤铜瓶", 30, 3)],
        "links": [("S1", 0, "pipe"), ("S1", 1, "pipe"), (0, 2, "pipe"), (0, 3, "pipe"), (1, "B1", "belt"), ("B1", 2, "belt"),
                  (1, "B2", "belt"), ("B2", 2, "belt")],
    }
    res = _check("R2", r2, {"300,300": [0.167, 0.389], "700,450": 0.389})
    assert res["main"]["iters"]["outer"] <= D.OUTER_MAX and not res["main"]["notes"], res["main"]["iters"]


def _side_feed():
    """芽针那台灌装机另有输入端送来 15 芽针溶液；种植机环在备路，清水 50。"""
    return {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 400), R["锦草瓶"]),
                    ("种植机", (300, 600), R["芽针种"]), ("采种机", (500, 700), R["采芽针"]), ("粉碎机", (500, 560), R["碎芽针"]),
                    ("仓库存货口", (700, 560), None), ("仓库存货口", (700, 640), None)],
        "nodes": {"S1": ("管道分流器", (150, 450)), "M": ("管道汇流器", (150, 600))},
        "gates": [(1, (40, 450), "清水", 50, "S1"), (2, (150, 250), "芽针粉末", 30, 0), (3, (150, 350), "锦草粉末", 30, 0),
                  (4, (600, 80), "赤铜瓶", 30, 1), (5, (800, 400), "赤铜瓶", 15, 2), (6, (800, 200), "芽针溶液", 15, 1)],
        "links": [("S1", 0, "pipe"), ("S1", "M", "pipe"), ("M", 3, "pipe"), (0, 1, "pipe"), (0, 2, "pipe"),
                  (3, 4, "belt"), (4, 3, "belt"), (3, 5, "belt"), (5, 6, "belt"), (5, 7, "belt")],
    }


def test_downstream_has_another_source():
    """下游另有来源（T11）：芽针那台灌装机 30 瓶、要 30 芽针溶液，另一个输入端已经送了 15 → 池子芽针那条只要再补 15 → 0.5；
    锦草那台 15 瓶 → 0.5。池子用 30 清水，备路上的种植机拿剩下的 20 → 0.667（不扣别的来源时池子芽针那条满速、多占 15 清水，环只剩 0.167）。"""
    _check("T11", _side_feed(), {"300,300": [0.5, 0.5], "600,200": 1.0, "300,600": 0.667, "500,700": 0.667, "500,560": 0.667})


def test_downstream_alternating_device():
    """下游是交替配方设备（主配方吃锦草溶液、交替配方吃芽针溶液）：它到底吃多少不知道，按不限算。
    芽针溶液经分流器（S9）或直连（S9b）同时去它和一台只有 15 瓶的灌装机：芽针那条不能被那台 15 瓶的压成 0.5，应是 1.0。
    S9b 是核对者找出来的：直连时筛直接去向只看主配方，把交替配方设备当成「不吃」筛掉了。"""
    alt_fill = (R["锦草瓶"], R["芽针瓶"])
    base = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (700, 150), alt_fill), ("灌装机", (700, 300), R["芽针瓶"]),
                    ("灌装机", (700, 500), R["锦草瓶"])],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (900, 150), "赤铜瓶", 30, 1), (5, (900, 300), "赤铜瓶", 15, 2), (6, (900, 500), "赤铜瓶", 30, 3)],
    }
    s9 = dict(base, nodes={"S1": ("管道分流器", (500, 220))},
              links=[(0, "S1", "pipe", ["芽针溶液"]), ("S1", 1, "pipe"), ("S1", 2, "pipe"), (0, 3, "pipe", ["锦草溶液"])])
    s9b = dict(base, links=[(0, 1, "pipe", ["芽针溶液"]), (0, 2, "pipe", ["芽针溶液"]), (0, 3, "pipe", ["锦草溶液"])])
    for name, sc in (("S9", s9), ("S9b", s9b)):
        res = _check(name, sc, {"300,300": [1.0, 1.0]})
        assert res["700,150"]["basis"][0].startswith("交替配方："), res["700,150"]["basis"][0]


def test_two_pipes_to_one_device():
    """两根管子都从池子通到同一台灌装机（15 瓶）：只算一个去向，下游吃得下 15 → 芽针那条 0.5（算两次就成了 30、不限）。"""
    s11 = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (700, 200), R["芽针瓶"]), ("灌装机", (700, 450), R["锦草瓶"])],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (900, 200), "赤铜瓶", 15, 1), (5, (900, 450), "赤铜瓶", 30, 2)],
        "links": [(0, 1, "pipe", ["芽针溶液"]), (0, 1, "pipe", ["芽针溶液"]), (0, 2, "pipe", ["锦草溶液"])],
    }
    _check("S11", s11, {"300,300": [0.5, 1.0], "700,200": 0.5, "700,450": 1.0})


def _three(water):
    return {
        "devices": [("扩容反应池", (300, 400), "池产物：芽针溶液(50)/锦草溶液(50)/液化息壤(50)"),
                    ("灌装机", (700, 150), R["芽针瓶"]), ("灌装机", (700, 400), R["锦草瓶"]), ("灌装机", (700, 650), R["液化瓶"])],
        "gates": [(1, (40, 400), "清水", water, 0), (2, (150, 250), "芽针粉末", 30, 0), (3, (150, 330), "锦草粉末", 30, 0),
                  (4, (150, 550), "息壤", 30, 0),
                  (5, (900, 150), "赤铜瓶", 15, 1), (6, (900, 400), "赤铜瓶", 30, 2), (7, (900, 650), "赤铜瓶", 10, 3)],
        "links": [(0, 1, "pipe"), (0, 2, "pipe"), (0, 3, "pipe")],
    }


def test_through_buffers_and_splitters():
    """下游穿过储存箱 / 分流器（S7、S8）：
    - 池子两种溶液 → 储气罐 → 两台各 15 瓶的灌装机：穿过储气罐相加，每种只吃得下 15 → 两条各 0.5；
      另一台池子 → 没出口的储气罐：去处不知道，不限 → 满速；
    - 芽针溶液 → 分流器 → 两台各 10 瓶的灌装机：合计吃得下 20/30 → 0.667；锦草溶液没有去向 → 不限。"""
    s7 = {"devices": [("反应池", (300, 300), POOL2), ("储气罐", (500, 300), None), ("灌装机", (700, 200), R["芽针瓶"]),
                      ("灌装机", (700, 400), R["锦草瓶"]), ("反应池", (300, 700), POOL2), ("储气罐", (500, 700), None)],
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (900, 200), "赤铜瓶", 15, 2), (5, (900, 400), "赤铜瓶", 15, 3),
                    (6, (40, 700), "清水", 60, 4), (7, (150, 600), "芽针粉末", 30, 4), (8, (150, 820), "锦草粉末", 30, 4)],
          "links": [(0, 1, "pipe"), (1, 2, "pipe"), (1, 3, "pipe"), (4, 5, "pipe")]}
    _check("S7", s7, {"300,300": [0.5, 0.5], "300,700": [1.0, 1.0]})
    s8 = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (700, 150), R["芽针瓶"]), ("灌装机", (700, 350), R["芽针瓶"])],
          "nodes": {"S1": ("管道分流器", (500, 250))},
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (900, 150), "赤铜瓶", 10, 1), (5, (900, 350), "赤铜瓶", 10, 2)],
          "links": [(0, "S1", "pipe"), ("S1", 1, "pipe"), ("S1", 2, "pipe")]}
    _check("S8", s8, {"300,300": [0.667, 1.0], "700,150": 0.333, "700,350": 0.333})


def test_three_recipes():
    """三条互不相干的配方（扩容反应池）：下游分别吃得下 15 / 30 / 10 → 上限 0.5 / 1 / 0.333，要清水 15 + 30 + 10 = 55。
    - 清水 90（T8a）：够，(0.5, 1, 0.333)；need 清水是三条之和 90；
    - 清水 40（T8b）：不够，按各条（已限过的）用量比例分 40/55 = 0.727 → (0.364, 0.727, 0.242)。"""
    res = _check("T8a", _three(90), {"300,400": [0.5, 1.0, 0.333], "700,150": 0.5, "700,400": 1.0, "700,650": 0.333})
    assert len(res["300,400"]["channels"]) == 3 and res["300,400"]["need"]["清水"] == 90, res["300,400"]
    res = _check("T8b", _three(40), {"300,400": [0.364, 0.727, 0.242], "700,150": 0.364, "700,400": 0.727, "700,650": 0.242})
    # 三条都没贴着上限（清水不够才跑不满）：不写「出料受限」，第一条就是「并行配方：」
    assert res["300,400"]["basis"][0].startswith("并行配方") and "不够时按各条用量比例分（推断）" in res["300,400"]["basis"][0],         res["300,400"]["basis"][:2]
    _check("T8b·乱序", _shuffled(_three(40)), {"300,400": [0.364, 0.727, 0.242]})


def _zero(both):
    """下游别的原料是 0：芽针那台灌装机一个瓶子都没有（both：两台都没有）。清水 80 → S1 → [池子, S2 → 两台锦草种植机]。"""
    sc = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 400), R["锦草瓶"]),
                    ("种植机", (300, 600), R["锦草种"]), ("种植机", (300, 860), R["锦草种"]), ("仓库存货口", (500, 600), None),
                    ("仓库存货口", (500, 860), None), ("仓库存货口", (500, 680), None), ("仓库存货口", (500, 940), None)],
        "nodes": {"S1": ("管道分流器", (150, 450)), "S2": ("管道分流器", (150, 730))},
        "gates": [(1, (40, 450), "清水", 80, "S1"), (2, (150, 250), "芽针粉末", 30, 0), (3, (150, 350), "锦草粉末", 30, 0),
                  (6, (100, 600), "锦草种子", 30, 3), (7, (100, 920), "锦草种子", 30, 4)],
        "links": [("S1", 0, "pipe"), ("S1", "S2", "pipe"), ("S2", 3, "pipe"), ("S2", 4, "pipe"), (0, 1, "pipe"), (0, 2, "pipe"),
                  (3, 5, "belt"), (4, 6, "belt"), (3, 7, "belt"), (4, 8, "belt")],
    }
    if not both:
        sc["gates"].append((5, (800, 400), "赤铜瓶", 30, 2))
    return sc


def test_zero_other_material_does_not_cap():
    """下游别的原料到手是 0（一个瓶子都没送到）时不按它截池子：多半是物流图漏连，截成 0 会把省下的清水送给别人、藏住别处的缺水。
    只有「有、但不够」才截（2026-11-19 定的防护；原型里这里截成 0，核对者的 T5a 期望芽针那条 0、种植机 0.833）。
    现在：池子不限，要 60；S1 水位平分 80 → 池子 40、两台种植机共 40 → 池子两条各 40/60 = 0.667，种植机各 20/30 = 0.667。
    两台灌装机都没瓶（T5b）一样；池子是间歇，不是没料，limit_by 是清水。"""
    for name, both in (("T5a", False), ("T5b", True)):
        res = _check(name, _zero(both), {"300,300": [0.667, 0.667], "300,600": 0.667, "300,860": 0.667})
        pool = res["300,300"]
        assert pool["state"] == "间歇" and pool["limit_by"] == "清水", pool
        assert all(c["demand_cap"] is None for c in pool["channels"]) and not pool["basis"][0].startswith("出料受限"), pool
        if not both:
            assert abs(res["600,400"]["rate"] - 0.667) < 0.006, res["600,400"]  # 有瓶的锦草灌装机跟着池子 0.667


def test_unknown_items_and_no_outlet():
    """- 瓶子来自物品没看到的仓库取货口（按缺口分，T6a）：那台灌装机的瓶子算够（按缺口分来的不当「别处已经送到的溶液」）→ 芽针那条 1.0；
      锦草那台 15 瓶 → 0.5。
    - 一种产物没有去向（T7a）或进仓库存货口（T7b）：那条不限（没建模「攒满 50 就停」，已知偏松）；芽针那台 15 瓶 → 0.5。
    - 已知偏差 T6b：取货口带子按缺口分时会被当成液体（现有规则 _unknown_fits：带子来的不限制物品种类），芽针那台的上限跟着偏，
      结果是 (0.4, 0.6)，按带子只能运固体算应是 (0.5, 0.5)。这里锁住现状，改了取货口规则要重新核这条。"""
    t6a = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 450), R["锦草瓶"]),
                    ("仓库取货口", (850, 200), None)],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (5, (850, 450), "赤铜瓶", 15, 2)],
        "links": [(0, 1, "pipe"), (0, 2, "pipe"), (3, 1, "belt")],
    }
    _check("T6a", t6a, {"300,300": [1.0, 0.5], "600,200": 1.0, "600,450": 0.5})
    t6b = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 450), R["锦草瓶"]),
                    ("仓库取货口", (850, 260), None)],
        "gates": [(1, (40, 300), "清水", 30, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (600, 60), "赤铜瓶", 20, 1), (5, (850, 450), "赤铜瓶", 30, 2)],
        "links": [(0, 1, "pipe"), (0, 2, "pipe"), (3, 1, "belt")],
    }
    _check("T6b（已知偏差）", t6b, {"300,300": [0.4, 0.6]})
    t7 = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("仓库存货口", (600, 450), None)],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (800, 200), "赤铜瓶", 15, 1)],
    }
    _check("T7a", dict(t7, links=[(0, 1, "pipe", ["芽针溶液"])]), {"300,300": [0.5, 1.0]})
    _check("T7b", dict(t7, links=[(0, 1, "pipe", ["芽针溶液"]), (0, 2, "pipe", ["锦草溶液"])]), {"300,300": [0.5, 1.0]})


def test_others_unaffected():
    """并行池子旁边：单条池子（只有一条配方，不进并行）只按结构背压，下游缺瓶不压它 → 满速、没有 channels；
    普通灌装机照旧；种植机 ↔ 采种机两种植物轮换照旧是「交替配方：」，importer._rotation_of 认得出。"""
    t10 = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 420), R["锦草瓶"]),
                    ("反应池", (300, 800), "池产物：芽针溶液(50)"), ("灌装机", (650, 800), R["芽针瓶"]),
                    ("灌装机", (300, 1100), R["清水瓶"])],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (800, 200), "赤铜瓶", 15, 1), (5, (800, 420), "赤铜瓶", 30, 2),
                  (6, (40, 800), "清水", 60, 3), (7, (150, 700), "芽针粉末", 30, 3), (8, (850, 800), "赤铜瓶", 15, 4),
                  (9, (40, 1100), "清水", 30, 5), (10, (500, 1100), "赤铜瓶", 15, 5)],
        "links": [(0, 1, "pipe"), (0, 2, "pipe"), (3, 4, "pipe")],
    }
    res = _check("T10", t10, {"300,300": [0.5, 1.0], "300,800": 1.0, "650,800": 0.5, "300,1100": 0.5})
    single = res["300,800"]
    assert single["state"] == "满速" and "channels" not in single and "need_capped" not in single, single
    assert all("channels" not in r and "need_capped" not in r for k, r in res.items() if k not in ("main", "avail", "300,300")), res.keys()
    t10b = {
        "devices": [("反应池", (300, 300), POOL2), ("灌装机", (600, 200), R["芽针瓶"]), ("灌装机", (600, 420), R["锦草瓶"]),
                    ("种植机", (300, 800), R["锦草种"]), ("采种机", (600, 800), R["采芽针"]), ("仓库存货口", (300, 1050), None),
                    ("仓库存货口", (600, 1050), None)],
        "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                  (4, (800, 200), "赤铜瓶", 15, 1), (5, (800, 420), "赤铜瓶", 30, 2),
                  (6, (40, 800), "清水", 60, 3), (7, (150, 700), "锦草种子", 30, 3), (8, (150, 900), "芽针种子", 30, 3)],
        "links": [(0, 1, "pipe"), (0, 2, "pipe"), (3, 4, "belt"), (4, 3, "belt"), (3, 5, "belt"), (4, 6, "belt")],
    }
    res = _check("T10b", t10b, {"300,800": 1.0, "600,800": 1.0})
    for key in ("300,800", "600,800"):
        assert "两种植物轮换" in res[key]["basis"][0] and importer._rotation_of(res[key]) is not None, res[key]["basis"][:1]


def test_liquid_gas_converter_still_alternates():
    """液气转化机配方表带 alt（应龙关 (950,879) 那种，设计如此）：仍按两路轮流做，依据以「交替配方：」开头，两条合计不超过 1。
    两路各送 6 → 各够跑 20%，合计 40%。"""
    sc = {
        "devices": [("液气转化机", (300, 300), ("液化息壤×1 → 息壤气×1（2秒）", "沉积酸×1 → 酸气×1（2秒）"))],
        "gates": [(1, (40, 250), "液化息壤", 6, 0), (2, (40, 350), "沉积酸", 6, 0)],
        "links": [],
    }
    res = _check("液气转化机", sc, {"300,300": 0.4})
    r = res["300,300"]
    assert r["basis"][0].startswith("交替配方：") and "channels" not in r, r["basis"][:1]
    assert res["main"]["iters"] == {"outer": 0, "inner": res["main"]["iters"]["inner"], "hit_cap": False} \
        and len(res["main"]["iters"]["inner"]) == 1  # 没有并行池子：不跑外层，只跑一趟


def test_basic_parallel():
    """plan 第 3 步的基本用例（池子 → 两台各 30 瓶的灌装机，下游不卡）：
    - 清水 60：两条各 1.0、满速，两台灌装机各到手 30，清水输入端 downstream = 60（原来按轮流算是 30 / 0.5）；
    - 清水 30：两条各 0.5，依据写「不够时按各条用量比例分（推断）」；
    - 芽针粉末 15、清水 60：芽针那条 0.5，锦草那条 1.0；
    - 只有芽针粉末（锦草粉末 0）、清水 45 经分流器先给池子（主路）、再给备路上一台吃清水的灌装机：芽针那条 1.0、锦草那条 0，
      池子只拿 30 清水（没料的那条不白占），备路拿到剩下的 15 → 0.5。"""
    def sc(water, yz, jc):
        gates = [(1, (40, 300), "清水", water, 0), (2, (150, 200), "芽针粉末", yz, 0), (4, (600, 60), "赤铜瓶", 30, 1),
                 (5, (600, 540), "赤铜瓶", 30, 2)]
        if jc:
            gates.append((3, (150, 420), "锦草粉末", jc, 0))
        return {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (560, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"])],
                "gates": gates, "links": [(0, 1, "pipe"), (0, 2, "pipe")]}
    only = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (560, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"]),
                        ("灌装机", (300, 700), R["清水瓶"])],
            "nodes": {"S1": ("管道分流器", (150, 450)), "M": ("管道汇流器", (150, 600))},
            "gates": [(1, (40, 450), "清水", 45, "S1"), (2, (150, 200), "芽针粉末", 30, 0), (4, (600, 60), "赤铜瓶", 30, 1),
                      (5, (600, 540), "赤铜瓶", 30, 2), (6, (500, 700), "赤铜瓶", 30, 3)],
            "links": [("S1", 0, "pipe"), ("S1", "M", "pipe"), ("M", 3, "pipe"), (0, 1, "pipe"), (0, 2, "pipe")]}
    res = _check("满水", sc(60, 30, 30), {"300,300": [1.0, 1.0], "560,220": 1.0, "560,380": 1.0})
    pool = res["300,300"]
    assert pool["state"] == "满速" and pool["need"] == {"清水": 60, "芽针粉末": 30, "锦草粉末": 30} and pool["rate"] == 1.0, pool
    assert res["560,220"]["got"]["芽针溶液"] == 30 and res["560,380"]["got"]["锦草溶液"] == 30
    water_gate = next(g for g in res["main"]["gates"] if g["item"] == "清水")
    assert water_gate["downstream"] == 60, water_gate
    res = _check("清水30", sc(30, 30, 30), {"300,300": [0.5, 0.5]})
    assert "不够时按各条用量比例分（推断）" in res["300,300"]["basis"][0] and res["300,300"]["limit_by"] == "清水", res["300,300"]["basis"][:1]
    _check("芽针粉末15", sc(60, 15, 30), {"300,300": [0.5, 1.0]})
    res = _check("只有芽针粉末", only, {"300,300": [1.0, 0.0], "560,220": 1.0, "300,700": 0.5})
    assert res["300,300"]["got"]["清水"] == 30 and res["300,700"]["got"]["清水"] == 15, (res["300,300"]["got"], res["300,700"]["got"])


def test_chain_members():
    """面板上首尾相接的两条（清水 + 息壤 → 液化息壤，液化息壤 + 污水 → 两种废液：武陵城扩容反应池 (1489,1055) 那种）并成一组净配方，
    再和互不相干的芽针溶液那条并行：channels 两条，第一条 members 两条，依据注明「首尾相接，按一条净配方算」。"""
    sc = {"devices": [("扩容反应池", (300, 300), "池产物：息壤(50)/壤晶废液(50)/芽针溶液(50)"),
                      ("灌装机", (650, 150), R["废液瓶"]), ("灌装机", (650, 300), R["惰废瓶"]), ("灌装机", (650, 450), R["芽针瓶"])],
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "息壤", 30, 0), (3, (150, 250), "污水", 30, 0),
                    (4, (150, 420), "芽针粉末", 30, 0), (5, (800, 150), "赤铜瓶", 30, 1), (6, (800, 300), "赤铜瓶", 30, 2),
                    (7, (800, 450), "赤铜瓶", 30, 3)],
          "links": [(0, 1, "pipe", ["壤晶废液"]), (0, 2, "pipe", ["惰性壤晶废液"]), (0, 3, "pipe", ["芽针溶液"])]}
    res = _check("首尾相接", sc, {"300,300": [1.0, 1.0]})
    r = res["300,300"]
    assert len(r["channels"][0]["members"]) == 2 and len(r["channels"][1]["members"]) == 1, r["channels"]
    assert "首尾相接，按一条净配方算" in r["basis"][0] and r["need"]["清水"] == 60, (r["basis"][:1], r["need"])


def test_structural_cap_on_one_recipe():
    """一种溶液的下游结构上只吃 15（芽针溶液经过限速 15 的准入口再进灌装机）：只有芽针那条受限（0.5），锦草那条满速；
    依据第一条是「出料受限：」。"""
    sc = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (700, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"])],
          "nodes": {"L": ("准入口", (500, 220))},
          "limits": {"L": 15},
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (700, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2)],
          "links": [(0, "L", "pipe", ["芽针溶液"]), ("L", 1, "pipe"), (0, 2, "pipe", ["锦草溶液"])]}
    res = _check("结构上限", sc, {"300,300": [0.5, 1.0], "700,220": 0.5, "560,380": 1.0})
    pool = res["300,300"]
    assert pool["basis"][0].startswith("出料受限：") and pool["channels"][0]["out_cap"] == 0.5 and pool["channels"][1]["out_cap"] == 1.0, pool
    # out_cap 取结构上限和需求上限里小的，这一例两个都是 0.5（下游需求上限也按准入口限速截住了）：单独断言 struct_cap，
    # 不然把 _backpressure 里按条记结构上限那两行删掉，这条测试照样过（2026-11-19 评审）
    assert [c["struct_cap"] for c in pool["channels"]] == [0.5, 1.0], pool["channels"]
    assert [c["demand_cap"] for c in pool["channels"]] == [0.5, 1.0], pool["channels"]


def test_infer_by_neighbors_pool_vs_filler():
    """配方是推断的、邻居一点都不支持时按邻居重挑，两条配方各自的原料都有人送：
    - 池子：两条同时跑（进并行，src=邻居推断），recipe_fixes 写「并行」（皇帝产线武陵城扩容反应池 (1313,1114) 就是这样）；
    - 非池子（灌装机）：照旧按两种配方轮流做（交替配方）。"""
    wrong = {"recipe": "液化息壤×1 + 蓝铁粉末×1 + 壤晶废液×1 → 惰性壤晶废液×1 + 壤晶×1（2秒）", "inferred": True}
    pool = {"devices": [("反应池", (300, 300), wrong), ("灌装机", (560, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"])],
            "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                      (4, (600, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2)],
            "links": [(0, 1, "pipe"), (0, 2, "pipe")]}
    res = _check("邻居推断池子", pool, {"300,300": [1.0, 1.0]})
    r = res["300,300"]
    assert [c["src"] for c in r["channels"]] == ["邻居推断", "邻居推断"] and "并行，推断" in r["basis"][0], r
    fx = next(f for f in res["main"]["recipe_fixes"] if f["device"] == "反应池")
    assert " + 并行 " in fx["to"] and "同时跑（并行，推断）" in fx["how"] and "交替" not in fx["to"] + fx["how"], fx
    fill = {"devices": [("灌装机", (300, 300), dict(wrong, recipe="蓝铁瓶×1 + 赤铜溶液×1 → 蓝铁瓶×1（2秒）"))],
            "gates": [(1, (40, 250), "赤铜瓶", 30, 0), (2, (40, 300), "芽针溶液", 30, 0), (3, (40, 350), "锦草溶液", 30, 0)],
            "links": []}
    res = _check("邻居推断灌装机", fill, {})
    r = res["300,300"]
    assert r["basis"][0].startswith("交替配方：") and "channels" not in r, r["basis"][:1]
    fx = next(f for f in res["main"]["recipe_fixes"] if f["device"] == "灌装机")
    assert " / 交替 " in fx["to"], fx


def test_author_single_recipe_clears_parallel():
    """作者在待确认里给池子指定了一条配方（fixes.json 的 recipes）：这台就只跑这一条，面板读出的并行配方不留（没有 channels）。
    液气转化机的交替配方同理不留。"""
    sc = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (560, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"]),
                      ("液气转化机", (300, 700), ("液化息壤×1 → 息壤气×1（2秒）", "沉积酸×1 → 酸气×1（2秒）"))],
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (600, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2), (6, (40, 650), "液化息壤", 6, 3),
                    (7, (40, 750), "沉积酸", 6, 3)],
          "links": [(0, 1, "pipe"), (0, 2, "pipe")]}
    want = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = _scene(Path(td), sc)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": {"recipes": {
            "反应池@300,300": want, "液气转化机@300,700": "液化息壤×1 → 息壤气×1（2秒）"}}}, ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    rows = {f"{r['x']},{r['y']}": r for r in res["devices"]}
    pool, conv = rows["300,300"], rows["300,700"]
    assert pool["recipe"] == want and "channels" not in pool and pool["need"] == {"清水": 30, "芽针粉末": 30}, pool
    assert not any(b.startswith(("并行配方", "交替配方")) for b in pool["basis"]), pool["basis"]
    assert not conv["basis"][0].startswith("交替配方：") and abs(conv["rate"] - 0.2) < 1e-6, conv  # 只做息壤气：6/30


def test_old_single_recipe_on_pool_not_silent():
    """老的 recipes[池子键]（手改的 fixes.json、旧版存下的）不再静默（2026-11-19 评审）：
    - 选的正好是并行池子现在的第一条：照样只跑这一条，但 recipe_fixes 记一条「只留这一条，原来并行的另几条已去掉」；
    - 同一台另有 pool_recipes：老的那条不用，notes 里说一句「老的配方指定没用上」。"""
    sc = {"devices": [("反应池", (300, 300), POOL2), ("灌装机", (560, 220), R["芽针瓶"]), ("灌装机", (560, 380), R["锦草瓶"])],
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (600, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2)],
          "links": [(0, 1, "pipe"), (0, 2, "pipe")]}
    yz, jc = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）", "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"

    def run(fix):
        with tempfile.TemporaryDirectory() as td:
            bd, led, rec, gl = _scene(Path(td), sc)
            (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fix}, ensure_ascii=False), encoding="utf-8")
            return D.analyze(bd, led, rec, gl)
    res = run({"recipes": {"反应池@300,300": yz}})
    fx = [f for f in res["recipe_fixes"] if f["device"] == "反应池"]
    assert len(fx) == 1 and fx[0]["to"] == yz and "原来并行的另几条已去掉" in fx[0]["how"], res["recipe_fixes"]
    res = run({"recipes": {"反应池@300,300": yz}, "pool_recipes": {"反应池@300,300": [yz, jc]}})
    pool = next(r for r in res["devices"] if r["x"] == 300 and r["y"] == 300)
    assert len(pool["channels"]) == 2, pool
    assert any(n["kind"] == "老的配方指定没用上" and yz in n["msg"] for n in res["notes"]), res["notes"]


def test_downstream_stuck_line_skips_capped_pool_product():
    """下游灌装机缺瓶、池子那条因此出料受限：灌装机的「卡在」只写缺瓶，不再写池子那种溶液（避免两台互相指认）；
    池子自己不报「卡在」（按出料上限要的清水到手了）。走 run_base（它写 design.json，只在临时目录里）。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = _scene(Path(td), _loop(60, True))
        arch = Path(td) / "档案"  # run_base 从蓝图档案读底账和配方表
        (arch / "ledger").mkdir(parents=True)
        (arch / "ledger" / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        res = D.run_base(bd, arch, "测试")
    rows = {f"{r['x']},{r['y']}": r for r in res["devices"]}
    pool, fill = rows["300,300"], rows["600,200"]
    assert not any(b.startswith("卡在：") for b in pool["basis"]) and pool["limit_by"] is None, pool["basis"]
    assert fill["basis"][0] == "卡在：赤铜瓶（能送到 15/30）", fill["basis"][:2]
    assert fill["available"]["芽针溶液"] == 15 and "channels" not in fill, fill  # 数字照旧（池子被限后能送到 15），只改那句话
    assert "灌装机@600,200" in pool["channels"][0]["named"], pool["channels"]


def _run_base(td: Path, sc: dict, fixes: dict | None = None) -> dict:
    """合成场景走 run_base（「卡在」是它补的；写 design.json，只在临时目录里）→ {坐标 "x,y": 行, "res": 结果}。"""
    bd, led, rec, gl = _scene(td, sc)
    if fixes:
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fixes}, ensure_ascii=False), encoding="utf-8")
    arch = td / "档案"
    (arch / "ledger").mkdir(parents=True)
    (arch / "ledger" / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
    (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
    assert not sc.get("limits"), "准入口限速走不进 run_base 的档案（这里没写 gate_limits）"
    res = D.run_base(bd, arch, "测试")
    return dict({f"{r['x']},{r['y']}": r for r in res["devices"]}, res=res)


def _pool_eats_pool(hetong: float) -> tuple[dict, dict]:
    """P1（扩容反应池：污水 + 液化息壤 → 两种废液 / 清水 + 锦草粉末 → 锦草溶液）的壤晶废液送进 P2；P2 作者指定两条：
    蓝铁粉末 + 壤晶废液×2、蓝铁粉末 + 赫铜溶液×2，共用 15 蓝铁粉末，赫铜溶液给 hetong。"""
    a = "蓝铁粉末×1 + 壤晶废液×2 → 污水×1 + 壤晶×1（2秒）"
    b = "蓝铁粉末×1 + 赫铜溶液×2 → 赫铜块×1 + 污水×1（2秒）"
    gates = [(1, (40, 200), "污水", 30, 0), (2, (40, 300), "液化息壤", 30, 0), (3, (40, 400), "清水", 30, 0),
             (4, (40, 500), "锦草粉末", 30, 0), (5, (600, 100), "蓝铁粉末", 15, 1),
             (6, (300, 700), "赤铜瓶", 30, 2), (7, (300, 800), "赤铜瓶", 30, 3)]
    if hetong:
        gates.append((8, (600, 600), "赫铜溶液", hetong, 1))
    sc = {"devices": [("扩容反应池", (300, 300), "池产物：惰性壤晶废液(50)/壤晶废液(50)/锦草溶液(50)"),
                      ("扩容反应池", (700, 300), None),
                      ("灌装机", (500, 650), R["惰废瓶"]), ("灌装机", (500, 850), R["锦草瓶"]),
                      ("仓库存货口", (950, 300), None), ("仓库存货口", (950, 400), None)],
          "gates": gates,
          "links": [(0, 1, "pipe", ["壤晶废液"]), (0, 2, "pipe", ["惰性壤晶废液"]), (0, 3, "pipe", ["锦草溶液"]),
                    (1, 4, "pipe", ["污水"]), (1, 5, "belt", ["壤晶"])]}
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = _scene(Path(td), sc)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": {"pool_recipes": {"扩容反应池@700,300": [a, b]}}},
                                                                ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    return {f"{r['x']},{r['y']}": r for r in res["devices"]}, res


def test_downstream_pool_shares_by_running_recipes():
    """下游是并行池子时，共用原料按别的条实际能跑多少分（和 chan_rates 同口径，2026-11-19 评审）。
    - 赫铜溶液 0：P2 第二条不跑，15 蓝铁全给第一条 → 第一条 50%、吃 30 壤晶废液 → P1 满速、没有出料上限；
      原来分母按两条满速用量相加（15/60），P2 只吃 15，P1 废液那条被定成 50%，两台一起低估还自洽收敛；
    - 赫铜溶液 6：P2 第二条 9%，P1 废液那条 0.83–0.92（eat 求的是「壤晶废液不限量时能跑多少」，按比例分共用原料时略往不截的方向偏）。"""
    rows, res = _pool_eats_pool(0)
    p1, p2 = rows["300,300"], rows["700,300"]
    assert [c["rate"] for c in p1["channels"]] == [1.0, 1.0] and all(c["demand_cap"] is None for c in p1["channels"]), p1["channels"]
    assert abs(p2["channels"][0]["rate"] - 0.5) <= 0.01 and p2["channels"][1]["rate"] == 0, p2["channels"]
    assert rows["500,650"]["rate"] == 1.0 and not res["iters"]["hit_cap"], (rows["500,650"], res["iters"])
    rows, res = _pool_eats_pool(6)
    p1, p2 = rows["300,300"], rows["700,300"]
    assert 0.83 <= p1["channels"][0]["rate"] <= 0.92, p1["channels"]
    assert abs(p2["channels"][1]["rate"] - 0.09) <= 0.01, p2["channels"]


def test_pool_names_downstream_pool():
    """池子串池子：P1 的液化息壤进 P2，P2 的污水只有 15 → P1 液化息壤那条被下游限到 50%。
    P1 的依据要点名 P2（和下游是普通设备时一样），channels[].named 带上 P2；P2 的「卡在」只列污水，
    不反过来列液化息壤（2026-11-19 评审：下游是池子时原来不点名，两台互相指认）。"""
    sc = {"devices": [("反应池", (300, 300), "池产物：液化息壤(50)/芽针溶液(50)"),
                      ("扩容反应池", (600, 300), "池产物：惰性壤晶废液(50)/壤晶废液(50)/锦草溶液(50)"),
                      ("仓库存货口", (900, 150), None), ("仓库存货口", (900, 300), None), ("仓库存货口", (900, 450), None),
                      ("仓库存货口", (300, 650), None)],
          "nodes": {"S1": ("管道分流器", (100, 480))},
          "gates": [(1, (30, 480), "清水", 90, "S1"), (2, (150, 200), "息壤", 30, 0), (3, (150, 400), "芽针粉末", 30, 0),
                    (4, (600, 80), "污水", 15, 1), (6, (600, 560), "锦草粉末", 30, 1)],
          "links": [("S1", 0, "pipe"), ("S1", 1, "pipe"), (0, 1, "pipe", ["液化息壤"]), (0, 5, "pipe", ["芽针溶液"]),
                    (1, 2, "pipe", ["惰性壤晶废液"]), (1, 3, "pipe", ["壤晶废液"]), (1, 4, "pipe", ["锦草溶液"])]}
    with tempfile.TemporaryDirectory() as td:
        rows = _run_base(Path(td), sc)
    p1, p2 = rows["300,300"], rows["600,300"]
    assert p1["channels"][0]["rate"] == 0.5 and p1["channels"][0]["demand_cap"] == 0.5, p1["channels"]
    assert "扩容反应池(600,300) 缺污水" in p1["basis"][0], p1["basis"][0]
    assert "扩容反应池@600,300" in p1["channels"][0]["named"], p1["channels"]
    stuck = next(b for b in p2["basis"] if b.startswith("卡在："))
    assert "污水" in stuck and "液化息壤" not in stuck, p2["basis"]


def test_capping_keeps_outer_converging():
    """外层定出料上限时「别的原料够跑多快」不算池子产物（capping）：T1 的环 + 清水 55 平分、芽针粉末只有 10，
    芽针灌装机的赤铜瓶和一台清水瓶灌装机共用一个分流器。这个结构内层本来就会跑满（absorb 限吃量在两个值之间跳，
    不在这一项的范围里，所以不断言 hit_cap），关掉 capping 后外层 8 轮不收敛、多出「池子出料上限没收敛」。
    已收敛的场景和四个据点里关掉 capping 结果都不变，只有这里能抓到它被删（2026-11-19 评审）。"""
    sc = _loop(55, False)
    sc = dict(sc, devices=sc["devices"] + [("灌装机", (900, 100), R["清水瓶"])], nodes=dict(sc["nodes"], S2=("分流器", (800, 50))))
    gates = [x for x in sc["gates"] if x[0] != 4] + [(4, (800, 10), "赤铜瓶", 30, "S2"), (7, (1000, 200), "清水", 30, 8)]
    sc["gates"] = [(no, p, it, (10 if no == 2 else r), d) for no, p, it, r, d in gates]
    sc["links"] = sc["links"] + [("S2", 1, "belt"), ("S2", 8, "belt")]
    res = _an("capping", sc)
    it = res["main"]["iters"]
    assert it["outer"] <= 3 and not any(n["kind"] == "池子出料上限没收敛" for n in res["main"]["notes"]), (it, res["main"]["notes"])
    got = _rates(res, "300,300")
    assert all(abs(a - b) <= 0.006 for a, b in zip(got, [0.333, 0.5])), got


# ------------------------------------------------------------------ 第 4 步：作者给池子选配方（fixes.json 的 pool_recipes）
YZ = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"
JC = "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"
POOL_KEY = f"反应池@{POOL[0]},{POOL[1]}"


def _with_fix(fix: dict, pool_recipe: str | None = None, run_base=False):
    """第 2 步那个小据点（池子配方表里没有）+ fixes.json 的这一段 → analyze 的结果（run_base=True：走 run_base，另返回第三遍跑了几次）。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec = _build(Path(td), pool_recipe)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fix}, ensure_ascii=False), encoding="utf-8")
        if not run_base:
            return D.analyze(bd, led, rec)
        arch = Path(td) / "档案"
        (arch / "ledger").mkdir(parents=True)
        (arch / "ledger" / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        old, calls = D.analyze, []

        def counted(*a, **k):
            calls.append(k.get("main_blocked", False))
            return old(*a, **k)
        D.analyze = counted
        try:
            res = D.run_base(bd, arch, "测试")
        finally:
            D.analyze = old
        return res, calls


def _pool_cards(res):
    return [p for p in res["pending"] if p.get("kind") == "池子配方"]


def test_unknown_pool_card_and_candidates():
    """池子没配方：pending 里有一张「池子配方」卡（没回答），候选第一名是芽针溶液 + 锦草溶液两条（进来清水和两种粉末、出去两种溶液）；
    这台仍是配方未知（候选不自动套用），依据提示去待确认里选；行上带 candidates、pool_src=不明。"""
    res = _no_recipe()
    cards = _pool_cards(res)
    assert len(cards) == 1, res["pending"]
    c = cards[0]
    assert c["key"] == POOL_KEY and c["answer"] is None and c["applied"] is False and c["slots"] == 5, c
    assert sorted(c["combos"][0]["recipes"]) == sorted([YZ, JC]) and c["combos"][0]["items"] == 5, c["combos"][:2]
    assert len(c["ins"]) == 3 and len(c["outs"]) == 2 and {i["item"] for i in c["ins"]} == {"清水", "芽针粉末", "锦草粉末"}, c["ins"]
    assert all(len(x["at"]) == 2 for x in c["ins"] + c["outs"]), c
    r = _row(res, "反应池", POOL)
    assert r["state"] == "配方未知" and r["pool_src"] == "不明" and r["candidates"] == c["combos"], r
    assert r["basis"][2].endswith("去待确认里选配方"), r["basis"]
    assert not any("pool_src" in d or "candidates" in d for d in res["devices"] if d["device"] != "反应池")  # 别的行不多键


def test_author_two_recipes_run_parallel():
    """作者勾芽针、锦草两条：和面板读出两种产物时一样两条同时跑（清水 60 → 都满速），来源记「作者」；卡片还在，标 applied、能撤销。
    同时有老的 recipes[键]：以 pool_recipes 为准。"""
    res = _with_fix({"pool_recipes": {POOL_KEY: [YZ, JC]}, "recipes": {POOL_KEY: YZ}})
    r = _row(res, "反应池", POOL)
    assert [c["src"] for c in r["channels"]] == ["作者", "作者"] and [c["rate"] for c in r["channels"]] == [1.0, 1.0], r.get("channels")
    assert r["state"] == "满速" and r["need"] == {"清水": 60, "芽针粉末": 30, "锦草粉末": 30} and r["pool_src"] == "作者", r
    assert "recipe_unknown" not in r and D.summary(res)["states"]["配方未知"] == 0
    fills = [_row(res, "灌装机", p) for p in FILL]
    assert all(f["rate"] == 1.0 and "blocked_by_unknown" not in f for f in fills), fills
    cards = _pool_cards(res)
    assert len(cards) == 1 and cards[0]["answer"] == [YZ, JC] and cards[0]["applied"] is True, cards
    fx = next(f for f in res["recipe_fixes"] if f["device"] == "反应池")
    assert fx["how"] == "作者指定（池子配方）" and fx["to"] == f"{YZ} + 并行 {JC}", fx


def test_author_single_recipe_via_pool_recipes():
    """作者只勾一条：单条池子，没有 channels；来源「作者」；取货口、邻居推断都不再改它。"""
    res = _with_fix({"pool_recipes": {POOL_KEY: [JC]}})
    r = _row(res, "反应池", POOL)
    assert r["recipe"] == JC and "channels" not in r and r["pool_src"] == "作者" and r["need"] == {"清水": 30, "锦草粉末": 30}, r


def test_author_chain_becomes_one_net_recipe():
    """扩容反应池勾首尾相接的三条（液化息壤、壤晶废液、壤晶）：并成一条净配方，单条池子、没有 channels，原料里没有只在池内
    自产的液化息壤（原样当三条并行算，后两条会因为「缺液化息壤」没料：别人的武陵城扩容反应池 (1570,1196)）。
    三条各 ×1 时壤晶那条要 2 份壤晶废液、池内只产 1 份，净配方还差 1 份壤晶废液从外面进（pool_channels.composite 的口径），这里输入端送。"""
    import pool_channels as PC  # noqa: F401  （pool_model 用它并组）
    from bpa import pool_model as PM
    a, b, c = "清水×1 + 息壤×1 → 液化息壤×1（2秒）", "污水×1 + 液化息壤×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）", \
        "蓝铁粉末×1 + 壤晶废液×2 → 污水×1 + 壤晶×1（2秒）"
    net = PM.groups("扩容反应池", [a, b, c])
    assert len(net) == 1 and len(net[0]["members"]) == 3, net
    sc = {"devices": [("扩容反应池", (300, 300), None), ("灌装机", (650, 300), R["惰废瓶"])],
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "息壤", 30, 0), (3, (150, 420), "蓝铁粉末", 30, 0),
                    (4, (40, 380), "壤晶废液", 30, 0), (5, (800, 300), "赤铜瓶", 30, 1)],
          "links": [(0, 1, "pipe")]}
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = _scene(Path(td), sc)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": {"pool_recipes": {"扩容反应池@300,300": [c, a, b]}}},
                                                                ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    r = next(x for x in res["devices"] if x["device"] == "扩容反应池")
    assert r["recipe"] == net[0]["recipe"] and "channels" not in r and r["pool_src"] == "作者", r
    assert r["state"] != "配方未知" and "液化息壤" not in r["need"] and r["rate"] > 0.99, r
    assert [p["applied"] for p in _pool_cards(res)] == [True], res["pending"]


def test_author_says_unused():
    """作者说这台没在用（[]）：出一行「没在用（作者说）」，不算配方未知；不再点名它；卡片只有那一张（answer=[]、applied），
    run_base 不多跑第三遍（这一行不借「溢流位」前缀）。"""
    res, calls = _with_fix({"pool_recipes": {POOL_KEY: []}}, run_base=True)
    r = _row(res, "反应池", POOL)
    assert r["state"] == "没在用（作者说）" and r["rate"] == 0.0 and r["recipe"] == "" and "recipe_unknown" not in r, r
    assert r["pool_src"] == "作者", r
    st = res["summary"]["states"]
    assert st["配方未知"] == 0 and st["没在用"] == 1, st
    assert all("blocked_by_unknown" not in d for d in res["devices"])
    cards = _pool_cards(res)
    assert len(res["pending"]) == 1 and len(cards) == 1 and cards[0]["answer"] == [] and cards[0]["applied"] is True, res["pending"]
    assert True not in calls and len(calls) == 2, calls  # 只有主遍和「能送到多少」那一遍


def test_bad_pool_fix_is_ignored():
    """fixes.json 里的池子配方放不下（反应池 5 格，芽针溶液 + 赤铜溶液 6 种物品）：不用，写一条说明；这台照旧配方未知，
    卡片带着这个回答、applied=False（作者能看到没用上）。"""
    bad = [YZ, "赤铜粉末×1 + 沉积酸×1 → 赤铜溶液×1（2秒）"]
    res = _with_fix({"pool_recipes": {POOL_KEY: bad}})
    r = _row(res, "反应池", POOL)
    assert r["state"] == "配方未知", r
    assert any(n.get("kind") == "池子配方放不下" and "超过 5 格" in n["msg"] for n in res["notes"]), res["notes"]
    c = _pool_cards(res)[0]
    assert c["answer"] == bad and c["applied"] is False, c


def test_unused_row_and_importer():
    """「没在用（作者说）」这一行：作者再点「对」→ 进 device_designs（kinds=idle，note「作者说没在用」）；没点 → 不进 unconfirmed。"""
    res = _with_fix({"pool_recipes": {POOL_KEY: []}})
    r = _row(res, "反应池", POOL)
    ex = importer.expectations(res, {}, "测试")
    assert not any(x["name"] == "反应池" for x in ex["unconfirmed"] + ex["device_designs"]), ex
    ex = importer.expectations(res, {"devices": {P.device_key(r): {"verdict": "ok"}}}, "测试")
    dd = [x for x in ex["device_designs"] if x["name"] == "反应池"]
    assert len(dd) == 1 and dd[0]["kinds"] == ["idle"] and dd[0]["note"].startswith("作者说没在用"), ex["device_designs"]


def _api_on(td: Path, pool_recipe: str | None, fix: dict | None = None):
    """小据点算一遍、design.json 写进项目目录（P.BPA 指到临时目录），返回 api.Api()。设计分析表和行上「改配方」走的就是这两个接口。"""
    sys.path.insert(0, str(ROOT / "src" / "app"))
    import api as A
    bd, led, rec = _build(td, pool_recipe)
    if fix is not None:
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fix}, ensure_ascii=False), encoding="utf-8")
    res = D.analyze(bd, led, rec)
    (bd / "design.json").write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
    P.BPA = td
    return A.Api()


def test_api_table_and_row_card():
    """界面层（第 5 步）：bpa_design 的行带上池子的新字段（别的行不多键）；配方未知的行不算进确认的分母；
    行上「改配方」（bpa_pool_card）：有待确认卡的用那张（带进出连线），没有的按行上数据现拼（没有进出清单、先勾上现在的配方），
    都不进 pending、不改「待确认 N 处」。"""
    old = P.BPA
    try:
        with tempfile.TemporaryDirectory() as td:  # 配方未知：卡片就是待确认那张
            a = _api_on(Path(td), None)
            r = a.bpa_design("项目", "测试")
            row = next(d for d in r["devices"] if d["device"] == "反应池")
            assert row["recipe_unknown"] is True and row["pool_src"] == "不明" and row["candidates"], row
            assert not any(k in d for d in r["devices"] if d["device"] != "反应池" for k in ("pool_src", "channels", "members")), r["devices"]
            assert r["progress"]["devices"][1] == len(r["devices"]) - 1, r["progress"]  # 配方未知不算进分母
            n_pend = len(r["pending"])
            c = a.bpa_pool_card("项目", "测试", row["key"])
            assert c["ok"] and c["card"]["kind"] == "池子配方" and len(c["card"]["ins"]) == 3 and c["card"]["current"] == [], c
            assert len(c["card"]["options"]) == 11 and c["card"]["image"].startswith("data:image/jpeg"), c["card"].keys()
            assert len(a.bpa_design("项目", "测试")["pending"]) == n_pend
            assert not a.bpa_pool_card("项目", "测试", "灌装机@560,220")["ok"]  # 不是池子
        with tempfile.TemporaryDirectory() as td:  # 面板读的单条配方：没有待确认卡，现拼
            a = _api_on(Path(td), YZ)
            r = a.bpa_design("项目", "测试")
            row = next(d for d in r["devices"] if d["device"] == "反应池")
            assert row["pool_src"] == "面板" and "members" not in row and not [p for p in r["pending"] if p["kind"] == "池子配方"], row
            card = a.bpa_pool_card("项目", "测试", row["key"])["card"]
            assert card["from_row"] and card["ins"] == [] and card["current"] == [YZ] and card["answer"] is None, card
            assert card["src"] == "面板" and "面板" in card["msg"] and card["slots"] == 5, card["msg"]
        with tempfile.TemporaryDirectory() as td:  # 作者勾了两条并行：行上 channels 两条，卡片用待确认那张（answered）
            a = _api_on(Path(td), None, {"pool_recipes": {POOL_KEY: [YZ, JC]}})
            row = next(d for d in a.bpa_design("项目", "测试")["devices"] if d["device"] == "反应池")
            assert [c["recipe"] for c in row["channels"]] == [YZ, JC] and row["pool_src"] == "作者", row
            card = a.bpa_pool_card("项目", "测试", row["key"])["card"]
            assert card["answer"] == [YZ, JC] and card["applied"] is True and card["current"] == [YZ, JC] and len(card["outs"]) == 2, card
    finally:
        P.BPA = old


def test_members_of_net_recipe():
    """单条池子的行上只有净配方：界面悬停要看组成，按配方现查（别人的武陵城扩容反应池 (1570,1196) 作者勾的三条并成一条）。"""
    from bpa import pool_model as PM
    a, b, c = "清水×1 + 息壤×1 → 液化息壤×1（2秒）", "污水×1 + 液化息壤×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）", \
        "蓝铁粉末×1 + 壤晶废液×2 → 污水×1 + 壤晶×1（2秒）"
    net = PM.groups("扩容反应池", [c, a, b])[0]
    assert sorted(PM.members_of("扩容反应池", net["recipe"])) == sorted(net["members"]) == sorted([a, b, c]), net
    assert PM.members_of("扩容反应池", a) == (a,) and PM.members_of("反应池", "不是配方") == ()


if __name__ == "__main__":
    sys.exit(run([test_api_table_and_row_card, test_members_of_net_recipe, test_unknown_pool_card_and_candidates, test_author_two_recipes_run_parallel, test_author_single_recipe_via_pool_recipes,
                  test_author_chain_becomes_one_net_recipe, test_author_says_unused, test_bad_pool_fix_is_ignored, test_unused_row_and_importer,
                  test_unknown_pool_gets_a_row, test_summary_counts_unknown_separately, test_downstream_names_the_unknown_pool,
                  test_name_at_most_three, test_known_pool_unchanged, test_importer_skips_unknown_rows,
                  test_basic_parallel, test_chain_members, test_structural_cap_on_one_recipe, test_pool_and_plant_loop_share_water,
                  test_two_products_one_downstream, test_two_pools_feed_one_downstream, test_pool_into_pool,
                  test_three_pools_in_series, test_reverse_coupling, test_downstream_has_another_source,
                  test_downstream_alternating_device, test_two_pipes_to_one_device, test_through_buffers_and_splitters, test_three_recipes,
                  test_zero_other_material_does_not_cap, test_unknown_items_and_no_outlet, test_others_unaffected,
                  test_liquid_gas_converter_still_alternates, test_infer_by_neighbors_pool_vs_filler, test_author_single_recipe_clears_parallel,
                  test_downstream_stuck_line_skips_capped_pool_product, test_downstream_pool_shares_by_running_recipes,
                  test_pool_names_downstream_pool, test_capping_keeps_outer_converging, test_old_single_recipe_on_pool_not_silent]))
