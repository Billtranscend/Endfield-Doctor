"""导出 .bpa.zip 时作者填的准入口限速合进 <据点>/gate_limits.json、fixes.json 带上（去掉准入口段），导入方按它们算（0.10.13）。

- 合成的行（project.gate_limits_export）当档案读数用、不带作者修正重算，每台设备的开工率、状态、到手和带修正算的一样
  （spec_gate.md 测试 9：导入方就是这样用的）；
- 导出 → 导入往返：fixes.json 放到 档案/bpa/fixes.json（准入口段不在里面），合成的限速放到 档案/ledger/gate_limits/；
  导入方的断水影响（design.cut_impact，近景巡检调它）认作者填的限速和作者的修正。
小据点照 tests/test_fix_api.py 的 project() 造，不读本机实测数据。
"""
import json
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools"), str(ROOT / "src" / "app"), str(ROOT / "tests")]

import blueprint as bpm  # noqa: E402
import test_gate_fixes as TG  # noqa: E402
from _testkit import run  # noqa: E402
from bpa import design as D  # noqa: E402
from bpa import importer  # noqa: E402
from bpa import pipenet as PN  # noqa: E402
from bpa import project as P  # noqa: E402
from test_fix_api import project  # noqa: E402

YS = (300, 500, 700, 900)
# 四条线：输入端 k 号 → 物品准入口 Lk → 粉碎机；第五条：输入端 5 号直接 → 粉碎机 (400,100)（只放作者填的限制通过数量）
SC4 = {"devices": [("粉碎机", (400, y), TG.R["碎芽针"]) for y in YS] + [("粉碎机", (400, 100), TG.R["碎芽针"])],
       "nodes": {f"L{k}": ("准入口", (200, y), "物品") for k, y in enumerate(YS, 1)},
       "gates": [(k, (40, y), "芽针", 30, f"L{k}") for k, y in enumerate(YS, 1)] + [(5, (40, 100), "芽针", 30, 4)],
       "links": [(f"L{k}", k - 1, "belt") for k in range(1, 5)]}
K = {k: D.gate_key(TG.BP, (200, y)) for k, y in enumerate(YS, 1)}
G1 = D.gate_key(TG.BP, (40, 300))
G5 = D.gate_key(TG.BP, (40, 100))
# 游戏读到：L1 限速 12、L2 限速 6、L4 限速 12；作者：L1 改成 18、L2 说都没开、L3 填 24、输入端 5 号填限制通过数量 20（没有累计数）。
# 0.10.13 A2 以前它不影响来料、放在 1 号上；A2 起开着就按一次性、不来料算，单独放第五条线（那台 0）：1 号那条线照样测 18 和断水影响，
# L4 那条照样只有游戏读到的限速 12（0.4），导入后丢了它测得出来（审查 2026-11-21：原来挪到 4 号，L4 怎么都是 0，测不到）
LIMITS = {"L1": {"limit": 12}, "L2": {"limit": 6}, "L4": {"limit": 12}}
MINE = {K[1]: TG._fix((200, 300), 18), K[2]: TG._fix((200, 500), None, None), K[3]: TG._fix((200, 700), 24),
        G5: TG._fix((40, 100), None, 20)}
POOL_KEY = "扩容反应池@1,1"
POOL_FIX = ["清水×1 + 芽针粉末×1 → 芽针溶液×1（2秒）"]


def _by_dev(res) -> dict:
    return {(r["device"], r["x"], r["y"]): (r["rate"], r["state"], r["got"]) for r in res["devices"]}


def _by_far(rows) -> dict:
    """合成的行按远景坐标（取整）找：远景坐标就是小据点里写的「蓝图里」的坐标，K 不是 1 时拼合图坐标 p 和它差得远。"""
    return {(round(r["far"][0]), round(r["far"][1])): r for r in rows}


def _drop_gates(bd) -> None:
    """导入方拿到的 fixes.json 没有准入口段：去掉它再重算。"""
    fx = json.loads((bd.parent.parent / "fixes.json").read_text(encoding="utf-8"))
    fx["测试"].pop("gates")
    (bd.parent.parent / "fixes.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
    assert D.load_fixes(bd).get("gates") is None


def test_export_rows_equivalent():
    """合成的 gate_limits 行：作者填的盖过同一个口的游戏读数（一个口一行），没改的游戏读数原样留着；
    当档案读数用、fixes.json 里不带准入口段重算，逐台和带作者修正算的一样。
    K = 1 和真实据点的 0.5787 各算一遍：far = F0 + K·拼合图坐标，写反了（除以 K）导入方就认不上口。"""
    for k in (1.0, 0.5787):
        with project(SC4, limits=LIMITS, gates_fix=MINE, K=k) as (bd, led, rec, gl):
            with_fix = D.analyze(bd, led, rec, gl)
            assert [r["rate"] for r in with_fix["devices"]] == [0.6, 1.0, 0.8, 0.4, 0.0], (k, [r["rate"] for r in with_fix["devices"]])
            out = P.gate_limits_export("项目", "测试")
            rows = out["rows"]
            assert len(rows) == 5 and out["area"] == "测试" and out["time"] == gl["time"], rows     # L4 的游戏读数 + 作者的 4 个口
            by = _by_far(rows)
            l1, l2, l3, l4, g5 = (by[(200, 300)], by[(200, 500)], by[(200, 700)], by[(200, 900)], by[(40, 100)])
            assert (l1["src"], l1["limit_on"], l1["limit"], l1["count_on"]) == ("作者", True, 18, False) and l1["time"], l1
            assert (l2["limit_on"], l2["limit"], l2["count_on"]) == (False, None, False), l2                   # 作者说都没开
            assert l3["limit"] == 24 and l3["bp"] == TG.BP and l3["p_bp"] == [200, 700], l3
            assert all(abs(a - b) < 0.5 for a, b in zip(l3["far"], (200, 700))), (k, l3["far"])               # 远景坐标换对了
            assert "src" not in l4 and l4["limit"] == 12, l4                                                     # 没改的原样
            assert (g5["count_on"], g5["count_limit"], g5["passed"], g5["gate_no"], g5["rate"]) == (True, 20, None, 5, 30), g5
            _drop_gates(bd)
            imported = D.analyze(bd, led, rec, out)
            assert _by_dev(imported) == _by_dev(with_fix), (k, _by_dev(imported), _by_dev(with_fix))
            gv = {r["key"]: r for r in imported["gate_view"]["gates"]}
            assert gv[K[1]]["src"] == "游戏" and gv[K[1]]["limit"] == 18 and gv[G5]["count"] == 20, gv[K[1]]  # 导入方当游戏读数用
            assert gv[K[4]]["limit"] == 12 and _by_dev(imported)[("粉碎机", 400, 900)][0] == 0.4, gv[K[4]]  # L4 的游戏读数导入后照样起作用
    with project(SC4, limits=LIMITS) as _:                                    # 作者没填：不合成，原样带档案文件
        assert P.gate_limits_export("项目", "测试") is None


def test_export_follows_fixes_and_keeps_passed():
    """导出取 fixes.json 里存的记录（存了、还没等重算就取消了任务也带上；撤销了的不带，回到游戏读数），位置取准入口表认上的口。
    输入端游戏里读到限制通过数量 20、已放满：作者只改限速、限制通过数量照读到的 20 存，导出带上读到的累计数，导入方照样按不来料算。"""
    lim = dict(LIMITS, g1={"limit_on": False, "count_on": True, "count_limit": 20, "passed": 20})
    mine = dict(MINE, **{G1: TG._fix((40, 300), 24, 20)})
    with project(SC4, limits=lim, gates_fix=mine) as (bd, led, rec, gl):
        fx = json.loads((bd.parent.parent / "fixes.json").read_text(encoding="utf-8"))
        g = fx["测试"]["gates"]
        g.pop(K[1])                                       # 撤销了 L1，还没重算（准入口表上这一行还是作者的）
        g[K[3]] = dict(g[K[3]], limit=12)                 # L3 改成 12，还没重算
        g[K[4]] = TG._fix((200, 900), 6)                  # 新填 L4，还没重算（表上这一行还是游戏读的）
        (bd.parent.parent / "fixes.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
        by = _by_far(P.gate_limits_export("项目", "测试")["rows"])
        assert "src" not in by[(200, 300)] and by[(200, 300)]["limit"] == 12, by[(200, 300)]   # 回到游戏读数
        assert by[(200, 700)]["src"] == "作者" and by[(200, 700)]["limit"] == 12, by[(200, 700)]
        assert by[(200, 900)]["src"] == "作者" and by[(200, 900)]["limit"] == 6, by[(200, 900)]
        g1 = by[(40, 300)]
        assert (g1["src"], g1["limit"], g1["count_limit"], g1["passed"]) == ("作者", 24, 20, 20), g1
        out = P.gate_limits_export("项目", "测试")
        with_fix = D.analyze(bd, led, rec, gl)
        _drop_gates(bd)
        imported = D.analyze(bd, led, rec, out)
        assert _by_dev(imported) == _by_dev(with_fix), (_by_dev(imported), _by_dev(with_fix))
        assert _by_dev(imported)[("粉碎机", 400, 300)][0] == 0.0, _by_dev(imported)              # 放满了：照样不来料


def test_export_import_roundtrip():
    """导出的 zip 带 fixes.json（去掉准入口段）和合成的 gate_limits.json；导入后放到 档案/bpa/fixes.json、档案/ledger/gate_limits/，
    导入方重算（和断水影响一样的 analyze）每台和作者的 design.json 一样；断水影响按作者填的限速 18 算（断前 18，不是 30）。
    旧 zip 没有 fixes.json 的情况在 tests/test_bpa_import.py。"""
    with project(SC4, limits=LIMITS, gates_fix=MINE) as (bd, led, rec, gl):
        fx = json.loads((bd.parent.parent / "fixes.json").read_text(encoding="utf-8"))
        fx["测试"].update(feeds={"天有洪炉@1,1": "none"}, pool_recipes={POOL_KEY: POOL_FIX})
        (bd.parent.parent / "fixes.json").write_text(json.dumps(fx, ensure_ascii=False), encoding="utf-8")
        P.rerun_design("项目", "测试")
        author = json.loads((bd / "design.json").read_text(encoding="utf-8"))
        td = Path(tempfile.mkdtemp(dir=P.BPA))
        zp = P.export("项目", td / "测试.bpa.zip", log=lambda *_: None)
        with zipfile.ZipFile(zp) as z:
            zfx = json.loads(z.read("fixes.json").decode("utf-8"))
            zgl = json.loads(z.read("测试/gate_limits.json").decode("utf-8"))
        assert zfx["测试"] == {"feeds": {"天有洪炉@1,1": "none"}, "pool_recipes": {POOL_KEY: POOL_FIX}}, zfx   # 准入口段去掉了
        assert sum(r.get("src") == "作者" for r in zgl["rows"]) == 4, zgl
        old = bpm.BLUEPRINTS
        bpm.BLUEPRINTS = td / "blueprints"
        bpm.BLUEPRINTS.mkdir()
        try:
            name = importer.import_zip(zp, log=lambda *_: None)
            dest = bpm.BLUEPRINTS / name
            ibd = importer.base_dir(name, "测试")
            assert ibd is not None and json.loads((dest / "bpa" / "fixes.json").read_text(encoding="utf-8")) == zfx
            assert D.load_fixes(ibd) == zfx["测试"], D.load_fixes(ibd)                     # 断水影响重算时读得到作者的修正
            igl = json.loads((dest / "ledger" / "gate_limits" / "测试.json").read_text(encoding="utf-8"))
            ired = json.loads((dest / "ledger" / "测试.json").read_text(encoding="utf-8"))
            irec = json.loads((dest / "ledger" / "测试_recipes.json").read_text(encoding="utf-8"))["devices"]
            mine = D.analyze(ibd, ired, irec, igl, PN.load_inputs(ibd))
            assert {k: v[:2] for k, v in _by_dev(mine).items()} == {(r["device"], r["x"], r["y"]): (r["rate"], r["state"]) for r in author["devices"]}
            lost = D.cut_impact(ibd, ired, irec, igl, PN.load_inputs(ibd), 1, "芽针")
            assert [(u["x"], u["y"], u["before"], u["after"]) for u in lost] == [(400, 300, 18.0, 0.0)], lost
        finally:
            bpm.BLUEPRINTS = old


if __name__ == "__main__":
    sys.exit(run([test_export_rows_equivalent, test_export_follows_fixes_and_keeps_passed, test_export_import_roundtrip]))
