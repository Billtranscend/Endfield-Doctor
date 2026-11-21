"""按邻居推配方不把兄弟设备的产物当原料、机身图标把关；池子待确认卡顺着配上对的暗管走（issue #1 第五轮续，2026-11-21）。
别人的武陵城天有洪炉 (698,582) 做息壤（机身图标息壤 0.69），和隔壁天有洪炉 (538,562) 共用一个清水分流器、息壤又送进同一个只进不出的
协议储存箱；design._infer_by_neighbors 两条路都把隔壁产的息壤算成它的原料，把它改成「壤晶废液 + 息壤 → 重息壤」，成了没料。
扩容反应池 (1798,454) 的污水从精炼炉经几对暗管过来，design._pool_links 碰到暗管就停，待确认卡片上写「物品不明」。
都是手搭的小物流图，直接调这两个函数；最后一条在临时目录里造小据点跑 design.analyze（机身图标的把握从配方表一路传到邻居推断）。
不依赖本机 debug 数据。"""
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
from bpa import recipe_icons as RI  # noqa: E402
from bpa import vision as V  # noqa: E402

XIRANG = "碳块×1 + 清水×1 → 息壤×1（2秒）"          # 天有洪炉做息壤（要稳定环境）
STABLE = "稳定碳块×2 + 清水×1 → 息壤×1（2秒）"       # 天有洪炉做息壤（普通状态）
HEAVY = "壤晶废液×5 + 息壤×10 → 重息壤×1（10秒）"    # 天有洪炉做重息壤
CARBON = "芽针×1 → 碳块×2（2秒）"                    # 精炼炉做碳块
SURE_ICON = ("息壤", 0.80, 0.30)                      # 认准了的机身图标（分 ≥0.55、领先 ≥0.25，和 recipe_icons 同一个门槛）
REAL_ICON = ("息壤", 0.69, 0.173)                     # 别人的武陵城那台天有洪炉的机身图标：息壤 0.69，领先重息壤 0.17（没认准）


class Net:
    """手搭的物流图：设备（名字、配方、是不是推断的）、物流件（分流器 / 汇流器 / 桥，带进出口的边）、连线。"""

    def __init__(self):
        self.devices, self.canvas, self.rec, self.nodes, self.links = [], [], {}, [], []

    def dev(self, name, recipe="", inferred=False):
        i = len(self.devices)
        self.devices.append({"name": name, "x": 100 * i, "y": 0})
        self.canvas.append((100.0 * i, 0.0))
        self.rec[i] = (recipe, inferred)
        return i

    def node(self, tp, **sides):
        """sides：分流器 in="S", outs=[…]；汇流器 out=…, ins=[…]；不给就是方向没认出来（或者是桥）。"""
        self.nodes.append(dict({"id": len(self.nodes), "type": tp, "c": [1000.0 + 50 * len(self.nodes), 500.0]}, **sides))
        return ("n", len(self.nodes) - 1)

    def _end(self, i):
        return {"name": self.devices[i]["name"], "c": list(self.canvas[i])}

    def link(self, a, b, layer="pipe", dir="a->b", a_side=None, b_side=None):
        """a、b：设备序号或 node() 返回的 ("n", id)；a_side / b_side：接在物流件的哪条边。"""
        lk = {"layer": layer, "dir": dir, "path": [[0, 0], [1, 1]], "basis": ["测试"], "jumps": []}
        if isinstance(a, int) and isinstance(b, int):
            lk.update(free=True, a=None, end="device", a_dev=self._end(a), device=self._end(b))
        elif isinstance(a, int):  # 设备 → 物流件：反过来记（物流件在 a 头），方向跟着反
            lk.update(a=b[1], a_side=b_side, end="device", device=self._end(a), dir={"a->b": "b->a", "b->a": "a->b"}.get(dir))
        elif isinstance(b, int):
            lk.update(a=a[1], a_side=a_side, end="device", device=self._end(b))
        else:
            lk.update(a=a[1], a_side=a_side, b=b[1], b_side=b_side, end="node")
        self.links.append(lk)

    def infer(self, icon=None, marks=(), alt=None):
        flow = {"nodes": self.nodes, "links": self.links}
        return D._infer_by_neighbors(flow, self.devices, self.canvas, self.rec, list(marks), alt={} if alt is None else alt, icon=icon)

    def gate(self, item):
        """输入端准入口（作者填了物品）→ (节点, 输入端标记)。"""
        g = self.node("准入口")
        return g, {"item": item, "p": list(self.nodes[g[1]]["c"])}


def _two_furnaces_on_a_splitter(sibling=True, oriented=True):
    """暗管出口 → 管道分流器（南进；东、北出）→ 东：天有洪炉 A（推断做息壤）、北：天有洪炉 B（配方可信，做息壤）。
    sibling=False：B 改接在分流器的进口上（B → 分流器 → A），B 产的是真送得到 A 的。oriented=False：分流器的进出口没认出来。"""
    n = Net()
    a = n.dev("天有洪炉", XIRANG, True)
    b = n.dev("天有洪炉", XIRANG)
    t = n.dev("暗管出口")
    s = n.node("管道分流器", **({"in": "S", "outs": ["E", "W", "N"]} if oriented else {}))
    n.link(s, a, a_side="E")
    if sibling:
        n.link(t, s, b_side="S")
        n.link(s, b, a_side="N")
    else:
        n.link(b, s, b_side="S")
        n.link(s, t, a_side="N")
    return n, a


def test_splitter_siblings_do_not_feed_each_other():
    """同一个分流器两个出口上的天有洪炉：B 产的息壤送不到 A，A 不改成吃息壤的重息壤（别人的武陵城 (698,582) 和 (538,562)）。"""
    n, a = _two_furnaces_on_a_splitter()
    assert n.infer() == [] and n.rec[a] == (XIRANG, True), n.rec[a]
    # 反例：B 接在分流器的进口上（B → 分流器 → A），B 的息壤真送得到 A → 照旧按邻居改成重息壤
    n, a = _two_furnaces_on_a_splitter(sibling=False)
    fx = n.infer()
    assert n.rec[a] == (HEAVY, True) and len(fx) == 1 and fx[0]["to"] == HEAVY, fx
    # 反例：分流器的进出口没认出来，分不出兄弟，照旧
    n, a = _two_furnaces_on_a_splitter(oriented=False)
    assert n.infer() and n.rec[a] == (HEAVY, True)


def test_unsure_junction_is_not_a_sibling_split():
    """分流器模板和明暗说法不一致（covered）、又没被小三角改过：朝向没认准，不分兄弟（照原来那样算）。"""
    n = {"type": "管道分流器", "in": "S", "outs": ["E", "W", "N"], "covered": True}
    assert D._junction_role(n, "E") is None and D._junction_role(n, "S") is None
    assert D._junction_role(dict(n, fixed_by_arrows=True), "E") == "out"
    assert D._junction_role(dict(n, covered=False), "S") == "in"


def test_merger_inlets_are_siblings():
    """同一个汇流器的两个进口：A、B 都往汇流器里送，B 产的到不了 A。反例：B 在汇流器出口那头（汇流器 → B 吃 A 的东西）不算兄弟。"""
    n = Net()
    a = n.dev("天有洪炉", XIRANG, True)
    b = n.dev("天有洪炉", XIRANG)
    m = n.node("管道汇流器", out="N", ins=["E", "W", "S"])
    n.link(a, m, layer="belt", b_side="E")
    n.link(b, m, layer="belt", b_side="W")
    assert n.infer() == [] and n.rec[a] == (XIRANG, True), n.rec[a]
    # 同一张图，A 接在汇流器的出口边上：B → 汇流器 → A，B 产的息壤送得到
    n.links[0] = dict(n.links[0], a_side="N", dir="a->b")
    assert n.infer() and n.rec[a] == (HEAVY, True)


def test_sink_box_inlets_are_siblings():
    """两台天有洪炉都往同一个协议储存箱里送息壤、箱子没有往外的线（只进不出）：互为兄弟。
    反例：有一根线方向没定，箱子不算只进不出，照旧穿过箱子（有出口的箱子是中转：景玉谷研磨机 → 储存箱 → 中容封装机）。"""
    n = Net()
    a = n.dev("天有洪炉", XIRANG, True)
    b = n.dev("天有洪炉", XIRANG)
    box = n.dev("协议储存箱")
    n.link(a, box, layer="belt")
    n.link(b, box, layer="belt")
    assert n.infer() == [] and n.rec[a] == (XIRANG, True), n.rec[a]
    n.links[1] = dict(n.links[1], dir=None)
    assert n.infer() and n.rec[a] == (HEAVY, True)


def test_both_sibling_paths_at_once():
    """别人的武陵城那台：清水分流器、只进不出的储存箱两条兄弟路同一深度都到 B；只堵一条另一条照样到。两条都是兄弟路才不改。"""
    n = Net()
    a = n.dev("天有洪炉", XIRANG, True)
    b = n.dev("天有洪炉", XIRANG)
    t = n.dev("暗管出口")
    box = n.dev("协议储存箱")
    s = n.node("管道分流器", **{"in": "S", "outs": ["E", "W", "N"]})
    n.link(t, s, b_side="S")
    n.link(s, a, a_side="E")
    n.link(s, b, a_side="N")
    n.link(a, box, layer="belt")
    n.link(b, box, layer="belt")
    assert n.infer() == [] and n.rec[a] == (XIRANG, True), n.rec[a]


def test_non_sibling_path_still_counts():
    """B 先从兄弟那一路（分流器两个出口，第 2 层）走到，又有一条不拐弯的路（B → 桥 → 桥 → A，第 3 层）：B 产的照常算。"""
    n, a = _two_furnaces_on_a_splitter()
    b = 1
    p, q = n.node("物流桥"), n.node("物流桥")
    n.link(b, p, layer="belt", b_side="W")
    n.link(p, q, layer="belt", a_side="E", b_side="W")
    n.link(q, a, layer="belt", a_side="E")
    assert n.infer() and n.rec[a] == (HEAVY, True), n.rec[a]


def test_sibling_gate_item_is_not_a_feed():
    """输入端准入口在兄弟那一路上（汇流器另一个进口接着准入口）也不算送到这台：准入口的清水往汇流器出口去，到不了往汇流器里送的 A。"""
    n = Net()
    a = n.dev("精炼炉", "砂叶×1 → 碳块×1（2秒）", True)
    m = n.node("汇流器", out="N", ins=["E", "W", "S"])
    g = n.node("准入口")
    n.link(a, m, layer="belt", b_side="E")
    n.link(g, m, layer="belt", a_side="E", b_side="W")
    flow = {"nodes": n.nodes, "links": n.links}
    marks = [{"item": "芽针", "p": list(n.nodes[g[1]]["c"])}]
    assert D._infer_by_neighbors(flow, n.devices, n.canvas, n.rec, marks, alt={}) == [], n.rec[a]
    # 反例：准入口接在汇流器出口那头往 A 送（A 接出口边）→ 芽针算送到，改成芽针那条
    n.links[0] = dict(n.links[0], a_side="N", dir="a->b")
    assert D._infer_by_neighbors(flow, n.devices, n.canvas, n.rec, marks, alt={}) and n.rec[a][0] == CARBON, n.rec[a]


def _heavy_supported():
    """天有洪炉 A（推断做息壤）的东边直接接着一台配方可信、做息壤的天有洪炉 B（B → A）：邻居说 A 是吃息壤的重息壤。"""
    n = Net()
    a = n.dev("天有洪炉", XIRANG, True)
    b = n.dev("天有洪炉", XIRANG)
    n.link(b, a, layer="belt")
    return n, a


def test_icon_guard_keeps_the_icon_product():
    """机身图标认准了息壤（分 ≥0.55、领先 ≥0.25）：邻居推断不换成产重息壤的配方。没认准（分 <0.55 或领先 <0.25，
    包括别人的武陵城那台 0.69 / 领先 0.17）照旧按邻居换：那个区间里图标会认错（低容 / 中容武陵电池），要靠邻居纠正。"""
    n, a = _heavy_supported()
    assert n.infer(icon={a: SURE_ICON}) == [] and n.rec[a] == (XIRANG, True), n.rec[a]
    assert D.ICON_GUARD_SCORE == RI.SURE_SCORE and D.ICON_GUARD_MARGIN == RI.SURE_MARGIN
    for weak in (("息壤", 0.54, 0.30), ("息壤", 0.80, 0.24), REAL_ICON):
        n, a = _heavy_supported()
        assert n.infer(icon={a: weak}) and n.rec[a] == (HEAVY, True), (weak, n.rec[a])
    n, a = _heavy_supported()  # 图标认的就是重息壤：换过去产物对得上，照换
    assert n.infer(icon={a: ("重息壤", 0.7, 0.3)}) and n.rec[a] == (HEAVY, True)
    n, a = _heavy_supported()  # 没有机身图标的（档案有配方表的据点）照旧
    assert n.infer() and n.rec[a] == (HEAVY, True)


def test_icon_guard_on_alternation():
    """邻居送来两种原料、两条配方打平（重息壤吃息壤、膨地啪吃液化息壤），本来按「两种配方轮流做」改；
    机身图标认准了息壤，两条都不产息壤 → 不改。图标认的是重息壤 → 照旧轮流做。"""
    def net():
        n = Net()
        a = n.dev("天有洪炉", XIRANG, True)
        b = n.dev("天有洪炉", XIRANG)
        g, mark = n.gate("液化息壤")
        n.link(b, a, layer="belt")
        n.link(g, a, a_side="E")
        return n, a, mark
    n, a, mark = net()
    alt = {}
    assert n.infer(icon={a: SURE_ICON}, marks=[mark], alt=alt) == [] and n.rec[a] == (XIRANG, True) and not alt, (n.rec[a], alt)
    n, a, mark = net()
    alt = {}
    fx = n.infer(icon={a: ("重息壤", 0.7, 0.3)}, marks=[mark], alt=alt)
    assert a in alt and " / 交替 " in fx[0]["to"] and {n.rec[a][0], alt[a][0]} == {HEAVY, "液化息壤×1 + 驮兽粪便×1 → 膨地啪×1（2秒）"}, fx


def test_icon_guard_allows_same_product_switch():
    """同一种产物换原料（稳定碳块 → 碳块，都产息壤）不受图标把关：旁边精炼炉送碳块，照换。"""
    n = Net()
    a = n.dev("天有洪炉", STABLE, True)
    c = n.dev("精炼炉", CARBON)
    n.link(c, a, layer="belt")
    fx = n.infer(icon={a: SURE_ICON})
    assert n.rec[a] == (XIRANG, True) and len(fx) == 1, fx


class _G:
    """design.Graph 的样子：out / inn 两张邻接表，边上带 link。"""

    def __init__(self):
        self.out, self.inn = defaultdict(list), defaultdict(list)

    def edge(self, u, v, **lk):
        lk = dict({"layer": "pipe", "path": [[0, 0], [1, 1]]}, **lk)
        self.out[u].append((v, lk))
        self.inn[v].append((u, lk))


REFINE = "赤铜矿×1 + 清水×1 → 赤铜块×1 + 污水×1（2秒）"   # 精炼炉做赤铜块，污水走管子
PURIFY = "惰性壤晶废液×4 → 清水×1 + 壤晶废液×1（2秒）"    # 提纯机


def _pool_net(paired=True, hops=6, feed=True, depth=12):
    """精炼炉 → hops 个物流件 → 暗管入口 ⇒（配对）暗管出口 → 扩容反应池；池子 → 暗管入口 ⇒ 暗管出口 → 提纯机。
    别人的武陵城扩容反应池 (1798,454)：右上进液口的污水从精炼炉 (1278,424) 经几对暗管、中间夹着管子和一个分流器过来。"""
    devices = [{"name": "扩容反应池"}, {"name": "暗管出口"}, {"name": "暗管入口"}, {"name": "精炼炉"},
               {"name": "暗管入口"}, {"name": "暗管出口"}, {"name": "提纯机"}]
    canvas = [(100.0 * k, 0.0) for k in range(len(devices))]
    G = _G()
    d = [("d", k) for k in range(len(devices))]
    G.edge(d[1], d[0])
    if paired:
        G.edge(d[2], d[1], tunnel=True)
        G.edge(d[4], d[5], tunnel=True)
    if feed:
        prev = d[3]
        for k in range(hops):
            G.edge(prev, ("n", k))
            prev = ("n", k)
        G.edge(prev, d[2])
    G.edge(d[0], d[4])
    G.edge(d[5], d[6])
    known = {3, 6}
    rec = {3: REFINE, 6: PURIFY}
    return D._pool_links(0, G, devices, canvas, lambda j: [rec[j]], known, {}, {}, depth=depth)


def test_pool_card_follows_paired_tunnels():
    """配上对的暗管当中转：进料管通到精炼炉（污水），出料管通到提纯机（吃惰性壤晶废液），都不再是物品不明。
    中间 6 个物流件：暗管出口、入口各占一层，原来的 6 层走不到精炼炉，层数 12 才够。"""
    ins, outs = _pool_net()
    assert ins["dir"] == "in" and ins["sup"] == ["污水"] and not ins["wild_in"] and ins["ends"] == ["精炼炉"], ins
    assert outs["dir"] == "out" and outs["dem"] == ["惰性壤晶废液"] and not outs["wild_out"] and outs["ends"] == ["提纯机"], outs


def test_pool_card_tunnel_without_pair_or_feed_is_unclear():
    """没配对的暗管照旧物品不明，记暗管名；配上了对、可对面的暗管入口什么都没接，也是物品不明（记对面那台）。"""
    ins, outs = _pool_net(paired=False)
    assert ins["wild_in"] and ins["sup"] == [] and ins["ends"] == ["暗管出口"], ins
    assert outs["wild_out"] and outs["ends"] == ["暗管入口"], outs
    ins, _ = _pool_net(feed=False)
    assert ins["wild_in"] and ins["sup"] == [] and ins["ends"] == ["暗管入口"], ins


def test_pool_card_tunnel_runs_out_of_depth_is_unclear():
    """穿过配对暗管以后层数用完还没走到设备：物品不明，记穿过的暗管（原来既不记不明也不记终点，打分按「解释不了」扣分）。"""
    ins, _ = _pool_net(hops=20)
    assert ins["wild_in"] and ins["sup"] == [] and ins["ends"] == ["暗管出口"], ins
    ins, _ = _pool_net(hops=6, depth=12)  # 走得到：照常
    assert not ins["wild_in"] and ins["ends"] == ["精炼炉"], ins


def _scene(td: Path, icon: tuple):
    """临时小据点：天有洪炉 A（机身图标推的配方：做息壤，推断）东边接着天有洪炉 B（认准了做息壤，B → A 一条带子）。
    配方表只有 recipe_icons.derive 那种行（带 product / score / margin），远景坐标 = 画布坐标。"""
    proj = td / "项目"
    bd = proj / "base" / "测试"
    bd.mkdir(parents=True)
    pa, pb = (300, 300), (500, 300)
    flow = {"nodes": [], "issues": [], "structures": [], "links": [
        {"layer": "belt", "free": True, "a": None, "end": "device", "dir": "a->b", "path": [list(pb), list(pa)], "basis": ["合成"],
         "jumps": [], "a_dev": {"name": "天有洪炉", "c": list(pb)}, "device": {"name": "天有洪炉", "c": list(pa)}}]}
    (bd / "flow.json").write_text(json.dumps(flow, ensure_ascii=False), encoding="utf-8")
    (bd / "transform.json").write_text(json.dumps({"K": 1.0, "F0": [0, 0], "bps": {}}), encoding="utf-8")
    V.wr(bd / "composite.png", np.full((700, 800, 3), 232, np.uint8))
    (proj / "inputs.json").write_text(json.dumps({"测试": {"marks": []}}, ensure_ascii=False), encoding="utf-8")
    led = {"devices": [{"name": "天有洪炉", "x": pa[0], "y": pa[1]}, {"name": "天有洪炉", "x": pb[0], "y": pb[1]}]}
    recipes = [{"name": "天有洪炉", "x": pa[0], "y": pa[1], "recipe": XIRANG, "inferred": True,
                "product": icon[0], "score": icon[1], "margin": icon[2]},
               {"name": "天有洪炉", "x": pb[0], "y": pb[1], "recipe": XIRANG, "inferred": False,
                "product": "息壤", "score": 0.8, "margin": 0.34}]
    return bd, led, recipes


def test_analyze_passes_body_icon_to_neighbors():
    """design.analyze 从配方表里机身图标那几列（recipe_icons.derive 写的 product / score / margin）取把握，交给邻居推断：
    认准了息壤的那台不改成重息壤；把握不够的照旧改。"""
    for icon, want in ((SURE_ICON, XIRANG), (REAL_ICON, HEAVY), (("息壤", 0.69, 0.05), HEAVY)):
        with tempfile.TemporaryDirectory() as td:
            bd, led, recipes = _scene(Path(td), icon)
            res = D.analyze(bd, led, recipes)
        row = next(r for r in res["devices"] if (r["x"], r["y"]) == (300, 300))
        assert row["recipe"] == want, (icon, row["recipe"], res.get("recipe_fixes"))


if __name__ == "__main__":
    sys.exit(run([v for k, v in list(globals().items()) if k.startswith("test_")]))
