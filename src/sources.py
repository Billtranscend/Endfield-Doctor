"""缺开矿 / 缺气液的判断：拿用户简报和蓝图的参考简报比源头物品（矿、清水、气体、沉积酸）。

源头物品 = 配方表里没有配方能生产的物品，再加上 diagnose.SOURCE_NAMES（如息壤气野外也有气点），
且在参考简报该地区理论生产 > 0。对每个地区、每个源头物品：
  - 用户理论生产 T < 参考理论 R × 95% → 缺设备（少放了矿机/水泵/气口）；
  - 当前生产 < 当前消耗且差额 ≥ max(10, 消耗 × 2%) → 库存在降（矿机没开满/被关、纯度低；矿机在协议区域外，
    巡检看不到，只能靠简报）。不管是否接近理论值：理论不随开关变；
  - T 够，但当前生产 C 明显偏低 → 来源供不上（矿点枯竭、气点产量低、泵没接好）；
  同一物品只报一条，取更具体的：缺设备 > 库存在降 > 来源供不上。
  - 用户简报里没有这个地区/物品 → 缺整个地区/物品。

「C 明显偏低」的判定（见 supply_short）：C < min(T, R) × 90%，同时
  - C 也低于参考基地自己的当前产量 × 90%（按蓝图搭好的基地本身也不满速跑，例如需求不够、准入口限速）；
  - 用 diagnose.Region.excuse 解释不了（爆仓降速、下游回堵、蓝图设计限速）。

用法：.venv\\Scripts\\python.exe src\\sources.py [--blueprint 1.6皇帝产线] [debug\\brief_xxx.json]
不给简报时用 debug 目录里最新的一份。
"""

import json
import sys
import time
from pathlib import Path

import blueprint as bp
from diagnose import DEBUG, RECIPES, SOURCE_NAMES, Item, Region

DEFAULT_BLUEPRINT = bp.DEFAULT
THEO_RATIO = 0.95  # 用户理论 < 参考理论 × 这个比例算缺设备
CUR_RATIO = 0.9  # 当前 < 期望 × 这个比例算来源供不上
DRAIN_MIN_GAP = 10  # 源头物品 耗 − 产 ≥ max(这个值, 消耗 × DRAIN_MIN_RATIO) 算库存在降
DRAIN_MIN_RATIO = 0.02

PRODUCED = {o for r in RECIPES for o in r["outputs"]}


def device_of(iid: str) -> str:
    """源头物品靠什么设备产出，用于措辞。"""
    if iid == "item_liquid_water":
        return "水泵/水口"
    if iid == "item_liquid_acid":
        return "耐酸水泵"
    if iid.startswith("item_gas_"):
        return "气体收集泵/气口"
    if iid.endswith("_ore") or iid == "item_quartz_sand":
        return "矿机"
    return "采集设备"


def is_source(i: Item) -> bool:
    return i.id not in PRODUCED or i.label in SOURCE_NAMES


def as_blueprint(blueprint: "str | bp.Blueprint | None") -> bp.Blueprint:
    return blueprint if isinstance(blueprint, bp.Blueprint) else bp.load(blueprint)


def load_reference(plan: bp.Blueprint) -> dict[str, Region]:
    if plan.reference_brief is None:
        raise FileNotFoundError(f"蓝图档案「{plan.name}」缺少 reference_brief.json（{plan.dir}）")
    return {name: Region(name, rows, plan) for name, rows in plan.reference_brief.items() if rows}


def design_table(blueprint: "str | bp.Blueprint | None") -> list[dict]:
    """参考简报里每个地区源头物品的设计产量。只看蓝图 areas.json 列出的地区（没有 areas.json 时看全部）。"""
    plan = as_blueprint(blueprint)
    out = []
    for name, reg in load_reference(plan).items():
        if plan.areas and name not in plan.areas:
            continue  # 用户不管的地区（如四号谷地），参考数据忽略
        for i in reg.items.values():
            if is_source(i) and i.theo_prod > 0:
                out.append({"region": name, "item": i.label, "id": i.id, "device": device_of(i.id),
                            "theo": i.theo_prod, "cur": i.cur_prod})
    return out


FLUID_SLACK = 0.9  # 流体当前用量低于已放置能力的这个比例 = 泵有余量，不提「缺设备」
FLUID_THEO_RATIO = 0.8  # 流体已放置能力低于参考的 80% 才提「缺设备」（理论值含野外/闲置水泵，波动大）


def supply_short(user: Region, i: Item, theo_ref: int, cur_ref: int) -> bool:
    expect = min(i.theo_prod, theo_ref)
    if i.cur_prod >= expect * CUR_RATIO:
        return False
    if i.cur_prod >= cur_ref * CUR_RATIO:  # 不比按蓝图搭好的基地差
        return False
    return user.excuse(i) is None


def draining_source(i: Item) -> bool:
    """源头物品当前生产 < 当前消耗，且差额 ≥ max(10, 消耗 × 2%)：库存在降。不管是否接近理论值
    （理论值不随矿机开关变：关掉一台矿机，理论不动、当前掉下来）。"""
    if i.stock is None:
        return False  # 流体没有库存，「库存在降」没意义（2026-11-07 清水 产 1401/耗 1437 被报成库存在降）；流体走「来源供不上」
    gap = i.cur_cons - i.cur_prod
    return gap > 0 and gap >= max(DRAIN_MIN_GAP, i.cur_cons * DRAIN_MIN_RATIO)


def compare(brief: dict, blueprint: "str | bp.Blueprint | None" = DEFAULT_BLUEPRINT) -> list[dict]:
    """返回每条结论：region, item, kind, reference, theo, cur, text, advice。都够的不返回。
    blueprint 可以是蓝图名或已加载的 Blueprint。"""
    plan = as_blueprint(blueprint)
    findings = []

    def add(region, item, kind, reference, theo, cur, text, advice):
        findings.append(dict(region=region, item=item, kind=kind, reference=reference,
                             theo=theo, cur=cur, text=text, advice=advice))

    design = design_table(plan)
    regions = {name: Region(name, rows, plan) for name, rows in brief.items() if rows}
    for name in dict.fromkeys(d["region"] for d in design):
        rows = [d for d in design if d["region"] == name]
        user = regions.get(name)
        if user is None:
            need = "、".join(f"{d['item']} {d['theo']}/分" for d in rows)
            add(name, None, "缺地区", None, None, None,
                f"{name}：简报里没有这个地区，蓝图需要 {need}",
                f"确认 {name} 已解锁并建好基地；读简报时是否选了这个地区")
            continue
        for d in rows:
            i = user.items.get(d["id"])
            r = d["theo"]
            if i is None:
                add(name, d["item"], "缺物品", r, None, None,
                    f"{name} {d['item']}：简报里没有，蓝图设计 {r}/分",
                    f"去 {name} 放 {d['device']}（简报没读到这一行也可能是 OCR 漏行）")
                continue
            # 同一物品只报一条，取更具体的：缺设备 > 库存在降 > 来源供不上
            # 流体（清水、气体）没有库存：当前生产 = 当前消耗，由下游用量决定，和参考比「来源供不上」没有意义；
            # 简报的理论值还把野外矿区的水泵、闲着没接的水泵都算进去（用户 2026-11-07 指出），所以流体只在
            # 已放置能力掉了两成以上才提「缺设备」，并且只当提示——真缺水靠巡检里缺水空闲的设备说话
            if i.stock is None:
                # 泵还有余量：用量没到已放置能力的 90%。泵出不去会堵（阻塞），游戏不把堵着的泵算进理论值，
                # 所以理论值比参考低只说明用得少，不是泵少了（2026-11-12 群友：报缺沉积酸，应龙关几个输入口都是阻塞）
                if i.theo_prod and (i.cur_prod or 0) < i.theo_prod * FLUID_SLACK:
                    continue
                if i.theo_prod < r * FLUID_THEO_RATIO:
                    add(name, d["item"], "缺设备", r, i.theo_prod, i.cur_prod,
                        f"{name} {d['item']}：已放置能力 {i.theo_prod}/分，参考基地 {r}/分，少了 {r - i.theo_prod}/分"
                        f"（流体的理论值含野外/闲置的{d['device']}，只作提示；当前 {i.cur_prod}/分由下游用量决定）",
                        f"对照一下 {name} 的{d['device']}是否被拆/关了；真缺的话巡检里会有因缺 {d['item']} 空闲的设备")
                continue
            if i.theo_prod < r * THEO_RATIO:
                add(name, d["item"], "缺设备", r, i.theo_prod, i.cur_prod,
                    f"{name} {d['item']}：已放置能力 {i.theo_prod}/分，蓝图设计 {r}/分，缺 {r - i.theo_prod}/分",
                    f"去 {name} 补放 {d['device']}")
            elif draining_source(i):
                ore = d["device"] == "矿机"
                dev = "矿机" if ore else d["device"]
                why = "地区等级不够导致矿物纯度低" if ore else "水口/气点产量低"
                add(name, d["item"], "库存在降", r, i.theo_prod, i.cur_prod,
                    f"{name} {d['item']} 产 {i.cur_prod}/分 < 耗 {i.cur_cons}/分，库存在降（蓝图设计 {r}/分）",
                    f"{dev}没开满、{dev}被关或{why}；{dev}在协议区域外（巡检看不到），请去野外"
                    f"{'矿点' if ore else '水口/气点'}检查")
            elif supply_short(user, i, r, d["cur"]):
                add(name, d["item"], "来源供不上", r, i.theo_prod, i.cur_prod,
                    f"{name} {d['item']}：当前 {i.cur_prod}/分，已放置能力 {i.theo_prod}/分，"
                    f"蓝图设计 {r}/分（参考基地当前 {d['cur']}/分）",
                    f"设备够但产不出：查 {d['device']} 是否接好、输出是否通畅，矿点/水口/气点是否枯竭或产量低")
    return findings


# ---------------------------------------------------------------- 报告

def report(brief: dict, blueprint: "str | bp.Blueprint | None") -> tuple[str, dict]:
    plan = as_blueprint(blueprint)
    design = design_table(plan)
    findings = compare(brief, plan)
    lines = [f"# 源头物品对照蓝图「{plan.name}」", "", "## 蓝图设计用量", "",
             "| 地区 | 物品 | 设备 | 设计产量（理论/分） | 参考基地当前/分 |", "|---|---|---|---|---|"]
    lines += [f"| {d['region']} | {d['item']} | {d['device']} | {d['theo']} | {d['cur']} |" for d in design]
    lines.append("")
    if not findings:
        lines.append("## 结论\n\n源头物品全部够用（没有缺开矿、缺水口、缺气口）。")
    else:
        lines.append(f"## 结论（{len(findings)} 条）")
        for kind in ("缺地区", "缺物品", "缺设备", "库存在降", "来源供不上"):
            group = [f for f in findings if f["kind"] == kind]
            if group:
                lines.append(f"\n### {kind}")
                for f in group:
                    lines.append(f"- **{f['text']}**")
                    lines.append(f"  - 建议：{f['advice']}")
    lines.append("")
    return "\n".join(lines), {"blueprint": plan.name, "design": design, "findings": findings}


def main(argv: list[str]) -> int:
    blueprint = DEFAULT_BLUEPRINT
    if "--blueprint" in argv:
        k = argv.index("--blueprint")
        blueprint = argv[k + 1]
        argv = argv[:k] + argv[k + 2 :]
    path = Path(argv[0]) if argv else max(DEBUG.glob("brief_*.json"), key=lambda p: p.stat().st_mtime)
    brief = json.loads(path.read_text(encoding="utf-8"))
    text, data = report(brief, blueprint)
    data["brief"] = path.name
    print(f"简报：{path.name}\n")
    print(text)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (DEBUG / f"sources_{stamp}.md").write_text(text, encoding="utf-8")
    (DEBUG / f"sources_{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已保存：debug\\sources_{stamp}.md / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
