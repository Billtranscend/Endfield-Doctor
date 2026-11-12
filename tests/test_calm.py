"""正常的基地别报一排待确认；真故障照旧要报（2026-11-12，按五份群友简报改）。

群友反馈「一检查就是异常」。深度巡检上两版收紧过，快速诊断还是老规则：同一个基地深度巡检 0 问题，
快速诊断却有 1–6 条待确认。四类原因和改法：
  1. 备路 / 提速的规则没进快速诊断：蓝铁瓶(芽针溶液) 各家都是 5–7/30（中容没爆仓，只有基础产量）→ Region.slow_tier_line；
  2. 流体差一点就报：污水 561/600、惰性壤晶废液 108/120 → 流体门槛 FLUID_UNDER_RATIO 0.85；
  3. 源头流体（清水）低于理论单独报一条 → 不报，真缺时由吃它的产物追上来；
  4. 重息壤 满仓时 重息壤气 3/60：它的消费配方可逆、建图时被丢了，回堵判不出来 → loose_consumers；
  5. 全爆仓的基地（作者自己的，brief_author_full）：流体因为下游满了用得少，被报成「疑似 清水 供给不足」→ Region.demand_limited。
另外：排查树有 ✗ 时复读一次简报，两次都 ✗ 才算（expert.confirm）；
深度巡检里一条线停了拆成 11 条「理论消耗下降」→ 按配方连成片合成一条（combine.drop_groups）。

样本 tests/data/calm/：brief_d / brief_t3 / brief_h_first / brief_h_later = 三位群友的简报（H 隔了一小时读了两次）；
brief_r1 + patrol_r1_武陵城 = 锦草、芽针药线真停了的那位；brief_author_full = 作者基地 11-12 实跑（中容、软饮、针剂、息壤、重息壤全爆仓）；
brief_author_blip = 同一天第二次实跑时第一遍读到的（液化息壤 理论产量那一刻是 120），85 秒后复读恢复。
另一个账号的在 tests/data/acct2。
每条放行规则后面都跟着一个「真故障仍然报」的反例。
"""
import copy
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

import blueprint as bp  # noqa: E402
import combine as C  # noqa: E402
import diagnose as D  # noqa: E402
import expert as E  # noqa: E402

DATA = ROOT / "tests" / "data"
PLAN = "1.6皇帝产线"
plan = bp.load(PLAN)


def brief(name: str) -> dict:
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def region(b: dict) -> D.Region:
    return D.Region("武陵", b["武陵"], plan)


def pending(b: dict) -> list[D.Finding]:
    return [f for f in region(b).diagnose() if f.level == "待确认"]


def edit(b: dict, name: str, fluid_tail: str | None = None, **kw) -> dict:
    """改简报里的一行（返回新的简报）。name 是物品名；灌装瓶用 fluid_tail 指定灌的是什么（物品 id 的结尾）。"""
    b = copy.deepcopy(b)
    rows = [r for r in b["武陵"] if r["name"] == name and (
        (fluid_tail is None and not r["fluid"]) or (fluid_tail and r["fluid"] and r["fluid"].endswith(fluid_tail)))]
    assert len(rows) == 1, (name, fluid_tail, len(rows))
    rows[0].update(kw)
    return b


def bottle_tail(b: dict, label: str) -> str:
    """显示名「蓝铁瓶(芽针溶液)」对应的那一行的灌装物 id。"""
    reg = region(b)
    name = label.split("(")[0]
    for r in b["武陵"]:
        if r["name"] == name and r["fluid"] and D.item_id(r["name"], r["fluid"]) == reg.by_label[label].id:
            return r["fluid"]
    raise AssertionError(label)


# ---------------------------------------------------------------- 快速诊断：正常的基地不报

def test_normal_bases_have_no_pending():
    """另一个账号、群友 H 一小时后：排查树全 ✓，待确认 0 条（0.10.5 是 1 条 / 0 条）。"""
    assert pending(brief("acct2/brief_quick.json")) == []
    assert pending(brief("calm/brief_h_later.json")) == []


def test_d_and_t3_keep_only_the_real_doubt():
    """群友 D 6 条 → 1 条（液化重息壤：重息壤满仓、备路该跑却没在产，深度巡检在应龙关也留了这一条待确认）；
    第三位群友 5 条 → 1 条（赫铜块 理论 90、当前 0，可能是真的，没有巡检数据）。"""
    d = pending(brief("calm/brief_d.json"))
    assert [f.affected for f in d] == [["蓝铁瓶(液化重息壤)"]], [f.text for f in d]
    t = pending(brief("calm/brief_t3.json"))
    assert len(t) == 1 and "赫铜块" in t[0].affected, [f.text for f in t]


def test_real_fault_still_reported():
    """R1（锦草、芽针药线真停了）：排查树 3 项 ✗ 不变；停的那条线上的待确认（芽针粉末 没吃饱）还在。
    以前另有 3 条「疑似 清水 / 息壤气 供给不足」：R1 的赤铜块、重息壤、息壤都爆仓，那几种流体是用得少，不是缺。"""
    b = brief("calm/brief_r1.json")
    p = pending(b)
    assert any("芽针粉末" in f.affected for f in p), [f.text for f in p]
    assert sum(s["status"] == "bad" for s in E.run(b, plan)) == 3


# ---------------------------------------------------------------- 1. 提速线只有基础产量

def test_slow_tier_line_excused():
    """中容没爆仓：蓝铁瓶(芽针溶液) 5–7/30 是基础产量。群友 D 仓库里空瓶多，以前被报成「疑似 芽针溶液 供给不足」。"""
    reg = region(brief("calm/brief_d.json"))
    for label in ("蓝铁瓶(芽针溶液)", "蓝铁瓶(锦草溶液)"):
        i = reg.by_label[label]
        assert reg.under(i) and "只有基础产量" in (reg.excuse(i) or ""), (label, reg.excuse(i))


def test_slow_tier_line_not_excused_when_packer_stops():
    """反例：做芽针针剂的封装机断了（它不再计入理论值，芽针针剂 理论产量 6→0）：蓝铁瓶(芽针溶液) 没人吃，不能当成基础产量放过。"""
    b = brief("calm/brief_d.json")
    b = edit(b, "芽针针剂", cur_prod=0, theo_prod=0)
    b = edit(b, "蓝铁瓶", bottle_tail(b, "蓝铁瓶(芽针溶液)")[-12:], cur_prod=0, cur_cons=0, theo_cons=0)
    reg = region(b)
    i = reg.by_label["蓝铁瓶(芽针溶液)"]
    assert reg.slow_tier_line(i) is None
    assert any("蓝铁瓶(芽针溶液)" in f.affected for f in reg.diagnose() if f.level == "待确认")


def test_slow_tier_line_needs_tier_really_off():
    """反例：中容爆仓了（备路触发、应该提速到 15 左右），蓝铁瓶(芽针溶液) 还是 5/30：不放过。"""
    b = brief("calm/brief_d.json")
    b = edit(b, "中容武陵电池", stock=90000)
    reg = region(b)
    assert reg.slow_tier_line(reg.by_label["蓝铁瓶(芽针溶液)"]) is None


# ---------------------------------------------------------------- 2. 流体门槛

def test_fluid_near_theory_not_flagged():
    """污水 561/600（93.5%）、惰性壤晶废液 108/120（90%）、壤晶废液 138/150（92%）是读数波动，不算低于理论。"""
    d, a, t = (region(brief(n)) for n in ("calm/brief_d.json", "acct2/brief_quick.json", "calm/brief_t3.json"))
    assert not d.under(d.by_label["污水"])
    assert not a.under(a.by_label["惰性壤晶废液"])
    assert not t.under(t.by_label["壤晶废液"])


def test_fluid_far_below_theory_still_flagged():
    """反例：污水掉到 300/600 仍然报。固体的门槛没变（灼铜块 60/90 照旧算低）。"""
    b = edit(brief("acct2/brief_quick.json"), "污水", cur_prod=300, cur_cons=300)
    hit = [f for f in pending(b) if "污水" in f.affected]
    assert hit, [f.text for f in pending(b)]
    h = region(brief("calm/brief_h_first.json"))
    assert h.under(h.by_label["灼铜块"])


# ---------------------------------------------------------------- 3. 源头流体

def test_source_fluid_alone_is_not_a_finding():
    """清水 1558/2370 只说明泵有富余：归到「正常」，不出待确认（群友 D、R1 以前各有一条）。"""
    fs = region(brief("calm/brief_d.json")).diagnose()
    assert not [f for f in fs if f.level == "待确认" and f.affected == ["清水"]]
    # 归到「正常」：能说出下游谁用得少时写「下游用得少」，说不出时写「按用量供给」
    assert any(f.level == "正常" and "清水" in f.affected and ("用得少" in f.text or "按用量供给" in f.text) for f in fs)


def test_water_shortage_still_traced_from_products():
    """反例：真缺水时吃清水的产物会低于理论又说不出别的原因，照样追到清水头上。
    另一个账号的简报上造一个：精炼炉只出 赤铜块 200/510，清水 总量掉到 900（基准 1860）。和 0.10.5 的结果一样：
    赤铜块 库存在降，归在「提醒」里，写明「生产受限于：疑似 清水 供给不足」。
    （清水总量还在基准九成以上时，这种情况会被写成「受 清水 的设计限速影响」，0.10.5 就是这样，这次没动。）"""
    b = edit(edit(brief("acct2/brief_quick.json"), "赤铜块", cur_prod=200), "清水", cur_prod=900, cur_cons=900)
    hit = [f for f in region(b).diagnose() if "赤铜块" in f.affected]
    assert hit and "疑似 清水 供给不足" in hit[0].text and hit[0].level == "提醒", [(f.level, f.text) for f in hit]


def test_non_source_item_not_called_source():
    """做它的配方在简报里对不上时别叫「源头」、别让人去查泵。"""
    reg = region(brief("calm/brief_t3.json"))
    root, _ = reg.find_root(reg.by_label["赫铜块"])
    assert "源头" not in root.text
    b = edit(brief("calm/brief_t3.json"), "中容武陵电池", stock=90000)  # 中容爆仓 → 提速线的放行不成立
    reg = region(b)
    root, _ = reg.find_root(reg.by_label["蓝铁瓶(锦草溶液)"])
    assert root.level == "待确认" and "源头" not in root.text and "泵" not in root.advice, (root.text, root.advice)


# ---------------------------------------------------------------- 4. 回堵：消费配方被丢掉的物品

def test_blocked_through_dropped_reversible_recipe():
    """群友 D：重息壤 满仓，重息壤气 产 3/60 是下游回堵（深度巡检点开面板判的也是这个）。"""
    reg = region(brief("calm/brief_d.json"))
    i = reg.by_label["重息壤气"]
    assert reg.under(i) and "重息壤 爆仓回堵" in (reg.excuse(i) or ""), reg.excuse(i)


def test_not_blocked_when_downstream_has_room():
    """反例：重息壤 没满时，重息壤气 3/60 不能用回堵解释。"""
    reg = region(edit(brief("calm/brief_d.json"), "重息壤", stock=5000))
    assert reg.blocked_path(reg.by_label["重息壤气"].id) is None


# ---------------------------------------------------------------- 5. 全爆仓的基地：流体是用得少

def test_full_base_has_no_pending():
    """作者基地全爆仓时实跑的简报：0.10.5 有 3 条待确认（清水、液化息壤、息壤气「供给不足」），现在 0 条。"""
    b = brief("calm/brief_author_full.json")
    assert pending(b) == [], [f.text for f in pending(b)]
    reg = region(b)
    assert "下游用得少" in reg.excuse(reg.by_label["锦草溶液"])  # 一路回堵，一路（赤铜瓶）和基准一致
    assert "爆仓回堵" in reg.excuse(reg.by_label["重息壤气"])  # 候选消费者有两条可逆配方，重息壤那一路满了


def test_fluid_not_excused_when_a_consumer_is_unexplained():
    """反例：锦草软饮 没满（库存 5000）而中容爆仓（提速备路触发了），蓝铁瓶(锦草溶液) 还是 0/30：
    这一路说不出原因，锦草溶液 18/60 就不能算「用得少」。"""
    b = edit(brief("calm/brief_author_full.json"), "锦草软饮", stock=5000)
    reg = region(b)
    assert reg.demand_limited(reg.by_label["锦草溶液"]) is None
    assert any("蓝铁瓶(锦草溶液)" in f.affected or "锦草溶液" in f.affected for f in pending(b)), [f.text for f in pending(b)]


# ---------------------------------------------------------------- 排查树复读

def test_confirm_clears_transient():
    """群友 H：第一次 灼铜块 60/90 打 ✗，一小时后 90/90。两次对一遍：没有 ✗，原因里留着第一次读到的。"""
    first = E.run(brief("calm/brief_h_first.json"), plan)
    second = E.run(brief("calm/brief_h_later.json"), plan)
    assert [s["title"] for s in first if s["status"] == "bad"] == ["灼铜块"]
    tree, cleared = E.confirm(first, second)
    assert cleared == ["灼铜块"] and not [s for s in tree if s["status"] == "bad"]
    step = next(s for s in tree if s["title"] == "灼铜块")
    assert step["status"] == "ok" and "第一次读是 ✗" in step["cause"]


def test_confirm_clears_real_transient_from_live_run():
    """作者基地 11-12 实跑碰到的：第一次读 液化息壤 理论产量 150→120，「清水理论消耗」打 ✗；85 秒后复读回到 150，
    这一步是提示（?）。也算复读后恢复，要记进 cleared。"""
    first = E.run(brief("calm/brief_author_blip.json"), plan)
    second = E.run(brief("calm/brief_author_full.json"), plan)
    assert [s["title"] for s in first if s["status"] == "bad"] == ["清水理论消耗"]
    tree, cleared = E.confirm(first, second)
    step = next(s for s in tree if s["title"] == "清水理论消耗")
    assert cleared == ["清水理论消耗"] and step["status"] == "info" and "第一次读是 ✗" in step["cause"]


def test_confirm_keeps_persistent_and_downgrades_one_off():
    """两次都 ✗ 的照旧（R1）；第一次正常、复读才 ✗ 的降成提示。"""
    r1 = E.run(brief("calm/brief_r1.json"), plan)
    tree, cleared = E.confirm(r1, copy.deepcopy(r1))
    assert cleared == [] and sum(s["status"] == "bad" for s in tree) == 3
    later = E.run(brief("calm/brief_h_later.json"), plan)
    first = E.run(brief("calm/brief_h_first.json"), plan)
    tree, cleared = E.confirm(later, first)  # 倒过来：先正常、后 ✗
    step = next(s for s in tree if s["title"] == "灼铜块")
    assert cleared == [] and step["status"] == "info" and "两次读数不一致" in step["cause"]


# ---------------------------------------------------------------- 深度巡检：一条线停了合成一条

def test_stalled_line_is_one_problem():
    """R1 武陵城：11 条「X 理论消耗下降：有消费者被断开」→ 1 条，标题点名理论产量掉了的产物，依据里 11 种物品都在。"""
    vs = C.run(DATA / "calm" / "brief_r1.json", DATA / "calm" / "patrol_r1_武陵城.json", PLAN, merge=False)[1]["verdicts"]
    bad = vs["问题"]
    assert len(bad) == 1, [v["title"] for v in bad]
    v = bad[0]
    assert "锦草软饮" in v["title"] and "芽针针剂" in v["title"] and "11 种物品" in v["title"]
    for x in ("锦草 60→0", "铁制零件 120→0", "蓝铁瓶 96→36", "赤铜瓶 60→30"):
        assert x in v["reason"], x


def test_unrelated_drops_stay_separate():
    """没有配方把它们连起来的两种物品各报各的。"""
    reg = region(brief("calm/brief_r1.json"))
    groups = C.drop_groups(reg, ["锦草", "锦草粉末", "源矿"])
    assert groups == [["锦草", "锦草粉末"], ["源矿"]], groups
    assert C.drop_groups(reg, ["锦草"]) == [["锦草"]]


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
