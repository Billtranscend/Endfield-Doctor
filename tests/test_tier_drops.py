"""备路没触发 / 原料见底时的「理论消耗比基准少」不报成「消费者被断开」（2026-11-12 另一个账号实测，tests/data/acct2）。

那个账号：中容武陵电池 73831（没爆仓）、赤铜块 132、重息壤 70002、蓝铁块 库存 1（靠跨地区传输补，没在补）。
基准简报是作者基地备路都触发时抓的，所以这个账号的赤铜瓶、液化重息壤、锦草溶液、蓝铁瓶 理论消耗都比基准少。
0.10.3 的结果：武陵城 5 条问题（致密源石粉末 送不到 2 台自动配方封装机 + 4 条「理论消耗下降：消费者被断开」）、3 条待确认，
排查树「清水理论消耗」✗ 指向灌装机。实际只有一件事：蓝铁块见底。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import blueprint as bp  # noqa: E402
import combine as C  # noqa: E402
import expert as E  # noqa: E402

DATA = ROOT / "tests" / "data" / "acct2"
PLAN = "1.6皇帝产线"


def run(area: str, brief: Path | None = None) -> dict:
    return C.run(brief or DATA / "brief_deep.json", DATA / f"patrol_{area}.json", PLAN, merge=False)[1]["verdicts"]


def test_main_area_one_root():
    v = run("武陵城")
    assert not v.get("问题"), [x.get("title") or x["reason"][:40] for x in v["问题"]]
    pend = v.get("待确认", [])
    assert len(pend) == 1 and pend[0]["title"].startswith("蓝铁块 库存见底"), [x.get("title") for x in pend]
    # 等蓝铁块的空闲塑形机收进这一条；蓝铁块理论产量 150→120 也写出来
    assert [d["name"] for d in pend[0]["members"]] == ["塑形机"], pend[0]["members"]
    assert "跨地区传输" in pend[0]["advice"] and "150→120" in pend[0]["reason"]
    # 两台阻塞的灌装机：下游芽针针剂 / 锦草软饮的提速备路没触发，只按基础产量吃，不是「输出传送带断了」
    slow = [x for x in v["正常"] if x["device"]["name"] == "灌装机" and "只有基础产量" in x["reason"]]
    assert len(slow) == 2, [x["reason"][:60] for x in v["正常"] if x["device"]["name"] == "灌装机"]
    tier = [x for x in v["正常"] if x.get("key") == "tierdrop"]
    assert len(tier) == 1 and all(n in tier[0]["reason"] for n in ("赤铜瓶", "液化重息壤", "锦草溶液")), tier


def test_auto_recipe_packer_not_starved():
    """点开面板确认是自动配方的封装机 (1207,932)(1346,932)：不拿别的配方说它「致密源石粉末 送不到」。"""
    v = run("武陵城")
    assert not any("送不到" in (x.get("title") or "") for lv in ("问题", "待确认") for x in v.get(lv, []))
    ok = [x for x in v["正常"] if x["device"]["name"] == "封装机" and x["device"]["y"] == 932]
    assert len(ok) == 2 and all("自动配方" in x["reason"] for x in ok), ok


def test_other_areas_no_repeat():
    """地区级的两条只在主分区（武陵城）出现一次，景玉谷、应龙关不重复。"""
    for area in ("景玉谷", "应龙关"):
        v = run(area)
        assert not v.get("问题") and not v.get("待确认"), (area, v.get("问题"), v.get("待确认"))
        assert not any(x.get("key") == "tierdrop" for x in v["正常"]), area


def test_real_disconnect_still_reported():
    """基准简报上把「灌装机 蓝铁瓶 + 锦草溶液」这条去掉（产物理论产量 30→0、锦草溶液 理论消耗 60→30）：
    这台不是备路、原料也没见底，仍然按「消费者被断开」报。"""
    brief = json.loads(json.dumps(bp.load(PLAN).reference_brief))
    for r in brief["武陵"]:
        if r["name"] == "蓝铁瓶" and r.get("fluid") == "item_liquid_plant_grass_1":  # 蓝铁瓶(锦草溶液)
            r["theo_prod"], r["cur_prod"], r["cur_cons"], r["theo_cons"] = 0, 0, 0, 0
        if r["name"] == "锦草溶液":
            r["theo_cons"], r["cur_cons"] = 30, 30
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "brief_20261112-000000.json"
        f.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        v = run("武陵城", f)
    assert any("锦草溶液 理论消耗" in (x.get("title") or "") for x in v.get("问题", [])), [x.get("title") for x in v.get("问题", [])]


G1, G2 = "item_liquid_plant_grass_1", "item_liquid_plant_grass_2"  # 锦草溶液、芽针溶液


def inject(change) -> dict:
    """在基准简报上改几行（注入一个故障），配武陵城那次巡检跑合并诊断。"""
    brief = json.loads(json.dumps(bp.load(PLAN).reference_brief))

    def setv(name, fluid=None, **kw):
        next(r for r in brief["武陵"] if r["name"] == name and r.get("fluid") == fluid).update(kw)

    change(setv)
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "brief_20261112-000000.json"
        f.write_text(json.dumps(brief, ensure_ascii=False), encoding="utf-8")
        return run("武陵城", f)


def titles(v: dict, level: str = "问题") -> list[str]:
    return [x.get("title") or "" for x in v.get(level, [])]


def test_backup_consumer_cut_while_backup_runs():
    """复查反例 S1：赤铜块 89967（差一点没满，备路其实在跑、赤铜瓶在产），吃赤铜瓶的灌装机被断开 → 仍然报。
    光看「赤铜块没严格满仓」就放行会漏掉。"""
    def change(setv):
        setv("赤铜瓶", G1, theo_prod=0, cur_prod=0)
        setv("赤铜瓶", None, stock=5000, theo_cons=30, cur_cons=0)
        setv("锦草溶液", None, theo_cons=30, cur_cons=32)
    assert any(t.startswith("赤铜瓶 理论消耗") for t in titles(inject(change)))


def test_base_rate_packer_cut_still_reported():
    """复查反例 S4：中容没爆仓时芽针针剂也有基础产量（理论 6）。做它的封装机被断开（理论产量 0）→ 仍然报，
    不能因为「芽针针剂是备路产物、中容没满」就算设计内。"""
    def change(setv):
        setv("中容武陵电池", None, stock=60000)
        setv("芽针针剂", None, stock=5000, theo_prod=0, cur_prod=0)
        setv("铁制零件", None, theo_cons=60, cur_cons=30)
        setv("蓝铁瓶", G2, theo_cons=0, cur_cons=0)
    t = titles(inject(change))
    assert any(x.startswith("蓝铁瓶(芽针溶液) 理论消耗") for x in t) and any(x.startswith("铁制零件 理论消耗") for x in t), t


def test_exhausted_because_producer_cut_is_problem():
    """复查反例 S6：做铁制零件的配件机断一台（理论产量 60→30）、库存耗光。见底的根子是生产端少了：按问题报，建议先查做它的设备。"""
    def change(setv):
        setv("铁制零件", None, stock=0, theo_prod=30, cur_prod=30, theo_cons=60, cur_cons=30)
        setv("锦草软饮", None, stock=5000)
        setv("芽针针剂", None, stock=5000)
    v = inject(change)
    hit = [x for x in v.get("问题", []) if x.get("key") == "short:铁制零件"]
    assert hit and "60→30" in hit[0]["title"] and "先查做 铁制零件 的设备" in hit[0]["advice"], (titles(v), titles(v, "待确认"))


def test_tree_water_step_info():
    """排查树：蓝铁瓶（空瓶）理论产量少不能算成「灌装机缺清水」；吃清水的产物都没少 → 只提示，不打 ✗。"""
    plan = bp.load(PLAN)
    for name in ("brief_quick.json", "brief_deep.json"):
        brief = json.loads((DATA / name).read_text(encoding="utf-8"))
        st = next(s for s in E.run(brief, plan) if s["key"] == "tree:清水理论消耗")
        assert st["status"] == "info" and "灌装机" not in st["where"]["machines"], (name, st)


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
