"""池子出口送什么（0.10.13 功能三，fixes.json 的 pool_outlets）：作者说池子出口送的是原料时，到手的原料一部分从出口转给下游，
下游先拿、池子用剩下的（issue #1 作者实测：扩容反应池 (1798,454) 带出口送息壤给天有洪炉，重息壤 2.14/分）。
合成小据点，走 run_base（「卡在」是它补的、第二遍要用主遍的比例），不读任何本机实测数据。

场景 A（作者那组的最小复刻）：扩容反应池勾液化息壤、壤晶废液两条（首尾相接，并成一条净配方），清水 / 污水 / 息壤各 30 进池子；
带出口 → 天有洪炉（息壤 60 + 壤晶废液 30 → 重息壤 6/分）；管出口经汇流器 → 天有洪炉；管出口 → 提纯机（惰性壤晶废液 120 →
清水 30 + 壤晶废液 30）→ 汇流器。手算：池子 r、洪炉 f，息壤 60f + 30r = 30，壤晶废液 30f = 30r + 7.5r → r = 2/7、f = 5/14。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src"), str(ROOT / "src" / "tools")]

from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
import test_pool_parallel as TP  # noqa: E402

YH = "清水×1 + 息壤×1 → 液化息壤×1（2秒）"
FY = "污水×1 + 液化息壤×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）"
JC = "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"
HL = "壤晶废液×5 + 息壤×10 → 重息壤×1（10秒）"
TC = "惰性壤晶废液×4 → 清水×1 + 壤晶废液×1（2秒）"
PK = "扩容反应池@300,300"
AUTHOR = {"belt": ["息壤"], "pipe": ["惰性壤晶废液", "壤晶废液"]}


def _scene_a(purifier=True, extra_waste=None, outlet_to_store=False):
    """场景 A。extra_waste：天有洪炉另有一个壤晶废液输入端；outlet_to_store：带出口改接仓库存货口（不接天有洪炉）。"""
    devs = [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), HL), ("提纯机", (300, 650), TC),
            ("仓库存货口", (900, 300), None)]
    gates = [(1, (40, 250), "清水", 30, 0), (2, (40, 350), "污水", 30, 0), (3, (150, 150), "息壤", 30, 0)]
    links = [(0, 1, "belt"), (0, "m", "pipe"), ("m", 1, "pipe"), (0, 2, "pipe"), (1, 3, "belt")]
    if purifier:
        links.append((2, "m", "pipe"))
    if extra_waste is not None:
        gates.append((4, (650, 100), "壤晶废液", extra_waste, 1))
    if outlet_to_store:
        devs.append(("仓库存货口", (300, 900), None))
        links = [lk for lk in links if lk[:3] != (0, 1, "belt")] + [(0, 4, "belt")]
    return {"devices": devs, "nodes": {"m": ("管道汇流器", (500, 200))}, "gates": gates, "links": links}


def _go(sc, outlets=None, recipes=(YH, FY), spy=None):
    """场景 sc + 作者勾的池子配方 + 出口回答 → run_base 的结果 {坐标 "x,y": 行, "res": 结果}。场景里的准入口限速写进档案
    （run_base 从档案读）。spy：列表，记下 run_base 每次调 analyze 的参数和结果顶层的 pass_split。"""
    fx = {"pool_recipes": {PK: list(recipes)}}
    if outlets is not None:
        fx["pool_outlets"] = {PK: outlets}
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec, gl = TP._scene(td, sc)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fx}, ensure_ascii=False), encoding="utf-8")
        arch = td / "档案"
        (arch / "ledger" / "gate_limits").mkdir(parents=True)
        (arch / "ledger" / "测试.json").write_text(json.dumps(led, ensure_ascii=False), encoding="utf-8")
        (arch / "ledger" / "测试_recipes.json").write_text(json.dumps({"devices": rec}, ensure_ascii=False), encoding="utf-8")
        if sc.get("limits"):
            (arch / "ledger" / "gate_limits" / "测试.json").write_text(json.dumps(gl, ensure_ascii=False), encoding="utf-8")
        old = D.analyze
        if spy is not None:
            def counted(*a, **k):
                r = old(*a, **k)
                spy.append((dict(k), r.get("pass_split")))
                return r
            D.analyze = counted
        try:
            res = D.run_base(bd, arch, "测试")
        finally:
            D.analyze = old
    return dict({f"{r['x']},{r['y']}": r for r in res["devices"]}, res=res)


_CACHE = {}


def _author():
    """场景 A + 作者的回答，算一次，几条测试共用（同时记下 run_base 怎么调 analyze）。"""
    if "a" not in _CACHE:
        spy = []
        _CACHE["a"] = _go(_scene_a(), AUTHOR, spy=spy)
        _CACHE["spy"] = spy
    return _CACHE["a"]


def _r(res, xy):
    return res[xy]["rate"]


def _kinds(res):
    return [n["kind"] for n in res["res"]["notes"]]


def test_author_case():
    """1. 作者的回答（带出口送息壤、两个管出口送两种废液）：池子 2/7、天有洪炉 5/14（重息壤 6 × 5/14 = 2.14/分，和作者实测一样）、
    提纯机 1/14；息壤到手 30，转给下游 150/7 = 21.43；池子依据第一句是 run_base 补的「卡在：息壤」，第二句是转送那句
    （写「作者说的」、规则来源，不写「你」）；外层没跑满。"""
    res = _author()
    assert abs(_r(res, "300,300") - 2 / 7) < 2e-3, res["300,300"]
    assert abs(_r(res, "650,300") - 5 / 14) < 2e-3, res["650,300"]
    assert abs(_r(res, "300,650") - 1 / 14) < 2e-3, res["300,650"]
    p = res["300,300"]
    po = p["pass_out"]
    assert len(po) == 1 and po[0]["item"] == "息壤" and po[0]["layer"] == "belt", po
    assert abs(po[0]["arrive"] - 30) < 0.01 and abs(po[0]["out"] - 150 / 7) < 0.02 and abs(po[0]["own"] - 60 / 7) < 0.02, po
    assert abs(po[0]["share"] - 5 / 7) < 2e-3, po
    assert p["basis"][0].startswith("卡在：息壤（能送到 8.57"), p["basis"][:2]  # 第二遍用了主遍的比例：池子自己只拿到 8.57
    assert p["basis"][1].startswith("带出口送息壤（作者说的）：息壤到手 30/分，从出口转给下游 21.4，池子自己用 8.57"), p["basis"][1]
    assert D.PT_RULE in p["basis"][1], p["basis"][1]
    assert p["got"]["息壤"] == round(60 / 7, 3), p["got"]
    hl = res["650,300"]
    assert any(b.startswith("扩容反应池从带出口转送 息壤") for b in hl["basis"]), hl["basis"]
    top = res["res"]
    assert abs(top["pass_split"][f"{PK}|息壤"] - 5 / 7) < 2e-3, top.get("pass_split")
    assert not top["iters"]["hit_cap"] and 2 <= top["iters"]["outer"] <= D.PT_OUTER_MAX, top["iters"]
    assert not any(n["kind"].startswith("出口") or n["kind"] == "产物没出口" for n in top["notes"]), top["notes"]


def test_run_base_passes_split():
    """run_base 先跑主遍，第二遍（能送到多少）、第三遍（主路堵死）用主遍定下的比例（直接用、不再定）。"""
    _author()
    spy = _CACHE["spy"]
    assert spy[0][0] == {} and spy[0][1], spy  # 主遍先跑、自己定比例
    assert len(spy) >= 2 and spy[1][0].get("rate_limited_intake") is False, spy
    for k, ps in spy[1:]:
        assert k.get("pass_split") == spy[0][1] and ps == spy[0][1], (k, ps, spy[0][1])


def test_belt_only_answer_same():
    """2. 只说了带出口（管出口没说）：结果和第 1 条一样。"""
    a, b = _author(), _go(_scene_a(), {"belt": ["息壤"]})
    for xy in ("300,300", "650,300", "300,650"):
        assert abs(_r(a, xy) - _r(b, xy)) < 1e-3, (xy, _r(a, xy), _r(b, xy))
    assert b["300,300"]["pass_out"] == a["300,300"]["pass_out"], (a["300,300"]["pass_out"], b["300,300"]["pass_out"])


def test_no_answer_unchanged():
    """3. 没回答：和原来一样，池子满速、天有洪炉没料（息壤 0）；design.json 里没有 pass_out、pass_split，run_base 不带 pass_split。"""
    spy = []
    res = _go(_scene_a(), spy=spy)
    assert _r(res, "300,300") == 1.0 and _r(res, "650,300") == 0.0, (res["300,300"]["state"], res["650,300"]["state"])
    assert "pass_split" not in res["res"] and not any("pass_out" in r for r in res["res"]["devices"])
    assert all("pass_split" not in k for k, _ in spy) and res["res"]["iters"]["outer"] == 0, (spy, res["res"]["iters"])


def test_no_purifier():
    """4. 没有提纯机补壤晶废液：天有洪炉的壤晶废液全靠池子，30f = 30r、60f + 30r = 30 → 两台都是 1/3。"""
    res = _go(_scene_a(purifier=False), {"belt": ["息壤"]})
    assert abs(_r(res, "300,300") - 1 / 3) < 2e-3 and abs(_r(res, "650,300") - 1 / 3) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))


def test_downstream_other_source():
    """5. 天有洪炉另有 60 壤晶废液：它不缺壤晶废液、要 60 息壤，先拿走全部 30 → 池子 0（没料，依据写「转给下游 30」），洪炉 0.5。"""
    res = _go(_scene_a(extra_waste=60), {"belt": ["息壤"]})
    assert _r(res, "300,300") == 0.0 and abs(_r(res, "650,300") - 0.5) < 2e-3, (res["300,300"], res["650,300"])
    assert any("转给下游 30，池子自己用 0" in b for b in res["300,300"]["basis"]), res["300,300"]["basis"]


def test_outlet_to_storage():
    """6. 带出口接的是仓库存货口：下游吃多少不知道，出口按满带送 → 息壤全转走，池子 0。"""
    res = _go(_scene_a(outlet_to_store=True), {"belt": ["息壤"]})
    assert _r(res, "300,300") == 0.0 and res["300,300"]["pass_out"][0]["out"] == 30.0, res["300,300"]


def test_intermediate_and_missing_product_notes():
    """7. 带出口什么都不送、管出口选中间产物液化息壤和惰性壤晶废液：中间产物不会算、写说明；三个出口都说了、壤晶废液没出口：
    写说明。数字和没回答一样；说明带 key（界面按 key 挂到这台的卡上），不写「你」。"""
    res = _go(_scene_a(), {"belt": [], "pipe": ["液化息壤", "惰性壤晶废液"]})
    ns = [n for n in res["res"]["notes"] if n["kind"] in ("出口送中间产物", "产物没出口")]
    assert sorted(n["kind"] for n in ns) == ["产物没出口", "出口送中间产物"], _kinds(res)
    assert all(n["key"] == PK and "你" not in n["msg"] for n in ns), ns
    assert "液化息壤" in next(n["msg"] for n in ns if n["kind"] == "出口送中间产物")
    assert "壤晶废液三个出口都没选" in next(n["msg"] for n in ns if n["kind"] == "产物没出口")
    assert _r(res, "300,300") == 1.0 and _r(res, "650,300") == 0.0, res["300,300"]
    assert "pass_split" not in res["res"] and "pass_out" not in res["300,300"]


def test_not_in_pool_ignored():
    """8. 出口选的东西不在池里（锦草粉末）、液体选在带出口上：没用上，写带 key 的说明，数字不变。"""
    res = _go(_scene_a(), {"belt": ["锦草粉末"], "pipe": ["息壤"]})
    ns = [n for n in res["res"]["notes"] if n["kind"] == "出口回答没用上"]
    assert len(ns) == 2 and all(n["key"] == PK for n in ns), ns
    assert any("锦草粉末不在这台池子在跑的配方里" in n["msg"] for n in ns) and any("息壤是固体，走不了管出口" in n["msg"] for n in ns), ns
    assert _r(res, "300,300") == 1.0 and "pass_out" not in res["300,300"]


def test_parallel_pool_with_pass():
    """9. 并行池子（液化息壤、锦草溶液两条）+ 带出口送息壤，天有洪炉只有 6 壤晶废液：它只能跑 20%、拿 12 息壤 →
    液化息壤那条 18/30 = 60%、锦草那条满速。"""
    sc = {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), HL), ("仓库存货口", (900, 300), None),
                      ("仓库存货口", (300, 900), None)],
          "gates": [(1, (40, 250), "清水", 60, 0), (2, (40, 350), "锦草粉末", 30, 0), (3, (150, 150), "息壤", 30, 0),
                    (4, (650, 100), "壤晶废液", 6, 1)],
          "links": [(0, 1, "belt"), (1, 2, "belt"), (0, 3, "pipe")]}
    res = _go(sc, {"belt": ["息壤"]}, recipes=(YH, JC))
    ch = res["300,300"]["channels"]
    assert abs(ch[0]["rate"] - 0.6) < 2e-3 and ch[1]["rate"] == 1.0, ch
    assert abs(_r(res, "650,300") - 0.2) < 2e-3, res["650,300"]
    assert abs(res["300,300"]["pass_out"][0]["out"] - 12) < 0.05 and not res["res"]["iters"]["hit_cap"], res["300,300"]["pass_out"]


def test_order_independent():
    """10. 设备、节点、输入端、连线倒过来排：结果一样。"""
    a, b = _author(), _go(TP._shuffled(_scene_a()), AUTHOR)
    for xy in ("300,300", "650,300", "300,650"):
        assert abs(_r(a, xy) - _r(b, xy)) < 1e-3, (xy, _r(a, xy), _r(b, xy))


def test_outlet_loops_back():
    """11. 带出口经分流器一路去天有洪炉、一路绕回池子自己：绕回的那路不算下游（不当不知道）。出口带子满 30、分流器对半分，
    15 回池子、15 给天有洪炉 → 池子到手 45、自己用 15（0.5），洪炉 15/60（0.25）；收敛、不跑满。"""
    sc = _scene_a()
    sc["nodes"] = dict(sc["nodes"], s=("分流器", (500, 420)))
    sc["links"] = [lk for lk in sc["links"] if lk[:3] != (0, 1, "belt")] + [(0, "s", "belt"), ("s", 1, "belt"), ("s", 0, "belt")]
    res = _go(sc, {"belt": ["息壤"]})
    assert abs(_r(res, "300,300") - 0.5) < 2e-3 and abs(_r(res, "650,300") - 0.25) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))
    po = res["300,300"]["pass_out"][0]
    assert abs(po["out"] - 30) < 0.05 and abs(po["arrive"] - 45) < 0.05 and not res["res"]["iters"]["hit_cap"], (po, res["res"]["iters"])


def test_gate_limit_on_outlet():
    """带出口到天有洪炉中间有准入口限速 12：下游实际吃得下的按准入口算（_take_at 走 gate_pass）→ 只转 12，池子 18/30 = 0.6，
    洪炉 12/60 = 0.2。不按准入口算的话池子会把息壤多转出去、又送不过去。"""
    sc = _scene_a()
    sc["nodes"] = dict(sc["nodes"], q=("准入口", (480, 300)))
    sc["links"] = [lk for lk in sc["links"] if lk[:3] != (0, 1, "belt")] + [(0, "q", "belt"), ("q", 1, "belt")]
    sc["limits"] = {"q": 12}
    res = _go(sc, {"belt": ["息壤"]})
    assert abs(_r(res, "300,300") - 0.6) < 2e-3 and abs(_r(res, "650,300") - 0.2) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))
    assert abs(res["300,300"]["pass_out"][0]["out"] - 12) < 0.05, res["300,300"]["pass_out"]


def test_downstream_other_material_not_capped_while_solving():
    """定比例那几趟，天有洪炉算「别的原料够跑多快」时不把从池子出口转来的息壤算进去（pt_capping）。提纯机的壤晶废液另有一根管子
    接仓库存货口（作者那台提纯机也是两个出口、「出料口主路 1/2」）：不这样的话洪炉因为息壤少、分流时不收提纯机的壤晶废液，
    息壤又因此给得少，收敛到「池子 1/3、洪炉 1/3」这个错的自洽解；对的是和场景 A 一样的 2/7、5/14。"""
    sc = _scene_a()
    sc["devices"] = sc["devices"] + [("仓库存货口", (300, 900), None)]
    sc["links"] = sc["links"] + [(2, 4, "pipe")]
    res = _go(sc, {"belt": ["息壤"]})
    assert abs(_r(res, "300,300") - 2 / 7) < 2e-3 and abs(_r(res, "650,300") - 5 / 14) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))
    assert not res["res"]["iters"]["hit_cap"], res["res"]["iters"]


def test_forwarded_material_from_first_round():
    """转送的料第一轮就按池子满速用量的 φ 成往下推（_reset 里的 fwd）。池子的壤晶废液经分流器分给天有洪炉和一台灌装机：
    手算池子 r = 1 − 2f、洪炉拿分流器的一半 15r → f = r/2 → r = 0.5、f = 0.25、灌装机 0.25，比例 0.5 一轮就定。
    原来转送从 0 开始时，比例 0.5 那趟内层第二轮就在「洪炉 0」上假收敛（洪炉第二轮限吃量用的还是第一轮的 0 息壤），外层 16 轮跑满。"""
    fl = "赤铜瓶×1 + 壤晶废液×1 → 赤铜瓶(壤晶废液)×1（2秒）"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), HL), ("灌装机", (650, 600), fl),
                      ("仓库存货口", (900, 300), None), ("仓库存货口", (900, 600), None), ("仓库存货口", (300, 900), None)],
          "nodes": {"s": ("管道分流器", (450, 450))},
          "gates": [(1, (40, 250), "清水", 30, 0), (2, (40, 350), "污水", 30, 0), (3, (150, 150), "息壤", 30, 0),
                    (4, (650, 800), "赤铜瓶", 30, 2)],
          "links": [(0, 1, "belt"), (0, "s", "pipe", ["壤晶废液"]), ("s", 1, "pipe"), ("s", 2, "pipe"), (1, 3, "belt"), (2, 4, "belt"),
                    (0, 5, "pipe", ["惰性壤晶废液"])]}
    res = _go(sc, {"belt": ["息壤"]})
    got = [_r(res, xy) for xy in ("300,300", "650,300", "650,600")]
    assert all(abs(a - b) < 2e-3 for a, b in zip(got, [0.5, 0.25, 0.25])), got
    it = res["res"]["iters"]
    assert not it["hit_cap"] and it["outer"] <= 2, it


def test_stale_key_and_unknown_recipe_noted():
    """出口回答的键在底账里找不到池子（底账重扫过）、池子配方还不知道：写带 key 的说明，不静默丢掉；数字和没回答一样。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = TP._scene(Path(td), _scene_a())
        fx = {"pool_outlets": {"扩容反应池@1,2": {"belt": ["息壤"]}, PK: {"belt": ["息壤"]}}}  # 没有 pool_recipes：配方未知
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fx}, ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    ns = {n["key"]: n["msg"] for n in res["notes"] if n["kind"] == "出口回答没用上"}
    assert "找不到这台池子" in ns.get("扩容反应池@1,2", "") and "(1,2)" in ns["扩容反应池@1,2"], ns
    assert "配方还不知道" in ns.get(PK, ""), ns
    assert "pass_split" not in res and all("你" not in m for m in ns.values()), ns


def _main(sc, fx):
    """只跑 analyze 主遍（开工率、转送比例都是主遍定的；用不到 run_base 补的「卡在」和第二、三遍时用它，快）。
    fx：这个据点的 fixes 段 → {坐标 "x,y": 行, "res": 结果}。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl = TP._scene(Path(td), sc)
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fx}, ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    return dict({f"{r['x']},{r['y']}": r for r in res["devices"]}, res=res)


def test_shallow_downstream_slope_uses_secant():
    """下游要的原料比到手的少（下游吃量随比例线性下降、斜率在 −1 到 0 之间）：夹逼区间两端都有了就用割线，几轮到根、不跑满。
    天有洪炉换成合成配方「息壤 27 + 液化息壤 30 → 重息壤」，液化息壤全靠池子的管出口：转出比例 φ，池子 1 − φ，
    30φ = 27(1 − φ) → φ = 9/19、池子 10/19。只先试「下游吃得下 / 到手」时每轮误差只乘 0.9，16 轮跑满、写「出口转送没收敛」。"""
    hl = "息壤×9 + 液化息壤×10 → 重息壤×1（20秒）"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), hl), ("仓库存货口", (900, 300), None)],
          "gates": [(1, (40, 250), "清水", 30, 0), (3, (150, 150), "息壤", 30, 0)],
          "links": [(0, 1, "belt"), (0, 1, "pipe"), (1, 2, "belt")]}
    res = _main(sc, {"pool_recipes": {PK: [YH]}, "pool_outlets": {PK: {"belt": ["息壤"]}}})
    top = res["res"]
    assert abs(top["pass_split"][f"{PK}|息壤"] - 9 / 19) < 1e-3, top["pass_split"]
    assert abs(_r(res, "300,300") - 10 / 19) < 2e-3 and abs(_r(res, "650,300") - 10 / 19) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))
    assert not top["iters"]["hit_cap"] and top["iters"]["outer"] <= 4 and "出口转送没收敛" not in _kinds(res), top["iters"]


def test_downstream_takes_nothing():
    """下游一点都吃不下（池子只勾液化息壤、不出壤晶废液，天有洪炉没有壤晶废液）：比例收到 0（「下游吃得下 / 到手」= 0 落在
    区间端点上也直接用，不对半逼近到 1/1024、凭空转出 0.03/分），池子满速；每台的开工率、状态、到手都和没回答时一样
    （按修正重算前后比对时不会把下游当成结果变了、误撤它的「对」）。"""
    sc, fx = _scene_a(purifier=False), {"pool_recipes": {PK: [YH]}}
    a, b = _main(sc, fx), _main(sc, dict(fx, pool_outlets={PK: {"belt": ["息壤"]}}))
    assert b["res"]["pass_split"][f"{PK}|息壤"] == 0.0 and b["300,300"]["pass_out"][0]["out"] == 0.0, b["300,300"].get("pass_out")
    for r in a["res"]["devices"]:
        xy = f"{r['x']},{r['y']}"
        assert (b[xy]["rate"], b[xy]["state"], b[xy]["got"]) == (r["rate"], r["state"], r["got"]), (xy, b[xy]["state"], r["state"])
    assert not b["res"]["iters"]["hit_cap"] and b["res"]["iters"]["outer"] <= 3, b["res"]["iters"]


def test_outlet_loops_back_downstream_not_full():
    """带出口经分流器一路回池子自己、一路去天有洪炉，洪炉吃不满一条带（壤晶废液只有输入端 5/分，最多吃 10 息壤）：
    按「实际交给下游的」（转出的减去绕回自己的）去比下游吃得下的 → 洪炉拿 10（1/6），池子用剩下的 20（2/3）。
    只按转出的比时比例停在「转出 = 10」，分流器对半分，洪炉只拿到 5、池子 0.833，违反「下游先拿」。
    pass_out 带 back（绕回自己的），依据写清转出里绕回自己和给下游各多少。"""
    sc = _scene_a(purifier=False)
    sc["nodes"] = dict(sc["nodes"], s=("分流器", (500, 420)))
    sc["devices"] = sc["devices"] + [("仓库存货口", (300, 900), None)]
    sc["links"] = [lk for lk in sc["links"] if lk[:3] not in ((0, 1, "belt"), ("m", 1, "pipe"))] + \
        [(0, "s", "belt"), ("s", 1, "belt"), ("s", 0, "belt"), ("m", 4, "pipe")]
    sc["gates"] = sc["gates"] + [(4, (650, 100), "壤晶废液", 5, 1)]
    res = _main(sc, {"pool_recipes": {PK: [YH, FY]}, "pool_outlets": {PK: {"belt": ["息壤"]}}})
    assert abs(_r(res, "300,300") - 2 / 3) < 2e-3 and abs(_r(res, "650,300") - 1 / 6) < 2e-3, (_r(res, "300,300"), _r(res, "650,300"))
    po = res["300,300"]["pass_out"][0]
    assert abs(po["out"] - po["back"] - 10) < 0.05 and abs(po["own"] - 20) < 0.05, po
    assert any("绕回这台自己" in b and "给下游 10" in b for b in res["300,300"]["basis"]), res["300,300"]["basis"]
    assert not res["res"]["iters"]["hit_cap"], res["res"]["iters"]


def test_two_pools_same_downstream():
    """两台池子的带出口都送息壤给同一台天有洪炉，洪炉的壤晶废液只有 15、只吃得下 30 息壤：给一台定比例时，另一台转来的算
    「别处来的」（按池子分账）→ 两台各转 15、各 0.5，洪炉 0.5。不分账时两台都按洪炉的全部吃量转，都是 1.0、池子都没料。"""
    k2 = "扩容反应池@300,600"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("扩容反应池", (300, 600), None), ("天有洪炉", (650, 450), HL),
                      ("仓库存货口", (900, 450), None), ("仓库存货口", (100, 900), None)],
          "gates": [(1, (40, 250), "清水", 30, 0), (2, (150, 150), "息壤", 30, 0), (3, (40, 550), "清水", 30, 1),
                    (4, (150, 700), "息壤", 30, 1), (5, (650, 100), "壤晶废液", 15, 2)],
          "links": [(0, 2, "belt"), (1, 2, "belt"), (2, 3, "belt"), (0, 4, "pipe"), (1, 4, "pipe")]}
    res = _main(sc, {"pool_recipes": {PK: [YH], k2: [YH]}, "pool_outlets": {PK: {"belt": ["息壤"]}, k2: {"belt": ["息壤"]}}})
    got = [_r(res, xy) for xy in ("300,300", "300,600", "650,450")]
    assert all(abs(a - 0.5) < 2e-3 for a in got), got
    assert abs(res["650,450"]["got"]["息壤"] - 30) < 0.05 and not res["res"]["iters"]["hit_cap"], (res["650,450"]["got"], res["res"]["iters"])


def test_upstream_not_backpressured_by_own_use():
    """上游是生产设备时，池子能出去的 = 自己用的 + 出口下游的（drain 包装）。提纯机产 60 清水进池子，池子用 30、管出口送清水
    给灌装机 30：提纯机满速。只按池子自己的用量算背压时提纯机被压到 50%，池子把到手的 30 全转走、自己没料。"""
    qs, gz = "污水×1 → 清水×1（1秒）", "清水×1 → 测试品×1（2秒）"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("提纯机", (100, 600), qs), ("灌装机", (650, 300), gz),
                      ("仓库存货口", (300, 900), None), ("仓库存货口", (900, 300), None)],
          "gates": [(1, (40, 650), "污水", 60, 1), (3, (150, 150), "息壤", 30, 0)],
          "links": [(1, 0, "pipe"), (0, 2, "pipe", ["清水"]), (0, 3, "pipe", ["液化息壤"]), (2, 4, "belt")]}
    res = _main(sc, {"pool_recipes": {PK: [YH]}, "pool_outlets": {PK: {"pipe": ["清水"]}}})
    got = [_r(res, xy) for xy in ("100,600", "300,300", "650,300")]
    assert got == [1.0, 1.0, 1.0], got
    assert abs(res["300,300"]["pass_out"][0]["out"] - 30) < 0.05, res["300,300"]["pass_out"]


def test_upstream_split_counts_outlet_without_self():
    """上游分流器问池子能吸收多少：自己用的 + 出口下游吃得下的，出口绕回池子自己的那路算 0（absorb 包装；按「不知道」算会把
    一整条带都算给池子）。息壤 30 经分流器分给池子和研磨机；池子只有 6 清水（自己吃 6 息壤），带出口经分流器一路回自己、
    一路去天有洪炉（壤晶废液 1.5，吃 3 息壤）→ 池子那路吸收 9，研磨机拿 21（0.7）；算成不知道时两路对半，研磨机 0.5。"""
    ym = "息壤×1 → 测试品×1（2秒）"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), HL), ("研磨机", (300, 700), ym),
                      ("仓库存货口", (900, 300), None), ("仓库存货口", (300, 1000), None), ("仓库存货口", (100, 900), None)],
          "nodes": {"A": ("分流器", (150, 300)), "s": ("分流器", (500, 420))},
          "gates": [(1, (40, 250), "清水", 6, 0), (3, (150, 150), "息壤", 30, "A"), (4, (650, 100), "壤晶废液", 1.5, 1)],
          "links": [("A", 0, "belt"), ("A", 2, "belt"), (0, "s", "belt"), ("s", 1, "belt"), ("s", 0, "belt"), (1, 3, "belt"),
                    (2, 4, "belt"), (0, 5, "pipe")]}
    res = _main(sc, {"pool_recipes": {PK: [YH]}, "pool_outlets": {PK: {"belt": ["息壤"]}}})
    got = [_r(res, xy) for xy in ("300,300", "650,300", "300,700")]
    assert all(abs(a - b) < 2e-3 for a, b in zip(got, [0.2, 0.05, 0.7])), got
    assert not res["res"]["iters"]["hit_cap"], res["res"]["iters"]


if __name__ == "__main__":
    sys.exit(run([v for k, v in dict(globals()).items() if k.startswith("test_")]))
