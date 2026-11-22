"""限制通过数量按一次性算（0.10.13 A2）：准入口开了「限制通过数量 N」，放满 N 个就不再放行（用户 2026-11-09 实测），
设计分析算长期开工率，按不放行算。线内的口、输入端，作者填的、游戏里读的都一样；N 不知道（游戏里读到开着、数量没读出来）的不挡。
都走 design.analyze 里的共用准入口函数 gate_pass（推料、吸收量、背压、并行池子出料上限、池子出口的下游吃量），输入端来料在 sources()，
几口合计在 pools()；下游第一台缺这种料的设备依据里说一句「上游准入口(x,y) 开了限制通过数量 N：…」。
审查后（2026-11-21）补的：gate_pass 把要往这种口送的料记进 shut（上游是生产设备、池子时推料账上是空的，靠它写那句、准入口表归「有料」）；
只能往这种口出料、原料吃饱的生产设备写间歇不写没料；限速和限制通过数量都开着时准入口段不写反；导入方认那句。
复审后（2026-11-21）补的：原料真缺的照旧写没料（分流器供料、因为出不去分不到的不算缺）；shut 每轮清、上游自己没料不记、分不到料的不记；
导入方只在口挡的正是最缺的那种料时认那句；准入口表几处界面文字（node 跑 app.js 里抽出来的函数）。
第三轮（2026-11-22）补的：出料堵死的设备按「整条线都不算出口堵塞」再算一遍（probe）判间歇 / 没料、并 shut，换掉只管分流器一层的 ghost：
生产设备直接连堵死设备（直连、隔物流桥、另有正常去向）、几口合计、并行池子经分流器、链式堵塞、缺一种原料照旧没料、
单路推料照记、分料下游不要的不记、背压试算 / 去向过滤只是问一问不记（验收脚本 S8–S16、S21、S22 和池子出口下游吃饱那条）。
第四轮（复审 2026-11-22）补的：probe 分两遍、正常的背压照算（主路备路、出料带子装不下、主路兄弟另一路过限量口、备路上的限量口），
池子下游需求上限、出口转送比例在 probe 里重新定（并行池子按下游需求堵死、一条出两种产物、主路上的池子吃一半、池子出口转给堵死设备、池子出口两个口），
池子出口后面的口堵死时池子留给自己用，去向全是死路的设备，分料问到池子出口不记 shut，probe 不改主分析的任何结果（复审脚本 c1–c4）。
收尾（2026-11-22）补的：出料堵死、原料有来料的设备，run_base 不再在依据开头插「卡在：X（能送到 0/30）」，行上带 fed_blocked（test_run_base_fed_blocked_not_stuck）。
最后一轮复审（2026-11-22）补的：run_base 的 limit_by 不跟 probe 变（只有「卡在」那句跳过 fed_blocked）；准入口表两个限量口串着、
一个口上挡下的料和 shut 不一样时的界面文字。同一轮试过、核实有回退而撤回的，各留一条防回退：并行池子按 probe 判没料、没收敛整遍不用、
同一遍才算齐（有料的判成没料）、放宽轮数（不相干的产线被拖到漏干）、fed_blocked 不点名（唯一缺料的口被藏住）。
估算判错的已知情形只锁数字；例外是并行池子全截到 0 的四例（原料真没有也写间歇），故意锁住现状，改池子怎么吸料时要一起改。
合成小据点（tests/test_gate_fixes.py 的 _scene、tests/test_pool_parallel.py 的 _scene、tests/test_fix_api.py 的 project），不读本机实测数据。
"""
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "tests"), str(ROOT / "src"), str(ROOT / "src" / "tools")]

from _testkit import run, skip  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import project as P  # noqa: E402
import test_gate_fixes as GF  # noqa: E402
import test_pool_outlets as PO  # noqa: E402
import test_pool_parallel as TP  # noqa: E402

KEY_L = GF.KEY_L
ONCE_L = "上游准入口(200,300) 开了限制通过数量 20{}：一次性的，放满就不再放行，长期按不送芽针算"


def _an(sc=GF.LINE, item=None, pool=None, fixes=None, alt=None, **kw):
    """GF._an 的做法，另外可以给准入口节点标物品（item {节点键: 物品}，flow.json 里的 item），给输入端设几口合计
    （pool {输入端编号: {"name", "total"}}，inputs.json 的 marks），fixes.json 这个据点再加几段（fixes，比如池子配方、池子出口），
    给设备配交替配方（alt {(x, y): 配方}，配方表的 alt 字段）→ (结果, {节点键: 准入口表那一行}, {"x,y": 设备行})。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl, nid = GF._scene(Path(td), sc, **kw)
        if alt:
            rec = [dict(r, alt=alt[(r["x"], r["y"])]) if (r["x"], r["y"]) in alt else r for r in rec]
        if fixes:
            fp = bd.parent.parent / "fixes.json"
            fx = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
            fx.setdefault("测试", {}).update(fixes)
            fp.write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
        if item:
            fl = json.loads((bd / "flow.json").read_text(encoding="utf-8"))
            for k, it in item.items():
                fl["nodes"][nid[k]]["item"] = it
            (bd / "flow.json").write_text(json.dumps(fl, ensure_ascii=False), encoding="utf-8")
        if pool:
            pj = bd.parent.parent / "inputs.json"
            inp = json.loads(pj.read_text(encoding="utf-8"))
            for m in inp["测试"]["marks"]:
                if m["no"] in pool:
                    m["pool"] = pool[m["no"]]
            pj.write_text(json.dumps(inp, ensure_ascii=False), encoding="utf-8")
        res = D.analyze(bd, led, rec, gl)
    gv = {r["gate"]: r for r in res["gate_view"]["gates"]}
    return res, {k: gv[i] for k, i in nid.items() if i in gv}, {f"{r['x']},{r['y']}": r for r in res["devices"]}


def _last(r):
    """依据最后一句（依据是空的 → None，断言照常报失败、不抛 IndexError）。"""
    return r["basis"][-1] if r["basis"] else None


def _ent(res, gid):
    """design.json 准入口段里这个口的那一条。"""
    return next(e for e in res["gates"] if e["gate"] == gid)


def test_inline_author_count():
    """线内的口作者填限制通过数量 20：放满就不再放行，下游粉碎机长期 0，依据末尾说是这个口（标「作者填」）；
    准入口表这一行通过 0、挡下 30、下游满速要 30（从出边算、不含自己）；design.json 准入口段下游 0、写「按一次性备料算」。"""
    res, gv, dv = _an(gates_fix={KEY_L: GF._fix((200, 300), None, 20)})
    r = dv["400,300"]
    assert r["rate"] == 0.0 and r["got"]["芽针"] == 0.0 and r["state"] == "没料", r
    assert _last(r) == ONCE_L.format("（作者填）"), r["basis"]
    assert sum("限制通过数量" in b for b in r["basis"]) == 1 and not any("准入口限速" in b for b in r["basis"]), r["basis"]
    L = gv["L"]
    assert L["flow"] == {} and L["held"] == {"芽针": 30} and L["downstream"] == 30 and L["count"] == 20 and L["src"] == "作者", L
    ent = _ent(res, L["gate"])
    assert ent["downstream"] == 0.0 and ent["msg"] == ["限制通过数量 20（作者填）：放满后不再放行，按一次性备料算"], ent
    assert gv["g1"]["flow"] == {"芽针": 30} and gv["g1"]["held"] == {}, gv["g1"]  # 输入端照常来料，挡在线内的口


def test_game_read_not_full_still_closed():
    """游戏里读到线内的口开了限制通过数量 20、累计才 5（还没放满）：照样按一次性、长期不放行算（以前线内的口根本不看它）；
    依据不标「作者填」；准入口段写「累计 5」。作者在这个口上另填限速 18、限制通过数量照读到的 20 → 照样 0，不按限速放 18。"""
    lim = {"L": {"limit_on": False, "count_on": True, "count_limit": 20, "passed": 5}}
    res, gv, dv = _an(limits=lim)
    r = dv["400,300"]
    assert r["rate"] == 0.0 and _last(r) == ONCE_L.format(""), r
    assert gv["L"]["src"] == "游戏" and gv["L"]["held"] == {"芽针": 30} and gv["L"]["count"] == 20, gv["L"]
    assert _ent(res, gv["L"]["gate"])["msg"] == ["限制通过数量 20（累计 5）：放满后不再放行，按一次性备料算"], _ent(res, gv["L"]["gate"])
    res, gv, dv = _an(limits=lim, gates_fix={KEY_L: GF._fix((200, 300), 18, 20)})
    assert dv["400,300"]["rate"] == 0.0 and _last(dv["400,300"]) == ONCE_L.format("（作者填）"), dv["400,300"]
    assert gv["L"]["held"] == {"芽针": 30} and gv["L"]["limit"] == 18, gv["L"]
    # 整条说明都比：限速那句不能按「下游需求 0」写成「下游没有用这种物品的设备」（审查 2026-11-21：从口本身算的需求被 once 截成 0）
    assert _ent(res, gv["L"]["gate"])["msg"] == ["限制通过数量 20（作者填，累计 5）：放满后不再放行，按一次性备料算",
                                                "限速 18 不起作用：限制通过数量已经按不放行算"], _ent(res, gv["L"]["gate"])


def test_count_unknown_not_blocked():
    """游戏里读到开了限制通过数量、数量没读出来：不知道 N，不挡（照常满速、不写那句依据），准入口段照实写没按它挡。"""
    res, gv, dv = _an(limits={"L": {"limit_on": False, "count_on": True}})
    r = dv["400,300"]
    assert r["rate"] == 1.0 and not any("限制通过数量" in b for b in r["basis"]), r
    assert gv["L"]["count"] is None and gv["L"]["read"]["count_on"] is True and gv["L"]["flow"] == {"芽针": 30}, gv["L"]
    assert gv["L"]["held"] == {}, gv["L"]
    ent = _ent(res, gv["L"]["gate"])
    assert ent["downstream"] == 30 and ent["msg"] == ["限制通过数量开着，但没读到数量：设计分析没按它挡，照常放行"], ent


def test_input_gate_count():
    """输入端开了限制通过数量（游戏里读到 20、没读到累计数；以前要累计到 20 才按不来料算）→ 来料 0：按「不来料」算，不是「来料被挡」
    （准入口表这口通过、挡下都是空的）；输入端没填流速时也不再提醒「输入端量不知道」（反正不来料）。
    下游粉碎机依据末尾说是输入端那个口（物品按输入端标的芽针）。"""
    lim = {"g1": {"limit_on": False, "count_on": True, "count_limit": 20}}
    for rate in (30, None):
        sc = dict(GF.LINE, gates=[(1, (40, 300), "芽针", rate, "L")])
        res, gv, dv = _an(sc, limits=lim)
        r = dv["400,300"]
        assert r["rate"] == 0.0, (rate, r)
        assert _last(r) == "上游准入口(40,300) 开了限制通过数量 20：一次性的，放满就不再放行，长期按不送芽针算", (rate, r["basis"])
        assert gv["g1"]["flow"] == {} and gv["g1"]["held"] == {} and gv["L"]["flow"] == {}, (rate, gv["g1"], gv["L"])
        assert not any(n["kind"] == "输入端量不知道" for n in res["notes"]), (rate, res["notes"])
    res, _, _ = _an(dict(GF.LINE, gates=[(1, (40, 300), "芽针", None, "L")]))  # 对照：没开限制通过数量、没填流速 → 照常提醒
    assert any(n["kind"] == "输入端量不知道" for n in res["notes"]), res["notes"]


def test_pool_total_skips_count_gate():
    """两个输入端几口合计 40（各自往一台要 30 的粉碎机送）：都没开时各分 20；2 号开了限制通过数量 → 不参加分，
    1 号拿 30（自己下游吃满）、2 号 0；1 号的来料依据写「2 号开了限制通过数量、不参加分」，2 号下游的粉碎机依据末尾说是它。"""
    sc = {"devices": [("粉碎机", (400, 300), GF.R["碎芽针"]), ("粉碎机", (400, 500), GF.R["碎芽针"])],
          "gates": [(1, (40, 300), "芽针", None, 0), (2, (40, 500), "芽针", None, 1)], "links": []}
    pool = {1: {"name": "芽针合计", "total": 40}, 2: {"name": "芽针合计", "total": 40}}
    _, _, dv = _an(sc, pool=pool)
    assert abs(dv["400,300"]["rate"] - 2 / 3) < 1e-3 and abs(dv["400,500"]["rate"] - 2 / 3) < 1e-3, (dv["400,300"], dv["400,500"])
    assert any(b.startswith("输入端1号 芽针（芽针合计 合计 40，这口分到 20）") for b in dv["400,300"]["basis"]), dv["400,300"]["basis"]
    res, gv, dv = _an(sc, pool=pool, limits={"g2": {"limit_on": False, "count_on": True, "count_limit": 20, "passed": 3}})
    a, b = dv["400,300"], dv["400,500"]
    assert a["rate"] == 1.0 and b["rate"] == 0.0, (a, b)
    assert any(s.startswith("输入端1号 芽针（芽针合计 合计 40，2 号开了限制通过数量、不参加分，这口分到 30）") for s in a["basis"]), a["basis"]
    assert _last(b) == "上游准入口(40,500) 开了限制通过数量 20：一次性的，放满就不再放行，长期按不送芽针算", b["basis"]
    assert not any("限制通过数量" in s for s in a["basis"] if s.startswith("上游")), a["basis"]
    assert gv["g2"]["flow"] == {} and gv["g2"]["held"] == {}, gv["g2"]


def test_fed_downstream_not_named():
    """下游的粉碎机另有一路芽针吃饱了（2 号输入端直接送 30）：开了限制通过数量的口照样挡下 30，但这台不缺，依据里不提它
    （对应皇帝产线武陵城采种机 (1020,1391) 靠种植机供锦草）。"""
    sc = dict(GF.LINE, gates=GF.LINE["gates"] + [(2, (400, 100), "芽针", 30, 0)])
    _, gv, dv = _an(sc, gates_fix={KEY_L: GF._fix((200, 300), None, 20)})
    r = dv["400,300"]
    assert r["rate"] == 1.0 and not any("限制通过数量" in b for b in r["basis"]), r
    assert gv["L"]["held"] == {"芽针": 30} and gv["L"]["flow"] == {}, gv["L"]


def test_through_splitter_and_storage():
    """分流器一路过开了限制通过数量的口、经协议储存箱到粉碎机 A，另一路直接到粉碎机 B：这一路吃量是 0，30 全给 B，A 一点都分不到
    （口上挡下的账是空的）。A 的依据照样说是这个口（穿过储存箱找下游第一台）：口上认出了物品（芽针）按它写；口上没认出物品时，
    按分流器算吃量时要往这个口送的芽针写（准入口表的 shut；审查 2026-11-21 以前账上空就不写）。"""
    sc = {"devices": [("粉碎机", (600, 200), GF.R["碎芽针"]), ("粉碎机", (600, 400), GF.R["碎芽针"]), ("协议储存箱", (450, 200), None)],
          "nodes": {"S": ("分流器", (200, 300), None), "L": ("准入口", (300, 200), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "S")],
          "links": [("S", "L", "belt"), ("L", 2, "belt"), (2, 0, "belt"), ("S", 1, "belt")]}
    fx = {D.gate_key(GF.BP, (300, 200)): GF._fix((300, 200), None, 20)}
    _, gv, dv = _an(sc, gates_fix={})  # 对照：口没开，两台各 15
    assert abs(dv["600,200"]["rate"] - 0.5) < 1e-3 and abs(dv["600,400"]["rate"] - 0.5) < 1e-3, (dv["600,200"], dv["600,400"])
    _, gv, dv = _an(sc, gates_fix=fx, item={"L": "芽针"})
    a, b = dv["600,200"], dv["600,400"]
    assert a["rate"] == 0.0 and b["rate"] == 1.0, (a, b)
    assert gv["L"]["held"] == {} and gv["L"]["flow"] == {} and gv["L"]["shut"] == ["芽针"], gv["L"]
    once_a = "上游准入口(300,200) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针算"
    assert _last(a) == once_a, a["basis"]
    assert not any("限制通过数量" in s for s in b["basis"]), b["basis"]
    _, gv, dv = _an(sc, gates_fix=fx)  # 口上没认出物品
    assert dv["600,200"]["rate"] == 0.0 and _last(dv["600,200"]) == once_a, dv["600,200"]
    assert gv["L"]["item"] is None and gv["L"]["shut"] == ["芽针"] and gv["L"]["held"] == {}, gv["L"]
    assert not any("限制通过数量" in s for s in dv["600,400"]["basis"]), dv["600,400"]["basis"]


def test_parallel_pool_output_through_count_gate():
    """并行池子芽针溶液那条的出口过开了限制通过数量的口（作者填）：背压 / 下游需求按 0 算，这条跑 0、锦草那条照常满速，
    下游灌装机芽针瓶 0（test_gate_fixes.test_parallel_pool_respects_author_limit 的场景，限速 18 换成限制通过数量 20）。"""
    sc = {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 220), GF.R["芽针瓶"]), ("灌装机", (560, 380), GF.R["锦草瓶"])],
          "nodes": {"L": ("准入口", (500, 220), "管道")},
          "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                    (4, (700, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", 30, 2)],
          "links": [(0, "L", "pipe", ["芽针溶液"]), ("L", 1, "pipe"), (0, 2, "pipe", ["锦草溶液"])]}
    res, gv, dv = _an(sc, gates_fix={D.gate_key(GF.BP, (500, 220)): GF._fix((500, 220), None, 20, kind="管道", item="")})
    pool = dv["300,300"]
    assert [c["rate"] for c in pool["channels"]] == [0.0, 1.0], pool["channels"]
    assert dv["700,220"]["rate"] == 0.0 and dv["560,380"]["rate"] == 1.0, (dv["700,220"], dv["560,380"])
    # 口上没认出物品、一种料都没推过去（池子这条被压成 0）：池子要往这里送的是芽针溶液（shut），下游满速要多少按它算 30
    # （审查 2026-11-21 以前账上空、按灌装机全部原料算 60），灌装机依据末尾说是这个口
    assert gv["L"]["count"] == 20 and gv["L"]["flow"] == {} and gv["L"]["held"] == {} and gv["L"]["shut"] == ["芽针溶液"], gv["L"]
    assert gv["L"]["downstream"] == 30, gv["L"]
    assert _last(dv["700,220"]) == "上游准入口(500,220) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针溶液算", \
        dv["700,220"]["basis"]
    assert not any("限制通过数量" in s for s in dv["560,380"]["basis"]), dv["560,380"]["basis"]
    assert not res["iters"]["hit_cap"], res["iters"]


# 生产设备 → 线内口 L → 下游：粉碎机（输入端 1 号直接送芽针 30）产芽针粉末 60/分，只能经 L 送到研磨机（要芽针粉末 30，假配方）
GRIND = "芽针粉末×1 → 芽针碎块×1（2秒）"
PROD = {"devices": [("粉碎机", (300, 300), GF.R["碎芽针"]), ("研磨机", (600, 300), GRIND)],
        "nodes": {"L": ("准入口", (450, 300), "物品")},
        "gates": [(1, (40, 300), "芽针", 30, 0)],
        "links": [(0, "L", "belt"), ("L", 1, "belt")]}
KEY_P = D.gate_key(GF.BP, (450, 300))
ONCE_P = "上游准入口(450,300) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针粉末算"


def test_producer_backpressure_through_count_gate():
    """上游是生产设备（审查 2026-11-21）：粉碎机的产物只能走开了限制通过数量的口（drain 经过 gate_pass）→ 背压压到 0，
    依据写「出料受限 … 0%」；原料吃饱了（到手 30/30），状态写间歇、不算没料（和并行池子全被截到 0 一样）。研磨机 0、没料。
    推料一点都没有（口上 flow、held 都空），挡的是芽针粉末靠 shut 知道：口上没认出物品时研磨机依据末尾照样说是这个口，
    准入口表下游满速要多少按芽针粉末算 30。对照：口没开时研磨机满速。"""
    _, gv, dv = _an(PROD, gates_fix={})
    assert dv["600,300"]["rate"] == 1.0 and gv["L"]["flow"] == {"芽针粉末": 30} and gv["L"]["shut"] == [], (dv["600,300"], gv["L"])
    for item in (None, {"L": "芽针粉末"}):
        res, gv, dv = _an(PROD, gates_fix={KEY_P: GF._fix((450, 300), None, 20, item="")}, item=item)
        a, b = dv["300,300"], dv["600,300"]
        assert a["rate"] == 0.0 and a["state"] == "间歇" and a["got"] == {"芽针": 30} == a["need"], (item, a)
        assert a["basis"][0] == "出料受限：产物下游最多吃得下满速的 0%（背压，主路顶住）", (item, a["basis"])
        assert not any("限制通过数量" in s for s in a["basis"]), (item, a["basis"])  # 那句只给口下游的设备
        assert b["rate"] == 0.0 and b["state"] == "没料" and _last(b) == ONCE_P, (item, b)
        L = gv["L"]
        assert L["flow"] == {} and L["held"] == {} and L["shut"] == ["芽针粉末"] and L["downstream"] == 30, (item, L)
        assert D.summary(res)["states"]["没料"] == 1 and D.summary(res)["states"]["间歇"] == 1, (item, D.summary(res))


def _prod(rate):
    """PROD，输入端 1 号填 rate（0 = 平时不来料）。"""
    return dict(PROD, gates=[(1, (40, 300), "芽针", rate, 0)])


FX_P = {KEY_P: GF._fix((450, 300), None, 20, item="")}
FED_P = "芽针有来料，只是这台产物出不去，没分给它（出料堵住，不算缺料）"


def test_starved_producer_stays_no_material():
    """复审 2026-11-21：上游粉碎机原料真缺（输入端填 0 / 输入端自己开了限制通过数量 / 出口限速 0 / 出口物品不对），出口又被挡死：
    照旧写没料（游戏里没原料一件都做不出来，显示的是缺料），summary 没料 2、间歇 0。只有原料有、只是出不去的才写间歇（上一条、下面分流器那条）。
    开了限制通过数量的出口：上游自己没料，shut 是空的（原来第 0 轮满速起步和每轮背压都记，表上写「上游要往这里送芽针粉末」、归有料），研磨机不写那句。"""
    key_g1 = D.gate_key(GF.BP, (40, 300))
    cases = {
        "输入端填 0": (_prod(0), FX_P, None),
        "输入端开了限制通过数量": (PROD, dict(FX_P, **{key_g1: GF._fix((40, 300), None, 20)}), None),
        "出口限速 0": (_prod(0), {KEY_P: GF._fix((450, 300), 0, None, item="")}, None),
        "出口物品不对": (_prod(0), {}, {"L": "锦草"}),
    }
    for name, (sc, fx, item) in cases.items():
        res, gv, dv = _an(sc, gates_fix=fx, item=item)
        a, b = dv["300,300"], dv["600,300"]
        assert a["rate"] == 0.0 and a["got"] == {"芽针": 0.0} and a["state"] == "没料", (name, a)
        assert a["basis"][0] == "出料受限：产物下游最多吃得下满速的 0%（背压，主路顶住）" and FED_P not in a["basis"], (name, a["basis"])
        assert b["state"] == "没料" and not any(s.startswith("上游准入口(450,300)") for s in b["basis"]), (name, b)
        st = D.summary(res)["states"]
        assert st["没料"] == 2 and st["间歇"] == 0, (name, st)
        assert gv["L"]["shut"] == [] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, (name, gv["L"])
    import test_fix_api as FA  # 准入口表：游戏里读到的这种口不算有料经过，默认筛选不显示（上游原料够的见 test_gate_table_groups_shut_gate）
    with FA.project(_prod(0), limits={"L": {"limit_on": False, "count_on": True, "count_limit": 20, "passed": 20}}):
        t = P.gate_table("项目", "测试")
    L = next(r for r in t["rows"] if r["c"] == [450, 300])
    assert L["shut"] == [] and L["group"] == "没追到" and not L["show"] and L["item_name"] == "", L
    assert t["counts"]["有料"] == 0 and t["counts"]["没追到"] == 1, t["counts"]


def _split_sc(rate, via_gate=False):
    """输入端 1 号（芽针 rate）→ 分流器 S → 粉碎机 A (300,300)、粉碎机 C (300,500)；A 只能经线内口 L (450,300) 出到研磨机 B (600,300)，
    C 直接出到研磨机 E (600,500)。via_gate：S 到 A 中间再过一个准入口 G (250,300)（限速 18 由调用方填）。"""
    sc = {"devices": [("粉碎机", (300, 300), GF.R["碎芽针"]), ("研磨机", (600, 300), GRIND),
                      ("粉碎机", (300, 500), GF.R["碎芽针"]), ("研磨机", (600, 500), GRIND)],
          "nodes": {"S": ("分流器", (200, 400), None), "L": ("准入口", (450, 300), "物品")},
          "gates": [(1, (40, 400), "芽针", rate, "S")],
          "links": [("S", 0, "belt"), ("S", 2, "belt"), (0, "L", "belt"), ("L", 1, "belt"), (2, 3, "belt")]}
    if via_gate:
        sc["nodes"] = dict(sc["nodes"], G=("准入口", (250, 300), "物品"))
        sc["links"] = [("S", "G", "belt"), ("G", 0, "belt")] + sc["links"][1:]
    return sc


def test_split_fed_producer_blocked():
    """复审 2026-11-21（审查修正时举的反例，原来没锁）：粉碎机 A 的芽针从分流器来，产物只能走开了限制通过数量的口 L。A 出不去，
    分流器按吸收量 0 一点都不分给它（到手 0），可原料其实有（游戏里带子堵回来，A 的原料是满的）：按「整条线都不算出口堵塞」再算一遍（probe），
    A 那一遍有芽针 → A 写间歇、依据说芽针有来料；L 记 shut（上游有料）、研磨机 B 写那句。分料结果不变：C 拿 15（下游只吃得下一半）、E 满速。
    输入端填 0：A 原料真缺 → 没料，L 的 shut 空、B 不写那句。"""
    res, gv, dv = _an(_split_sc(60), gates_fix=FX_P)
    a, b, c, e = dv["300,300"], dv["600,300"], dv["300,500"], dv["600,500"]
    assert a["rate"] == 0.0 and a["got"] == {"芽针": 0.0} and a["state"] == "间歇", a
    assert a["basis"][:2] == ["出料受限：产物下游最多吃得下满速的 0%（背压，主路顶住）", FED_P], a["basis"]
    assert c["got"] == {"芽针": 15.0} and abs(c["rate"] - 0.5) < 1e-3 and e["rate"] == 1.0, (c, e)
    assert c["basis"][-1] == "输入端1号 芽针 60/分 → 分流器主路 1/2 → 15" and FED_P not in c["basis"], c["basis"]
    assert gv["L"]["shut"] == ["芽针粉末"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, gv["L"]
    assert b["state"] == "没料" and _last(b) == ONCE_P, b
    st = D.summary(res)["states"]
    assert (st["满速"], st["间歇"], st["没料"]) == (1, 2, 1), st
    res, gv, dv = _an(_split_sc(0), gates_fix=FX_P)
    a = dv["300,300"]
    assert a["state"] == "没料" and FED_P not in a["basis"], a
    assert gv["L"]["shut"] == [] and not any(s.startswith("上游准入口(450,300)") for s in dv["600,300"]["basis"]), (gv["L"], dv["600,300"])
    assert D.summary(res)["states"]["没料"] == 4, D.summary(res)
    # S 到 A 中间另有一个限速 18 的口 G：probe 那遍穿过 G 照样送到 A（间歇），但不进 G 的账（G 实际一点都没过：通过、挡下都空）
    fx = dict(FX_P, **{D.gate_key(GF.BP, (250, 300)): GF._fix((250, 300), 18, None, item="")})
    _, gv, dv = _an(_split_sc(60, via_gate=True), gates_fix=fx)
    assert dv["300,300"]["state"] == "间歇" and dv["300,300"]["got"] == {"芽针": 0.0}, dv["300,300"]
    assert gv["G"]["flow"] == {} and gv["G"]["held"] == {} and gv["G"]["shut"] == [], gv["G"]
    assert dv["300,500"]["got"] == {"芽针": 15.0}, dv["300,500"]


def test_pool_total_zero_no_shut():
    """几口合计（输入端 1、2 号，1 号经开了限制通过数量的口 L 到粉碎机 A，2 号直接到粉碎机 B）：合计 40 时 1 号分不到（L 不放行），
    L 记 shut（合计有料、A 要芽针）；合计 0（平时不来料）时没有料，L 不记 shut（复审 2026-11-21：pools 算各口吃量时合计是 0 不记）。"""
    sc = {"devices": [("粉碎机", (400, 300), GF.R["碎芽针"]), ("粉碎机", (400, 500), GF.R["碎芽针"])],
          "nodes": {"L": ("准入口", (200, 300), "物品")},
          "gates": [(1, (40, 300), "芽针", None, "L"), (2, (40, 500), "芽针", None, 1)], "links": [("L", 0, "belt")]}
    fx = {KEY_L: GF._fix((200, 300), None, 20, item="")}
    for total, shut in ((40, ["芽针"]), (0, [])):
        pool = {1: {"name": "芽针合计", "total": total}, 2: {"name": "芽针合计", "total": total}}
        _, gv, dv = _an(sc, pool=pool, gates_fix=fx)
        assert dv["400,300"]["rate"] == 0.0 and dv["400,500"]["rate"] == (1.0 if total else 0.0), (total, dv["400,300"], dv["400,500"])
        assert gv["L"]["shut"] == shut and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, (total, gv["L"])


def test_partly_fed_producer_blocked():
    """原料只来一半（输入端直接送 15/30），产物只能走开了限制通过数量的口：原料有、只是出不去，写间歇不写没料（有一种原料一点来料都没有
    才算没料）；上游有料，口上记 shut、研磨机写那句。"""
    _, gv, dv = _an(_prod(15), gates_fix=FX_P)
    a = dv["300,300"]
    assert a["rate"] == 0.0 and a["got"] == {"芽针": 15.0} and a["state"] == "间歇" and FED_P not in a["basis"], a
    assert gv["L"]["shut"] == ["芽针粉末"] and _last(dv["600,300"]) == ONCE_P, (gv["L"], dv["600,300"])


def test_split_branch_not_wanted_no_shut():
    """分流器一路过开了限制通过数量的口，口后面的研磨机不吃芽针（要芽针粉末）；另一路直接到粉碎机：这一路下游吃量 0、分不到料，
    口上不记 shut（gate_pass 只记 q > 0 的，复审 2026-11-21 补的锁）。"""
    sc = {"devices": [("研磨机", (600, 200), GRIND), ("粉碎机", (600, 400), GF.R["碎芽针"])],
          "nodes": {"S": ("分流器", (200, 300), None), "L": ("准入口", (300, 200), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "S")],
          "links": [("S", "L", "belt"), ("L", 0, "belt"), ("S", 1, "belt")]}
    _, gv, dv = _an(sc, gates_fix={D.gate_key(GF.BP, (300, 200)): GF._fix((300, 200), None, 20, item="")})
    assert dv["600,400"]["rate"] == 1.0, dv["600,400"]
    assert gv["L"]["shut"] == [] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, gv["L"]


def test_wrong_item_not_held():
    """物品不对的料送到开了限制通过数量的口：先按物品不对挡（一点不过），不记成「被挡下」、也不进 shut
    （审查 2026-11-21：先判限制通过数量的话，准入口表多出一行假的挡下量）。口上认出的芽针照样是这个口挡的料，下游缺芽针照写。"""
    sc = dict(GF.LINE, gates=[(1, (40, 300), "锦草", 30, "L")])
    _, gv, dv = _an(sc, gates_fix={KEY_L: GF._fix((200, 300), None, 20)}, item={"L": "芽针"})
    assert gv["L"]["held"] == {} and gv["L"]["flow"] == {} and gv["L"]["shut"] == [], gv["L"]
    assert dv["400,300"]["rate"] == 0.0 and _last(dv["400,300"]) == ONCE_L.format("（作者填）"), dv["400,300"]


def test_partly_fed_downstream_named():
    """下游粉碎机另有一路芽针 15（2 号输入端直接送）、缺一半：照样写是哪个口挡的（只有吃饱的才不写）。"""
    sc = dict(GF.LINE, gates=GF.LINE["gates"] + [(2, (400, 100), "芽针", 15, 0)])
    _, gv, dv = _an(sc, gates_fix={KEY_L: GF._fix((200, 300), None, 20)})
    r = dv["400,300"]
    assert r["rate"] == 0.5 and r["got"]["芽针"] == 15 and _last(r) == ONCE_L.format("（作者填）"), r


def test_not_named_through_producer():
    """口 → 粉碎机 A（另有输入端 2 号送芽针 30、吃饱）→ 研磨机 B（要 A 的芽针粉末，另外也要芽针、没有来路）：
    B 缺芽针，但隔着生产设备 A，不是这个口下游的第一台，不写那句；A 吃饱了也不写。"""
    sc = {"devices": [("粉碎机", (300, 300), GF.R["碎芽针"]), ("研磨机", (600, 300), "芽针粉末×1 + 芽针×1 → 芽针碎块×1（2秒）")],
          "nodes": {"L": ("准入口", (200, 300), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "L"), (2, (300, 100), "芽针", 30, 0)],
          "links": [("L", 0, "belt"), (0, 1, "belt")]}
    _, gv, dv = _an(sc, gates_fix={KEY_L: GF._fix((200, 300), None, 20)})
    a, b = dv["300,300"], dv["600,300"]
    assert gv["L"]["held"] == {"芽针": 30}, gv["L"]
    assert a["got"]["芽针"] == 30 and not any("限制通过数量" in s for s in a["basis"]), a
    assert b["got"]["芽针"] == 0.0 and b["rate"] == 0.0 and not any("限制通过数量" in s for s in b["basis"]), b


def test_gate_table_groups_shut_gate():
    """准入口表（project.gate_table）：开了限制通过数量、上游是生产设备的口，通过、被挡下都是空的，按 shut 归「有料」、默认显示，
    物品按挡下的芽针粉末写（审查 2026-11-21：原来归「没追到」，游戏里读到的这种口默认筛选里看不到，有料的口数也少一个）。"""
    import test_fix_api as FA
    with FA.project(PROD, limits={"L": {"limit_on": False, "count_on": True, "count_limit": 20, "passed": 20}}):
        t = P.gate_table("项目", "测试")
    L = next(r for r in t["rows"] if r["c"] == [450, 300])
    assert L["src"] == "游戏" and L["count"] == 20 and L["flow"] == {} and L["held"] == {} and L["shut"] == ["芽针粉末"], L
    assert L["group"] == "有料" and L["show"] and L["item_name"] == "芽针粉末" and L["item"] is None, L
    assert t["counts"]["有料"] == 1 and t["counts"]["没追到"] == 0, t["counts"]


def test_importer_names_count_gate():
    """导入方（importer.expectations）：作者点过「对」、因上游准入口开了限制通过数量没料的设备，巡检说明里的原因取那句（加在依据末尾，
    不取第一句不相干的来料行），产物记进 design_limits（和准入口限速一样是设计上的限量）。几口合计来料行里的「N 号开了限制通过数量、
    不参加分」不算。复审 2026-11-21：口挡的不是这台最缺的料（缺两种，卡到 0 的是跟口无关的锦草）时不认那句，原因照旧取「卡在：…」、
    产物不记 design_limits。"""
    from bpa import importer
    once = "上游准入口(500,300) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针粉末算"
    once2 = "上游准入口(200,500) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针算"
    stuck2 = "卡在：锦草（能送到 0/30）、芽针（能送到 15/30）"
    rows = [{"device": "反应池", "x": 700, "y": 300, "rate": 0.0, "state": "没料", "recipe": "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）",
             "need": {"清水": 30, "芽针粉末": 30}, "got": {"清水": 30, "芽针粉末": 0}, "available": {"清水": 30, "芽针粉末": 0},
             "basis": ["输入端2号 清水 60/分 → 60", once]},
            {"device": "粉碎机", "x": 400, "y": 300, "rate": 0.5, "state": "间歇", "recipe": GF.R["碎芽针"],
             "basis": ["输入端1号 芽针（芽针合计 合计 15，2 号开了限制通过数量、不参加分，这口分到 15）"]},
            {"device": "研磨机", "x": 400, "y": 500, "rate": 0.0, "state": "没料", "recipe": "芽针×1 + 锦草×1 → 芽针碎块×1（2秒）",
             "need": {"芽针": 30, "锦草": 30}, "got": {"芽针": 15, "锦草": 0}, "available": {"芽针": 15, "锦草": 0},
             "basis": [stuck2, "输入端2号 芽针 15/分 → 15", once2]}]
    conf = {"devices": {f"{r['device']}@{r['x']},{r['y']}": {"verdict": "ok"} for r in rows}}
    out = importer.expectations({"devices": rows}, conf, "测试")
    notes = {d["name"]: d["note"] for d in out["device_designs"]}
    assert once in notes["反应池"] and "输入端2号" not in notes["反应池"], notes
    assert out["limits"].get("芽针溶液", "").endswith(f"{once}（蓝图分析，作者确认）"), out["limits"]
    assert "芽针粉末" not in out["limits"], out["limits"]  # 几口合计那句不当限量
    assert stuck2 in notes["研磨机"] and once2 not in notes["研磨机"], notes
    assert "芽针碎块" not in out["limits"], out["limits"]


def _outlet(count):
    """池子出口场景（test_pool_outlets 场景 A，作者只说带出口送息壤）：带出口到天有洪炉中间有个准入口 q，
    游戏里读到 q 的限制通过数量 count（None = 没开）。→ run_base 的 {坐标: 行, "res": 结果}"""
    sc = PO._scene_a()
    sc["nodes"] = dict(sc["nodes"], q=("准入口", (480, 300)))
    sc["links"] = [lk for lk in sc["links"] if lk[:3] != (0, 1, "belt")] + [(0, "q", "belt"), ("q", 1, "belt")]
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec, _gl = TP._scene(td, sc)
        fx = {"pool_recipes": {PO.PK: [PO.YH, PO.FY]}, "pool_outlets": {PO.PK: {"belt": ["息壤"]}}}
        (bd.parent.parent / "fixes.json").write_text(json.dumps({"测试": fx}, ensure_ascii=False), encoding="utf-8")
        row = {"p": [480, 300], "limit_on": False, "count_on": count is not None, "count_limit": count, "passed": None}
        res = D.run_base(bd, GF._archive(td, led, rec, {"area": "测试", "rows": [row]}), "测试")
    return dict({f"{r['x']},{r['y']}": r for r in res["devices"]}, res=res)


def test_pool_outlet_through_count_gate():
    """池子出口转送原料经过开了限制通过数量的口（池子出口的「下游吃得下多少」_take_at 走 gate_pass）：下游一点都吃不下，
    转送比例定成 0，池子自己用全部息壤（满速），天有洪炉没息壤 → 0。对照：口没开时照常转送（比例 5/7，和 test_pool_outlets 一样）。"""
    off = _outlet(None)
    assert abs(off["res"]["pass_split"][f"{PO.PK}|息壤"] - 5 / 7) < 2e-3, off["res"]["pass_split"]
    on = _outlet(20)
    assert on["res"]["pass_split"][f"{PO.PK}|息壤"] < 1e-3, on["res"]["pass_split"]
    po = on["300,300"]["pass_out"][0]
    assert po["item"] == "息壤" and po["out"] < 1e-3 and abs(po["own"] - 30) < 1e-3, po
    assert on["300,300"]["rate"] == 1.0 and on["650,300"]["rate"] == 0.0, (on["300,300"], on["650,300"])
    # 口上没认出物品、转送比例定成 0 推料一点都没有：池子出口要往这个口送的息壤记在 shut 里，天有洪炉依据末尾照样说是这个口（审查 2026-11-21）
    assert _last(on["650,300"]) == "上游准入口(480,300) 开了限制通过数量 20：一次性的，放满就不再放行，长期按不送息壤算", on["650,300"]["basis"]
    assert not on["res"]["iters"]["hit_cap"], on["res"]["iters"]


# ---- 第三轮（2026-11-22）：出料堵死的设备按「整条线都不算出口堵塞」再算一遍（probe）判间歇 / 没料、并 shut。
# 用 data/recipes.json 里真实的配方（2 秒一份 = 30/分）：粉碎机 芽针 30 → 芽针粉末 60；精炼炉 芽针粉末 30 → 碳粉末 60；
# 研磨机 碳粉末 60 + 砂叶粉末 30 → 致密碳粉末 30；种植机 清水 30 + 芽针种子 30 → 芽针 60。场景和验收脚本 S8–S16、S21、S22 一样。
CR = GF.R["碎芽针"]
RF = "芽针粉末×1 → 碳粉末×2（2秒）"
GD = "碳粉末×2 + 砂叶粉末×1 → 致密碳粉末×1（2秒）"
DF = "致密碳粉末×1 → 稳定碳块×1（2秒）"
PL = "清水×1 + 芽针种子×1 → 芽针×2（2秒）"
BLOCKED0 = "出料受限：产物下游最多吃得下满速的 0%（背压，主路顶住）"
FX_750 = {D.gate_key(GF.BP, (750, 300)): GF._fix((750, 300), None, 20, item="")}
ONCE_750 = "上游准入口(750,300) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送碳粉末算"


def _fed(item):
    """出料堵死、到手 0 的原料那句（依据第二句）。"""
    return f"{item}有来料，只是这台产物出不去，没分给它（出料堵住，不算缺料）"


def _x1(kind):
    """粉碎机 A (300,300)（输入端 1 号芽针 30）→ 精炼炉 B (600,300) → 口 L (750,300) → 研磨机 C (900,300)（另有输入端 2 号砂叶粉末 30）。
    direct：A、B 设备直接连；two：A 另一个出料口直接连正常的精炼炉 E (600,500)；bridge：A → 物流桥 N → B。"""
    sc = {"devices": [("粉碎机", (300, 300), CR), ("精炼炉", (600, 300), RF), ("研磨机", (900, 300), GD)],
          "nodes": {"L": ("准入口", (750, 300), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, 0), (2, (900, 100), "砂叶粉末", 30, 2)],
          "links": [(0, 1, "belt"), (1, "L", "belt"), ("L", 2, "belt")]}
    if kind == "two":
        sc["devices"] = sc["devices"] + [("精炼炉", (600, 500), RF)]
        sc["links"] = sc["links"] + [(0, 3, "belt")]
    if kind == "bridge":
        sc["nodes"] = dict(sc["nodes"], N=("物流桥", (450, 300), "物品"))
        sc["links"] = [(0, "N", "belt"), ("N", 1, "belt")] + sc["links"][1:]
    return sc


def test_direct_link_blocked_producer():
    """生产设备直接连出料堵死的设备（复审 2026-11-22，ghost 只管分流器那一层，这里漏了）：精炼炉 B 的碳粉末只能走开了限制通过数量的口 L，
    粉碎机 A 直接连 B：A 产物的去向过滤按 B 吸收量 0 把它筛掉，B 到手 0。不算出口堵塞时 B 有芽针粉末 → B 写间歇、依据第二句说有来料；
    L 记 shut [碳粉末]（上游有料）、研磨机 C 写那句。A 另有一个正常去向（E 满速）、A 经物流桥连 B（推料不过滤，B 到手 30）结果一样。
    数字不变：B 开工率 0、到手 0（物流桥那种 30）。"""
    for kind in ("direct", "two", "bridge"):
        res, gv, dv = _an(_x1(kind), gates_fix=FX_750)
        a, b, c = dv["300,300"], dv["600,300"], dv["900,300"]
        assert a["state"] == ("满速" if kind == "two" else "间歇"), (kind, a)
        assert b["rate"] == 0.0 and b["state"] == "间歇" and b["basis"][:1] == [BLOCKED0], (kind, b)
        if kind == "bridge":
            assert b["got"] == {"芽针粉末": 30.0} and _fed("芽针粉末") not in b["basis"], (kind, b)
        else:
            assert b["got"] == {"芽针粉末": 0.0} and b["basis"][1:2] == [_fed("芽针粉末")], (kind, b)
        assert gv["L"]["shut"] == ["碳粉末"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {} and gv["L"]["downstream"] == 30, (kind, gv["L"])
        assert c["state"] == "没料" and _last(c) == ONCE_750, (kind, c)
        st = D.summary(res)["states"]
        if kind == "two":
            assert dv["600,500"]["got"] == {"芽针粉末": 30.0} and dv["600,500"]["rate"] == 1.0, dv["600,500"]
            assert (st["满速"], st["间歇"], st["没料"]) == (2, 1, 1), (kind, st)
        else:
            assert (st["满速"], st["间歇"], st["没料"]) == (0, 2, 1), (kind, st)


def test_run_base_fed_blocked_not_stuck():
    """走 run_base：出料堵死、原料有来料的设备（直连那种，精炼炉 B 到手 0）依据开头不再插「卡在：芽针粉末（能送到 0/30）」，
    和「芽针粉末有来料…不算缺料」打架；行上带 fed_blocked。下游研磨机 C 真缺碳粉末，「卡在：碳粉末」照写。
    对照：别的设备行没有 fed_blocked 这个键（design.json 字段不变）；数字和 analyze 一样（0.10.13 A2）。"""
    with tempfile.TemporaryDirectory() as td:
        bd, led, rec, gl, _nid = GF._scene(Path(td), _x1("direct"), gates_fix=FX_750)
        res = D.run_base(bd, GF._archive(Path(td), led, rec, gl), "测试")
    dv = {f"{r['x']},{r['y']}": r for r in res["devices"]}
    a, b, c = dv["300,300"], dv["600,300"], dv["900,300"]
    assert b["state"] == "间歇" and b["rate"] == 0.0 and b["got"] == {"芽针粉末": 0.0}, b
    assert b["basis"][:2] == [BLOCKED0, _fed("芽针粉末")] and not any(s.startswith("卡在：") for s in b["basis"]), b["basis"]
    assert b["fed_blocked"] == ["芽针粉末"] and b["available"] == {"芽针粉末": 0.0}, b
    assert c["state"] == "没料" and c["basis"][0] == "卡在：碳粉末（能送到 0/60）" and _last(c) == ONCE_750, c["basis"]
    assert "fed_blocked" not in a and "fed_blocked" not in c, (a, c)


def _x5(pooled):
    """输入端 1 号 → 粉碎机 B (300,300)（只能经口 L (450,300) 出到精炼炉 Q (600,300)）；2 号 → 粉碎机 D (300,500)（产物没去向，不堵）。
    pooled：1、2 号几口合计 60、不填流速；否则各填 30。"""
    r = None if pooled else 30
    return {"devices": [("粉碎机", (300, 300), CR), ("精炼炉", (600, 300), RF), ("粉碎机", (300, 500), CR)],
            "nodes": {"L": ("准入口", (450, 300), "物品")},
            "gates": [(1, (40, 300), "芽针", r, 0), (2, (40, 500), "芽针", r, 2)],
            "links": [(0, "L", "belt"), ("L", 1, "belt")]}


def test_pool_total_blocked_producer():
    """几口合计（复审 2026-11-22，ghost 没管 pools()）：粉碎机 B 出不去、吸收量 0，合计 60 全分给 D（D 满速 30、B 到手 0）。
    不算出口堵塞时 B 能分到芽针 → 间歇、依据说有来料；L 记 shut [芽针粉末]、精炼炉 Q 写那句。对照：各填 30（不合计），B 到手 30，间歇、不写那句。"""
    pool = {1: {"name": "合计", "total": 60}, 2: {"name": "合计", "total": 60}}
    for pooled in (True, False):
        res, gv, dv = _an(_x5(pooled), gates_fix=FX_P, pool=pool if pooled else None)
        b, q, d = dv["300,300"], dv["600,300"], dv["300,500"]
        assert d["rate"] == 1.0 and d["got"] == {"芽针": 30.0}, (pooled, d)
        assert b["rate"] == 0.0 and b["state"] == "间歇" and b["basis"][:1] == [BLOCKED0], (pooled, b)
        if pooled:
            assert b["got"] == {"芽针": 0.0} and b["basis"][1:2] == [FED_P], b
        else:
            assert b["got"] == {"芽针": 30.0} and FED_P not in b["basis"], b
        assert gv["L"]["shut"] == ["芽针粉末"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, (pooled, gv["L"])
        assert q["state"] == "没料" and _last(q) == ONCE_P, (pooled, q)
        st = D.summary(res)["states"]
        assert (st["满速"], st["间歇"], st["没料"]) == (1, 1, 1), (pooled, st)


def _par_split(bottle=30):
    """芽针粉末经分流器 S 分给反应池 (300,300)（并行：芽针溶液、锦草溶液）和精炼炉 X (150,100)；芽针溶液那条只能经管道口 L (500,220)
    出到灌装机 F1 (700,220)；锦草溶液给灌装机 (560,380)（赤铜瓶 bottle/分）。"""
    return {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 220), GF.R["芽针瓶"]), ("灌装机", (560, 380), GF.R["锦草瓶"]),
                        ("精炼炉", (150, 100), RF)],
            "nodes": {"L": ("准入口", (500, 220), "管道"), "S": ("分流器", (150, 200), "物品")},
            "gates": [(1, (40, 300), "清水", 60, 0), (2, (40, 200), "芽针粉末", 30, "S"), (3, (150, 420), "锦草粉末", 30, 0),
                      (4, (700, 60), "赤铜瓶", 30, 1), (5, (600, 540), "赤铜瓶", bottle, 2)],
            "links": [(0, "L", "pipe", ["芽针溶液"]), ("L", 1, "pipe"), (0, 2, "pipe", ["锦草溶液"]), ("S", 0, "belt"), ("S", 3, "belt")]}


def test_parallel_pool_split_blocked_channel():
    """并行池子经分流器拿原料（复审 2026-11-22，ghost 没管并行池子按条的吸收量）：芽针粉末经分流器 S 分给反应池和精炼炉 X，池子芽针溶液那条
    只能经开了限制通过数量的管道口 L 出到灌装机 F1：这条结构背压 0、吸收量 0，分流器全给 X（X 满速）。不算出口堵塞时池子这条有料 →
    L 记 shut [芽针溶液]、下游满速要 30、F1 写那句。数字不变：两条 0 / 100%。"""
    res, gv, dv = _an(_par_split(), gates_fix={D.gate_key(GF.BP, (500, 220)): GF._fix((500, 220), None, 20, kind="管道", item="")})
    pool = dv["300,300"]
    assert [c["rate"] for c in pool["channels"]] == [0.0, 1.0] and pool["got"]["芽针粉末"] == 0.0, pool
    assert dv["150,100"]["rate"] == 1.0 and dv["150,100"]["got"] == {"芽针粉末": 30.0}, dv["150,100"]
    assert gv["L"]["shut"] == ["芽针溶液"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {} and gv["L"]["downstream"] == 30, gv["L"]
    assert _last(dv["700,220"]) == "上游准入口(500,220) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针溶液算", \
        dv["700,220"]["basis"]
    assert not res["iters"]["hit_cap"], res["iters"]


def _chain(water):
    """种植机 A (200,300)（输入端清水 water、芽针种子 30）→ 粉碎机 B (400,300) → 精炼炉 C (600,300) → 口 L (750,300) → 研磨机 D (900,300)
    （另有输入端砂叶粉末 30）。"""
    return {"devices": [("种植机", (200, 300), PL), ("粉碎机", (400, 300), CR), ("精炼炉", (600, 300), RF), ("研磨机", (900, 300), GD)],
            "nodes": {"L": ("准入口", (750, 300), "物品")},
            "gates": [(1, (40, 250), "清水", water, 0), (2, (40, 350), "芽针种子", 30, 0), (3, (900, 100), "砂叶粉末", 30, 3)],
            "links": [(0, 1, "belt"), (1, 2, "belt"), (2, "L", "belt"), ("L", 3, "belt")]}


def test_chain_blocked():
    """链式（复审 2026-11-22，ghost 只管一层）：A → B → C → 限量口，C 出不去、吸收量 0，B 的产物推不过去，C 到手 0。
    不算出口堵塞时一层层都有料 → C 写间歇，C 依据说芽针粉末有来料；L 记 shut [碳粉末]、研磨机 D 写那句；summary 间歇 3、没料 1。
    A、B 写间歇是因为开工率 50%（种植机、粉碎机一出二，下游只吃一半 / 一条带子只装 30），不是因为链尾堵死：上游设备写不写间歇看开工率，
    结构背压只往上传一层，1:1 配方的链上更上游的设备写满速（老的数字口径，最后一轮复审 2026-11-22 核实，没改）。
    对照：A 的清水填 0（链头真缺料）→ 不堵也一层层都没料，A、B、C、D 都写没料，C 不写有来料，L 的 shut 空，D 不写那句
    （「不算出口堵塞」那遍要从满速起步迭代到收敛：只推一两轮的话，起步时满速推下去的料会让链尾看着有料）。"""
    res, gv, dv = _an(_chain(30), gates_fix=FX_750)
    a, b, c, d = dv["200,300"], dv["400,300"], dv["600,300"], dv["900,300"]
    assert a["state"] == "间歇" and b["state"] == "间歇", (a, b)
    assert c["rate"] == 0.0 and c["got"] == {"芽针粉末": 0.0} and c["state"] == "间歇", c
    assert c["basis"][:2] == [BLOCKED0, _fed("芽针粉末")], c["basis"]
    assert gv["L"]["shut"] == ["碳粉末"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, gv["L"]
    assert d["state"] == "没料" and _last(d) == ONCE_750, d
    st = D.summary(res)["states"]
    assert (st["间歇"], st["没料"]) == (3, 1), st
    res, gv, dv = _an(_chain(0), gates_fix=FX_750)
    assert [dv[k]["state"] for k in ("200,300", "400,300", "600,300", "900,300")] == ["没料"] * 4, \
        [(dv[k]["state"], dv[k]["got"]) for k in ("200,300", "400,300", "600,300", "900,300")]
    assert not any("不算缺料" in s for s in dv["600,300"]["basis"]), dv["600,300"]["basis"]
    assert gv["L"]["shut"] == [] and not any(s.startswith("上游准入口(750,300)") for s in dv["900,300"]["basis"]), (gv["L"], dv["900,300"])
    assert D.summary(res)["states"]["没料"] == 4, D.summary(res)


def test_one_raw_missing_stays_starved():
    """出料堵死、缺一种原料：研磨机 X 的砂叶粉末有来料（30），碳粉末根本没有来路；产物只能经限量口 L 出到精炼炉 Y。
    不堵也做不出来 → 照旧没料（依据里没有「不算缺料」）；L 的 shut 空、Y 不写那句；summary 没料 2。"""
    sc = {"devices": [("研磨机", (300, 300), GD), ("精炼炉", (600, 300), DF)],
          "nodes": {"L": ("准入口", (450, 300), "物品")},
          "gates": [(1, (300, 100), "砂叶粉末", 30, 0)],
          "links": [(0, "L", "belt"), ("L", 1, "belt")]}
    res, gv, dv = _an(sc, gates_fix=FX_P)
    x, y = dv["300,300"], dv["600,300"]
    assert x["got"] == {"碳粉末": 0.0, "砂叶粉末": 30.0} and x["state"] == "没料" and x["basis"][:1] == [BLOCKED0], x
    assert not any("不算缺料" in s for s in x["basis"]), x["basis"]
    assert gv["L"]["shut"] == [] and not any(s.startswith("上游准入口(450,300)") for s in y["basis"]), (gv["L"], y)
    assert D.summary(res)["states"]["没料"] == 2, D.summary(res)


def test_shut_moving_vs_asking():
    """shut 只记真有料在动时送到口上的（复审 2026-11-22）：
    - 单路推料推到口上的照记，不看下游要不要：输入端芽针 30 → 口 L → 精炼炉（吃芽针粉末），L 挡下芽针 30、shut [芽针]
      （皇帝产线三个芽针口就是这样，和 held 一样）；
    - 分料时下游不要的那一路不记，背压试算（drain）只是问一问也不记：粉碎机 P → 分流器 S → 口 L → 粉碎机 Q（吃芽针、不吃芽针粉末），
      S 另一路 → 精炼炉 R（吃芽针粉末，满速）。芽针粉末分不到 L 那一路，L 的 shut 空（drain 按「下游不吃这种料、不知道」问到 L 时
      原来记成 [芽针粉末]）。"""
    sc = {"devices": [("精炼炉", (400, 300), RF)], "nodes": {"L": ("准入口", (200, 300), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "L")], "links": [("L", 0, "belt")]}
    _, gv, dv = _an(sc, gates_fix={KEY_L: GF._fix((200, 300), None, 20, item="")})
    assert gv["L"]["shut"] == ["芽针"] and gv["L"]["held"] == {"芽针": 30} and gv["L"]["flow"] == {}, gv["L"]
    sc = {"devices": [("粉碎机", (200, 300), CR), ("粉碎机", (600, 200), CR), ("精炼炉", (600, 400), RF)],
          "nodes": {"S": ("分流器", (350, 300), None), "L": ("准入口", (450, 200), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, 0)],
          "links": [(0, "S", "belt"), ("S", "L", "belt"), ("L", 1, "belt"), ("S", 2, "belt")]}
    _, gv, dv = _an(sc, gates_fix={D.gate_key(GF.BP, (450, 200)): GF._fix((450, 200), None, 20, item="")})
    assert dv["600,400"]["got"] == {"芽针粉末": 30.0} and dv["600,400"]["rate"] == 1.0, dv["600,400"]
    assert gv["L"]["shut"] == [] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, gv["L"]


def test_pool_outlet_downstream_full_no_shut():
    """池子出口转送的原料（0.10.13 功能三）过开了限制通过数量的口 q，口后面的精炼炉 H 另有输入端芽针粉末 30、已经吃饱：
    转送按下游吃得下的量问（_pt_ask），H 不要，q 的 shut 空、归没追到（复审 2026-11-22）。粉碎机 P 直接连池子，P 产物的去向过滤
    问到池子出口经过 q（absorb 递归）只是问一问，不记。对照：H 没有别的来路 → q 的 shut [芽针粉末]、H 写那句。数字不变：转送比例 0。"""
    pk = "反应池@300,300"
    fixes = {"pool_recipes": {pk: ["清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"]}, "pool_outlets": {pk: {"belt": ["芽针粉末"]}}}
    for full in (True, False):
        sc = {"devices": [("粉碎机", (100, 300), CR), ("反应池", (300, 300), None), ("精炼炉", (650, 300), RF), ("仓库存货口", (300, 650), None)],
              "nodes": {"q": ("准入口", (480, 300), "物品")},
              "gates": [(1, (40, 200), "芽针", 30, 0), (2, (300, 100), "清水", 60, 1)] + ([(3, (650, 100), "芽针粉末", 30, 2)] if full else []),
              "links": [(0, 1, "belt"), (1, "q", "belt"), ("q", 2, "belt"), (1, 3, "pipe")]}
        res, gv, dv = _an(sc, gates_fix={D.gate_key(GF.BP, (480, 300)): GF._fix((480, 300), None, 20, item="")}, fixes=fixes)
        po = dv["300,300"]["pass_out"][0]
        assert po["item"] == "芽针粉末" and po["arrive"] == 30.0 and po["out"] < 1e-3 and dv["300,300"]["rate"] == 1.0, (full, dv["300,300"])
        h = dv["650,300"]
        once_h = "上游准入口(480,300) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送芽针粉末算"
        if full:
            assert h["rate"] == 1.0 and gv["q"]["shut"] == [] and once_h not in h["basis"], (h, gv["q"])
        else:
            assert h["rate"] == 0.0 and gv["q"]["shut"] == ["芽针粉末"] and _last(h) == once_h, (h, gv["q"])
        assert gv["q"]["flow"] == {} and gv["q"]["held"] == {}, (full, gv["q"])


# ---- 第四轮（复审 2026-11-22）：probe 分两遍，正常的背压都照算。probe 1「出料堵死的设备当它吃得下」只去掉 0 的背压；probe 2「整条线都
# 不算出口堵塞」另外在算背压（drain）、并行池子下游需求上限（eat）时把堵死型的口当放行。料怎么分（推料、分料、池子出口转送）都照样挡；
# 外层（池子下游需求上限、出口转送比例）在 probe 里重新定；跑完外层结果放回、内层重跑。（甲）每种原料哪一遍有都算有，（乙）shut 并起来。
# （最后一轮复审 2026-11-22 试过「同一遍里都 > 0」「没收敛的那一遍不用」，都已撤回，见文件末尾那一节）
# 场景和复审脚本（review/tools 的 c1–c4、repro.py）一样，另加自查的「主路兄弟另一路过限量口」「并行池子一条出两种产物」。
TWO = "芽针×1 + 砂叶粉末×1 → 致密芽针×1（2秒）"   # 假配方：要芽针 30 + 砂叶粉末 30
USE = "致密芽针×1 → 芽针块×1（2秒）"
X30 = "芽针×1 → 芽针粉末×1（2秒）"   # 假配方：芽针 30 → 芽针粉末 30
C15 = "芽针粉末×1 → 芽针碎块×1（4秒）"  # 假配方：要芽针粉末 15
PKEY = "反应池@300,300"
PT_FIX = {"pool_recipes": {PKEY: ["清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"]}, "pool_outlets": {PKEY: {"belt": ["芽针粉末"]}}}


def _once(p, kind="物品"):
    """坐标 p 的线内口作者填限制通过数量 20（口上物品不标）。"""
    return {D.gate_key(GF.BP, p): GF._fix(p, None, 20, kind=kind, item="")}


def _once_at(p, item):
    return f"上游准入口({p[0]},{p[1]}) 开了限制通过数量 20（作者填）：一次性的，放满就不再放行，长期按不送{item}算"


def _backup(c1, gate=True):
    """输入端 1 号芽针 30 → 分流器 S；主路 → 粉碎机 X (300,200) → 研磨机 C1 (500,200)（配方 c1）；备路（单进单出汇流器 M）→
    研磨机 D (500,400)（另有输入端 2 号砂叶粉末 30）→ 口 L (650,400)（gate=False：D 直接连 E）→ 研磨机 E (800,400)。"""
    nodes = {"S": ("分流器", (150, 300), "物品"), "M": ("汇流器", (300, 400), "物品")}
    out = [(2, 3, "belt")]
    if gate:
        nodes["L"] = ("准入口", (650, 400), "物品")
        out = [(2, "L", "belt"), ("L", 3, "belt")]
    return {"devices": [("粉碎机", (300, 200), CR), ("研磨机", (500, 200), c1), ("研磨机", (500, 400), TWO), ("研磨机", (800, 400), USE)],
            "nodes": nodes, "gates": [(1, (40, 300), "芽针", 30, "S"), (2, (500, 600), "砂叶粉末", 30, 2)],
            "links": [("S", 0, "belt"), (0, 1, "belt"), ("S", "M", "belt"), ("M", 2, "belt")] + out}


def test_backup_path_blocked_producer():
    """主路 / 备路（复审第四轮 F1）：分流器主路上的粉碎机 X 只跑 50%（C1 只吃 30；或 C1 要 60、但 X 的出料带子只装 30），只吃 15 芽针，
    剩下的走备路到研磨机 D；D 的产物只能走限量口 L。游戏里备路那 15 到了 D，D 原料齐、只是出料堵住 → 间歇（依据第二句说芽针有来料），
    L 记 shut [致密芽针]、E 写那句，summary 没料只算 E。第三轮 probe 把背压整个去掉，X 吃满 30、D 分不到，判成没料（ghost 版判对了）。
    对照：D 直接连 E，D 拿到备路那 15、跑 50%。数字不变：X 50%、D 0、D 到手芽针 0。"""
    _, _, dv = _an(_backup(GRIND, gate=False))
    assert dv["500,400"]["got"] == {"芽针": 15.0, "砂叶粉末": 30.0} and dv["500,400"]["rate"] == 0.5, dv["500,400"]
    for c1 in (GRIND, "芽针粉末×2 → 芽针碎块×1（2秒）"):
        res, gv, dv = _an(_backup(c1), gates_fix=_once((650, 400)))
        x, d, e = dv["300,200"], dv["500,400"], dv["800,400"]
        assert x["rate"] == 0.5 and x["got"] == {"芽针": 15.0}, (c1, x)
        assert d["rate"] == 0.0 and d["got"] == {"芽针": 0.0, "砂叶粉末": 30.0} and d["state"] == "间歇", (c1, d)
        assert d["basis"][:2] == [BLOCKED0, _fed("芽针")], (c1, d["basis"])
        assert gv["L"]["shut"] == ["致密芽针"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, (c1, gv["L"])
        assert e["state"] == "没料" and _last(e) == _once_at((650, 400), "致密芽针"), (c1, e)
        assert D.summary(res)["states"]["没料"] == 1, (c1, D.summary(res))


def _sibling_gate():
    """_backup(GRIND) 再加一路：粉碎机 X 的另一个出料口 → 限量口 L1 (450,250) → 研磨机 W (600,250)。"""
    sc = _backup(GRIND)
    return dict(sc, devices=sc["devices"] + [("研磨机", (600, 250), GRIND)], nodes=dict(sc["nodes"], L1=("准入口", (450, 250), "物品")),
                links=sc["links"] + [(0, "L1", "belt"), ("L1", 4, "belt")])


def test_backup_path_sibling_capped_by_gate():
    """主路上的粉碎机 X 另一路出料过限量口 L1（X 只因为 L1 才跑 50%），备路上的研磨机 D 只能经限量口 L 出料（复审第四轮自查）：
    游戏里 L1 放满以后 X 只吃 15，多的 15 走备路到 D，D 原料齐、只是出不去 → 间歇，L 记 shut [致密芽针]；L1 记 shut [芽针粉末]。
    只按「整条线都不算出口堵塞」那遍（probe 2：L1 也放开，X 吃满 30）判的话 D 分不到、判成没料（第三轮、复审试修都是这样），
    所以还有一遍「出料堵死的设备当它吃得下」（probe 1：L1 照样挡），D 在这一遍里芽针、砂叶粉末都到手 > 0。数字不变：X 50%、D 0。"""
    res, gv, dv = _an(_sibling_gate(), gates_fix={**_once((650, 400)), **_once((450, 250))})
    x, d = dv["300,200"], dv["500,400"]
    assert x["rate"] == 0.5 and x["got"] == {"芽针": 15.0}, x
    assert d["rate"] == 0.0 and d["got"] == {"芽针": 0.0, "砂叶粉末": 30.0} and d["state"] == "间歇", d
    assert d["basis"][:2] == [BLOCKED0, _fed("芽针")], d["basis"]
    assert gv["L"]["shut"] == ["致密芽针"] and gv["L1"]["shut"] == ["芽针粉末"], (gv["L"], gv["L1"])
    assert _last(dv["800,400"]) == _once_at((650, 400), "致密芽针") and _last(dv["600,250"]) == _once_at((450, 250), "芽针粉末"), \
        (dv["800,400"]["basis"], dv["600,250"]["basis"])


def _par_demand():
    """并行池子 (300,300) 芽针溶液那条经管道分流器 S 分给口 L (580,150) → 灌装机 F1 (700,150)，和灌装机 F2 (700,300)（另有输入端 6 号
    芽针溶液 30、已经吃饱）；锦草溶液那条给灌装机 (560,450)。"""
    return {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 150), GF.R["芽针瓶"]), ("灌装机", (700, 300), GF.R["芽针瓶"]),
                        ("灌装机", (560, 450), GF.R["锦草瓶"])],
            "nodes": {"S": ("管道分流器", (450, 220), "管道"), "L": ("准入口", (580, 150), "管道")},
            "gates": [(1, (40, 300), "清水", 60, 0), (2, (150, 200), "芽针粉末", 30, 0), (3, (150, 420), "锦草粉末", 30, 0),
                      (4, (700, 40), "赤铜瓶", 30, 1), (5, (850, 300), "赤铜瓶", 30, 2), (6, (850, 380), "芽针溶液", 30, 2),
                      (7, (600, 560), "赤铜瓶", 30, 3)],
            "links": [(0, "S", "pipe", ["芽针溶液"]), ("S", "L", "pipe"), ("L", 1, "pipe"), ("S", 2, "pipe"), (0, 3, "pipe", ["锦草溶液"])]}


def test_parallel_pool_demand_capped_channel():
    """并行池子那条被「下游需求上限」压成 0（复审第四轮 F2）：芽针溶液那条的结构背压不是 0（F2 那一路算得进），是下游需求上限 0
    （F1 在限量口后面、F2 吃饱了）。第三轮 probe 只看结构背压、不触发，L 的 shut 空、下游满速要多少按 F1 全部原料写 60、F1 不写那句。
    现在主分析算下游需求时 L 挡掉过 > 0 的量就跑 probe，probe 里下游需求上限按 L 放行重新定 → 这条有料往 L 送：shut [芽针溶液]、
    下游 30、F1 写那句。数字不变：两条 0 / 100%。"""
    _, gv, dv = _an(_par_demand(), gates_fix=_once((580, 150), kind="管道"))
    pool = dv["300,300"]
    assert [c["rate"] for c in pool["channels"]] == [0.0, 1.0], pool["channels"]
    assert dv["700,150"]["rate"] == 0.0 and dv["700,300"]["rate"] == 1.0, (dv["700,150"], dv["700,300"])
    assert gv["L"]["shut"] == ["芽针溶液"] and gv["L"]["downstream"] == 30 and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, gv["L"]
    assert _last(dv["700,150"]) == _once_at((580, 150), "芽针溶液"), dv["700,150"]["basis"]


CO_KEY = "扩容反应池@300,300"
CO_FIX = {"pool_recipes": {CO_KEY: ["蓝铁粉末×1 + 赫铜溶液×2 → 赫铜块×1 + 污水×1（2秒）", "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"]}}


def _coproduct():
    """扩容反应池 (300,300)（作者配方两条并行：蓝铁粉末 + 赫铜溶液 → 赫铜块 + 污水；清水 + 芽针粉末 → 芽针溶液）：赫铜块给研磨机 D (600,300)
    （假配方，产物赫铜粉末只能经口 L2 (750,300) 出到 (900,300)），污水只能经管道口 L1 (450,500) 出到 (600,500)（假配方）。"""
    return {"devices": [("扩容反应池", (300, 300), None), ("研磨机", (600, 300), "赫铜块×1 → 赫铜粉末×1（2秒）"),
                        ("研磨机", (900, 300), "赫铜粉末×1 → 芽针块×1（2秒）"), ("研磨机", (600, 500), "污水×1 → 砂叶×1（2秒）")],
            "nodes": {"L2": ("准入口", (750, 300), "物品"), "L1": ("准入口", (450, 500), "管道")},
            "gates": [(1, (40, 250), "蓝铁粉末", 30, 0), (2, (40, 350), "赫铜溶液", 60, 0), (3, (300, 100), "清水", 30, 0),
                      (4, (150, 150), "芽针粉末", 30, 0)],
            "links": [(0, 1, "belt"), (1, "L2", "belt"), ("L2", 2, "belt"), (0, "L1", "pipe", ["污水"]), ("L1", 3, "pipe")]}


def test_pool_coproduct_dead_feeds_blocked():
    """并行池子那条配方出两种产物，污水那路只能过限量口 L1、赫铜块给出料堵死的研磨机 D（复审第四轮自查）：主分析里这条的下游需求上限是 0，
    「出料堵死的设备当它吃得下」（probe 1）里 L1 照样挡、这条还是 0，D 一点赫铜块都没有；「整条线都不算出口堵塞」（probe 2）里这条在跑，
    D 有赫铜块 → 间歇（依据第二句说有来料）。（甲）哪一遍里原料都有就算有，只看 probe 1 会判成没料。L1 记 shut [污水]、L2 记 shut [赫铜粉末]。
    数字不变：这条 0、另一条 100%，D 0。"""
    _, gv, dv = _an(_coproduct(), gates_fix={**_once((750, 300)), **_once((450, 500), kind="管道")}, fixes=CO_FIX)
    assert [c["rate"] for c in dv["300,300"]["channels"]] == [0.0, 1.0], dv["300,300"]["channels"]
    d = dv["600,300"]
    assert d["rate"] == 0.0 and d["got"] == {"赫铜块": 0.0} and d["state"] == "间歇" and d["basis"][:2] == [BLOCKED0, _fed("赫铜块")], d
    assert gv["L1"]["shut"] == ["污水"] and gv["L2"]["shut"] == ["赫铜粉末"], (gv["L1"], gv["L2"])


def _pool_main(bottle):
    """输入端 2 号芽针粉末 30 → 分流器 S；主路 → 并行池子 (300,300)（芽针溶液那条给灌装机 (700,220)，它的赤铜瓶只有 bottle）；
    备路（单进单出汇流器 M）→ 精炼炉 D (300,600) → 口 L (480,600) → 研磨机 E (650,600)（另有输入端 6 号砂叶粉末 30）。"""
    return {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 220), GF.R["芽针瓶"]), ("灌装机", (560, 420), GF.R["锦草瓶"]),
                        ("精炼炉", (300, 600), RF), ("研磨机", (650, 600), GD)],
            "nodes": {"S": ("分流器", (150, 400), "物品"), "M": ("汇流器", (200, 600), "物品"), "L": ("准入口", (480, 600), "物品")},
            "gates": [(1, (40, 300), "清水", 60, 0), (2, (40, 400), "芽针粉末", 30, "S"), (3, (150, 200), "锦草粉末", 30, 0),
                      (4, (700, 60), "赤铜瓶", bottle, 1), (5, (600, 540), "赤铜瓶", 30, 2), (6, (650, 750), "砂叶粉末", 30, 4)],
            "links": [("S", 0, "belt"), ("S", "M", "belt"), ("M", 3, "belt"), (3, "L", "belt"), ("L", 4, "belt"),
                      (0, 1, "pipe", ["芽针溶液"]), (0, 2, "pipe", ["锦草溶液"])]}


def test_pool_on_main_path_keeps_demand_cap():
    """并行池子在分流器主路上、下游需求上限 0.5 是正常的（下游灌装机只有赤铜瓶 15），备路上的精炼炉 D 只能经限量口 L 出料（复审第四轮）：
    游戏里池子只吃 15，备路那 15 到 D，D 有料往 L 送 → L 记 shut [碳粉末]、E 写那句。probe 里下游需求上限要按 L 放行重新定（还是 0.5），
    只是清空的话池子吃满 30、D 分不到，shut 空（复审试修 fix2 就是这样）。数字不变：池子 75%、D 0。"""
    _, gv, dv = _an(_pool_main(15), gates_fix=_once((480, 600)))
    pool = dv["300,300"]
    assert [c["rate"] for c in pool["channels"]] == [0.5, 1.0] and pool["got"]["芽针粉末"] == 15.0, pool
    assert dv["300,600"]["rate"] == 0.0 and dv["300,600"]["got"] == {"芽针粉末": 0.0}, dv["300,600"]
    assert gv["L"]["shut"] == ["碳粉末"] and _last(dv["650,600"]) == _once_at((480, 600), "碳粉末"), (gv["L"], dv["650,600"])


def test_backup_gate_partly_backpressured():
    """限量口在备路上、上游只是部分背压（复审第四轮 F3）：粉碎机 X（芽针 30 → 芽针粉末 30）→ 分流器 S；主路研磨机 C1 只要 15；
    备路（单进单出汇流器 M）→ 限量口 L → 研磨机 C2。X 被背压到 50%（只因为备路那头过不去），没有开工率 0 的设备，主路吃饱以后备路
    一点都分不到。按定义（乙），不算出口堵塞时 X 满速、多的 15 走备路推到 L：shut [芽针粉末]、C2 写那句（第三轮 probe 不触发，漏了）。
    对照：分流器两路平分（没有汇流器），分料时问得到 L，本来就记。数字不变：X 50%、C1 满速、C2 0。"""
    for backup in (True, False):
        nodes = {"S": ("分流器", (250, 300), "物品"), "L": ("准入口", (550, 400), "物品")}
        links = [(0, "S", "belt"), ("S", 1, "belt"), ("S", "L", "belt"), ("L", 2, "belt")]
        if backup:
            nodes["M"] = ("汇流器", (400, 400), "物品")
            links = [(0, "S", "belt"), ("S", 1, "belt"), ("S", "M", "belt"), ("M", "L", "belt"), ("L", 2, "belt")]
        sc = {"devices": [("粉碎机", (100, 300), X30), ("研磨机", (500, 200), C15), ("研磨机", (700, 400), GRIND)],
              "nodes": nodes, "gates": [(1, (40, 300), "芽针", 30, 0)], "links": links}
        _, gv, dv = _an(sc, gates_fix=_once((550, 400)))
        assert dv["100,300"]["rate"] == 0.5 and dv["500,200"]["rate"] == 1.0 and dv["700,400"]["rate"] == 0.0, (backup, dv)
        assert gv["L"]["shut"] == ["芽针粉末"] and gv["L"]["flow"] == {} and gv["L"]["held"] == {}, (backup, gv["L"])
        assert _last(dv["700,400"]) == _once_at((550, 400), "芽针粉末"), (backup, dv["700,400"]["basis"])


def _pt_line(gate):
    """反应池 (300,300)（作者配方 清水 + 芽针粉末 → 芽针溶液，带出口送芽针粉末；输入端 1 号芽针粉末 60、2 号清水 30，管道出口接仓库存货口）
    → 带出口 → 精炼炉 D (600,300) → 口 L (750,300)（gate=False：直接连）→ 研磨机 E (900,300)（另有输入端 3 号砂叶粉末 30）。"""
    nodes = {"L": ("准入口", (750, 300), "物品")} if gate else {}
    out = [(1, "L", "belt"), ("L", 2, "belt")] if gate else [(1, 2, "belt")]
    return {"devices": [("反应池", (300, 300), None), ("精炼炉", (600, 300), RF), ("研磨机", (900, 300), GD), ("仓库存货口", (300, 600), None)],
            "nodes": nodes, "gates": [(1, (40, 300), "芽针粉末", 60, 0), (2, (300, 100), "清水", 30, 0), (3, (900, 100), "砂叶粉末", 30, 2)],
            "links": [(0, 1, "belt"), (0, 3, "pipe")] + out}


def test_pool_outlet_to_blocked_device():
    """池子出口转送给出料堵死的设备（复审第四轮 F4，rv3 列过、第三轮没管到）：精炼炉 D 的碳粉末只能走限量口 L，主分析里出口转送比例
    按 D 吃得下 0 定成 0，D 到手 0；第三轮 probe 照用这个 0，D 判成没料。现在 probe 里转送比例按 D 不堵重新定 → D 有芽针粉末 → 间歇、
    依据第二句说有来料，L 记 shut [碳粉末]、E 写那句。数字不变：转送比例 0、池子自己用 30。对照：D 直接连 E，池子转 15、D 跑 50%。"""
    _, _, dv = _an(_pt_line(False), fixes=PT_FIX)
    assert dv["300,300"]["pass_out"][0]["out"] == 15.0 and dv["600,300"]["rate"] == 0.5, (dv["300,300"], dv["600,300"])
    _, gv, dv = _an(_pt_line(True), gates_fix=FX_750, fixes=PT_FIX)
    po, d = dv["300,300"]["pass_out"][0], dv["600,300"]
    assert po["share"] == 0.0 and po["out"] == 0.0 and po["own"] == 30.0 and dv["300,300"]["rate"] == 1.0, dv["300,300"]
    assert d["rate"] == 0.0 and d["got"] == {"芽针粉末": 0.0} and d["state"] == "间歇" and d["basis"][:2] == [BLOCKED0, _fed("芽针粉末")], d
    assert gv["L"]["shut"] == ["碳粉末"] and _last(dv["900,300"]) == ONCE_750, (gv["L"], dv["900,300"])


def _pt_gate_dead(product):
    """反应池 (300,300)（作者配方、带出口送芽针粉末；输入端 1 号芽针粉末 30、2 号清水 60）带出口 → 口 q (480,300) → 精炼炉 H (650,300)；
    池子的芽针溶液经管道给 (300,600)（配方 product），它的产物芽针块只能经口 L2 (500,600) 出到研磨机 (700,600)。"""
    return {"devices": [("反应池", (300, 300), None), ("精炼炉", (650, 300), RF), ("灌装机", (300, 600), product),
                        ("研磨机", (700, 600), "芽针块×1 → 芽针碎块×1（2秒）")],
            "nodes": {"q": ("准入口", (480, 300), "物品"), "L2": ("准入口", (500, 600), "物品")},
            "gates": [(1, (40, 300), "芽针粉末", 30, 0), (2, (300, 100), "清水", 60, 0), (3, (150, 650), "赤铜瓶", 30, 2)],
            "links": [(0, "q", "belt"), ("q", 1, "belt"), (0, 2, "pipe"), (2, "L2", "belt"), ("L2", 3, "belt")]}


def test_pool_outlet_gate_dead_pool_keeps():
    """池子出口后面的口 q 堵死（复审第四轮）：游戏里出口那条带子堵在 q 前，池子把芽针粉末留给自己用、照常出芽针溶液。芽针溶液给灌装机 F
    （假配方 芽针溶液 + 赤铜瓶 → 芽针块），F 的产物只能走口 L2：F 原料有、只是出不去 → 间歇；q 记 shut [芽针粉末]（池子有料往这里送）、
    H 写那句，L2 记 shut [芽针块]。probe 里池子出口转送照样在 q 挡（和分料一样是「料怎么分」）：要是把 q 也当放行，池子把芽针粉末全转给
    q 后面的 H、自己没料，F 就被判成没料。"""
    _, gv, dv = _an(_pt_gate_dead("芽针溶液×1 + 赤铜瓶×1 → 芽针块×1（2秒）"), gates_fix={**_once((480, 300)), **_once((500, 600))}, fixes=PT_FIX)
    po, f = dv["300,300"]["pass_out"][0], dv["300,600"]
    assert po["out"] == 0.0 and dv["300,300"]["rate"] == 1.0, dv["300,300"]
    assert f["rate"] == 0.0 and f["got"] == {"芽针溶液": 0.0, "赤铜瓶": 30.0} and f["state"] == "间歇", f
    assert f["basis"][:2] == [BLOCKED0, _fed("芽针溶液")], f["basis"]
    assert gv["q"]["shut"] == ["芽针粉末"] and gv["L2"]["shut"] == ["芽针块"], (gv["q"], gv["L2"])
    assert _last(dv["650,300"]) == _once_at((480, 300), "芽针粉末") and _last(dv["700,600"]) == _once_at((500, 600), "芽针块"), \
        (dv["650,300"]["basis"], dv["700,600"]["basis"])


def _pt_two_gates():
    """反应池 (300,300)（作者配方、带出口送芽针粉末；输入端 1 号芽针粉末 30、2 号清水 60，管道出口接仓库存货口）带出口 → 口 q (480,300)
    → 精炼炉 H (650,300) → 口 L2 (800,300) → 研磨机 E (950,300)（另有输入端 3 号砂叶粉末 30）。"""
    return {"devices": [("反应池", (300, 300), None), ("精炼炉", (650, 300), RF), ("研磨机", (950, 300), GD), ("仓库存货口", (300, 650), None)],
            "nodes": {"q": ("准入口", (480, 300), "物品"), "L2": ("准入口", (800, 300), "物品")},
            "gates": [(1, (40, 300), "芽针粉末", 30, 0), (2, (300, 100), "清水", 60, 0), (3, (950, 100), "砂叶粉末", 30, 2)],
            "links": [(0, "q", "belt"), ("q", 1, "belt"), (1, "L2", "belt"), ("L2", 2, "belt"), (0, 3, "pipe")]}


def test_pool_outlet_gate_then_blocked_device():
    """池子出口 → 口 q → 精炼炉 H，H 的产物又只能经口 L2 出（复审第四轮 F5 的 q8）：主分析里 H 出料堵死、吃得下 0，池子出口按下游吃得下
    问（_pt_ask）问不出来；probe 里 H 当它吃得下，那一遍的 _pt_ask 问到 q → q 记 shut [芽针粉末]、H 写那句。H 的芽针粉末只能从 q 来
    （进料路上的口放满了）→ 照旧没料、不写「有来料」；H 自己没料，L2 的 shut 空、E 不写那句。数字不变：转送比例 0。"""
    _, gv, dv = _an(_pt_two_gates(), gates_fix={**_once((480, 300)), **_once((800, 300))}, fixes=PT_FIX)
    h = dv["650,300"]
    assert dv["300,300"]["pass_out"][0]["out"] == 0.0 and h["rate"] == 0.0 and h["state"] == "没料", (dv["300,300"], h)
    assert _last(h) == _once_at((480, 300), "芽针粉末") and not any("不算缺料" in s for s in h["basis"]), h["basis"]
    assert gv["q"]["shut"] == ["芽针粉末"] and gv["L2"]["shut"] == [], (gv["q"], gv["L2"])
    assert not any(s.startswith("上游准入口(800,300)") for s in dv["950,300"]["basis"]), dv["950,300"]["basis"]


def test_split_asking_pool_outlet_no_shut():
    """分流器分料时问到池子出口后面的口（复审第四轮，原来就有的小问题）：粉碎机 P → 分流器 S → 反应池（带出口送芽针粉末 → 口 q → 精炼炉 H）
    / 精炼炉 Z。H 另有来料、吃饱了，池子一点不转（比例 0），q 上没有料在动 → shut 空、准入口表归没追到。H 的另一路来料是输入端（先推）
    还是序号靠后的粉碎机（后推）都一样（原来后推时分流器问池子吸收量顺着出口问到 q，记成 [芽针粉末]；出口这一路只由 _pt_ask 问）。"""
    for late in (False, True):
        sc = {"devices": [("粉碎机", (100, 300), CR), ("反应池", (300, 300), None), ("精炼炉", (650, 300), RF),
                          ("仓库存货口", (300, 650), None), ("精炼炉", (200, 500), RF)],
              "nodes": {"S": ("分流器", (200, 300), "物品"), "q": ("准入口", (480, 300), "物品")},
              "gates": [(1, (40, 200), "芽针", 30, 0), (2, (300, 100), "清水", 60, 1), (3, (800, 300), "芽针粉末", 60, 2)],
              "links": [(0, "S", "belt"), ("S", 1, "belt"), ("S", 4, "belt"), (1, "q", "belt"), ("q", 2, "belt"), (1, 3, "pipe")]}
        if late:
            sc["devices"] = sc["devices"] + [("粉碎机", (650, 600), CR)]
            sc["gates"] = sc["gates"][:2] + [(3, (800, 600), "芽针", 30, 5)]
            sc["links"] = sc["links"] + [(5, 2, "belt")]
        _, gv, dv = _an(sc, gates_fix=_once((480, 300)), fixes=PT_FIX)
        assert dv["300,300"]["pass_out"][0]["out"] == 0.0 and dv["650,300"]["rate"] == 1.0, (late, dv["300,300"], dv["650,300"])
        assert gv["q"]["shut"] == [] and gv["q"]["flow"] == {} and gv["q"]["held"] == {}, (late, gv["q"])


def test_dead_end_blocked_producer():
    """去向全是死路、不经过口的出料堵死设备：粉碎机 A 的芽针粉末经物流桥 N 出去，N 后面那条连线只走锦草粉末，背压 0，分流器一点不分给它。
    probe 里放开堵死型的口以后背压还是 0 的也不算 → A 分得到芽针 → 间歇、依据第二句说有来料（和第三轮、ghost 版一样）。"""
    sc = {"devices": [("粉碎机", (300, 200), CR), ("研磨机", (600, 200), GRIND), ("粉碎机", (300, 400), CR), ("研磨机", (600, 400), GRIND)],
          "nodes": {"S": ("分流器", (150, 300), "物品"), "N": ("物流桥", (450, 200), "物品")},
          "gates": [(1, (40, 300), "芽针", 30, "S")],
          "links": [("S", 0, "belt"), ("S", 2, "belt"), (0, "N", "belt"), ("N", 1, "belt", ["锦草粉末"]), (2, 3, "belt")]}
    _, _, dv = _an(sc)
    a = dv["300,200"]
    assert a["rate"] == 0.0 and a["got"] == {"芽针": 0.0} and a["state"] == "间歇" and a["basis"][:2] == [BLOCKED0, FED_P], a
    assert dv["300,400"]["got"] == {"芽针": 15.0}, dv["300,400"]


# ---- 最后一轮复审（2026-11-22）：三个角度（语义、数字不变、下游消费方）复审第四轮 + 收尾，核实人逐条复现以后成立的几条。
# 场景照复审脚本（semantics/tools 的 s_cross、s_pool，verify_consumers/tools 的 f1–f4，verify_invariance/tools 的 v0_even、patho、
# f1_backup_chain）改写，配方都是 data/recipes.json 里真有的（灌装机沿用 GF.R 的写法）。
SC_SEED = "芽针×1 → 芽针种子×1（2秒）"                            # 采种机
TC = "惰性壤晶废液×4 → 清水×1 + 壤晶废液×1（2秒）"                  # 提纯机
HLR = "壤晶废液×5 + 息壤×10 → 重息壤×1（10秒）"                     # 天有洪炉
CHA = "污水×1 + 液化息壤×1 → 惰性壤晶废液×1 + 壤晶废液×1（2秒）"     # 扩容反应池
CHB = "清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"
ORE, CRU, REF, PART = "蓝铁矿×1 → 蓝铁块×1（2秒）", "蓝铁块×1 → 蓝铁粉末×1（2秒）", "蓝铁粉末×1 → 蓝铁块×1（2秒）", "蓝铁块×1 → 铁制零件×1（2秒）"


def _rb(sc, fixes=None, **kw):
    """走 run_base（三遍合并，写 design.json、gates_view.json）→ (结果, {节点键: 准入口表那一行}, {"x,y": 设备行})。
    fixes：fixes.json 这个据点再加几段（池子配方）；kw 交给 GF._scene（gates_fix、limits）。"""
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        bd, led, rec, gl, nid = GF._scene(td, sc, **kw)
        if fixes:
            fp = bd.parent.parent / "fixes.json"
            fx = json.loads(fp.read_text(encoding="utf-8")) if fp.exists() else {}
            fx.setdefault("测试", {}).update(fixes)
            fp.write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
        res = D.run_base(bd, GF._archive(td, led, rec, gl), "测试")
        gv = {r["gate"]: r for r in json.loads((bd / "gates_view.json").read_text(encoding="utf-8"))["gates"]}
    return res, {k: gv[i] for k, i in nid.items() if i in gv}, {f"{r['x']},{r['y']}": r for r in res["devices"]}


def _no_probe(f, *a, **kw):
    """关掉 probe（D.PROBE = False）跑 f(*a, **kw)，跑完还原。"""
    D.PROBE = False
    try:
        return f(*a, **kw)
    finally:
        D.PROBE = True


def _nums(res, gv):
    """probe 不能改的数字：每台开工率、到手、available、limit_by、并行池子每条开工率、pass_out，每个口的 flow / held，pass_split。"""
    return ({k: (r["rate"], r["got"], r.get("available"), r.get("limit_by"), [c["rate"] for c in r.get("channels") or []], r.get("pass_out"))
             for k, r in ((f"{r['x']},{r['y']}", r) for r in res["devices"])},
            {k: (g["flow"], g["held"]) for k, g in gv.items()}, res.get("pass_split"))


def _cross():
    """种植机 X (850,300)（清水 + 芽针种子 → 芽针），产物只能经限量口 LX (930,300) 出到仓库存货口。两种原料各来自一边：
    - 清水：扩容反应池 R (300,300)（作者勾两条并行：①污水 + 液化息壤 → 惰性壤晶废液 + 壤晶废液；②清水 + 芽针粉末 → 芽针溶液，出到仓库存货口）
      ①的壤晶废液只能经管道限量口 L1 (450,450) 出到天有洪炉，惰性壤晶废液给提纯机 (600,300)，提纯机的清水经管道汇流器 NA 给 X；
    - 芽针种子：输入端 6 号芽针 30 → 分流器 S1，主路 → 粉碎机 Z (300,800)（一路直连精炼炉，一路过限量口 L2 (400,900) 到精炼炉，
      只因为 L2 跑 50%）；备路（单进单出汇流器 M1）→ 采种机 (600,700) → 物流桥 NB2 → X。
    probe 1（L1、L2 照样挡）：①的下游需求上限 0、停，X 没有清水；Z 只吃 15，备路 15 → X 有芽针种子。
    probe 2（堵死型的口当放行）：①在跑，X 有清水；Z 吃满 30，备路一点没有 → X 没有芽针种子。"""
    nodes = {"L1": ("准入口", (450, 450), "管道"), "NA": ("管道汇流器", (750, 300), "管道"), "LX": ("准入口", (930, 300), "物品"),
             "S1": ("分流器", (200, 700), "物品"), "M1": ("汇流器", (400, 700), "物品"), "NB2": ("物流桥", (750, 700), "物品"),
             "L2": ("准入口", (400, 900), "物品")}
    links = [(0, "L1", "pipe", ["壤晶废液"]), ("L1", 1, "pipe"), (0, 3, "pipe", ["惰性壤晶废液"]), (0, 2, "pipe", ["芽针溶液"]),
             (3, "NA", "pipe", ["清水"]), ("NA", 4, "pipe"), (4, "LX", "belt"), ("LX", 5, "belt"),
             ("S1", 7, "belt"), ("S1", "M1", "belt"), ("M1", 6, "belt"), (6, "NB2", "belt"), ("NB2", 4, "belt"),
             (7, 8, "belt"), (7, "L2", "belt"), ("L2", 9, "belt")]
    return {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (600, 450), HLR), ("仓库存货口", (300, 100), None),
                        ("提纯机", (600, 300), TC), ("种植机", (850, 300), PL), ("仓库存货口", (990, 300), None),
                        ("采种机", (600, 700), SC_SEED), ("粉碎机", (300, 800), CR), ("精炼炉", (550, 850), RF), ("精炼炉", (550, 980), RF)],
            "nodes": nodes,
            "gates": [(1, (100, 250), "污水", 30, 0), (2, (100, 300), "液化息壤", 30, 0), (3, (100, 350), "清水", 30, 0),
                      (4, (100, 400), "芽针粉末", 30, 0), (6, (40, 700), "芽针", 30, "S1")],
            "links": links}


CROSS_KW = dict(gates_fix={**_once((450, 450), kind="管道"), **_once((930, 300)), **_once((400, 900))},
                fixes={"pool_recipes": {"扩容反应池@300,300": [CHA, CHB]}})


def test_mixed_probe_limit_by():
    """种植机 X 的芽针种子只在 probe 1 有、清水只在 probe 2 有：状态是估算的已知错（判间歇，试过「同一遍」又撤回，见下一条），这里不锁状态，
    只锁 probe 不动的数字（开工率、到手、limit_by 清水，和关掉 probe 一样），另锁三个口判对了的 shut（L1 那个是 probe 补上的）。"""
    _, gv, dv = _rb(_cross(), **CROSS_KW)
    _, _, dv0 = _no_probe(_rb, _cross(), **CROSS_KW)
    x, x0 = dv["850,300"], dv0["850,300"]
    assert x["rate"] == x0["rate"] == 0.0 and x["got"] == x0["got"] == {"清水": 0.0, "芽针种子": 15.0}, (x, x0)
    assert x["limit_by"] == x0["limit_by"] == "清水", (x, x0)
    assert gv["LX"]["shut"] == [] and gv["L1"]["shut"] == ["壤晶废液"] and gv["L2"]["shut"] == ["芽针粉末"], (gv["LX"], gv["L1"], gv["L2"])


def _two_rivals():
    """研磨机 X (800,400) 两种原料都是备路溢流来的：碳粉末来自主路研磨机 C 吃不完的 6（C 一路限速 12、一路限量），砂叶粉末来自主路粉碎机 Z
    （只能往限量口出、永远不吃）让给备路粉碎机的。probe 1 放开 Z、没了砂叶粉末；probe 2 放开 C、没了碳粉末。"""
    sy = "砂叶×1 → 砂叶粉末×3（2秒）"
    return {"devices": [("研磨机", (800, 400), GD), ("研磨机", (420, 150), GD), ("粉碎机", (420, 800), sy), ("粉碎机", (420, 550), sy),
                        ("仓库存货口", (940, 150), None), ("仓库存货口", (950, 400), None), ("仓库存货口", (800, 800), None)],
            "nodes": {"S1": ("分流器", (150, 200), "物品"), "M1": ("汇流器", (250, 360), "物品"), "B1": ("物流桥", (600, 360), "物品"),
                      "S2": ("分流器", (150, 700), "物品"), "M2": ("汇流器", (250, 560), "物品"), "B2": ("物流桥", (620, 500), "物品"),
                      "C1": ("准入口", (600, 110), "物品"), "C2": ("准入口", (600, 200), "物品"),
                      "LX": ("准入口", (880, 400), "物品"), "LZ": ("准入口", (600, 800), "物品")},
            "gates": [(1, (40, 200), "碳粉末", 30, "S1"), (2, (420, 40), "砂叶粉末", 30, 1), (3, (40, 700), "砂叶", 30, "S2")],
            "links": [("S1", 1, "belt"), ("S1", "M1", "belt"), ("M1", "B1", "belt"), ("B1", 0, "belt"),
                      (1, "C1", "belt"), ("C1", 4, "belt"), (1, "C2", "belt"), ("C2", 4, "belt"),
                      ("S2", 2, "belt"), ("S2", "M2", "belt"), ("M2", 3, "belt"), (3, "B2", "belt"), ("B2", 0, "belt"),
                      (2, "LZ", "belt"), ("LZ", 6, "belt"), (0, "LX", "belt"), ("LX", 5, "belt")]}


def test_fed_in_either_probe():
    """（甲）每种原料哪一遍到手 > 0 都算有：两遍各放开一台抢料的设备时，不能要求同一遍凑齐（第三轮核实 2026-11-22 撤回的回退：
    游戏里两种料同时到 X、只是出不去，却写「没料；卡在：碳粉末（能送到 0/60）」，summary 多算一台没料，导入方照抄）。"""
    fx = {**_once((600, 200)), **_once((880, 400)), **_once((600, 800)),
          D.gate_key(GF.BP, (600, 110)): GF._fix((600, 110), 12, None, item="")}
    res, _, dv = _rb(_two_rivals(), gates_fix=fx)
    x = dv["800,400"]
    assert x["state"] == "间歇" and x["fed_blocked"] == ["碳粉末"] and _fed("碳粉末") in x["basis"], x
    assert not any(s.startswith("卡在：") for s in x["basis"]) and res["summary"]["states"]["没料"] == 0, (x["basis"], res["summary"])


def _pool_dead(powder=None, water=60):
    """反应池 R (300,300)（并行：芽针溶液、锦草溶液），清水输入端 1 号 water；芽针粉末 2 号、锦草粉末 3 号各 powder/分（None = 不接）。
    芽针溶液只能经管道口 L1 (500,220) 出到灌装机 F1 (700,220)，锦草溶液只能经管道口 L2 (500,380) 出到 F2 (700,380)（赤铜瓶各 30）。"""
    gates = [(1, (40, 300), "清水", water, 0), (4, (700, 60), "赤铜瓶", 30, 1), (5, (700, 540), "赤铜瓶", 30, 2)]
    if powder is not None:
        gates += [(2, (150, 200), "芽针粉末", powder, 0), (3, (150, 420), "锦草粉末", powder, 0)]
    return {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 220), GF.R["芽针瓶"]), ("灌装机", (700, 380), GF.R["锦草瓶"])],
            "nodes": {"L1": ("准入口", (500, 220), "管道"), "L2": ("准入口", (500, 380), "管道")},
            "gates": gates,
            "links": [(0, "L1", "pipe", ["芽针溶液"]), ("L1", 1, "pipe"), (0, "L2", "pipe", ["锦草溶液"]), ("L2", 2, "pipe")]}


POOL_ONCE = {**_once((500, 220), kind="管道"), **_once((500, 380), kind="管道")}
POOL_LIM0 = {D.gate_key(GF.BP, p): GF._fix(p, 0, None, kind="管道", item="") for p in ((500, 220), (500, 380))}
POOL_IN_ONCE = {D.gate_key(GF.BP, p): GF._fix(p, None, 20, item=it) for p, it in (((150, 200), "芽针粉末"), ((150, 420), "锦草粉末"))}


def _pool_fed_elsewhere():
    """对照：反应池 (300,300) 两条的产物直接给灌装机（不经任何准入口），两台灌装机另有输入端送芽针溶液 / 锦草溶液 30、已经吃饱 →
    下游吃得下 0、两条都截到 0（别的原因）；粉末也没有。另有一条 输入端芽针 30 → 粉碎机 (300,700) → 限量口 LS (450,700) → 精炼炉 (600,700)，
    让两遍 probe 都跑（probe 2 里这台池子的下游需求上限还是 0）。"""
    return {"devices": [("反应池", (300, 300), GF.POOL2), ("灌装机", (700, 220), GF.R["芽针瓶"]), ("灌装机", (700, 380), GF.R["锦草瓶"]),
                        ("粉碎机", (300, 700), CR), ("精炼炉", (600, 700), RF)],
            "nodes": {"LS": ("准入口", (450, 700), "物品")},
            "gates": [(1, (40, 300), "清水", 60, 0), (4, (700, 60), "赤铜瓶", 30, 1), (5, (700, 540), "赤铜瓶", 30, 2),
                      (7, (850, 220), "芽针溶液", 30, 1), (8, (850, 380), "锦草溶液", 30, 2), (6, (40, 700), "芽针", 30, 3)],
            "links": [(0, 1, "pipe", ["芽针溶液"]), (0, 2, "pipe", ["锦草溶液"]), (3, "LS", "belt"), ("LS", 4, "belt")]}


def test_parallel_pool_all_capped_idle():
    """并行池子每条配方都被出料上限截到 0：一律写间歇，probe 不改它的状态（最后一轮复审 2026-11-22 试过按 probe 判没料，当天撤回：
    有料的会判成没料、列假「卡在」，状态一变 run_base 还多跑主路堵死那一遍、多出开工率）。已知限制：原料真没有也写间歇、不列「卡在」：
    P1 两种粉末都没来路、P1c 粉末输入端开了限制通过数量、P1d 清水 0、P1z 出口换成限速 0；P2 原料都有，L1 shut [芽针溶液]。
    走 run_base：状态、limit_by、数字和关掉 probe 一样，没有 if_main_blocked。
    对照：两条是因为下游灌装机已经吃饱才截到 0（不经任何准入口）、粉末也没有 → 间歇，同一据点里粉碎机照样按 probe 判间歇。"""
    water0 = _pool_dead(30, water=0)
    cases = [("P1", _pool_dead(), POOL_ONCE), ("P1c", _pool_dead(30), {**POOL_ONCE, **POOL_IN_ONCE}),
             ("P1d", water0, POOL_ONCE), ("P1z", _pool_dead(), POOL_LIM0), ("P2", _pool_dead(30), POOL_ONCE)]
    for name, sc, fx in cases:
        res, gv, dv = _rb(sc, gates_fix=fx)
        r = dv["300,300"]
        assert r["rate"] == 0.0 and [c["rate"] for c in r["channels"]] == [0.0, 0.0] and r["state"] == "间歇", (name, r)
        assert r["limit_by"] is None and "fed_blocked" not in r and "if_main_blocked" not in r, (name, r)
        assert not any(s.startswith("卡在：") for s in r["basis"]), (name, r["basis"])
        if name == "P2":
            assert gv["L1"]["shut"] == ["芽针溶液"] and gv["L2"]["shut"] == ["锦草溶液"], (gv["L1"], gv["L2"])
        res0, gv0, dv0 = _no_probe(_rb, sc, gates_fix=fx)
        assert dv0["300,300"]["state"] == r["state"], (name, dv0["300,300"])
        assert _nums(res, gv) == _nums(res0, gv0), name
    # 到手全是 0、原料都经分流器有来路（另一路各过一个限量口）的也是间歇：不能拿「到手全 0」当没料
    sp = _pool_dead(30)
    sp["nodes"].update(SW=("管道分流器", (120, 300), "管道"), SA=("分流器", (200, 180), "物品"), SB=("分流器", (200, 440), "物品"),
                       DW=("准入口", (120, 520), "管道"), DA=("准入口", (200, 60), "物品"), DB=("准入口", (200, 560), "物品"))
    ways = (("SW", "DW", "pipe"), ("SA", "DA", "belt"), ("SB", "DB", "belt"))
    sp["links"] += [x for s, g, la in ways for x in ((s, 0, la), (s, g, la), (g, 0, la))]
    sp["gates"] = [(1, (40, 300), "清水", 60, "SW"), (4, (700, 60), "赤铜瓶", 30, 1), (5, (700, 540), "赤铜瓶", 30, 2),
                   (2, (60, 180), "芽针粉末", 30, "SA"), (3, (60, 440), "锦草粉末", 30, "SB")]
    _, _, dv = _rb(sp, gates_fix={**POOL_ONCE, **_once((120, 520), kind="管道"), **_once((200, 60)), **_once((200, 560))})
    r = dv["300,300"]
    assert set(r["got"].values()) == {0.0} and r["state"] == "间歇", r
    # 两样合起来：下游吃饱、三种料又都经分流器有来路，两遍 probe 里到手也全是 0，照样间歇（不能拿「probe 里全 0」当没料）
    sp = _pool_fed_elsewhere()
    sp["nodes"].update(SW=("管道分流器", (120, 300), "管道"), SA=("分流器", (200, 180), "物品"), SB=("分流器", (200, 440), "物品"),
                       DW=("准入口", (120, 520), "管道"), DA=("准入口", (200, 60), "物品"), DB=("准入口", (200, 560), "物品"))
    sp["links"] += [x for s, g, la in ways for x in ((s, 0, la), (s, g, la), (g, 0, la))]
    sp["gates"] = [(1, (40, 300), "清水", 60, "SW"), (2, (60, 180), "芽针粉末", 30, "SA"), (3, (60, 440), "锦草粉末", 30, "SB")] + sp["gates"][1:]
    res, _, dv = _rb(sp, gates_fix={**_once((450, 700)), **_once((120, 520), kind="管道"), **_once((200, 60)), **_once((200, 560))})
    r = dv["300,300"]
    assert set(r["got"].values()) == {0.0} and r["state"] == "间歇" and res["summary"]["states"]["没料"] == 1, r
    for probe in (True, False):
        kw = dict(gates_fix=_once((450, 700)))
        _, _, dv = _an(_pool_fed_elsewhere(), **kw) if probe else _no_probe(_an, _pool_fed_elsewhere(), **kw)
        p = dv["300,300"]
        assert [c["rate"] for c in p["channels"]] == [0.0, 0.0] and p["got"]["芽针粉末"] == 0.0 and p["state"] == "间歇", (probe, p)
        assert p["basis"][0].startswith("出料受限：") and dv["700,220"]["rate"] == 1.0, (probe, p["basis"], dv["700,220"])
        assert dv["300,700"]["state"] == ("间歇" if probe else "没料"), (probe, dv["300,700"])  # probe 确实跑了


def test_sole_gate_still_named():
    """fed_blocked 的料也照写「上游准入口…长期按不送X算」（第三轮核实 2026-11-22 撤回「不点名」）：反应池 X 的污水只有两路，限量口 L1，和一台
    赤铜块只能往限量口出、游戏里永远不产污水的精炼炉（只在 probe 里产）。L1 是唯一缺料的口，不点名就一句都看不到它。"""
    sc = {"devices": [("反应池", (600, 300), CHA), ("精炼炉", (300, 450), "赤铜矿×1 + 清水×1 → 赤铜块×1 + 污水×1（2秒）"),
                      ("仓库存货口", (900, 300), None), ("仓库存货口", (500, 700), None)],
          "nodes": {"L1": ("准入口", (300, 200), "管道"), "LX": ("准入口", (750, 300), "管道"), "LP": ("准入口", (400, 620), "物品")},
          "gates": [(1, (100, 200), "污水", 30, "L1"), (2, (600, 100), "液化息壤", 30, 0), (3, (100, 420), "赤铜矿", 30, 1),
                    (4, (100, 520), "清水", 30, 1)],
          "links": [("L1", 0, "pipe"), (1, 0, "pipe"), (0, "LX", "pipe"), ("LX", 2, "pipe"), (1, "LP", "belt"), ("LP", 3, "belt")]}
    fx = {**_once((300, 200), kind="管道"), **_once((750, 300), kind="管道"), **_once((400, 620))}
    _, _, dv = _rb(sc, gates_fix=fx)
    x = dv["600,300"]
    assert x["rate"] == 0.0 and x["got"]["污水"] == 0.0 and _last(x) == _once_at((300, 200), "污水"), x


# f1：碳粉末 60 经分流器 S 分给研磨机 D (600,300)、E (600,500)；D 另有砂叶粉末直送 15 和经限量口 L1 (450,200) 的 30，产物只能走限量口 L2 (750,300)
F1_SC = {"devices": [("研磨机", (600, 300), GD), ("研磨机", (600, 500), GD), ("精炼炉", (900, 300), DF)],
         "nodes": {"S": ("分流器", (400, 400), None), "L1": ("准入口", (450, 200), "物品"), "L2": ("准入口", (750, 300), "物品")},
         "gates": [(1, (40, 400), "碳粉末", 60, "S"), (2, (600, 100), "砂叶粉末", 15, 0), (3, (600, 700), "砂叶粉末", 30, 1),
                   (4, (40, 200), "砂叶粉末", 30, "L1")],
         "links": [("S", 0, "belt"), ("S", 1, "belt"), ("L1", 0, "belt"), (0, "L2", "belt"), ("L2", 2, "belt")]}


def test_run_base_limit_by_unchanged():
    """run_base 的 limit_by 不跟 probe 变（最后一轮复审 consumers #2）：研磨机 D 出料堵死、碳粉末到手 0 只是因为出不去没分给它（fed_blocked），
    砂叶粉末真只有 15/30。收尾时「卡在」跳过 fed_blocked 的料，limit_by 也跟着从剩下的取，开 probe 成了砂叶粉末、关 probe 是碳粉末
    （文档写「limit_by 的数不动」）。现在 limit_by 照旧按全部原料排（开 / 关都是碳粉末），只有「卡在：」那句不列碳粉末、照列砂叶粉末；
    available 开 / 关一样。"""
    fx = {**_once((450, 200)), **_once((750, 300))}
    _, _, on = _rb(F1_SC, gates_fix=fx)
    _, _, off = _no_probe(_rb, F1_SC, gates_fix=fx)
    d, d0 = on["600,300"], off["600,300"]
    assert d["state"] == "间歇" and d["fed_blocked"] == ["碳粉末"] and d0["state"] == "没料" and "fed_blocked" not in d0, (d, d0)
    assert d["limit_by"] == d0["limit_by"] == "碳粉末", (d["limit_by"], d0["limit_by"])
    assert d["available"] == d0["available"] == {"碳粉末": 0.0, "砂叶粉末": 15.0}, (d["available"], d0["available"])
    assert d["basis"][0] == "卡在：砂叶粉末（能送到 15/30）" and d0["basis"][0] == "卡在：碳粉末（能送到 0/60）、砂叶粉末（能送到 15/30）", \
        (d["basis"], d0["basis"])
    assert sum(s.startswith("卡在：") for s in d["basis"]) == 1 and _fed("碳粉末") in d["basis"], d["basis"]
    assert _last(d) == _once_at((450, 200), "砂叶粉末"), d["basis"]  # 真缺的那种照写是哪个口


def _snake(k, per=5, dx=170, dy=150, x0=90, y0=70):
    """长链的第 k 台蛇形排开（每行 per 台）。"""
    r, c = divmod(k, per)
    return (x0 + (per - 1 - c if r % 2 else c) * dx, y0 + r * dy)


def _iron_chain(n):
    """输入端蓝铁矿 0（平时不来料）→ 精炼炉 / 粉碎机交替串 n 台 → 限量口 L (930,880) → 末台 (930,960)。"""
    devs = [("精炼炉", _snake(k), ORE) if k == 0 else ("粉碎机", _snake(k), CRU) if k % 2 else ("精炼炉", _snake(k), REF) for k in range(n)]
    devs.append(("配件机", (930, 960), PART) if n % 2 else ("精炼炉", (930, 960), REF))
    return {"devices": devs, "nodes": {"L": ("准入口", (930, 880), "物品")}, "gates": [(1, (30, 20), "蓝铁矿", 0, 0)],
            "links": [(k, k + 1, "belt") for k in range(n - 1)] + [(n - 1, "L", "belt"), ("L", n, "belt")]}


def test_probe_rounds_enough_for_short_chain():
    """probe 的轮数也不能比主分析少：断料每轮只往下传一台，19 台以内的断料链主分析、probe 都算得完 → 链尾没料、L 的 shut 空、没有提醒
    （改成 5、10 轮时 6、11 台的链就是假间歇）。20 台起判错是已知限制，这里不锁。"""
    for n in (6, 11, 19):
        res, gv, dv = _an(_iron_chain(n), gates_fix=_once((930, 880)))
        tail = dv["{},{}".format(*_snake(n - 1))]
        assert not res["notes"] and tail["state"] == "没料" and "fed_blocked" not in tail and gv["L"]["shut"] == [], (n, tail, gv["L"])


def _backup_chain(n=20):
    """输入端蓝铁矿 30 → 分流器 S：主路 → 精炼炉 X (250,60)，只能经限量口 L1 (450,60) 出到配件机；备路（单进单出汇流器 M）→ 精炼炉 / 粉碎机
    交替 n 台的长链 → 链尾 Z (750,960)（只能经限量口 L2 (930,900) 出）。"""
    devs = [("精炼炉", (250, 60), ORE), ("配件机", (650, 60), PART)]
    links = [("S", 0, "belt"), (0, "L1", "belt"), ("L1", 1, "belt"), ("S", "M", "belt"), ("M", 2, "belt")]
    for k in range(n):
        p = _snake(k, dy=110, y0=340)
        devs.append(("精炼炉", p, ORE) if k == 0 else ("粉碎机", p, CRU) if k % 2 else ("精炼炉", p, REF))
    z = len(devs)
    devs += [("粉碎机", (750, 960), CRU), ("精炼炉", (960, 960), REF)] if n % 2 else [("精炼炉", (750, 960), REF), ("配件机", (960, 960), PART)]
    links += [(2 + k, 3 + k, "belt") for k in range(n - 1)] + [(z - 1, z, "belt"), (z, "L2", "belt"), ("L2", z + 1, "belt")]
    return {"devices": devs, "nodes": {"S": ("分流器", (100, 150), "物品"), "M": ("汇流器", (100, 260), "物品"),
                                       "L1": ("准入口", (450, 60), "物品"), "L2": ("准入口", (930, 900), "物品")},
            "gates": [(1, (30, 150), "蓝铁矿", 30, "S")], "links": links}


def _seed_lines(*waters):
    """互不相连的几套种植线：种植机 ↔ 采种机的环（清水 30）→ 粉碎机 X (500,700)（只能往限量口 LX 出）→ 精炼炉；种子分流器另一路是种植机 C
    （清水只有 w、只能往限量口出，游戏里不吃，probe 里一起放开、每圈漏掉一点）。第 k 套整体右移 k×1300。"""
    sd, sc = "芽针×1 → 芽针种子×1（2秒）", {"devices": [], "nodes": {}, "gates": [], "links": []}
    fx = {}
    for k, w in enumerate(waters):
        o, p, n = len(sc["devices"]), (lambda x, y: (x + 1300 * k, y)), (lambda s: f"{s}{k}" if k else s)
        sc["devices"] += [("种植机", p(200, 300), PL), ("采种机", p(500, 300), sd), ("粉碎机", p(500, 700), CR), ("种植机", p(800, 150), PL),
                          ("精炼炉", p(900, 700), RF), ("仓库存货口", p(950, 150), None)]
        sc["nodes"].update({n("SS"): ("分流器", p(600, 200), "物品"), n("SP"): ("分流器", p(300, 450), "物品"),
                            n("LX"): ("准入口", p(700, 700), "物品"), n("LC"): ("准入口", p(880, 150), "物品")})
        sc["gates"] += [(1 + 20 * k, p(60, 300), "清水", 30, o), (2 + 20 * k, p(800, 40), "清水", w, o + 3)]
        sc["links"] += [(o, n("SP"), "belt"), (o, o + 1, "belt"), (n("SP"), o + 2, "belt"), (o + 1, n("SS"), "belt"), (n("SS"), o, "belt"),
                        (n("SS"), o + 3, "belt"), (o + 2, n("LX"), "belt"), (n("LX"), o + 4, "belt"),
                        (o + 3, n("LC"), "belt"), (n("LC"), o + 5, "belt")]
        fx.update({**_once(p(700, 700)), **_once(p(880, 150))})
    return sc, fx


def test_probe_neighbour_line_independent():
    """内层是整个据点一起判收敛的：probe 的轮数不能放宽（第三轮核实 2026-11-22 撤回的回退：放宽到 5 倍时，旁边一套慢慢漏料的种植线把这一套
    从 19 轮拖到漏干，粉碎机 X 从间歇变「没料；卡在：芽针（能送到 0/30）」、LX 的 shut 空、下游不点名，主分析没有提醒）。"""
    got = []
    for waters in ((30,), (30, 1)):
        sc, fx = _seed_lines(*waters)
        res, gv, dv = _rb(sc, gates_fix=fx)
        x, y = dv["500,700"], dv["900,700"]
        assert x["state"] == "间歇" and gv["LX"]["shut"] == ["芽针粉末"], (waters, x, gv["LX"])
        assert _last(y) == _once_at((700, 700), "芽针粉末"), (waters, y["basis"])
        got.append((x["state"], x["basis"], y["state"], y["basis"]))
    assert got[0] == got[1], got


def _unsettled_ring(ring=True):
    """粉碎机 X (100,900)（输入端芽针 30）→ 限量口 LX (300,900) → 精炼炉 (500,900)。ring：同一据点另有一片不相干、probe 里来回跳的：
    池子和种植机 ↔ 采种机的环共用清水 55（test_capping_keeps_outer_converging 的形状），环上粉碎机 C (500,560) 的去向是死路（连线只走锦草粉末）。"""
    devs, nodes, gates, links = [], {}, [], []
    if ring:
        devs = [("反应池", (300, 300), GF.POOL2), ("灌装机", (600, 200), GF.R["芽针瓶"]), ("灌装机", (600, 400), GF.R["锦草瓶"]),
                ("种植机", (300, 600), "清水×1 + 芽针种子×1 → 芽针×2（2秒）"), ("采种机", (500, 700), "芽针×1 → 芽针种子×1（2秒）"),
                ("粉碎机", (500, 560), CR), ("精炼炉", (900, 560), RF), ("灌装机", (900, 100), "赤铜瓶×1 + 清水×1 → 赤铜瓶(清水)×1（2秒）")]
        nodes = {"S1": ("管道分流器", (150, 450), "管道"), "S2": ("分流器", (800, 50), "物品"), "N": ("物流桥", (700, 560), "物品")}
        gates = [(1, (40, 450), "清水", 55, "S1"), (2, (150, 250), "芽针粉末", 10, 0), (3, (150, 350), "锦草粉末", 30, 0),
                 (4, (800, 10), "赤铜瓶", 30, "S2"), (5, (800, 400), "赤铜瓶", 15, 2), (7, (990, 200), "清水", 30, 7)]
        links = [("S1", 0, "pipe"), (0, 1, "pipe"), (0, 2, "pipe"), (3, 4, "belt"), (4, 3, "belt"), (3, 5, "belt"),
                 ("S1", 3, "pipe"), ("S2", 1, "belt"), ("S2", 7, "belt"), (5, "N", "belt"), ("N", 6, "belt", ["锦草粉末"])]
    x = len(devs)
    devs += [("粉碎机", (100, 900), CR), ("精炼炉", (500, 900), RF)]
    nodes["LX"] = ("准入口", (300, 900), "物品")
    gates.append((9, (40, 900), "芽针", 30, x))
    links += [(x, "LX", "belt"), ("LX", x + 1, "belt")]
    return {"devices": devs, "nodes": nodes, "gates": gates, "links": links}


def test_probe_unsettled_keeps_other_lines():
    """probe 是整个据点一起算的：有一片来回跳、没收敛时不能整遍作废（最后一轮复审第二轮核实 2026-11-22：作废时不相干的粉碎机
    芽针 30/30、只能往限量口出，也从间歇退成没料、LX 的 shut 变空、下游不点名，主分析一句提醒都没有）。
    有没有那片环，这条线的状态、依据、shut 一样。"""
    fx = _once((300, 900))
    got = []
    for ring in (False, True):
        res, gv, dv = _an(_unsettled_ring(ring), gates_fix=fx)
        x, y = dv["100,900"], dv["500,900"]
        assert not res["notes"] and x["state"] == "间歇" and gv["LX"]["shut"] == ["芽针粉末"], (ring, res["notes"], x, gv["LX"])
        assert _last(y) == _once_at((300, 900), "芽针粉末"), (ring, y["basis"])
        got.append((x["state"], x["basis"], y["state"], y["basis"], gv["LX"]["shut"]))
    assert got[0] == got[1], got
    # 没收敛在外层的（池子出口转送比例 16 轮定不下来）也不能作废：天有洪炉照样间歇，不相干的粉碎机线不受影响
    pk = "扩容反应池@300,300"
    sc = {"devices": [("扩容反应池", (300, 300), None), ("天有洪炉", (650, 300), HLR), ("提纯机", (300, 650), TC), ("仓库存货口", (900, 300), None),
                      ("粉碎机", (100, 900), CR), ("精炼炉", (500, 900), RF)],
          "nodes": {"m": ("管道汇流器", (500, 200), "管道"), "G2": ("准入口", (780, 300), "物品"), "LX": ("准入口", (300, 900), "物品")},
          "gates": [(1, (40, 230), "清水", 30, 0), (2, (40, 350), "污水", 10, 0), (3, (150, 150), "息壤", 30, 0), (4, (40, 600), "锦草粉末", 60, 0),
                    (6, (40, 900), "芽针", 30, 4)],
          "links": [(0, 1, "belt"), (0, "m", "pipe"), ("m", 1, "pipe"), (0, 2, "pipe"), (1, "G2", "belt"), ("G2", 3, "belt"), (4, "LX", "belt"),
                    ("LX", 5, "belt")]}
    pfix = {"pool_recipes": {pk: ["清水×1 + 息壤×1 → 液化息壤×1（2秒）", CHA, "清水×1 + 锦草粉末×1 → 锦草溶液×1（2秒）"]},
            "pool_outlets": {pk: {"belt": ["息壤"]}}}
    _, gv, dv = _rb(sc, fixes=pfix, gates_fix={**_once((780, 300)), **fx})
    assert dv["650,300"]["state"] == "间歇" and dv["100,900"]["state"] == "间歇" and gv["LX"]["shut"] == ["芽针粉末"], (dv["650,300"], dv["100,900"])
    assert _last(dv["500,900"]) == _once_at((300, 900), "芽针粉末"), dv["500,900"]["basis"]


def _probe_scenes():
    """会跑 probe 的场景 → [(名字, _an 的参数)]：并行池子按结构背压 / 下游需求上限堵死、池子出口转送（转给堵死设备、出口后面的口堵死、
    和堵死设备抢分流器）、交替配方、链式、主路备路、主路上的池子；最后一轮复审加的：两种原料各只在一遍 probe 里有、并行池子每条都被
    截到 0（原料都有、下游吃饱）、备路长链（probe 20 轮跑不完）、有一片来回跳没收敛的。"""
    q6 = {"devices": [("反应池", (300, 300), None), ("精炼炉", (650, 300), RF), ("精炼炉", (300, 600), RF), ("研磨机", (600, 600), GD),
                      ("仓库存货口", (100, 450), None)],
          "nodes": {"S": ("分流器", (150, 300), "物品"), "L": ("准入口", (450, 600), "物品")},
          "gates": [(1, (40, 300), "芽针粉末", 30, "S"), (2, (300, 100), "清水", 30, 0), (3, (600, 750), "砂叶粉末", 30, 3)],
          "links": [("S", 0, "belt"), ("S", 2, "belt"), (0, 1, "belt"), (2, "L", "belt"), ("L", 3, "belt"), (0, 4, "pipe")]}
    q7 = {"devices": [("研磨机", (200, 300), X30), ("精炼炉", (500, 200), RF), ("研磨机", (500, 450), "砂叶粉末×1 → 砂叶块×1（2秒）"),
                      ("研磨机", (800, 200), GD)],
          "nodes": {"S": ("分流器", (350, 300), "物品"), "L": ("准入口", (650, 200), "物品")},
          "gates": [(1, (40, 250), "芽针", 30, 0), (2, (40, 350), "砂叶", 15, 0), (3, (800, 60), "砂叶粉末", 30, 3)],
          "links": [(0, "S", "belt"), ("S", 1, "belt"), ("S", 2, "belt"), (1, "L", "belt"), ("L", 3, "belt")]}
    pipe_l = {D.gate_key(GF.BP, (500, 220)): GF._fix((500, 220), None, 20, kind="管道", item="")}
    return [("并行池子结构背压", dict(sc=_par_split(), gates_fix=pipe_l)),
            ("并行池子结构背压 + 下游需求上限", dict(sc=_par_split(15), gates_fix=pipe_l)),
            ("并行池子下游需求上限", dict(sc=_par_demand(), gates_fix=_once((580, 150), kind="管道"))),
            ("并行池子两种产物", dict(sc=_coproduct(), gates_fix={**_once((750, 300)), **_once((450, 500), kind="管道")}, fixes=CO_FIX)),
            ("主路上的池子", dict(sc=_pool_main(15), gates_fix=_once((480, 600)))),
            ("池子出口转给堵死设备", dict(sc=_pt_line(True), gates_fix=FX_750, fixes=PT_FIX)),
            ("池子出口后面的口堵死", dict(sc=_pt_gate_dead("芽针溶液×1 + 赤铜瓶×1 → 芽针块×1（2秒）"),
                                         gates_fix={**_once((480, 300)), **_once((500, 600))}, fixes=PT_FIX)),
            ("池子和堵死设备抢分流器", dict(sc=q6, gates_fix=_once((450, 600)), fixes=PT_FIX)),
            ("池子出口两个口", dict(sc=_pt_two_gates(), gates_fix={**_once((480, 300)), **_once((800, 300))}, fixes=PT_FIX)),
            ("主路兄弟另一路过限量口", dict(sc=_sibling_gate(), gates_fix={**_once((650, 400)), **_once((450, 250))})),
            ("交替配方", dict(sc=q7, gates_fix=_once((650, 200)), alt={(200, 300): "砂叶×1 → 砂叶粉末×1（2秒）"})),
            ("链式", dict(sc=_chain(30), gates_fix=FX_750)),
            ("主路备路", dict(sc=_backup(GRIND), gates_fix=_once((650, 400)))),
            ("两种原料各只在一遍里有", dict(sc=_cross(), **CROSS_KW)),
            ("并行池子全截到 0、原料都有", dict(sc=_pool_dead(30), gates_fix=POOL_ONCE)),
            ("有一片来回跳没收敛", dict(sc=_unsettled_ring(), gates_fix=_once((300, 900)))),
            ("并行池子下游吃饱截到 0", dict(sc=_pool_fed_elsewhere(), gates_fix=_once((450, 700)))),
            ("备路长链", dict(sc=_backup_chain(), gates_fix={**_once((450, 60)), **_once((930, 900))}))]


def _strip(res):
    """去掉 probe 管的几处：状态、依据里「有来料」那句和「上游准入口…」那句、行上的 fed_blocked，准入口表的 shut 和按 shut 认物品算的下游需求。"""
    rows = [dict({k: v for k, v in r.items() if k != "fed_blocked"}, state=None,
                 basis=[b for b in r["basis"] if "不算缺料" not in b and not b.startswith("上游准入口(")]) for r in res["devices"]]
    gates = [dict(g, shut=None, downstream=None) for g in res["gate_view"]["gates"]]
    return dict(res, devices=rows, gate_view=dict(res["gate_view"], gates=gates))


def test_probe_leaves_main_untouched():
    """probe 不能改主分析的任何结果（复审第四轮 F5：第三轮逐个放回内层写的状态，漏放一个容器测试锁不住）。现在 probe 跑完只放回外层的结果
    （池子下游需求上限和点名、出口转送比例），主分析的内层从头重跑。会跑 probe 的场景各跑一遍，再关掉 probe（D.PROBE = False）跑一遍：
    probe 管的几处确实变了（不然等于没比），去掉这几处以后结果一个字都不差（开工率、到手、并行池子每条、出口转送、口的通过 / 挡下、
    依据、提醒、迭代轮数……）。"""
    for name, kw in _probe_scenes():
        on = _an(**kw)[0]
        D.PROBE = False
        try:
            off = _an(**kw)[0]
        finally:
            D.PROBE = True
        assert on != off, name
        a, b = _strip(on), _strip(off)
        bad = [k for k in a if a[k] != b[k]]
        assert not bad, (name, bad, [(x, y) for x, y in zip(a["devices"], b["devices"]) if x != y][:1])


UI = ROOT / "src" / "app" / "ui"


def _ui_eval(exprs: list[str]) -> list[str]:
    """app.js 里准入口表的几个渲染函数（连同 esc、gnum、bpaGateDirty）原样抽出来，用 node 逐个求值 → 去掉 HTML 标签后的文字。
    不载入整个界面（要 DOM）；本机没有 node 就跳过。"""
    node = shutil.which("node")
    if not node:
        skip("本机没有 node")
    js = (UI / "app.js").read_text(encoding="utf-8")
    parts = []
    for name in ("esc", "gnum"):
        m = re.search(r"^const " + name + r" = .*$", js, re.M)
        assert m, name
        parts.append(m.group(0))
    for name in ("bpaGateFlow", "bpaGateReadWords", "bpaGateSrc", "bpaGateDirty"):
        m = re.search(r"^function " + name + r"\(.*?^\}\r?$", js, re.M | re.S)
        assert m, name
        parts.append(m.group(0))
    prog = ("'use strict';\nconst bpa = {gEdits: {}, gt: {read_date: '11-09', rows: []}};\n" + "\n".join(parts)
            + "\nconst out = [" + ",\n".join(exprs) + "].map(s => String(s).replace(/<[^>]+>/g, ''));\n"
            + "process.stdout.write(JSON.stringify(out));\n")
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "gate_ui.js"
        p.write_text(prog, encoding="utf-8")
        cp = subprocess.run([node, str(p)], capture_output=True, timeout=60)
    assert cp.returncode == 0, cp.stderr.decode("utf-8", "replace")[:300]
    return json.loads(cp.stdout.decode("utf-8"))


# f3：粉碎机 P (300,300)（输入端芽针 30）→ 限量口 L1 (450,300) → 限量口 L2 (550,300) → 精炼炉 Q (700,300)
F3_SC = {"devices": [("粉碎机", (300, 300), CR), ("精炼炉", (700, 300), RF)],
         "nodes": {"L1": ("准入口", (450, 300), "物品"), "L2": ("准入口", (550, 300), "物品")},
         "gates": [(1, (40, 300), "芽针", 30, 0)],
         "links": [(0, "L1", "belt"), ("L1", "L2", "belt"), ("L2", 1, "belt")]}
# f4：输入端 1 号芽针 30 → 汇流器 M → 限量口 L (500,300) → 分流器 S → 粉碎机 Q1 (750,200)、精炼炉 Q2 (750,400)；
# 粉碎机 P (300,500)（输入端 2 号芽针 30）的芽针粉末 → M
F4_SC = {"devices": [("粉碎机", (300, 500), CR), ("粉碎机", (750, 200), CR), ("精炼炉", (750, 400), RF)],
         "nodes": {"M": ("汇流器", (400, 300), None), "L": ("准入口", (500, 300), "物品"), "S": ("分流器", (600, 300), None)},
         "gates": [(1, (40, 300), "芽针", 30, "M"), (2, (300, 650), "芽针", 30, 0)],
         "links": [(0, "M", "belt"), ("M", "L", "belt"), ("L", "S", "belt"), ("S", 1, "belt"), ("S", 2, "belt")]}


def test_gate_ui_words():
    """准入口表界面文字（审查、复审 2026-11-21，原来只有 scratchpad 里的自查脚本验过）：
    - 「设计分析里」一栏：开了限制通过数量、没有通过 / 被挡下的量的线内口，shut 有料写「上游要往这里送X」；shut 空的分开说：上下游都追到了
      写「分析里上游没有料要往这里送」（上游设备自己没料），上游没追到写「没追到上游设备」，只是出边没追到下游设备（皇帝产线武陵城 (853,780)）
      不说上游没追到；输入端开了的另标「按不来料算」；没开的口照旧「没有料经过」；
    - 来源一栏：作者改过、游戏里读到限制通过数量开着但数量没读出来，括号里不再套括号、不写「设计分析不按它挡」；游戏读的那一行照旧另说一句；
    - 表上说明、帮助：原料有、只是出不去的写间歇，原料真缺的照旧写没料；
    - 最后一轮复审（2026-11-22，consumers #3、#4；行是 analyze 算出来的准入口表行）：两个限量口串着（f3），后面的 L2 shut 空、上下游都追到，
      括号里加「或前面还有口已经挡住」（上游粉碎机原料吃饱、也不是分料）；前面的 L1 下游需求被 L2 截成 0，括号里加「后面还有口挡住」。
      一个口上挡下芽针 30、shut 里还有推料账上没有的芽针粉末（f4，物品一栏「看不出」）：写料名，加「上游还要往这里送芽针粉末（按不放行算）」。"""
    _, g3, d3 = _an(F3_SC, gates_fix={**_once((450, 300)), **_once((550, 300))})
    assert d3["300,300"]["state"] == "间歇" and d3["300,300"]["got"] == {"芽针": 30.0}, d3["300,300"]
    assert g3["L1"]["shut"] == ["芽针粉末"] and g3["L1"]["downstream"] == 0 and g3["L1"]["to"], g3["L1"]
    assert g3["L2"]["shut"] == [] and g3["L2"]["downstream"] == 30 and g3["L2"]["from"] and g3["L2"]["to"], g3["L2"]
    _, g4, d4 = _an(F4_SC, gates_fix=_once((500, 300)))
    L4 = g4["L"]
    assert L4["flow"] == {} and L4["held"] == {"芽针": 30} and L4["shut"] == ["芽针", "芽针粉末"] and P.gate_item(L4) == "", L4
    assert _last(d4["750,400"]) == _once_at((500, 300), "芽针粉末"), d4["750,400"]["basis"]
    rows = [json.dumps(dict(r, group="有料"), ensure_ascii=False) for r in (g3["L1"], g3["L2"], L4)]
    up, down = "[{name: '粉碎机', c: [300, 300]}]", "[{name: '研磨机', c: [600, 300]}]"
    once = "开了限制通过数量，设计分析按不放行算（长期 0）"
    got = _ui_eval([
        f"bpaGateFlow({{group: '有料', flow: {{}}, held: {{}}, shut: ['芽针粉末'], count: 20, downstream: 30, from: {up}, to: {down}}})",
        f"bpaGateFlow({{group: '没追到', flow: {{}}, held: {{}}, shut: [], count: 20, downstream: 30, from: {up}, to: {down}}})",
        f"bpaGateFlow({{group: '没追到', flow: {{}}, held: {{}}, shut: [], count: 1, downstream: 0, from: {up}, to: []}})",
        f"bpaGateFlow({{group: '没追到', flow: {{}}, held: {{}}, shut: [], count: 20, downstream: 30, from: [], to: {down}}})",
        f"bpaGateFlow({{group: '没追到', flow: {{}}, held: {{}}, shut: [], count: null, downstream: 30, from: {up}, to: {down}}})",
        "bpaGateFlow({group: '输入端', input_no: 1, input_rate: 30, count: 20, flow: {}, held: {}})",
        f"bpaGateFlow({{group: '有料', flow: {{}}, held: {{'芽针': 30}}, shut: ['芽针'], count: 20, downstream: 30, from: [], to: {down}}})",
        "bpaGateReadWords({limit_on: false, count_on: true, count_limit: null, passed: null})",
        "bpaGateReadWords({limit_on: true, limit: 12, count_on: true, count_limit: 20, passed: 20})",
        "bpaGateSrc({key: 'k', src: '作者', count: 20, limit: null, read: {limit_on: false, count_on: true, count_limit: null}}, 0)",
        "bpaGateSrc({key: 'k', src: '游戏', count: null, limit: null, read: {limit_on: false, count_on: true, count_limit: null}}, 0)",
    ] + [f"bpaGateFlow({r})" for r in rows])
    assert got[0] == f"{once}：上游要往这里送芽针粉末；下游满速要 30/分", got[0]
    assert got[1] == f"{once}；分析里上游没有料要往这里送（上游设备自己没料、分料时分不到这一路，或前面还有口已经挡住）；下游满速要 30/分", got[1]
    assert got[2] == f"{once}；下游没追到设备", got[2]
    assert got[3] == f"{once}；没追到上游设备；下游满速要 30/分", got[3]
    assert got[4] == "没有料经过", got[4]
    assert got[5] == "输入端 1 号，填的 30/分；开了限制通过数量，设计分析按不来料算", got[5]
    assert got[6] == "通过 0/分，被挡下 30/分；下游满速要 30/分", got[6]
    assert got[7] == "：不限速，限制通过数量开着、数量没读出来", got[7]
    assert got[8] == " 12，限制通过数量 20（已放满）", got[8]
    assert got[9].startswith("你改的（游戏里读到：不限速，限制通过数量开着、数量没读出来） ") and "不按它挡" not in got[9], got[9]
    assert "游戏里读的（11-09）限制通过数量开着，数量没读出来：设计分析不按它挡" in got[10], got[10]
    assert got[11] == f"{once}：上游要往这里送芽针粉末；下游的设备不用这种料（或后面还有口挡住、连接没追到）", got[11]
    assert got[12] == f"{once}；分析里上游没有料要往这里送（上游设备自己没料、分料时分不到这一路，或前面还有口已经挡住）；下游满速要 30/分", got[12]
    assert got[13] == "芽针 通过 0/分，被挡下 30/分；上游还要往这里送芽针粉末（按不放行算）；下游满速要 30/分", got[13]
    # 「原料有」是两遍估算、会判错，池子还有例外：表上说明和帮助是同一段话，连它前后的句子一起比（只核片段时写反了也过）
    rule = ('上游生产设备的产物只能走这个口的，出不去，开工率跟着算 0，依据写「出料受限」：'
            '原料有、只是产物出不去的写间歇，不算没料。原料有没有是估的：先假设产物出不去的设备照常收料，再假设上游也不会因为产物出不去而停，各算一遍；两遍里开了限制通过数量、限速 0 的口都照样不往下送料。每种原料有一遍送得到就算有，有一种两遍都送不到的照旧写没料。估算可能判错，比如很长的断料链、绕圈供料、备路上的设备、另一路来料的上游自己也出不去。同时跑几条配方的池子例外：每条都出不去时一律写间歇，原料有没有看不出来（已知限制）。口下游第一台缺这种料的设备')
    app, hlp = (UI / "app.js").read_text(encoding="utf-8"), (UI / "help.js").read_text(encoding="utf-8")
    for text in (app, hlp):
        assert rule in re.sub(r"\n\s*", "", text) and "开了它的口一律按不送料算" in text, rule
    assert "分析里上游没有料要往这里送" in hlp and "前面还有口已经挡住" in hlp
    assert "后面还有口挡住" in hlp and "上游还要往这里送" in hlp


if __name__ == "__main__":
    sys.exit(run([test_inline_author_count, test_game_read_not_full_still_closed, test_count_unknown_not_blocked, test_input_gate_count,
                  test_pool_total_skips_count_gate, test_fed_downstream_not_named, test_through_splitter_and_storage,
                  test_parallel_pool_output_through_count_gate, test_producer_backpressure_through_count_gate,
                  test_starved_producer_stays_no_material, test_split_fed_producer_blocked, test_partly_fed_producer_blocked,
                  test_split_branch_not_wanted_no_shut, test_pool_total_zero_no_shut, test_wrong_item_not_held,
                  test_partly_fed_downstream_named, test_not_named_through_producer, test_gate_table_groups_shut_gate,
                  test_importer_names_count_gate, test_pool_outlet_through_count_gate, test_direct_link_blocked_producer,
                  test_run_base_fed_blocked_not_stuck,
                  test_pool_total_blocked_producer, test_parallel_pool_split_blocked_channel, test_chain_blocked,
                  test_one_raw_missing_stays_starved, test_shut_moving_vs_asking, test_pool_outlet_downstream_full_no_shut,
                  test_backup_path_blocked_producer, test_backup_path_sibling_capped_by_gate, test_parallel_pool_demand_capped_channel,
                  test_pool_coproduct_dead_feeds_blocked, test_pool_on_main_path_keeps_demand_cap, test_backup_gate_partly_backpressured, test_pool_outlet_to_blocked_device,
                  test_pool_outlet_gate_dead_pool_keeps, test_pool_outlet_gate_then_blocked_device, test_split_asking_pool_outlet_no_shut,
                  test_dead_end_blocked_producer, test_mixed_probe_limit_by, test_fed_in_either_probe, test_parallel_pool_all_capped_idle,
                  test_sole_gate_still_named, test_run_base_limit_by_unchanged, test_probe_neighbour_line_independent, test_probe_rounds_enough_for_short_chain, test_probe_unsettled_keeps_other_lines, test_probe_leaves_main_untouched,
                  test_gate_ui_words]))
