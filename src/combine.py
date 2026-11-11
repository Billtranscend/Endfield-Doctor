"""P3：把俯瞰巡检结果（debug/patrol_*.json）和简报诊断（debug/brief_*.json）合在一起下结论。

巡检告诉我们「哪台设备停着、停在哪、做什么配方」，简报告诉我们「产物和原料的库存、产量」。
按分级生产（主路 + 备路溢流）原理逐台判断：
  1. 面板状态是 生产中/运行中 的，俯瞰里的标记只是闪烁 → 正常。
  2. 阻塞（输出满、下游不收）：
     - 产物爆仓 → 正常（按设计停等）；
     - 产物没爆仓，但下游爆仓回堵（Region.blocked_path）→ 正常；
     - 否则 → 问题：输出口送不出去。
  3. 空闲中（没原料进来）：
     - 产物是备路产物、备路条件没满足 → 正常；
     - 原料库存为 0 → 沿上游追根因（Region.find_root）；
     - 原料是流体 → 待确认（流体供给）；
     - 原料有库存 → 问题：原料有却没送到；
     - 其他 → 待确认。
  4. 对照 P2 诊断里的「待确认」：巡检点开的设备产物出现在影响列表里的，附上巡检结论（已核实）。

用法：.venv\\Scripts\\python.exe src\\combine.py [--blueprint 名] [--no-merge] [debug\\brief_xxx.json] [debug\\patrol_xxx.json]
不给参数时简报取最新一份，巡检取最新一份快速模式的；之后同一分区的定点深查结果（guessed 为 false）
按坐标 25 像素内覆盖快速条目，--no-merge 关掉。
"""

import json
import re
import sys
import time
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

import blueprint as bp
from panel_recipe import NO_RECIPE, POOL_FIBER, POOL_NAMES, parse_pool_id
from diagnose import DEBUG, ID_NAME, RECIPES, SOURCE_NAMES, Item, Region, Root

RECIPE_BY_ID = {r["id"]: r for r in RECIPES}
STATUS_RE = re.compile(r"(生产中|运行中|空闲中|阻塞|未激活|已关闭|关闭|停机)")  # 旧巡检文件的状态带 OCR 噪声，如「0阻塞」
OFF_WORDS = {"已关闭": "关闭", "停机": "关闭"}  # 面板状态词归一成「关闭」（手动关掉或断电）
DEFAULT_REGION = "武陵"
ORDER = ("问题", "待确认", "正常")

# 巡检点到的物流件（标记周围没找到生产设备时，patrol 记下的第一个物流件）：归到「信息」一节
LOGISTICS_PARTS = {
    "暗管出口", "暗管入口", "多口暗管入口", "多口暗管出口",
    "仓库取货口", "仓库存货口", "仓库存取线基段", "仓库存取线源桩",
    "物品准入口", "管道准入口", "分流器", "汇流器", "管道分流器", "管道汇流器",
    "物流桥", "管道桥", "传送带", "管道", "污水接入口", "产物排出口", "储液罐", "储气罐", "协议储存箱",
}
PICKUP_PARTS = {"仓库取货口", "仓库存取线基段", "仓库存取线源桩"}  # 空闲 = 没在出货
GATE_PARTS = {"物品准入口", "管道准入口"}  # 空闲/阻塞 = 可能是限速口
IGNORED_PARTS = {"供电桩", "息壤供电桩", "息壤中继器", "中继器", "供电终端"}  # 供电类：状态和产线无关，直接不列
UNLABELED = "（无标签）"  # 快速模式下标记旁边没找到标签
GUESS_NOTE = "（快速模式：按标签猜的设备名，状态来自俯瞰标记）"
DEEP_NOTE = "（已定点深查）"
NEAR_NOTE = "（近景定向巡检：设备按底账和标签核对过，状态来自 12 秒标记采样，配方取自档案配方表）"
FLOW_RATIO = 0.85  # 阻塞设备的产物当前生产 ≥ 理论 × 这个比例：产线在满负荷流动，阻塞是瞬时的
FLOW_CONS_RATIO = 0.95  # 或当前生产 ≥ 当前消耗 × 这个比例（产多少用多少，下游在正常取）
FLOW_MIN_RATIO = 0.3  # 上一条还要求当前消耗 ≥ 理论消耗 × 这个比例（理论消耗为 0 时看当前生产 / 理论生产）
MERGE_PX = 25  # 深查结果和快速结果按基地坐标匹配的距离
PAIR_PX = 450  # 上游阻塞 + 下游空闲配对的最大距离（基地坐标）
LINE_PX = 300  # 同一产物的几对、上游互相相距 ≤ 这个距离：合并成「这条线断了」一条
# 源头设备产出的流体（显示名）：阻塞时并进同一流体的「根因」条目作为可能的断点
SOURCE_DEVICE_FLUID = {"水泵": "清水", "二型耐酸水泵": "沉积酸"}
# 源头设备：没有配方（矿、水、气从地图上采），阻塞只能是后面的输出管/带断了
SOURCE_DEVICES = {"水泵", "二型耐酸水泵", "气体收集泵", "水驱矿机", "电驱矿机", "二型电驱矿机"}
SURPLUS_RATIO = 1.2  # 流体当前生产 ≥ 当前消耗 × 这个比例……
SURPLUS_MIN = 20  # ……且差额 ≥ 这么多（/分）：产得出但没被用掉
SURPLUS_MIN_DEVICES = 2  # 至少这么多台空闲设备吃这种流体，才合并成一条问题
STARVED_RATIO = 0.75  # 固体原料库存充足、但当前消耗 < 理论消耗 × 这个比例：送不到（准入口/取货口物品被改错等）。F 一半时间是 330/810，后来 510/810=63%，0.6 抓不住
HALVED_RATIO = 0.6  # 阻塞设备的产物当前产、耗都 < 蓝图参考简报当前值 × 这个比例：这条线本身出了事
ROOT_DROP_RATIO = 0.9  # 源头 S 当前生产 < 参考当前 × 这个比例，才能拿它解释下游产物的腰斩
DROP_MIN, DROP_RATIO = 5, 0.05  # 理论消耗比蓝图参考低 > max(5, 参考 × 5%)：有消费者被断开
GATE_UNIT = 6  # 气体准入口的最小档：气体理论消耗正好少 6 的倍数 = 关掉了几个口
RESET_HINT = "新放的设备默认是空的"
# 需要气体散布机提供环境的设备 → 它们的环境气体（面板配方标签「稳定环境」= 惰气）。缺环境时面板顶部标「无环境」、
# 设备空闲但原料槽全满。2026-11-04 故障 C：惰气准入口 6→0 → 三台天有洪炉无环境 → 息壤减半 → 壤晶链整排空闲
ENV_MACHINES = {"天有洪炉": "惰气", "提纯机": None, "气体反应炉": None}  # None = 不确定是哪种气，按下降的气体匹配
ENV_SHORT_RATIO = 0.6  # 环境设备的产物当前生产 < 理论 × 这个比例时，下游缺它的条目算连带  # 重新放置设备后忘了设配方/出货口：理论消耗跟着掉，2026-11-04 酸气 6→0 就是这样


@dataclass
class Device:
    name: str
    status: str  # 归一化后的面板状态：阻塞 / 空闲中 / 生产中 / 运行中 / ?
    badge: str
    x: int
    y: int
    recipe: str  # 可读配方文字
    output: str | None  # 产物物品 id
    recipe_id: str | None
    guessed: bool = False  # 快速模式：名字按标签首字猜的，可能是「A|B|C」几个候选
    deep: bool = False  # 已被之后的定点深查结果覆盖
    env: str | None = None  # 深查面板顶部的环境提示：「无环境」
    seen: str = ""  # 连拍里标记出现次数（快速模式）
    passes: int = 2  # 两遍扫描里出现的遍数；旧文件没有这个字段按 2（算稳定）
    foreign: str | None = None  # 面板原料格有「!」且堆着配方外的物品（显示名）：准入口/分流器物品设错了
    shot: str = ""  # 看到它的每屏截图文件名 + 标记在图里的位置（定位图用）
    sx: int = 0
    sy: int = 0
    panel: str = ""  # 面板图文件名
    warn: str = ""  # 面板其他警告（息壤气供给不足）
    flow: str = ""  # 暗管面板流速（单位/秒）
    near: bool = False  # 近景定向巡检（near_patrol）：名字按底账 + 标签核对过，状态来自 12 秒标记采样，配方取自档案配方表

    @property
    def where(self) -> str:
        return f"({self.x}, {self.y})"

    @property
    def off(self) -> bool:
        """被关闭（俯瞰里设备顶部灰色圆圈叉，或面板状态「关闭/已关闭/停机」）。
        深查条目以面板状态为准（面板没读出时才看标记）；快速模式条目按俯瞰标记。"""
        if self.status == "关闭":
            return True
        return self.badge == "关闭" and (self.guessed or self.status in ("?", ""))

    @property
    def off_overridden(self) -> bool:
        """深查面板显示在工作、俯瞰标记却是关闭：按面板判，依据里要说明。"""
        return not self.guessed and self.badge == "关闭" and not self.off

    @property
    def outputs(self) -> list[str]:
        """产物候选：快速模式从俯瞰产物图标认，可能是「item_a|item_b」。"""
        return [o for o in (self.output or "").split("|") if o]

    @property
    def candidates(self) -> list[str]:
        return [n for n in self.name.split("|") if n]

    @property
    def kind(self) -> str:
        """unlabeled / ignored（供电类）/ logistics（物流件）/ machine（生产设备，含混合候选）。"""
        if self.name == UNLABELED:
            return "unlabeled"
        names = set(self.candidates)
        if names <= IGNORED_PARTS:
            return "ignored"
        if names <= LOGISTICS_PARTS | IGNORED_PARTS:
            return "logistics"
        return "machine"

    @property
    def machines(self) -> list[str]:
        """候选里的生产设备名（混合候选时去掉物流件）。"""
        return [n for n in self.candidates if n not in LOGISTICS_PARTS and n not in IGNORED_PARTS]


@dataclass
class Verdict:
    level: str  # 问题 / 待确认 / 正常；沿上游追到的沿用 Root.level（可能是 设计内、闲置）
    device: Device
    reason: str  # 判断依据
    advice: str = ""
    chain: list[str] = field(default_factory=list)
    fallback: bool = False  # 配方没认出、靠兜底规则判出来的
    partner: Device | None = None  # 配对规则：上游阻塞的是 device，下游空闲的是 partner
    title: str = ""  # 合并条目（流体有余收不到、连锁归根）的标题；有标题时 members 是涉及的设备
    members: list[Device] = field(default_factory=list)
    key: str = ""  # 合并用的标记：源头设备阻塞「source:流体」、连锁归根「root:流体」

    def as_dict(self) -> dict:
        d = asdict(self)
        d["device"] = asdict(self.device)
        d["partner"] = asdict(self.partner) if self.partner else None
        d["members"] = [asdict(x) for x in self.members]
        d["affected"] = [asdict(x) for x in (self.members or [y for y in (self.device, self.partner) if y])]
        return d


# ---------------------------------------------------------------- 读入

PATROL_META: dict = {}  # 最近一次 load_patrol 读到的元信息（power 等），report 里用


def load_patrol(path: Path) -> tuple[str | None, str | None, list[Device]]:
    """返回（地区, 分区, 设备）。旧格式是设备列表；新格式是 {"region", "area", "time", "devices"}。"""
    raw = json.loads(path.read_text(encoding="utf-8"))
    PATROL_META.clear()
    if isinstance(raw, dict):
        PATROL_META["power"] = raw.get("power") or ""
        PATROL_META["checks"] = raw.get("checks") or []
    if isinstance(raw, dict):
        region, area, rows = raw.get("region"), raw.get("area"), raw.get("devices", [])
    else:
        region, area, rows = None, None, raw
    devices = []
    for r in rows:
        m = STATUS_RE.search(r.get("status") or "")
        devices.append(
            Device(
                name=r.get("name") or "?",
                status=OFF_WORDS.get(m.group(1), m.group(1)) if m else "?",
                badge=OFF_WORDS.get(r.get("badge") or "?", r.get("badge") or "?"),
                x=r.get("world_x"),
                y=r.get("world_y"),
                recipe=r.get("recipe") or "配方：?",
                output=r.get("output"),
                recipe_id=r.get("recipe_id"),
                guessed=bool(r.get("guessed")),
                env=r.get("env"),
                seen=r.get("seen") or "",
                passes=int(r.get("passes", 2)),
                foreign=r.get("foreign"),
                shot=r.get("shot") or "",
                sx=int(r.get("sx") or 0),
                sy=int(r.get("sy") or 0),
                panel=r.get("panel") or "",
                warn=r.get("warn") or "",
                flow=r.get("flow") or "",
                near=bool(r.get("near")),
            )
        )
    return region, area, devices


def pick_region(
    brief: dict, region: str | None, devices: list[Device], plan: bp.Blueprint, area: str | None = None
) -> str:
    if region and brief.get(region):
        return region
    if not region and area:
        found = next((r for r, areas in plan.areas.items() if area in areas), None)
        if found and brief.get(found):
            print(f"巡检文件没写地区，按分区「{area}」在蓝图 areas.json 里查到「{found}」")
            return found
    if region:
        print(f"提示：巡检地区「{region}」不在简报里，改按产物匹配地区")
    outputs = [o for d in devices for o in d.outputs]
    hits = {}
    for name, rows in brief.items():
        if rows:
            ids = Region(name, rows, plan).items
            hits[name] = sum(o in ids for o in outputs)
    best = max(hits, key=hits.get, default=None)
    if best and hits[best] > 0 and list(hits.values()).count(hits[best]) == 1:
        print(f"巡检文件没写地区，按产物匹配到「{best}」（{hits[best]}/{len(outputs)} 个产物在简报里）")
        return best
    print(f"提示：巡检文件没写地区，产物也对不上唯一地区，默认按「{DEFAULT_REGION}」处理")
    return DEFAULT_REGION


def recipe_of(reg: Region, d: Device) -> dict | None:
    if d.recipe_id in RECIPE_BY_ID:
        return RECIPE_BY_ID[d.recipe_id]
    if d.output:  # 没有配方 id 时，按「这种设备 + 这个产物」找唯一配方
        names = set(d.machines) or {d.name}
        cands = [r for r in RECIPES if d.output in r["outputs"] and r["machine_name"] in names]
        if len(cands) == 1:
            return cands[0]
    return None


def label_of(reg: Region, iid: str) -> str:
    """显示名复用简报行的 Item.label；简报里没有这个物品时退回物品表名。"""
    return reg.items[iid].label if iid in reg.items else ID_NAME.get(iid, iid)


# ---------------------------------------------------------------- 判断

def tier_missing(reg: Region, label: str) -> tuple[dict, list[str]] | None:
    """label 是备路产物且备路没触发时，返回 (规则, 没爆仓的条件)。"""
    rule = next((r for r in reg.blueprint.tier_rules if r["product"] == label), None)
    if rule is None:
        return None
    missing = [n for n in rule["requires_full"] if not (n in reg.by_label and reg.tier_full(reg.by_label[n]))]
    return (rule, missing) if missing else None


def in_use(reg: Region, iid: str) -> bool:
    """简报里这个物品有生产或消耗记录（当前或理论）。"""
    i = reg.items.get(iid)
    return i is not None and i.active


def waiting_downstream(reg: Region, out_id: str, devices: list[Device]) -> str | None:
    """产物的下游配方在等另一种备路原料时，返回说明；否则 None。"""
    downstream = [r for r in RECIPES if out_id in r["inputs"] and any(in_use(reg, o) for o in r["outputs"])]
    for r in downstream:
        outs = "、".join(label_of(reg, o) for o in r["outputs"])
        for y in r["inputs"]:
            if y == out_id:
                continue
            y_label = label_of(reg, y)
            why = []
            tm = tier_missing(reg, y_label)
            if tm:
                why.append(f"{'、'.join(tm[1])} 未爆仓（{tm[0]['note']}）")
            idle = [x for x in devices if y in x.outputs and x.status == "空闲中"]
            if idle:
                why.append(f"本次巡检里供给它的 {'、'.join(f'{x.name}{x.where}' for x in idle)} 空闲中")
            if why:
                y_stock = reg.fmt(reg.items[y]) if y in reg.items else y_label
                return (
                    f"下游 {r['machine_name']}（做 {outs}，还要 {y_label}×{r['inputs'][y]}）在等备路原料 {y_label}，"
                    f"{y_label} 的备路未触发：{'；'.join(why)}；原料 {y_stock}"
                )
    return None


def explain_blocked(reg: Region, out: Item, devices: list[Device]) -> tuple[str, str] | None:
    """产物阻塞能解释时返回 (等级, 原因)：爆仓 / 下游回堵 / 下游在等备路原料。"""
    if reg.full(out):
        return "正常", f"产物已爆仓，按设计停等：{reg.fmt(out)}，上限 {reg.cap}"
    path = reg.blocked_path(out.id)
    if path:
        return "正常", f"下游 {path[-1]} 爆仓回堵（{' → '.join(path)}）；产物 {reg.fmt(out)}"
    far = None if theo_cons_dropped(reg, out.id) else downstream_full(reg, out.id)
    if far:
        # 用户 2026-11-07：阻塞本身不是异常，持续阻塞多半是这条线的最终产物爆仓了。在用配方图有时接不上（多配方池子、
        # 刚停下来的设备理论值归 0），就按全配方表追几步，下游有爆仓产物就按回堵算
        return "正常", f"下游 {far[-1]} 爆仓回堵（按配方表追到：{' → '.join(far)}）；产物 {reg.fmt(out)}"
    wait = waiting_downstream(reg, out.id, devices)
    if wait:
        return "设计内", f"{wait}；产物 {reg.fmt(out)}"
    # 满负荷产线上阻塞标记会持续两三秒：产物在正常流动，阻塞就是瞬时的
    # 「产多少用多少」还要求流量不太小（避免产 2 耗 2 也被放过）：当前消耗 ≥ 理论消耗 × 30%，
    # 理论消耗为 0 时改看当前生产 ≥ 理论生产 × 30%
    busy = (
        out.cur_cons >= out.theo_cons * FLOW_MIN_RATIO
        if out.theo_cons > 0
        else out.cur_prod >= out.theo_prod * FLOW_MIN_RATIO
    )
    flowing = (out.theo_prod > 0 and out.cur_prod >= out.theo_prod * FLOW_RATIO) or (
        out.cur_cons > 0 and out.cur_prod >= out.cur_cons * FLOW_CONS_RATIO and busy
    )
    if flowing:
        return "正常", f"产线满负荷，输出缓冲瞬时满，阻塞标记是暂时的；产物 {reg.fmt(out)}"
    if out.theo_prod > 0:  # 明显低于理论：看简报能不能解释（自身 excuse，或 P2 归因到上游）
        ex = reg.excuse(out)
        if ex:
            return "正常", f"产物低于理论，但简报能解释：{ex}；产物 {reg.fmt(out)}"
        root = upstream_root(reg, out)
        if root and root.level == "待确认":
            # 根因是上游的源头不足 / 流体不足 / 送不到：不算正常，记待确认，但只指向那条上游待确认，不另报问题
            return "待确认", f"低于理论已归因到上游 {root.text}（见待确认）；产物 {reg.fmt(out)}"
        if root:  # 设计限速 / 备路 / 爆仓 / 已满产不够分 / 闲置
            return "设计内", f"低于理论属设计内：{root.text}；产物 {reg.fmt(out)}"
    return None


def upstream_root(reg: Region, out: Item) -> Root | None:
    """P2 把这个产物的低产归因到别的物品时返回那个根因；根因就是它自己时返回 None。"""
    root, _ = reg.find_root(out)
    kind, _, ids = root.key.partition(":")
    if kind == "excused-fluid":  # key 里是流体显示名，必然是上游原料
        return root
    return None if out.id in ids.split("+") else root


def candidate_recipes(reg: Region, d: Device) -> list[dict]:
    """兜底：配方没认出时，这种设备的所有配方里、产物在本地区简报有生产/消耗记录的那些。"""
    names = set(d.machines) or {d.name}  # 快速模式名字可能是几个候选，取所有候选设备的配方并集
    return [r for r in RECIPES if r["machine_name"] in names and any(in_use(reg, o) for o in r["outputs"])]


def recipe_text(reg: Region, r: dict) -> str:
    def side(xs: dict) -> str:
        return " + ".join(f"{label_of(reg, i)}×{n}" for i, n in xs.items())

    return f"{side(r['inputs'])} → {side(r['outputs'])}"


def judge_blocked(reg: Region, d: Device, devices: list[Device]) -> Verdict:
    if not d.output:
        cands = candidate_recipes(reg, d)
        if not cands:
            return Verdict("待确认", d, "阻塞，但没认出配方/产物，没法对照简报", "进俯瞰点开这台设备看配方和输出口")
        # 兜底：每个候选产物都能解释才算正常
        outs = list(dict.fromkeys(o for r in cands for o in r["outputs"] if in_use(reg, o)))
        parts, bad, worst = [], [], "正常"
        for o in outs:
            ex = explain_blocked(reg, reg.items[o], devices)
            if ex is None or bucket(ex[0]) != "正常":  # 归因到上游待确认的也不算解释通
                bad.append(ex[1] if ex else reg.fmt(reg.items[o]))
                continue
            worst = "设计内" if ex[0] == "设计内" else worst
            parts.append(f"{label_of(reg, o)}：{ex[1]}")
        if bad:
            return Verdict(
                "待确认",
                d,
                f"没认出配方（兜底按候选产物 {'、'.join(label_of(reg, x) for x in outs)} 判断）；"
                f"阻塞解释不了的候选产物：{'；'.join(bad)}",
                "进俯瞰点开这台设备看配方和输出口",
                fallback=True,
            )
        return Verdict(worst, d, "没认出配方，兜底看所有候选产物都能解释 —— " + " | ".join(parts), fallback=True)

    out = reg.items.get(d.output)
    if out is None:
        return Verdict("待确认", d, f"阻塞，产物 {label_of(reg, d.output)} 不在简报里", "去俯瞰看输出口接到哪里")
    # 产耗同时比蓝图基准腰斩：即使产物爆仓也不当「爆仓停等」放过
    halved = halved_line(reg, d, out)
    if halved:
        return halved
    ex = explain_blocked(reg, out, devices)
    if ex:
        return Verdict(ex[0], d, ex[1])
    if out.theo_prod == 0:
        return Verdict(
            "待确认", d, f"阻塞，产物理论生产为 0、当前也没在流动，判断不了：{reg.fmt(out)}", "进俯瞰看输出口接到哪里"
        )
    # 阻塞本身不是异常（用户 2026-11-07：深度报告里多次把爆仓回堵判成问题）。两遍扫描都阻塞、简报又解释不通的，
    # 只给待确认；真断了的输出带靠「上游阻塞 + 下游空闲」配对、「固体送不到」等有旁证的规则抓
    return Verdict(
        "待确认",
        d,
        f"两遍扫描都阻塞，简报解释不了：产物没爆仓、在用的下游也没满：{reg.fmt(out)}，上限 {reg.cap}；"
        "阻塞多半是下游满了，隔几分钟再看一次，还堵就去看输出口",
        "去看这台设备的输出口：传送带、取货口、管道是否断开或接错；或过几分钟再跑一次巡检看是否还堵",
    )


def theo_cons_dropped(reg: Region, iid: str) -> bool:
    """物品的理论消耗比参考简报低 5% 以上：有消费者没在算它（被断开 / 没料），不能用「下游满了回堵」解释。"""
    i, ref = reg.items.get(iid), ref_theo_cons(reg, iid)
    return bool(i and ref and i.theo_cons < ref * 0.95)


def downstream_full(reg: Region, iid: str, hops: int = 4) -> list[str] | None:
    """按全配方表（不限本地区在用的）从 iid 往下游走最多 hops 步，碰到仓库已爆仓的产物就返回路径（显示名）。
    只走简报里有记录（active）的物品，避免追到根本没在做的东西。"""
    start = reg.items.get(iid)
    if start is None:
        return None
    frontier = [(iid, [start.label])]
    seen = {iid}
    for _ in range(hops):
        nxt = []
        for cur, path in frontier:
            for r in RECIPES:
                if cur not in r["inputs"]:
                    continue
                for o in r["outputs"]:
                    if o in seen:
                        continue
                    it = reg.items.get(o)
                    if it is None or not it.active:
                        continue
                    seen.add(o)
                    if reg.full(it):
                        return path + [it.label]
                    nxt.append((o, path + [it.label]))
        frontier = nxt
        if not frontier:
            break
    return None


def flowing_full(out: Item) -> bool:
    """产物当前生产 ≥ 理论 × 85%：产线在满负荷运转，设备的空闲/阻塞标记只是瞬时的。"""
    return out.theo_prod > 0 and out.cur_prod >= out.theo_prod * FLOW_RATIO


def waiting_tier_input(reg: Region, recipe: dict) -> str | None:
    """配方的某个原料是备路产物且备路没触发时返回说明（不管它在仓库里有多少：备路设备吃传送带，不从仓库取）。"""
    for iid in recipe["inputs"]:
        x = label_of(reg, iid)
        tm = tier_missing(reg, x)
        if tm:
            return f"在等备路原料 {x}，{x} 的备路未触发：{'、'.join(tm[1])} 未爆仓（{tm[0]['note']}）"
    return None


def baseline_slow(reg: Region, label: str) -> str | None:
    """没有备路规则时的替代：这个产物在参考简报里就没跑满，现在也不低于基准 → 做它的设备空着是设计内（diagnose.Region.baseline_excuse）。"""
    i = reg.by_label.get(label)
    return reg.baseline_excuse(i) if i is not None else None


def baseline_products(reg: Region) -> set[str]:
    """和基准一致的「本来就慢」的产物名（没有备路规则的蓝图用它顶替 tier_rules 的产物集合）。"""
    return {i.label for i in reg.items.values() if reg.baseline_excuse(i)}


def judge_idle(reg: Region, d: Device, devices: list[Device] | None = None) -> Verdict:
    # 备路未触发：主路产物没全爆仓，这台备路设备空着是正常的
    tm = tier_missing(reg, label_of(reg, d.output)) if d.output else None
    if tm:
        return Verdict("正常", d, f"备路未触发：{'、'.join(tm[1])} 未爆仓（{tm[0]['note']}）")
    bx = baseline_slow(reg, label_of(reg, d.output)) if d.output else None
    if bx:
        return Verdict("正常", d, bx)

    recipe = recipe_of(reg, d)
    if recipe is None and not d.output:
        cands = candidate_recipes(reg, d)
        if cands:
            return judge_idle_fallback(reg, d, cands)
    if recipe is None:
        return Verdict("待确认", d, "空闲中，但没认出配方，没法查原料", "进俯瞰点开这台设备看配方和进料口")

    inputs: list[Item] = []
    missing = []
    for iid in recipe["inputs"]:
        if iid in reg.items:
            inputs.append(reg.items[iid])
        else:
            missing.append(label_of(reg, iid))
    zero = [x for x in inputs if x.stock == 0]
    fluids = [x for x in inputs if x.fluid]
    plenty = [x for x in inputs if reg.plenty(x)]
    facts = "；".join(reg.fmt(x) for x in inputs) + (f"；简报里没有 {'、'.join(missing)}" if missing else "")

    # 原料是备路产物、备路没触发：备路设备不从仓库取，仓库里有多少都没用（库存 0 也一样），等着是设计内。
    # 要放在「库存为 0 往上游追」之前：赤铜瓶库存 0 时追到「塑形机没吃饱」是错的，塑形机本来就只吃溢流（2026-11-07）
    wait = waiting_tier_input(reg, recipe)
    if wait:
        return Verdict("设计内", d, f"{wait}；原料：{facts}")
    # 有原料库存为 0 时，它才是卡住的那个，先沿它往上游追
    if zero:
        root, chain = reg.find_root(zero[0])
        return Verdict(root.level, d, f"原料 {zero[0].label} 库存为 0，上游根因：{root.text}", root.advice, chain)
    # 原料是轮换产物（多配方反应池轮着做）、这会儿没在做（理论产 0）：吃它的设备等着是设计内
    # 2026-11-06 应龙关 灌装机(蓝铁瓶液化重息壤)：液化重息壤 理论产在历史简报里 0/30 来回切，当前 0 → 灌装机空闲
    rot = [x for x in inputs if x.label in reg.blueprint.rotating_products and x.theo_prod == 0]
    if rot:
        x = rot[0]
        return Verdict("设计内", d, f"原料 {x.label} 是轮换产物（{reg.blueprint.rotating_products[x.label]}），这会儿没在做（理论产 0），等着是设计内；原料：{facts}")
    # 产物已爆仓：这台是分流后的溢流位，上游不再往它送料，空着是设计内
    # （代价：产物满仓时真断了的进料带看不出来，如 15:10 的分离芯 89990）
    out = reg.items.get(d.output) if d.output else None
    if out and reg.full(out):
        return Verdict("正常", d, f"产物 {out.label} 已爆仓，溢流位没料进来是设计内：{reg.fmt(out)}")
    # 满速产线上设备偶尔空闲一两秒（等上一轮产物被取走），和阻塞的瞬时情况一样
    if out and flowing_full(out):
        return Verdict("正常", d, f"产线满负荷运转，空闲标记是暂时的；产物 {reg.fmt(out)}")
    if fluids:
        stall = producers_stalled(reg, d, fluids, devices or [])
        if stall:
            return Verdict("设计内", d, f"{stall}；原料：{facts}")
        names = "、".join(x.label for x in fluids)
        also = f"（固体原料 {'、'.join(x.label for x in plenty)} 有库存，也一并看进料口）" if plenty else ""
        return Verdict(
            "待确认", d, f"流体 {names} 供给可能不足：{facts}", f"查 {names} 的泵/暗管/准入口{also}"
        )
    normal_notes = bp.normal_notes(reg.blueprint.playbook, reg.name)  # 错题本 normal + 跨地区传输送进来的
    if out and out.label in normal_notes:
        return Verdict("正常", d, f"{out.label} 是错题本里的低优先级/设计内产物（{normal_notes[out.label]}）；原料：{facts}")
    # 某个原料供不应求：库存不多、产出几乎全被吃掉 → 这台在等那个原料，不是「有库存却没送到」
    # 2026-11-06 武陵城 封装机(优质锦草软饮)：赤铜零件 90000 够、赤铜瓶(锦草溶液) 270 产 15 耗 15，被按赤铜零件报成问题
    scarce = [x for x in inputs if not reg.plenty(x) and (x.stock or 0) > 0 and x.cur_prod > 0 and x.cur_cons >= x.cur_prod * 0.9]
    if scarce and plenty:
        x = scarce[0]
        return Verdict("正常", d, f"原料 {x.label} 供不应求（库存 {x.stock}，当前产 {x.cur_prod} 几乎全被耗掉），这台在等它；{x.label} 的产量由上游设计决定；原料：{facts}")
    if plenty:
        names = "、".join(x.label for x in plenty)
        # 产物产量比理论少、正好少这台的量：进一步坐实是它停了（B：分离芯 当前产 120 < 理论 180）
        gap = f"；产物 {out.label} 当前产 {out.cur_prod} < 理论 {out.theo_prod}，少的正是这台的量" if out and out.cur_prod < out.theo_prod else ""
        return Verdict(
            "问题",
            d,
            f"原料有库存却没送到：{facts}（上限 {reg.cap}，充足线 > {reg.cap // 100}）{gap}",
            f"查这台设备的进料口：{names} 的取货口、传送带、准入口限速；"
            "若这台设备旁的取货口设的物品不对（取货口改错），也会这样",
        )
    return Verdict("待确认", d, f"原料不多但不为 0，判断不了：{facts}", "进俯瞰看进料口和上游设备")


def producers_stalled(reg: Region, d: Device, inputs: list[Item], devices: list[Device]) -> str | None:
    """原料 X 在本次巡检里的生产设备全在阻塞，且每台都有别的产物爆仓/下游回堵（多配方反应池：息壤 满 → 整池停，
    惰性壤晶废液 就时断时续）：吃 X 的这台空闲是连带，不是 X 的管道/泵有问题。2026-11-07 exe 全扫 提纯机 (1673,1137)。"""
    for x in inputs:
        prods = [p for p in devices if p is not d and p.kind == "machine" and x.id in p.outputs and not p.off]
        if not prods or not all(effective_status(p) == "阻塞" for p in prods):
            continue
        whys = []
        for p in prods:
            full = [label_of(reg, o) for o in p.outputs if o != x.id and o in reg.items and (reg.full(reg.items[o]) or reg.blocked_path(o))]
            if not full:
                break
            whys.append(f"{p.name} {p.where}（{'、'.join(full)} 爆仓/回堵）")
        else:
            return f"原料 {x.label} 的生产设备 {'、'.join(whys)} 因别的产物满而停等，{x.label} 时断时续，这台等着是连带"
    return None


def judge_idle_fallback(reg: Region, d: Device, cands: list[dict]) -> Verdict:
    """兜底：配方没认出的空闲设备，每条候选配方都能解释（备路未触发 / 原料为 0 且追到根因）才算正常。"""
    explained, unexplained, chain = [], [], []
    worst = "正常"
    for r in cands:
        text = recipe_text(reg, r)
        tm = next((tier_missing(reg, label_of(reg, o)) for o in r["outputs"] if tier_missing(reg, label_of(reg, o))), None)
        if tm:
            explained.append(f"{text}：备路未触发，{'、'.join(tm[1])} 未爆仓")
            continue
        wait = waiting_tier_input(reg, r)
        if wait:
            explained.append(f"{text}：{wait}")
            continue
        full_speed = [reg.items[o] for o in r["outputs"] if o in reg.items and flowing_full(reg.items[o])]
        if full_speed:
            explained.append(f"{text}：产线满负荷运转，空闲标记是暂时的（{reg.fmt(full_speed[0])}）")
            continue
        zero =[reg.items[i] for i in r["inputs"] if i in reg.items and reg.items[i].stock == 0]
        if zero:
            root, sub = reg.find_root(zero[0])
            explained.append(f"{text}：原料 {zero[0].label} 库存为 0，根因 [{root.level}] {root.text}")
            chain += sub
            if root.level == "待确认":  # 根因本身待确认时不能算正常
                worst = "待确认"
            elif root.level != "正常" and worst == "正常":
                worst = root.level
            continue
        have = "、".join(reg.fmt(reg.items[i]) if i in reg.items else label_of(reg, i) for i in r["inputs"])
        unexplained.append(f"{text}（原料：{have}）")
    union = "、".join(dict.fromkeys(label_of(reg, i) for r in cands for i in r["inputs"]))
    head = f"没认出配方，兜底看 {len(cands)} 条候选配方（原料并集：{union}）"
    if unexplained:
        return Verdict(
            "待确认",
            d,
            f"{head}；解释不了的配方：{'；'.join(unexplained)}"
            + (f"；能解释的：{'；'.join(explained)}" if explained else ""),
            "进俯瞰点开这台设备看配方和进料口",
            chain,
            fallback=True,
        )
    return Verdict(worst, d, f"{head}，都能解释 —— {' | '.join(explained)}", chain=chain, fallback=True)


def effective_status(d: Device) -> str:
    """面板状态没读出来时按俯瞰标记。"""
    return d.status if d.status not in ("?", "") else d.badge


def pool_recipes(d: Device) -> list[dict]:
    """面板读到「池产物：…」的反应池：它配置的那几条通道配方（pool_channels，面板列的物品不全是产物）；不是池子返回 []。
    2026-11-10 武陵城扩容反应池 (1489,1055) 面板列「息壤」，是「清水 + 息壤 → 液化息壤」的原料：
    按产物找不到这条，原料里就没有清水，断水停了被兜底配方说成「重息壤 送不到」。"""
    if "池产物：" not in (d.recipe or ""):
        return []
    import pool_channels as PC
    ids = []
    name_id = PC._name_id()
    for a, _ in PC.slot_items(d.recipe.split("池产物：", 1)[1]):
        if a in name_id:
            ids.append(name_id[a])
    return PC.channels(d.machines[0] if len(d.machines) == 1 else d.name, ids)


def recipe_inputs(reg: Region, d: Device) -> set[str]:
    """设备配方的原料（产物有多个候选时取各候选配方原料的并集）；认不出配方时为空。
    反应池按面板列的全部通道配方算（池子几条配方同时跑）。"""
    out: set[str] = set()
    for r in pool_recipes(d):
        out |= set(r["inputs"])
    for k, o in enumerate(d.outputs):
        r = recipe_of(reg, replace(d, output=o, recipe_id=d.recipe_id if k == 0 else None))
        if r:
            out |= set(r["inputs"])
    if not d.outputs and d.recipe_id in RECIPE_BY_ID:
        out |= set(RECIPE_BY_ID[d.recipe_id]["inputs"])
    return out


def idle_by_design(reg: Region, d: Device) -> str | None:
    """空闲设备的任一候选产物爆仓 / 下游回堵 / 备路未触发，或配方在等备路原料：它空着是设计内，返回原因。"""
    for k, o in enumerate(d.outputs):
        i = reg.items.get(o)
        if i is not None and (reg.full(i) or reg.blocked_path(o)):
            return f"{i.label} 爆仓或下游回堵"
        if tier_missing(reg, label_of(reg, o)):
            return f"{label_of(reg, o)} 备路未触发"
        r = recipe_of(reg, replace(d, output=o, recipe_id=d.recipe_id if k == 0 else None))
        if r and waiting_tier_input(reg, r):
            return waiting_tier_input(reg, r)
        bx = baseline_slow(reg, label_of(reg, o))
        if bx:
            return bx  # 产物在基准里就没跑满（没有备路规则的蓝图靠这个），2026-11-07 exe 全扫：灌装机 赤铜耐压罐(惰气) 基准 0/30
    return None


_REF_ITEMS: dict[tuple[str, str], dict[str, Item]] = {}


def ref_item(reg: Region, iid: str) -> Item | None:
    """蓝图参考简报里同地区的这个物品；没有参考简报或没有这一行时返回 None。"""
    plan = reg.blueprint
    rows = (plan.reference_brief or {}).get(reg.name)
    if not rows:
        return None
    key = (plan.name, reg.name)
    if key not in _REF_ITEMS:
        _REF_ITEMS[key] = Region(reg.name, rows, plan).items
    return _REF_ITEMS[key].get(iid)


def ref_theo_cons(reg: Region, iid: str) -> int | None:
    """蓝图参考简报里这个物品的理论消耗；没有参考简报或没有这一行时返回 None。"""
    i = ref_item(reg, iid)
    return i.theo_cons if i else None


def halved_line(reg: Region, d: Device, out: Item) -> Verdict | None:
    """产物 P 的当前产、耗同时低于蓝图参考简报当前值的 60%：不是单纯爆仓回堵，这条线本身出了事。"""
    ref = ref_item(reg, out.id)
    if ref is None or ref.cur_prod <= 0 or ref.cur_cons <= 0:
        return None
    if out.cur_prod >= ref.cur_prod * HALVED_RATIO or out.cur_cons >= ref.cur_cons * HALVED_RATIO:
        return None
    if reg.full(out) or reg.blocked_path(out.id):
        # 产物已爆仓（或下游全满回堵）：产量只跟着下游需求走，产耗一起比参考低只是下游收得少（砂叶/砂叶种子的种植循环就是这样），
        # 不能据此怀疑输出带断了——按爆仓停等处理。断了的进料/出料靠「空闲设备原料有库存却没送到」等规则抓
        return None
    return Verdict(
        "待确认",
        d,
        f"{out.label} 这条线产耗同时比蓝图基准低一半以上（产 {out.cur_prod}/耗 {out.cur_cons}，"
        f"基准 {ref.cur_prod}/{ref.cur_cons}），{out.label} 的生产设备 {d.name} {d.where} 阻塞，"
        f"可能是它的输出传送带断了；{reg.fmt(out)}",
        "去俯瞰看这台设备的输出口：传送带是否被拆或断开",
        key=f"halved:{out.label}",
    )


def pair_broken(reg: Region, devices: list[Device]) -> list[Verdict]:
    """上游 A 阻塞、产物 P；下游 B 空闲、原料含 P；两台相距 ≤ 450 像素 → P 从 A 送不到 B，中间断了。
    每台设备只配一次；多个 B 满足时取最近的。关闭的设备不参与（关闭规则优先）。"""
    live = [d for d in devices if not d.off and d.kind == "machine"]
    blocked = [d for d in live if effective_status(d) == "阻塞" and d.outputs]
    # 下游空闲本身已按设计解释得通的（产物爆仓/回堵、备路未触发、在等备路原料）不参与配对
    idle = [(d, recipe_inputs(reg, d)) for d in live if effective_status(d) == "空闲中" and not idle_by_design(reg, d)]
    used: set[int] = set()
    out: list[Verdict] = []
    raw: list[tuple[Device, Device, str, float]] = []  # (上游, 下游, 产物, 距离)
    for a in blocked:
        best = None  # (距离, B, P)
        full_slots = pool_full_labels(a)  # 池子：只有标「满」的产物格才是堵住它的
        for p in a.outputs:
            if p in reg.items and reg.items[p].theo_cons == 0 and ref_theo_cons(reg, p) == 0:
                continue  # 蓝图里本来就没有设备消耗 P：下游这条配方并没在用，不是断了
            if p in reg.items and reg.full(reg.items[p]):
                continue  # P 仓库已满：上游阻塞是仓满回堵，不是送不到下游（2026-11-07 exe 全扫：塑形机 赤铜耐压罐 89995）
            if not theo_cons_dropped(reg, p) and downstream_full(reg, p):
                continue  # P 的下游有爆仓产物且 P 的理论消耗没掉：最终产物满了回堵上来，不是中间断了（用户 2026-11-07）
                # 理论消耗掉了 = 有消费者没在算它（断了/没料），这时上游阻塞 + 下游空闲就是断点证据，照常配对
            if full_slots is not None and label_of(reg, p) not in full_slots:
                continue  # 池子里这个产物格没满（0），不是它堵的（2026-11-07：扩容反应池 惰性壤晶废液(0)，堵的是 息壤/壤晶废液(50)）
            for b, inputs in idle:
                if b is a or id(b) in used or p not in inputs:
                    continue
                dist = ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5
                if dist <= PAIR_PX and (best is None or dist < best[0]):
                    best = (dist, b, p)
        if best is None:
            continue
        dist, b, p = best
        used |= {id(a), id(b)}
        raw.append((a, b, p, dist))

    # 同一产物、上游互相相距 ≤ 300 像素的几对，多半是同一条线断了：合并成一条
    clusters: list[list[tuple]] = []
    for pr in raw:
        near = [c for c in clusters if c[0][2] == pr[2] and any(dist2(pr[0], q[0]) <= LINE_PX for q in c)]
        for c in near[1:]:  # 把这一对连起来的几组并成一组
            near[0].extend(c)
            clusters.remove(c)
        if near:
            near[0].append(pr)
        else:
            clusters.append([pr])

    for c in clusters:
        p = c[0][2]
        fluid = reg.items[p].fluid if p in reg.items else p.startswith(("item_liquid_", "item_gas_"))
        link = "管道" if fluid else "传送带"
        stock = reg.fmt(reg.items[p]) if p in reg.items else label_of(reg, p)
        ref = ref_theo_cons(reg, p)
        dropped = p in reg.items and reg.items[p].theo_cons == 0 and ref
        if dropped:
            stock += f"；理论消耗从蓝图的 {ref}/分 掉到 0：消耗它的设备没接上"
        advice = (
            f"先看下游设备是不是重新放置过、没设配方/出货口（{RESET_HINT}）；"
            f"再去俯瞰看这{'两' if len(c) == 1 else '几'}台之间的{link}"
            + ("、管道准入口、暗管" if fluid else "、物品准入口、分流器")
            + "是否断开或接错"
        )
        if len(c) == 1:
            a, b, _, dist = c[0]
            out.append(
                Verdict(
                    "问题",
                    a,
                    f"{label_of(reg, p)} 从 {a.name} {a.where} 送不到 {b.name} {b.where}，中间的{link}可能断了或接错"
                    f"（上游阻塞、下游空闲，相距 {dist:.0f} 像素）；{stock}",
                    advice,
                    partner=b,
                    key=f"line:{label_of(reg, p)}",
                )
            )
            continue
        ups, downs = [x[0] for x in c], [x[1] for x in c]
        up_names = "、".join(dict.fromkeys(d.name for d in ups))
        down = "、".join(f"{d.name} {d.where}" for d in downs)
        out.append(
            Verdict(
                "问题",
                ups[0],
                f"{label_of(reg, p)} 这条线断了：上游 {up_names} {'、'.join(d.where for d in ups)} 阻塞，"
                f"下游 {down} 空闲（中间的{link}可能断了或接错）；{stock}",
                advice,
                title=f"{label_of(reg, p)} 这条线断了（{len(ups)} 台上游阻塞、{len(downs)} 台下游空闲）",
                members=ups + downs,
                key=f"line:{label_of(reg, p)}",
            )
        )
    return out


def pool_full_labels(d: Device) -> set[str] | None:
    """反应池面板文字「池产物：A(0)/B(50)/C(50)…」里数量 ≥ 50 的产物名；不是池子返回 None。"""
    if not (d.recipe_id or "").startswith("pool:") or "池产物：" not in (d.recipe or ""):
        return None
    seg = d.recipe.split("池产物：", 1)[1].split("；", 1)[0]
    return {m.group(1) for m in re.finditer(r"([^/（(]+?)\((\d+)\)", seg) if int(m.group(2)) >= 50}


def dist2(a: Device, b: Device) -> float:
    return ((a.x - b.x) ** 2 + (a.y - b.y) ** 2) ** 0.5


SURPLUS_RATIO = 0.9  # 流体当前消耗 < 源头设备理论产量 × 这个比例：泵比用的多，多出来的泵排不出去会堵


def source_surplus(reg: Region | None, fluid: str, devices: list[Device] | None) -> str | None:
    """源头流体供大于求时返回说明：当前消耗明显低于源头设备的理论产量，且这次巡检里没有吃它的设备持续空闲。
    用户 2026-11-08：基地外的水泵堵塞很正常，消耗没有生产的快（当时清水 产1753/耗1722，泵理论 2610；息壤爆仓、洪炉用水少）。
    真断管时消费者会缺水：要么巡检里有吃它的空闲设备（这里就不放过），要么简报判「清水供不上」成根因，
    这条 source: 条目会被并进根因、升成问题（见 report 里「源头设备阻塞并进根因」）。"""
    if reg is None or not fluid:
        return None
    it = reg.by_label.get(label_fluid(fluid))
    if it is None or not it.theo_prod or it.cur_cons >= it.theo_prod * SURPLUS_RATIO:
        return None
    for x in devices or []:
        if effective_status(x) == "空闲中" and x.passes >= 2 and x.recipe_id and it.id in (RECIPE_BY_ID.get(x.recipe_id) or {}).get("inputs", []):
            return None
    return (f"{it.label}供大于求：当前消耗 {it.cur_cons}/分，源头设备理论能出 {it.theo_prod}/分，多出来的泵排不出去就会堵，"
            f"正常（巡检里没有吃{it.label}的设备空闲；简报 {reg.fmt(it)}）")


def judge_source_blocked(d: Device, reg: Region | None = None, devices: list[Device] | None = None) -> Verdict | None:
    """源头设备（水泵、矿机、气体收集泵……）阻塞：它没有原料可缺，只能是产出送不出去——
    送不出去可能是后面断了，也可能是用的比产的少（供大于求，见 source_surplus）。"""
    names = set(d.candidates) & SOURCE_DEVICES
    if not names or effective_status(d) != "阻塞":
        return None
    kind = "传送带" if any("矿机" in n for n in names) else "暗管/管道"
    fluid = next((SOURCE_DEVICE_FLUID[n] for n in names if n in SOURCE_DEVICE_FLUID), "")
    what = label_fluid(fluid) if fluid else "产出"
    ok = source_surplus(reg, fluid, devices)
    if ok:
        # key 仍是 source:流体——简报若判这种流体供不上（根因），这条会被并进根因、升成问题
        return Verdict("正常", d, f"{'|'.join(d.candidates)} {d.where} 阻塞：{ok}", key=f"source:{fluid}")
    return Verdict(
        "问题",
        d,
        f"{'|'.join(d.candidates)} {d.where} 的{what}送不出去：它后面的{kind}可能断了",
        f"去俯瞰沿这台设备的输出口查{kind}、准入口是否断开或接错",
        title=f"{what}的源头设备 {'|'.join(d.candidates)} {d.where} 阻塞：后面的{kind}可能断了" if fluid else "",
        key=f"source:{fluid}" if fluid else "",
    )


def label_fluid(fluid: str) -> str:
    """SOURCE_DEVICE_FLUID 里的值是显示名还是 id 都能转成显示名。"""
    return ID_NAME.get(fluid, fluid)


def starved_solid(reg: Region, r: dict) -> Item | None:
    """配方里的固体原料：库存充足、自己按理论满速在产（≥ 85%），当前消耗却 < 理论消耗 × 60%——有货也在产，
    就是送不进消费者。生产也上不去的（如上游缺清水、自己爆仓停产）不算：那是别的原因。"""
    for iid in r["inputs"]:
        x = reg.items.get(iid)
        if (
            x is not None
            and not x.fluid
            and reg.plenty(x)
            and x.theo_cons > 0
            and x.cur_cons < x.theo_cons * STARVED_RATIO
            and x.theo_prod > 0
            and x.cur_prod >= x.theo_prod * FLOW_RATIO
        ):
            return x
    return None


DESIGN_KEEP = 0.9  # 设计限速的产物：当前产量 ≥ 目标 / 基准当前产量 × 这个比例，算按设计在跑


def design_limited_ok(reg: Region, d: Device) -> str | None:
    """这台设备的产物是蓝图设计限速的（tier_rules.design_limits），而且产量达到了错题本目标或基准：它缺原料空着是设计，返回说明。
    2026-11-10 灰度包实测：景玉谷封装机 (752,732) 做中容武陵电池，致密源石粉末准入口限 6、分到每台约 1.2/分，大部分时间空着；
    中容当时 15/分（目标 14.7），却被「致密源石粉末 送不到」报成问题。真断料时中容产量会掉到目标以下，照报。"""
    targets = {t["product"]: t["per_min"] for t in reg.blueprint.playbook.get("targets", [])}
    for o in d.outputs:
        lab = label_of(reg, o)
        note = reg.blueprint.design_limits.get(lab)
        it = reg.items.get(o)
        if not note or it is None:
            continue
        ref = ref_item(reg, o)
        goal = targets.get(lab) or (ref.cur_prod if ref is not None and ref.cur_prod else None)
        if reg.full(it) or (goal and it.cur_prod >= goal * DESIGN_KEEP):
            return f"{lab} 是设计限速产物（{note}），当前产 {it.cur_prod}/分已达到{'目标' if lab in targets else '基准'} {goal}：这台缺料空着是设计如此"
    return None


def group_starved_solid(reg: Region, devices: list[Device], used: set[int]) -> list[Verdict]:
    """空闲设备的候选配方里，有固体原料库存充足、消耗却远低于理论：按这种固体合并成一条问题。
    先看候选产物对应的配方，都不命中再看这种设备在本地区在用的全部配方（快速模式常把产物猜错）。
    备路未触发 / 在等备路原料的设备不算（它们空着是设计内）。放在「流体有余」之前。"""
    groups: dict[str, tuple[Item, list[Device]]] = {}
    for d in devices:
        if id(d) in used or d.off or d.kind != "machine" or effective_status(d) != "空闲中":
            continue  # 没认出产物的也参加：按这种设备在本地区在用的配方兜底（应龙关两台空闲固气转化机有一台认不出产物）
        if any(tier_missing(reg, label_of(reg, o)) for o in d.outputs) or overflow_product(reg, d) or design_limited_ok(reg, d):
            continue
        pool = pool_recipes(d)
        cands = pool or [recipe_of(reg, replace(d, output=o, recipe_id=d.recipe_id if k == 0 else None)) for k, o in enumerate(d.outputs)]
        # 自己候选产物已经爆仓的配方也不算：空着是爆仓停等，不是原料送不到
        # （2026-11-07 定向巡检：没点开面板的封装机猜成 中容/锦草软饮，两者都 90000 满，却被按致密源石粉末报「送不到」）
        cands = [r for r in cands if r and not any((i := reg.items.get(o)) is not None and reg.full(i) for o in r["outputs"])]
        hit, fallback = None, False
        # 点开面板确认过配方的设备（guessed=False）只按它自己的配方判，不拿同种设备的其他配方兜底；
        # 兜底配方里产物已经爆仓的也不算：做那种东西的设备空着是溢流设计，不是原料送不到
        # （封装机猜成锦草软饮时别拿满仓的电池配方说它缺致密源石粉末；固气转化机的气态赤铜没有仓位，F 不受影响）
        others = [
            r for r in candidate_recipes(reg, d)
            if not any((i := reg.items.get(o)) is not None and reg.full(i) for o in r["outputs"])
        ]
        # 快速模式猜的、或点开了面板但配方没认出来的（recipe_id 为空）才用兜底配方；面板认出配方的只按自己的
        # 面板读到池产物的反应池：它配置的通道就是全部配方，不拿同类池子的其他配方兜底
        for rs in ((cands, others) if (d.guessed or not d.recipe_id) and not pool else (cands,)):
            hit = next((x for r in rs if not waiting_tier_input(reg, r) and (x := starved_solid(reg, r))), None)
            if hit:
                break
            fallback = True  # 自己产物对应的配方解释不了，才看这种设备的其他配方
        if hit:
            groups.setdefault(hit.id, (hit, []))[1].append((d, fallback))
    out = []
    for x, pairs in groups.values():
        # 只靠「同种设备的其他配方」兜底命中的单台设备不算：产物认错时它自己的配方更能解释（B：封装机做分离芯，
        # 别拿做致密源石粉末的配方说它缺致密源石粉末）；F 那种 4 台固气转化机一起缺赤铜块的仍成立
        if all(fb for _, fb in pairs) and len(pairs) < 2:
            continue
        members = [d for d, _ in pairs]
        used |= {id(d) for d in members}
        where = "、".join(f"{d.name} {d.where}" for d in members)
        out.append(
            Verdict(
                "问题",
                members[0],
                f"{x.label} 送不到这些设备（固体原料有库存，消耗 {x.cur_cons} 远低于理论 {x.theo_cons}）：{where}；{reg.fmt(x)}",
                "检查它们前面的准入口/取货口设的物品是否被改错（首先查这个）、传送带是否断了",
                title=f"{x.label} 送不到 {len(members)} 台设备",
                members=members,
                key=f"starved:{x.label}",
            )
        )
    return out


def env_gas_drops(reg: Region) -> list[tuple[Item, int]]:
    """理论消耗比参考正好少 6 的倍数的气体：（气体, 参考理论消耗）。一个给气体散布机的准入口被关就是这样。"""
    return [
        (x, ref)
        for x, ref in theo_drops(reg)
        if x.id.startswith("item_gas_") and (ref - x.theo_cons) % GATE_UNIT == 0
    ]


def env_machine_of(d: Device) -> str | None:
    """设备名唯一且是环境类设备时返回设备名；「气体反应炉|气体收集泵|…」这种按首字猜的候选不算。"""
    return d.machines[0] if len(d.machines) == 1 and d.machines[0] in ENV_MACHINES else None


def short_outputs(reg: Region, members: list[Device]) -> dict[str, Item]:
    """这些设备候选配方的产物里，当前生产 < 理论 × ENV_SHORT_RATIO 的：缺环境的直接后果。"""
    outs = {o for d in members for r in candidate_recipes(reg, d) for o in r["outputs"]}
    return {
        o: reg.items[o]
        for o in outs
        if o in reg.items and reg.items[o].theo_prod > 0 and reg.items[o].cur_prod < reg.items[o].theo_prod * ENV_SHORT_RATIO
    }


def group_env_starved(reg: Region, devices: list[Device], used: set[int]) -> list[Verdict]:
    """缺环境：需要气体散布机环境的设备（天有洪炉等）空闲，且
      - 深查面板标了「无环境」，或
      - 它的环境气体理论消耗比参考正好少 6 的倍数（给散布机的准入口被关/限速改 0）
    → 合并成一条问题，指向散布机的气体准入口。原料本身不缺（面板里原料槽是满的），所以不走「流体供给不足」那条。"""
    idle = [
        d
        for d in devices
        if id(d) not in used
        and not d.off
        and d.kind == "machine"
        and effective_status(d) == "空闲中"
        and (d.env == "无环境" or env_machine_of(d))
    ]
    if not idle:
        return []
    drops = env_gas_drops(reg)
    groups: dict[str, tuple[Item | None, int, list[Device]]] = {}
    for d in idle:
        m = env_machine_of(d)
        want = ENV_MACHINES.get(m) if m else None
        hit = next(((x, ref) for x, ref in drops if want is None or x.label == want), None)
        if hit is None and d.env != "无环境":
            continue  # 只是空闲、没有缺环境的证据：交给普通空闲规则
        key = hit[0].label if hit else "？"
        groups.setdefault(key, (hit[0] if hit else None, hit[1] if hit else 0, []))[2].append(d)
    out = []
    for gas, ref, members in groups.values():
        used |= {id(d) for d in members}
        names = "、".join(sorted({d.name for d in members}))
        where = "、".join(f"{d.name} {d.where}{'（面板「无环境」）' if d.env == '无环境' else ''}" for d in members)
        parts = [f"{where} 空闲"]
        if gas is not None:
            n = (ref - gas.theo_cons) // GATE_UNIT
            parts.append(f"{gas.label} 理论消耗从 {ref} 降到 {gas.theo_cons}：差 {ref - gas.theo_cons} = {n} 个给气体散布机的准入口被关")
        if any(d.env == "无环境" for d in members):
            parts.append("面板顶部标「无环境」= 散布机没把环境气体送到")
        short = short_outputs(reg, members)
        if short:
            parts.append("后果：" + "、".join(f"{x.label} 当前产 {x.cur_prod} < 理论 {x.theo_prod}" for x in short.values()))
        gas_text = gas.label if gas else "环境气体"
        confirmed = any(d.env == "无环境" for d in members)
        # 气体理论消耗是地区级数字，哪个分区的散布机没气光看简报分不出来：没点开面板确认「无环境」的只算待确认
        fix = (
            f"找给气体散布机供{gas_text}的管道准入口：限速是否被改成 0（气体准入口按 6 一档）、管道是否断；"
            "原料不用查（缺环境的设备原料槽是满的）"
        )
        out.append(
            Verdict(
                "问题" if confirmed else "待确认",
                members[0],
                "；".join(parts),
                fix if confirmed else f"点开其中一台看面板顶部是否标「无环境」（深查会自动读）；是的话：{fix}",
                title=f"{names} {'缺环境' if confirmed else '可能缺环境'}：{gas_text}没送到气体散布机（{len(members)} 台）",
                members=members,
                key=f"env:{gas.label if gas else '?'}",
            )
        )
    return out


def fold_blocked_feeders(reg: Region, verdicts: list[Verdict]) -> None:
    """根因条目（root:…）的受影响设备空闲后，给它们送料的上游设备产物积压 → 阻塞，会被「产耗腰斩，可能输出传送带断了」
    单独报出来。2026-11-07：清水短 → 种植机空闲 → 采种机(芽针种子) 阻塞。这类并进根因作连带。"""
    roots = [v for v in verdicts if v.key.startswith("root:") and v.members]
    if not roots:
        return
    for v in list(verdicts):
        if v.title or bucket(v.level) != "待确认" or not v.device.output or v.device.status != "阻塞":
            continue
        for r in roots:
            eaters = [m for m in r.members if (rec := recipe_of(reg, m)) and v.device.output in rec["inputs"]]
            if eaters:
                verdicts.remove(v)
                r.members.append(v.device)
                r.reason += f"；连带：{v.device.name} {v.device.where} 阻塞——它的产物 {label_of(reg, v.device.output)} 被空闲的 {eaters[0].name} 吃不动，积压"
                break


def fold_env_cascade(reg: Region, verdicts: list[Verdict]) -> None:
    """缺环境设备的产物（如息壤）减产后，吃它的设备空着、再往下游堵，会被「这条线断了」「送不到」等规则各自报成问题。
    这些条目降为待确认，标明可能是缺环境的连带。"""
    for ev in [v for v in verdicts if v.key.startswith("env:")]:
        short = short_outputs(reg, ev.members)
        if not short:
            continue
        for v in verdicts:
            if v is ev or v.key.startswith("env:") or bucket(v.level) != "问题":
                continue
            members = v.members or [x for x in (v.device, v.partner) if x]
            eaters = [d for d in members if any(set(r["inputs"]) & set(short) for r in candidate_recipes(reg, d))]
            if not eaters:
                continue
            item = next(x for o, x in short.items() if any(o in r["inputs"] for d in eaters for r in candidate_recipes(reg, d)))
            v.level = "待确认"
            v.reason += (
                f"；可能是{ev.title.split('：')[0]}的连带：{item.label} 当前产 {item.cur_prod} < 理论 {item.theo_prod}，"
                f"{'、'.join(sorted({d.name for d in eaters}))} 等不到 {item.label}"
            )
            v.advice = "先修缺环境那条，再看这里是否恢复" + (f"；{v.advice}" if v.advice else "")


def waiting_tier(reg: Region, d: Device) -> str | None:
    """这台空闲设备的任一候选产物是没触发的备路，或任一候选配方在等备路原料：返回说明，否则 None。"""
    for k, o in enumerate(d.outputs):
        if tier_missing(reg, label_of(reg, o)):
            return f"{label_of(reg, o)} 备路未触发"
        r = recipe_of(reg, replace(d, output=o, recipe_id=d.recipe_id if k == 0 else None))
        if r and waiting_tier_input(reg, r):
            return waiting_tier_input(reg, r)
        bx = baseline_slow(reg, label_of(reg, o))
        if bx:
            return bx  # 产物在基准里就没跑满（没有备路规则的蓝图靠这个），2026-11-07 exe 全扫：灌装机 赤铜耐压罐(惰气) 基准 0/30
    return None


def group_surplus_fluid(reg: Region, devices: list[Device], used: set[int]) -> list[Verdict]:
    """流体 F 产得出（当前生产 ≥ 消耗 × 1.2 且差额 ≥ 20）却有 ≥ 2 台吃 F 的设备空闲：合并成一条问题。
    设备按候选产物对应的配方判断吃不吃 F；一台设备只归到余量最大的那种流体。
    不看产物是否爆仓：流体有余、消费者却空着，本身就说明进料口那一段有问题。"""
    fluids = sorted(
        (
            i
            for i in reg.items.values()
            if i.fluid and i.cur_prod >= i.cur_cons * SURPLUS_RATIO and i.cur_prod - i.cur_cons >= SURPLUS_MIN
        ),
        key=lambda i: i.cur_prod - i.cur_cons,
        reverse=True,
    )
    # 只看认出了（候选）产物的设备，按产物对应的配方判断吃不吃 F；没认出产物的兜底设备不算
    idle = [
        (d, recipe_inputs(reg, d))
        for d in devices
        if id(d) not in used and not d.off and d.kind == "machine" and effective_status(d) == "空闲中" and d.outputs
    ]
    out = []
    for f in fluids:
        # 在等备路原料（如赤铜瓶：塑形机只吃赤铜块溢流）或自己就是备路设备的，空着是设计内，不算「收不到流体」（2026-11-07）
        members = [d for d, inputs in idle if f.id in inputs and id(d) not in used and not waiting_tier(reg, d)]
        if len(members) < SURPLUS_MIN_DEVICES:
            continue
        used |= {id(d) for d in members}
        where = "、".join(f"{d.name} {d.where}" for d in members)
        out.append(
            Verdict(
                "问题",
                members[0],
                f"{f.label} 产得出（产 {f.cur_prod}/耗 {f.cur_cons}，理论 产{f.theo_prod}/耗{f.theo_cons}）"
                f"但这些设备收不到：{where}",
                "检查它们前面的管道准入口限速是否被调小，或管道/暗管断了",
                title=f"{f.label} 产得出但 {len(members)} 台设备收不到",
                members=members,
            )
        )
    return out


_LATEST_AREA_PATROL: dict[str, tuple[str, list[dict]]] | None = None
CONTEXT_PATROLS: list[Path] | None = None  # 本次运行产出的巡检文件；设了就只用它们（exe/全基地巡检），没设才扫 debug（开发期）


def set_context_patrols(paths) -> None:
    """限定「最近各分区扫描」只看这些文件（full_patrol 本次跑出来的）。产品里不能依赖 debug 目录的历史文件。"""
    global CONTEXT_PATROLS, _LATEST_AREA_PATROL
    CONTEXT_PATROLS = [Path(p) for p in paths]
    _LATEST_AREA_PATROL = None


def latest_patrol_by_area() -> dict[str, tuple[str, list[dict]]]:
    """每个分区最近一次巡检：分区名 → (文件名, devices)。源头短缺时用来说「吃它的设备上次见于哪个分区、几台空闲」。"""
    global _LATEST_AREA_PATROL
    if _LATEST_AREA_PATROL is None:
        out: dict[str, tuple[str, list[dict]]] = {}
        files = sorted(CONTEXT_PATROLS, key=lambda f: f.name) if CONTEXT_PATROLS is not None else sorted(DEBUG.glob("patrol_*.json"))
        for f in files:
            try:
                d = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(d, dict) and d.get("area") and d.get("devices"):  # 旧格式的 patrol JSON 是列表，跳过
                out[d["area"]] = (f.name, d["devices"])  # 按文件名排序，后者覆盖前者 = 最新
        _LATEST_AREA_PATROL = out
    return _LATEST_AREA_PATROL


def downstream_summary(reg: Region, label: str) -> tuple[str, str]:
    """源头 X 短缺时的下游说明（用户 2026-11-06：看产能数字不如看缺它之后谁停了）。
    返回（依据补充, 建议补充）：吃 X 的配方→产物（当前/理论）；这些设备种类上次扫描见于哪个分区、几台空闲。"""
    item = reg.by_label.get(label)
    if item is None:
        return "", ""
    users = reg.consumers.get(item.id) or []
    if not users:
        return "", ""
    chain = []
    machines: set[str] = set()
    for r in users:
        machines.add(r["machine_name"])
        for o in r["outputs"]:
            it = reg.items.get(o)
            if it is not None:
                chain.append(f"{r['machine_name']}→{it.label}（当前 {it.cur_prod}/理论 {it.theo_prod}）")
    seen = []
    for area_name, (fname, devs) in latest_patrol_by_area().items():
        mine = [d for d in devs if d.get("name") in machines]
        if not mine:
            continue
        idle = [d for d in mine if d.get("status") in ("空闲中", "未激活") or d.get("badge") == "空闲中"]
        seen.append(f"{area_name} {len(mine)} 台{'、' + str(len(idle)) + ' 台空闲' if idle else ''}（{fname[7:-5]}）")
    reason = "；下游：" + "、".join(dict.fromkeys(chain)) if chain else ""
    if seen:
        reason += "；这些设备上次扫描见于：" + "、".join(seen)
    advice = ""
    idle_areas = [x.split(" ")[0] for x in seen if "空闲" in x]
    if idle_areas:
        advice = f"；先去 {'、'.join(idle_areas)} 看空闲的那几台，顺着它们的进料暗管往上找断点"
    return reason, advice


def fluid_consumer_status(reg: Region, label: str) -> tuple[dict[str, int], list[str]]:
    """最近各分区扫描里「配方吃这种流体」的设备状态计数，和其中空闲/未激活的那些（名字 坐标 分区）。
    用户 2026-11-06：看水的产能不如看缺水之后谁停了——水/气按需供给，下游爆仓用量就低，只有有设备因缺它而空闲才算真短缺。"""
    item = reg.by_label.get(label)
    if item is None:
        return {}, []
    user_ids = {r["id"] for r in RECIPES if item.id in r["inputs"]}
    # 池子：只有「可能在跑的产物」里有吃这种流体的配方才算它的消费设备（2026-11-07：壤晶废液池不吃清水，之前被算成「因缺清水空闲」）
    pool_eaters = {r["machine_name"] for r in RECIPES if item.id in r["inputs"]}
    counts: dict[str, int] = {}
    idle: list[str] = []
    for area_name, (fname, devs) in latest_patrol_by_area().items():
        for d in devs:
            rid = d.get("recipe_id") or ""
            if rid.startswith("pool:"):
                pm = parse_pool_id(rid)
                outs = set((pm or {}).get("out") or [])
                if not any(r["id"] in user_ids and r["outputs"].keys() & outs for r in RECIPES):
                    if outs or d.get("name") not in pool_eaters:
                        continue  # 读到了产物且都不吃它 / 这种池子没有吃它的配方
            elif rid not in user_ids:
                continue
            st = d.get("status") or "?"
            if st in ("空闲中", "未激活") and d.get("env") == "无环境":
                st = "无环境"  # 空闲是因为散布机没气（面板顶部「无环境」），不算因缺这种流体而停
            counts[st] = counts.get(st, 0) + 1
            if st in ("空闲中", "未激活"):
                idle.append(f"{d.get('name')} ({d.get('world_x')}, {d.get('world_y')}) @{area_name}")
    return counts, idle


def group_source_roots(reg: Region, verdicts: list[Verdict], roots: dict[str, tuple[str, str, str]]) -> list[Verdict]:
    """连锁归根：源头 S 已判定供不上（sources）或产量低于理论（diagnose）时，
    把「流体 S 供给可能不足」以及原料链经过 S 的待确认条目收拢成一条。roots：显示名 →（等级, 说明, 建议）。
    从 verdicts 里移除被收拢的条目，返回新的合并条目。"""
    def upstream(label: str, seen: frozenset = frozenset()) -> set[str]:
        """物品的上游原料（显示名），沿本地区在用的配方递归。"""
        i = reg.by_label.get(label)
        if i is None or label in seen:
            return set()
        out = set()
        for x in reg.inputs_of(i):
            out |= {x.label} | upstream(x.label, seen | {label})
        return out

    def passes(v: Verdict, label: str, s_dropped: bool) -> bool:
        if f"流体 {label}" in v.reason or any(label in s for s in v.chain):
            return True
        # 「流体 X 供给可能不足」里的 X 是由 S 做出来的（如芽针溶液 ← 清水）：原料链经过 S
        short = re.findall(r"流体 (\S+?) 供给可能不足", v.reason)
        # 「产耗同时腰斩」的产物，上游经过 S、且 S 自己也比蓝图基准明显低（< 90%）时，才算 S 供不上的连锁结果
        # （如 息壤 ← 清水、碳块 ← 芽针 ← 清水）。几乎所有产物都追得到矿和水，S 只差一点时解释不了腰斩
        if v.key.startswith("halved:") and s_dropped and halved_via(v.key.removeprefix("halved:"), label) and v.device.status != "阻塞":
            return True  # 阻塞的设备是出口堵了，不是缺 S（2026-11-07 三台阻塞的种植机被并进清水根因）
        return any(label in upstream(x) for x in short)

    def halved_via(p_label: str, s_label: str) -> bool:
        """P 的腰斩只在 S 是 P 的直接原料时才算 S 的连锁结果（息壤 ← 清水）。
        隔一层的（分离芯 ← 息壤 ← 清水）和输出口断了分不开，留给「产耗同时腰斩」那条待确认。"""
        p = reg.by_label.get(p_label)
        return p is not None and any(x.label == s_label for x in reg.inputs_of(p))

    out = []
    for label, (level, text, advice, kind) in roots.items():
        s, ref = reg.by_label.get(label), None
        if s is not None:
            ref = ref_item(reg, s.id)
        s_dropped = bool(s and ref and ref.cur_prod > 0 and s.cur_prod < ref.cur_prod * ROOT_DROP_RATIO)
        hit = [v for v in verdicts if not v.title and bucket(v.level) == "待确认" and passes(v, label, s_dropped)]
        if not hit:
            continue
        for v in hit:
            verdicts.remove(v)
        members = [v.device for v in hit]
        where = "、".join(f"{d.name} {d.where}" for d in members)
        if kind == "供给不足（巡检）":
            # 只有巡检证据的流体根因：≥2 台设备因缺它空闲 → 问题；1 台 → 待确认
            level = "问题" if len(hit) >= 2 else "待确认"
            text = f"{text}；巡检里 {len(hit)} 台设备因缺 {label} 停着"
        out.append(
            Verdict(
                level,
                members[0],
                f"{text}；受影响设备：{where}",
                advice,
                title=f"根因：{label} {kind}",
                members=members,
                key=f"root:{label}",
            )
        )
    return out


def source_roots(reg: Region, short: list[dict] | None) -> dict[str, tuple[str, str, str, str]]:
    """源头根因：显示名 →（等级, 说明, 建议, 标题后缀）。sources 判「来源供不上 / 缺设备 / 库存在降」的 → 问题；
    只有 diagnose 判源头流体低于理论的 → 待确认。"""
    roots: dict[str, tuple[str, str, str, str]] = {}
    for f in short or []:
        if f["region"] == reg.name and f.get("item"):
            roots[f["item"]] = ("问题", f"{f['item']} {f['kind']}：{f['text']}", f["advice"], f"{f['kind']}（见 sources）")
    for i in reg.items.values():
        # 清水这类源头流体另有配方产出（提纯机 → 清水），但主要来源还是泵：也按源头看
        if i.label in roots or not i.fluid or (reg.producers.get(i.id) and i.label not in SOURCE_NAMES):
            continue
        if reg.under(i) and not reg.excuse(i):
            # 简报里流体的理论值含野外/闲置的泵，当前值又由下游用量决定（用户 2026-11-07），单看简报只能算线索；
            # 等级由巡检证据定：group_source_roots 里 ≥2 台设备因缺它空闲才升为问题
            roots[i.label] = (
                "待确认",
                f"源头 {i.label} 供给不足（简报 当前 {i.cur_prod}/理论 {i.theo_prod}，理论含野外/闲置的泵，仅作参考）",
                f"去俯瞰查 {i.label} 的泵、暗管、准入口",
                "供给不足（巡检）",
            )
    return roots


CUR_KEEP = 0.95  # 实际消耗不低于参考实际消耗的这个比例：理论消耗下降只是按设计来回跳，不算消费者被断开


def theo_drops(reg: Region) -> list[tuple[Item, int]]:
    """理论消耗比蓝图参考下降的物品：（物品, 参考理论消耗）。
    下降 > max(5, 参考 × 5%) 算；气体下降是 6 的倍数时也算（一个 6 的准入口被关，惰气 150→144 只差 6）。"""
    out = []
    normal = set(bp.normal_notes(reg.blueprint.playbook, reg.name))
    flip = {x["item"] for x in reg.blueprint.playbook.get("theo_flip", [])}
    for i in reg.items.values():
        if i.label in normal:
            continue  # 错题本里明说的设计改动（如 #5 废水处理器换成暗管 → 污水理论消耗比参考低）
        if i.label in flip:
            continue  # 理论消耗按设计来回跳（应龙关液气转化机交替产酸气/息壤气 → 沉积酸 60/30/0），2026-11-08
        ref = ref_item(reg, i.id)
        if ref is None or ref.theo_cons <= 0:
            continue
        if i.theo_prod == 0 and ref.theo_prod == 0 and ref.cur_cons == 0 and i.cur_cons == 0:
            # 没人生产、参考里也没在流动的物品（青叶莓：只有一台设备偶尔从库存里拿 6/分），
            # 它的理论消耗在 6 和 0 之间来回跳只是那台设备有没有在算，不是消费者被断开
            continue
        drop = ref.theo_cons - i.theo_cons
        gas6 = i.id.startswith("item_gas_") and drop >= GATE_UNIT and drop % GATE_UNIT == 0
        if drop > max(DROP_MIN, ref.theo_cons * DROP_RATIO) or gas6:
            if not gas6 and ref.cur_cons > 0 and i.cur_cons >= ref.cur_cons * CUR_KEEP:
                # 实际消耗没少（和参考一样多）：理论值只是在按设计来回跳。武陵城池子按设计跑 2.5 条「蓝铁粉末 + 壤晶废液 → 壤晶」，
                # 蓝铁粉末 理论消耗 60↔90、壤晶废液 120↔180，实际一直 75 / 150（11-08、11-09 正常简报都这样；
                # 2026-11-10 接回水后两次简报 60 / 120 被报成「消费者被断开」）。真断开时实际消耗跟着掉（断水时 0 / 0）
                continue
            cons = reg.consumers.get(i.id, [])
            if cons and all(
                any(o in reg.items and (reg.full(reg.items[o]) or reg.blocked_path(o)) for o in r["outputs"]) for r in cons
            ):
                # 吃它的配方个个都有产物爆仓/回堵（壤晶 满 → 做壤晶的反应池停、切别的配方，壤晶废液 理论消耗 180↔120 来回跳）：
                # 下降是消费者按设计停工，不是被断开。2026-11-07 exe 全扫
                continue
            out.append((i, ref.theo_cons))
    return out


def attach_theo_drops(
    reg: Region, devices: list[Device], verdicts: list[Verdict], skip: dict[str, str] | None = None
) -> tuple[list[str], set[str]]:
    """理论消耗下降 = 有消费者被断开。能定位到本分区的就地报问题（改 verdicts）。
    返回（定位不到的说明, 本分区定位到的物品显示名）。skip：别的分区已经定位到的物品 → 分区名，这里不再报。
    - 已有同一物品的「这条线断了」条目：把下降并进去，不另报；
    - 吃它的设备种类正是某条「这条线断了」下游或「固体送不到」那批设备：作为连带并进去；
    - 巡检里有原料含这个物品的空闲设备：合并成一条问题，这些设备原来各自的待确认/问题条目收进来。"""
    grouped = {id(d) for v in verdicts if v.title for d in v.members}
    unlocated: list[str] = []
    located: set[str] = set()
    for x, ref in theo_drops(reg):
        if skip and x.label in skip:
            continue  # 已在别的分区并进了相关条目
        drop = ref - x.theo_cons
        env = next((v for v in verdicts if v.key == f"env:{x.label}"), None)
        if env is not None and bucket(env.level) == "问题":
            located.add(x.label)  # 面板确认了「无环境」：已经归到「缺环境」那条
            continue
        text = (
            f"{x.label} 的理论消耗从 {ref} 降到 {x.theo_cons}：有用 {x.label} 的设备没接上"
            f"（重新放置的设备没设配方/出货口（{RESET_HINT}）/ 传送带断了 / 准入口限速被改成 0 / 取货口物品被改错）"
        )
        if x.id.startswith("item_gas_") and drop % GATE_UNIT == 0:
            text += (
                f"；差 {drop} = {drop // GATE_UNIT} 个给气体散布机/固气转化机的准入口被关掉，"
                "受影响的是需要该环境的设备（天有洪炉、提纯机）"
            )
            if env is not None:
                text += f"；本分区可能缺环境的设备：{'、'.join(f'{d.name} {d.where}' for d in env.members)}（见待确认）"
        line = next((v for v in verdicts if v.key == f"line:{x.label}"), None)
        if line:
            if "理论消耗从蓝图" not in line.reason:
                line.reason += f"；{text}"
            located.add(x.label)
            continue
        # 连锁：吃 X 的设备种类正是某条已报的「这条线断了」下游设备（反应池断开 → 污水、液化息壤、壤晶废液也跟着降），
        # 或「固体送不到」的那批设备（固气转化机没有赤铜块 → 不再计息壤气供给）→ 并进那条，不另报
        users = {r["machine_name"] for r in RECIPES if x.id in r["inputs"]}
        cascade = next(
            (
                v
                for v in verdicts
                # 「送不到」只收流体（设备没原料时不再抽的气/液）；固体下降仍归「这条线断了」
                if (v.key.startswith("line:") or (v.key.startswith("starved:") and x.fluid))
                and any(d.name in users for d in (v.members or [v.partner]) if d)
            ),
            None,
        )
        if cascade:
            if cascade.key.startswith("starved:"):
                names = "、".join(sorted({d.name for d in cascade.members if d.name in users}))
                cascade.reason += f"；连带：{x.label} 理论消耗 {ref}→{x.theo_cons}（{names}没有原料时不再计 {x.label} 供给）"
            else:
                cascade.reason += f"；连带：{x.label} 理论消耗 {ref}→{x.theo_cons}"
            located.add(x.label)
            continue
        affected = [
            d
            for d in devices
            if id(d) not in grouped
            and not d.off
            and d.kind == "machine"
            and effective_status(d) == "空闲中"
            and x.id in recipe_inputs(reg, d)
        ]
        if not affected:
            unlocated.append(text)
            continue
        ids = {id(d) for d in affected}
        for v in [v for v in verdicts if not v.title and id(v.device) in ids and bucket(v.level) != "正常"]:
            verdicts.remove(v)
        affected = [d for d in affected if not any(v.device is d for v in verdicts)]  # 判为正常的不收
        if not affected:
            unlocated.append(text)
            continue
        verdicts.append(
            Verdict(
                "问题",
                affected[0],
                f"{text}；巡检里吃 {x.label} 的空闲设备：{'、'.join(f'{d.name} {d.where}' for d in affected)}",
                f"去俯瞰查这些设备：是否重新放置过、没设配方/出货口（{RESET_HINT}）；"
                "前面的传送带/管道、准入口限速（是否被改成 0）、取货口设的物品",
                title=f"{x.label} 理论消耗 {ref}→{x.theo_cons}：有消费者被断开",
                members=affected,
                key=f"drop:{x.label}",
            )
        )
        located.add(x.label)
    return unlocated, located


REGION_STARVED_RATIO = 0.75  # 地区级提示：固体库存充足、生产满速，当前消耗 < 理论 × 这个比例


def region_starved_hints(reg: Region, scanned_areas: set[str] | None = None, devices: list[Device] | None = None) -> list[Verdict]:
    """只看简报就能给的提示：固体 X 库存充足、生产者满速在产，消耗却明显低于理论 → 有吃 X 的设备没吃到。
    不扫那个分区看不到是哪台（2026-11-04 F：应龙关准入口把赤铜块改成砂叶，只扫武陵城的报告里什么都没有，
    但简报里赤铜块 消耗 510 < 理论 810、下游气态赤铜/灼铜块/灼铜零件都掉到 83%）。
    消费者里有备路/溢流位的（它们本来就常空）会让理论消耗偏高，所以只给待确认，并列出消费者种类和下游掉速。"""
    out = []
    tier_products = {r["product"] for r in reg.blueprint.tier_rules} | baseline_products(reg)
    for x in reg.items.values():
        if x.fluid or x.theo_cons <= 0 or x.theo_prod <= 0:
            continue
        if not reg.plenty(x) or x.cur_prod < x.theo_prod * FLOW_RATIO or x.cur_cons >= x.theo_cons * REGION_STARVED_RATIO:
            continue
        users = [r for r in RECIPES if x.id in r["inputs"] and any(in_use(reg, o) for o in r["outputs"])]
        if not users:
            continue
        limited = set(reg.blueprint.design_limits) | tier_products  # design_limits 是 产物名 → 说明 的字典
        if x.label in limited:
            continue  # 这种物品本身就是蓝图里用准入口限量供给的（重息壤限 6/分），消耗低于理论是设计
        if all(any(label_of(reg, o) in limited for o in r["outputs"]) for r in users):
            continue  # 吃它的全是备路/溢流/限速设备（壤晶 → 中容电池封装机），本来就吃得少
        kinds = {}
        for r in users:
            outs = [label_of(reg, o) for o in r["outputs"]]
            kinds.setdefault(r["machine_name"], set()).update(outs)
        # 下游：吃 X 的配方的产物，以及再下一级，当前产量 < 理论 90% 的
        # 下游只看一级：吃 X 的配方的直接产物里，不是备路/限速、当前产量 < 理论 90% 的
        down = []
        for o in dict.fromkeys(o for r in users for o in r["outputs"]):
            it = reg.items.get(o)
            if it is not None and it.label not in limited and it.theo_prod > 0 and it.cur_prod < it.theo_prod * 0.9:
                down.append(f"{it.label} {it.cur_prod}/{it.theo_prod}")
        design = [m for m, outs in kinds.items() if outs & tier_products]
        # 巡检里这种设备有「间歇空闲」的（只在一遍里空闲、或点开确认时标记已不在）：供料时断时续，正是准入口限速/改错的样子
        # 只挂「吃 X 做的不是备路/溢流产物」的设备种类：配件机吃赤铜块只做赤铜零件（备路），它间歇空闲是设计
        real_kinds = {m for m, outs in kinds.items() if not outs <= limited}
        user_ids = {r["id"] for r in users}
        user_outs = {o for r in users for o in r["outputs"]}

        def eats_x(d: Device) -> bool:
            # 配方已知的设备要真的吃 X 才算：固气转化机既有吃赤铜块的也有吃气态灼铜的，只按设备种类挂会把后者算进来
            if d.recipe == NO_RECIPE:
                return False  # 没有在跑的配方：什么都不吃（2026-11-06 固气转化机 (1043,984) 被挂成赤铜块的间歇消费者）
            if d.recipe_id:
                return d.recipe_id in user_ids
            if d.output:
                return bool(set(d.output.split("|")) & user_outs)
            return bool(set(d.machines) & real_kinds)

        flicker = [
            d for d in (devices or [])
            if d.passes < 2 and d.badge == "空闲中" and set(d.machines) & real_kinds and eats_x(d) and not overflow_product(reg, d)
        ]
        text = (
            f"{x.label} 的消费者有一部分没吃到：库存 {x.stock} 充足、生产 {x.cur_prod}/{x.theo_prod} 满速，"
            f"当前消耗 {x.cur_cons} 只有理论 {x.theo_cons} 的 {x.cur_cons * 100 // x.theo_cons}%；"
            f"吃它的设备：{'、'.join(f'{m}（{"/".join(sorted(o))}）' for m, o in kinds.items())}"
        )
        if design:
            text += f"；其中 {'、'.join(design)} 做的是备路/溢流产物，本来就常空，差额可能有一部分是设计内"
        if down:
            text += "；下游连带掉速：" + "、".join(down)
        if flicker:
            text += "；巡检里这些设备间歇空闲（供料时断时续）：" + "、".join(f"{d.name} {d.where}" for d in flicker)
        out.append(
            Verdict(
                "待确认",
                flicker[0] if flicker else Device(UNLABELED, "?", "?", 0, 0, "", None, None),
                text,
                "先查这些设备前面的准入口：物品是否被改错、限速是否被调小；再看取货口、传送带",
                title=f"{x.label} 消耗远低于理论：{len(flicker)} 台消费设备间歇空闲" if flicker else f"{x.label} 消耗远低于理论：可能有消费者没吃到（简报级提示）",
                members=flicker,
                key=f"rstarved:{x.label}",
            )
        )
    return out


def judge_foreign(reg: Region, d: Device) -> Verdict:
    """面板原料格上有「!」、格里堆着配方外的物品：前面的准入口（或分流器）物品设错了，原料进不来。
    直接证据，不看库存/产量（2026-11-05 应龙关 F：准入口赤铜块改成砂叶，固气转化机原料格 50 砂叶、空闲中）。"""
    r = recipe_of(reg, d)
    need = "、".join(label_of(reg, i) for i in r["inputs"]) if r else "配方原料"
    return Verdict(
        "问题", d,
        f"面板原料格有「!」，格里堆着 {d.foreign}，不是配方原料（{d.recipe}）；原料进不来，设备{d.status}",
        f"查它前面的准入口/分流器：物品设成了 {d.foreign}，改回 {need}",
        key=f"foreign:{d.foreign}",
    )


def judge_off(reg: Region, d: Device) -> Verdict:
    """设备被关闭（手动关掉或断电）：报问题，写清少了哪个产物的产量。"""
    if d.name == UNLABELED:
        return Verdict("问题", d, f"{d.where} 有一台设备被关闭（没认出是哪种设备）", "进俯瞰到这个坐标点开看，把开关打开")
    if d.kind == "logistics":  # 快速模式按关闭标记旁的标签猜成了物流件，多半猜错
        return Verdict(
            "问题",
            d,
            f"{d.where} 有一台设备被关闭（标签旁是 {'、'.join(d.candidates)}，可能猜错了）",
            "进俯瞰到这个坐标点开看，把开关打开",
        )
    advice = "进俯瞰点开这台设备，把开关打开"
    if d.outputs:
        made = " 或 ".join(label_of(reg, o) for o in d.outputs)
        return Verdict("问题", d, f"{d.name} {d.where} 被关闭（手动关掉或断电），产物 {made} 少了这台的产量", advice)
    names = set(d.machines) or {d.name}
    outs = list(dict.fromkeys(o for r in RECIPES if r["machine_name"] in names for o in r["outputs"]))
    active = [o for o in outs if in_use(reg, o)] or outs  # 优先列本地区在用的产物
    maybe = "、".join(label_of(reg, o) for o in active) or "（这种设备没有配方）"
    return Verdict("问题", d, f"{d.name} {d.where} 被关闭（手动关掉或断电），可能影响：{maybe}", advice)


def overflow_product(reg: Region, d: Device) -> str | None:
    """设备的产物登记为「溢流位产物」（tier_rules.overflow_products：汇流器/分流后段，主路有料就轮不到它）时返回产物名。"""
    for o in d.outputs:
        if label_of(reg, o) in reg.blueprint.overflow_products:
            return label_of(reg, o)
    return None


def judge_pool(reg: Region, d: Device) -> Verdict | None:
    """多配方反应池（recipe_id "pool:{json}"，带转盘读数）空闲时：某条在用配方的原料不在池里（没这一格或计数 0），
    而仓库有库存 → 问题（原料没送进池，查它的准入口/传送带/取货口）。2026-11-06 G 故障：用户关掉应龙关扩容反应池的息壤进料口，
    转盘只剩 沉积酸(50)/清水(50)，液化息壤 格 0，面板空闲；简报层面液化息壤只从 120 掉到 111（别区也在产），单看简报抓不到。
    备路原料（液化重息壤 ← 重息壤，重息壤没爆仓）不算缺。判不出返回 None，走原来的多候选逻辑。"""
    info = parse_pool_id(d.recipe_id)
    if info is None or info.get("wheel") is None or d.status != "空闲中":
        return None
    present: set[str] = set()
    for item, count, is_input in info["wheel"]:
        if item and (count or 0) > 0:
            present |= set(item.split("|"))
    outs = info.get("out") or []
    missing_plenty: list[tuple[str, str]] = []
    for r in RECIPES:
        if r["machine_name"] not in POOL_NAMES or not set(r["outputs"]) & set(outs):
            continue
        out_label = label_of(reg, next(iter(r["outputs"])))
        if tier_missing(reg, out_label):
            continue  # 这条是备路配方、备路没触发：原料不来是设计
        for iid in r["inputs"]:
            if iid in present:
                continue
            it = reg.items.get(iid)
            if it is not None and not it.fluid and reg.plenty(it):
                missing_plenty.append((label_of(reg, iid), out_label))
    if not missing_plenty:
        return None
    names = "、".join(dict.fromkeys(n for n, _ in missing_plenty))
    have = "、".join(
        f"{'|'.join(label_of(reg, x) for x in (i or '?').split('|')) if i else '?'}({c})" for i, c, inp in info["wheel"] if inp
    ) or "没有任何原料"
    return Verdict(
        "问题", d,
        f"池内没有原料 {names}（转盘里的原料只有 {have}），仓库却有库存："
        + "；".join(reg.fmt(reg.by_label[n]) for n, _ in dict.fromkeys(missing_plenty) if n in reg.by_label)
        + f"；做 {'、'.join(dict.fromkeys(o for _, o in missing_plenty))} 的配方停了，面板空闲中",
        f"查 {names} 进这台池子的路：准入口（物品/开关/限速）、传送带、取货口",
        key=f"pool_starved:{names}",
    )


# ---------------------------------------------------------------- 两种植物轮换（任务计划第 13 项）
PLANTERS = ("种植机", "采种机")
PARTNER_R = 140  # 远景坐标：5×5 的种植机/采种机并排时中心相距约 100，搭档在这个范围内找最近的另一种机器


def plant_of(reg: Region, d: Device) -> str | None:
    """种植机种的、采种机采的是哪种植物（显示名）。认不出配方返回 None。"""
    r = recipe_of(reg, d)
    if r is None or d.name not in PLANTERS:
        return None
    ids = r["outputs"] if d.name == "种植机" else r["inputs"]
    names = [label_of(reg, i) for i in ids if not i.startswith("item_liquid")]
    names = [n.removesuffix("种子") for n in names]
    return names[0] if names else None


def plant_rotation(reg: Region, d: Device, devices: list[Device] | None) -> str | None:
    """作者 2026-11-09：一对种植机 / 采种机可以在两种植物之间轮换，某一刻两台可能错开（种植机在种锦草、采种机在采芽针）。
    这台空闲/阻塞、它的植物在登记过的轮换对里、旁边最近的搭档这次读到正在做轮换对里的另一种 → 设计内。
    没读到搭档、或搭档做的不是另一种：不改判（可能真是缺料/堵）。"""
    pairs = reg.blueprint.rotating_plants
    if not pairs or d.name not in PLANTERS or effective_status(d) not in ("空闲中", "阻塞") or not devices:
        return None
    mine = plant_of(reg, d)
    if mine is None:
        return None
    for pr in pairs:
        if mine not in pr["plants"] or (pr.get("area") and getattr(reg, "area", None) and pr["area"] != reg.area):
            continue
        other = pr["plants"][1] if pr["plants"][0] == mine else pr["plants"][0]
        cands = [e for e in devices if e is not d and e.name in PLANTERS and e.name != d.name and e.x is not None and d.x is not None
                 and (e.x - d.x) ** 2 + (e.y - d.y) ** 2 <= PARTNER_R ** 2]
        if not cands:
            return None
        e = min(cands, key=lambda e: (e.x - d.x) ** 2 + (e.y - d.y) ** 2)
        if plant_of(reg, e) == other:
            st = effective_status(d)
            why = "等" + (f"{mine}种子" if d.name == "种植机" else mine) if st == "空闲中" else f"产的{mine}没人收"
            return (f"两种植物轮换（{mine} / {other}{'：' + pr['note'] if pr.get('note') else ''}）：旁边的{e.name} {e.where} 这会儿在做{other}，"
                    f"这台{why}是两台错开的间隙，设计内")
    return None


_PLANT_HISTORY: dict = {}


def plant_history(area: str | None) -> dict:
    """历史深查面板（不是快速模式猜的）读到的种植机/采种机产物：{(设备名, x, y): {植物…}}，同一台按 15 远景 px 内归并。同一进程缓存。"""
    key = area or ""
    if key in _PLANT_HISTORY:
        return _PLANT_HISTORY[key]
    groups: list[tuple[str, int, int, set]] = []
    files = sorted(CONTEXT_PATROLS, key=lambda f: f.name) if CONTEXT_PATROLS is not None else sorted(DEBUG.glob("patrol_*.json"))
    for f in files:  # 回归测试指定了巡检上下文时只看上下文里的（不读 debug）
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict) or (area and raw.get("area") != area):
            continue
        for r in raw.get("devices", []):
            if r.get("name") not in PLANTERS or r.get("guessed") or r.get("near") or not r.get("output") or "|" in r["output"]:
                continue
            x, y = r.get("world_x"), r.get("world_y")
            if x is None or y is None:
                continue
            nm = ID_NAME.get(r["output"], r["output"]).removesuffix("种子")
            g = next((g for g in groups if g[0] == r["name"] and abs(g[1] - x) <= 15 and abs(g[2] - y) <= 15), None)
            if g is None:
                groups.append((r["name"], x, y, {nm}))
            else:
                g[3].add(nm)
    _PLANT_HISTORY[key] = {(g[0], g[1], g[2]): g[3] for g in groups}
    return _PLANT_HISTORY[key]


def plant_history_hint(reg: Region, d: Device) -> str | None:
    """这台种植机/采种机以前的面板读到过两种以上植物、又没登记成轮换：提示可能是两种植物轮换（只加说明，不改判）。"""
    if d.name not in PLANTERS or d.x is None:
        return None
    area = getattr(reg, "area", None)
    if not area:
        return None  # 不知道分区：各分区坐标会撞，不查
    seen = next((v for (n, x, y), v in plant_history(area).items() if n == d.name and abs(x - d.x) <= 15 and abs(y - d.y) <= 15), set())
    if len(seen) < 2 or any(set(pr["plants"]) <= seen for pr in reg.blueprint.rotating_plants):
        return None
    return f"以前点开面板读到过 {'、'.join(sorted(seen))} 几种植物：可能是两种植物轮换（设计），确认后在蓝图页「两种植物轮换」里登记，以后就不报"


def judge(reg: Region, d: Device, devices: list[Device]) -> Verdict:
    """judge_core 的结果，加上两种植物轮换（任务计划第 13 项）。"""
    if not d.off and not d.foreign:
        rot = plant_rotation(reg, d, devices)
        if rot:
            return Verdict("设计内", d, rot)
    v = judge_core(reg, d, devices)
    if bucket(v.level) != "正常":
        hint = plant_history_hint(reg, d)
        if hint:
            v.reason = f"{v.reason}（{hint}）"
    return v


def judge_core(reg: Region, d: Device, devices: list[Device]) -> Verdict:
    """产物只有一个候选时按原规则判；有多个候选（快速模式认图标）时，任一候选能解释成正常就算正常。"""
    if d.off:  # 被关闭：一律报问题，产物爆仓也要报（用户关掉的设备就是要被发现）
        return judge_off(reg, d)
    if d.foreign:
        return judge_foreign(reg, d)
    pool = judge_pool(reg, d)
    if pool is not None:
        return pool
    if d.recipe == NO_RECIPE:
        # 面板没有在跑的配方（配方栏只有「配方一览」按钮）：2026-11-05 应龙关一趟就有 5–7 台这样的固气转化机/配件机
        # （未激活或空闲中），是蓝图里的备用位/从没进过料的，按闲置算正常；真被清了配方的靠用户看原因栏辨认
        # 2026-11-07 新蓝图实测（武陵城 5 台配件机、景玉谷 2 台封装机）：这是「自动配方」模式——面板只有「本设备可自动生产的配方一览」，
        # 来什么原料做什么；优先级/溢流传送带把料送来之前它就是空闲的，理论值也不计它。属设计内，不要让用户去「选配方」
        return Verdict("正常", d, "自动配方模式（面板只有「可自动生产的配方一览」，没有固定配方）：来料才动，空闲是在等上游溢流/优先级传送带送料，设计内")
    if d.status == "未激活":
        # 面板「未激活」：设备没启动过——没料进来。用户 2026-11-04：和溢流一个道理，上游多口暗管的汇流器口
        # 只在主路满了才把料分过来（管道汇流器和传送带特性相同）。登记为溢流位产物的按设计内，否则按空闲判。
        ov = overflow_product(reg, d)
        if ov:
            return Verdict("正常", d, f"面板「未激活」：{ov} 是溢流位产物（汇流器后段），主路没满就没料进来，设计内")
        d = replace(d, status="空闲中")
        v = judge_core(reg, d, devices)
        v.reason = "面板「未激活」（没启动过、没料进来），按空闲判：" + v.reason
        return v
    ov = overflow_product(reg, d)
    if ov and effective_status(d) == "空闲中":
        return Verdict("正常", d, f"{ov} 是溢流位产物（汇流器/分流后段），主路没满就没料进来，设计内")
    if d.passes < 2 and d.guessed:
        # 两遍扫描只有一遍看到这个标记：满负荷产线上的阻塞/空闲每个周期闪几秒，隔几分钟再看就不在了
        return Verdict("正常", d, f"标记只在两遍扫描中的一遍出现（连拍 {d.seen}），是瞬时的")
    src = judge_source_blocked(d, reg, devices)
    if src:
        return src
    outs = d.outputs
    if len(outs) <= 1 or d.status in ("生产中", "运行中"):
        return judge_one(reg, d, devices)
    names = " 或 ".join(label_of(reg, o) for o in outs)
    tried = []
    for k, o in enumerate(outs):
        # recipe_id 是第一个候选的配方；其他候选按「设备 + 产物」重新找配方
        one = replace(d, output=o, recipe_id=d.recipe_id if k == 0 else None)
        v = judge_one(reg, one, devices)
        if bucket(v.level) == "正常":
            return Verdict(v.level, d, f"产物可能是 {names}；按 {label_of(reg, o)} 解释：{v.reason}", v.advice, v.chain)
        tried.append(f"按 {label_of(reg, o)}：[{v.level}] {v.reason}")
    return Verdict(
        "待确认",
        d,
        f"产物可能是 {names}，都解释不了 —— " + " | ".join(tried),
        "对这个坐标跑深入模式，点开面板确认配方",
    )


def judge_one(reg: Region, d: Device, devices: list[Device]) -> Verdict:
    if d.status in ("生产中", "运行中"):
        return Verdict("正常", d, f"面板显示「{d.status}」，俯瞰里的「{d.badge}」标记只是闪烁")
    # 废水处理机这类没有配方的汇点：没有产物，空闲或状态没读出都是正常
    if d.name == "废水处理机" and d.status in ("?", "空闲中") and not any(r["machine_name"] == d.name for r in RECIPES):
        return Verdict("正常", d, "废水处理机没有产物，空闲表示没有多余污水，属正常", fallback=True)
    status = d.status if d.status != "?" else d.badge  # 面板状态没读出来时，按俯瞰标记判断
    if status == "阻塞":
        return judge_blocked(reg, d, devices)
    if status == "空闲中":
        return judge_idle(reg, d, devices)
    return Verdict("待确认", d, f"状态没认出（面板 {d.status}，标记 {d.badge}）", "进俯瞰点开这台设备看状态")


def bucket(level: str) -> str:
    """沿上游追到的 Root.level（设计内、闲置、提醒）放进哪一节：设计内/闲置算正常，提醒算待确认。"""
    if level in ORDER:
        return level
    return "正常" if level in ("设计内", "闲置") else "待确认"


# ---------------------------------------------------------------- 报告

def logistics_info(d: Device) -> str:
    """物流件的状态说明（只作信息，不算问题也不算待确认）。名字是几个候选时，有一个候选符合就用那条规则。"""
    s, names = d.status, set(d.candidates)
    multi = len(names) > 1
    if d.off:  # 只剩供电件会走到这里（关闭的物流件按问题报）
        text = f"供电件 {'|'.join(d.candidates)} 关闭，可能导致附近设备断电"
    elif names & PICKUP_PARTS and s == "空闲中":
        text = "取货口没有在出货（下游没在取），通常是下游爆仓或备路未触发"
    elif names == {"暗管入口"} and s == "空闲中":
        text = "暗管入口空闲：没有流体进入这根暗管（上游没产出、泵/准入口没开，或管子没接上）"
    elif "暗管出口" in names and s == "空闲中":
        text = ("若是暗管出口：" if multi else "") + "这根暗管当前没有流体通过（「送不到」的直接线索）"
    elif names & GATE_PARTS and s in ("空闲中", "阻塞"):
        text = f"准入口 {d.where} 状态「{s}」，可能是限速口"
    else:
        text = f"状态「{s}」（标记「{d.badge}」）"
    if multi:
        text = f"候选 {'、'.join(d.candidates)}：{text}"
    if d.flow:
        text = f"流速 {d.flow} 单位/秒；" + text
    return text + (NEAR_NOTE if d.near else GUESS_NOTE if d.guessed else "")


def capacity_check_lines(reg: Region) -> list[str]:
    """产能自检（用户错题本 #2/#6 的目标值，单位 /分钟）：当前产量 ≥ 目标 90% 算达标；库存爆仓按设计停等；否则低于目标并附排查链。"""
    pb = reg.blueprint.playbook
    targets = pb.get("targets") or []
    if not targets:
        return []
    hints = {h["product"]: h["text"] for h in pb.get("hints", [])}
    out = ["\n## 产能自检（错题本目标，/分钟）"]
    power = PATROL_META.get("power") or ""
    if power:
        pm = re.match(r"(\d+)/(\d+)", power)
        warn = ""
        if pm and int(pm.group(2)) > 0 and int(pm.group(1)) >= int(pm.group(2)) * 0.95:
            warn = "——接近上限，错题本 #3：电量超过 9800 → 自行在核心旁放置热能池"
        out.append(f"- 电量：用电/上限 {power}{warn}")
    for c in PATROL_META.get("checks") or []:
        exp = c.get("expect_flow")
        if c.get("found") is None:
            out.append(f"- ? 核查 {c['id']}：没点开（{c.get('error') or '未知'}）——{c.get('note', '')}")
            continue
        if c.get("expect_rate") is not None:  # 准入口：当前速度要跑满
            exp = c["expect_rate"]
            speed = c.get("speed") or ""
            try:
                ok = speed != "" and float(speed) >= float(exp) * 0.9
            except ValueError:
                ok = False
            sw = "" if c.get("limit_on") is None else ("（限速开）" if c.get("limit_on") else "（限速关！）")
            out.append(f"- {'✓' if ok else '✗'} 核查 {c['id']}：{c['found']} {c.get('status') or ''} 当前速度 {speed or '?'}/分，限速 {c.get('limit') or '?'}{sw}（期望跑满 {exp}）@{c.get('world')}" + ("" if ok else f"——{c.get('note', '')}"))
            continue
        flow = c.get("flow") or ""
        try:
            ok = exp is None or (flow != "" and float(flow) >= float(exp) * 0.9)
        except ValueError:
            ok = False
        line = f"- {'✓' if ok else '✗'} 核查 {c['id']}：{c['found']} {c.get('status') or ''} 流速 {flow or '?'}/秒（期望 {exp}/秒）@{c.get('world')}"
        if not ok:
            # 用户 2026-11-06：流速低先看下游——这根管送的流体若被下游爆仓回堵（重息壤 90000 → 重息壤气/息壤气 链），流速掉是设计内
            pb_check = next((x for x in reg.blueprint.playbook.get("checks", []) if x.get("id") == c.get("id")), {})
            fl = reg.by_label.get(c.get("fluid") or pb_check.get("fluid") or "")
            ex = reg.excuse(fl) if fl is not None else None
            if ex:
                line = line.replace("✗", "✓(设计内)", 1) + f"——{fl.label} {ex}，下游用量低所以流速低，不是接入不足"
            else:
                line += f"——{c.get('note', '')}"
        out.append(line)
    for t in targets:
        i = reg.by_label.get(t["product"])
        if i is None:
            out.append(f"- {t['product']}：简报里没有这一行")
            continue
        goal = t["per_min"]
        if i.cur_prod >= goal * 0.9:
            out.append(f"- ✓ {i.label}：当前 {i.cur_prod}/目标 {goal}")
        elif reg.full(i):
            out.append(f"- ✓ {i.label}：当前 {i.cur_prod}/目标 {goal}，库存 {i.stock} 已爆仓，按设计停等")
        else:
            line = f"- ✗ {i.label}：当前 {i.cur_prod}/目标 {goal}（理论 {i.theo_prod}，库存 {i.stock}）"
            if t.get("note"):
                line += f"（{t['note']}）"
            out.append(line)
            if i.label in hints:
                out.append(f"  - 错题本：{hints[i.label]}")
    return out


def unlabeled_lines(unlabeled: list[Device]) -> list[str]:
    """无标签的标记合并成一条待确认，按阻塞/空闲列坐标，给出深入模式命令。"""
    def where(status):
        return "、".join(d.where for d in unlabeled if d.status == status)

    parts = [f"{s[:2]} {where(s)}" for s in ("阻塞", "空闲中") if where(s)]
    others = [d for d in unlabeled if d.status not in ("阻塞", "空闲中")]
    if others:
        parts.append("其他 " + "、".join(d.where for d in others))
    at = ";".join(f"{d.x},{d.y}" for d in unlabeled)
    return [
        f"- **{len(unlabeled)} 个标记没认出设备**：{'；'.join(parts)}",
        "  - 依据：快速模式下标记旁边没找到设备标签",
        f"  - 建议：对这些坐标跑深入模式：python src\\patrol.py --deep --at {at}",
    ]


def report(
    reg: Region,
    devices: list[Device],
    brief_name: str,
    patrol_name: str,
    area: str | None = None,
    source_short: list[dict] | None = None,
    drops_elsewhere: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """source_short：sources.compare 里「来源供不上 / 缺设备」的结论，用来做连锁归根（没有就只用 diagnose 的判断）。"""
    # 「（闪烁）」：点之前再截图时标记已经消失，不列入报告，只计数
    flicker = sum(d.name == "（闪烁）" for d in devices)
    devices = [d for d in devices if d.name != "（闪烁）"]
    deep_checked = sum(d.deep for d in devices)
    fast = sum(d.guessed for d in devices)
    deep = len(devices) - fast
    # 物流件单独归到「信息」；供电类和产线无关，直接不列；无标签的合并成一条待确认
    # 供电件一般不列；但关闭的供电件可能让附近设备断电，放进「信息」
    power = sum(d.kind == "ignored" and not d.off for d in devices)
    parts = [d for d in devices if (d.kind == "logistics" and not d.off) or (d.kind == "ignored" and d.off)]
    # 被关闭的条目（无标签、标签猜成物流件的也算）已经是明确结论：一律和生产设备一起判，报问题
    # 两遍扫描只出现一遍的标记（快速模式）：瞬时，不进任何规则，直接记正常；无标签的也不列待确认
    # 入口堆着配方外物品的（d.foreign）是面板上的直接证据：标记间歇也要报，和关闭的一样不进分组
    transient = [d for d in devices if d.passes < 2 and not d.off and not d.foreign]  # 面板确认过名字的间歇设备也算瞬时，不进常规规则
    devices = [d for d in devices if not (d.passes < 2 and not d.off and not d.foreign)]
    unlabeled = [d for d in devices if d.kind == "unlabeled" and not d.off]
    devices = [d for d in devices if d.kind == "machine" or (d.kind in ("unlabeled", "logistics") and d.off)]
    idle_pipes = [d for d in parts if "暗管出口" in d.candidates and d.status == "空闲中"]
    pipe_hint = "；".join(
        f"巡检看到 {d.where} 处{'暗管出口' if len(d.candidates) == 1 else '暗管（可能是出口）'}空闲，可能就是这条管"
        for d in idle_pipes
    )
    # 上游阻塞 + 下游空闲 = 中间断了：配对的两台合并成一条问题，覆盖各自原来的判断（关闭的除外）
    # 关闭和源头设备阻塞优先（它们在 judge 里先判），不参与配对和流体合并
    first = {id(d) for d in devices if d.off or d.foreign or judge_source_blocked(d)}
    pairs = pair_broken(reg, [d for d in devices if id(d) not in first])
    paired = {id(x) for v in pairs for x in (v.device, v.partner, *v.members) if x}
    # 流体有余但消费者空闲：合并成一条（准入口限速或管道）
    # 固体原料有库存、消耗却远低于理论：先于「流体有余」判（快速模式常把产物猜成吃流体的那个）
    taken = first | paired
    # 缺环境（气体散布机没气）先于「固体送不到」「流体有余」「流体供给不足」：缺环境的设备原料槽是满的
    env = group_env_starved(reg, devices, taken)
    starved = group_starved_solid(reg, devices, taken)
    surplus = group_surplus_fluid(reg, devices, taken)
    grouped = paired | {id(d) for v in env + starved + surplus for d in v.members}
    verdicts = pairs + env + starved + surplus + [judge(reg, d, devices) for d in devices if id(d) not in grouped]
    verdicts += [
        Verdict("正常", d, f"标记只在两遍扫描中的一遍出现（连拍 {d.seen}），是瞬时的" + ("" if d.guessed else "；名字按面板"))
        for d in transient if d.kind == "machine"
    ]
    # 连锁归根：源头流体供不上时，「流体 S 供给可能不足」这类待确认收拢到一条根因下
    # 理论消耗比蓝图参考下降 = 有消费者被断开（放在根因归并之前）。能在本分区定位的（并进同物品的条目，
    # 或巡检里有吃它的空闲设备）报问题；定位不到的列在「简报：理论消耗下降」一节，不计入本分区问题数
    unlocated, located = attach_theo_drops(reg, devices, verdicts, drops_elsewhere)
    # 定位不到分区的下降是地区级的事实：只在主分区（areas.json 里 "main": true，如武陵城协议核心）计为问题一次，
    # 其他分区列为信息，避免 full_patrol 汇总时每个分区重复算
    main_area = next((a for a in reg.blueprint.areas.get(reg.name, []) if reg.blueprint.area_options(a).get("main")), None)
    if unlocated and area and area == main_area:
        for text in unlocated:
            label = text.split(" 的理论消耗")[0]
            verdicts.append(
                Verdict(
                    "问题",
                    Device(UNLABELED, "?", "?", 0, 0, "", None, None),
                    f"{text}；本次巡检没找到吃它的空闲设备（消费者可能在协议区域外，或是环境类设备）",
                    f"按物品找它的消费设备：是否重新放置过、没设配方/出货口（{RESET_HINT}）；"
                    "前面的准入口限速是否被改成 0、传送带/管道是否断了",
                    title=f"{label} 理论消耗下降：有消费者被断开（巡检没定位到设备）",
                    key=f"drop:{label}",
                )
            )
        unlocated = []
    elif unlocated and main_area:
        unlocated = [f"{t}（地区级，已在 {main_area} 的报告里计为问题）" for t in unlocated]
    foreign_inputs = {
        label_of(reg, i)
        for v in verdicts if v.key.startswith("foreign:") and (r := recipe_of(reg, v.device))
        for i in r["inputs"]
    }
    hints = region_starved_hints(reg, devices=transient)
    # 本分区有间歇空闲的消费设备时就在本分区报（带设备）；没有就只在主分区报一次简报级提示
    if area and (area == main_area or any(h.members for h in hints)):
        for h in hints:
            if area != main_area and not h.members:
                continue
            label = h.key.removeprefix("rstarved:")
            if any(v.key == f"starved:{label}" for v in verdicts) or (drops_elsewhere and label in drops_elsewhere):
                continue  # 本分区或别的分区已经定位到具体设备
            if label in foreign_inputs:
                continue  # 吃它的设备入口堆着别的东西，已经作为问题报了
            verdicts.append(h)
    fold_env_cascade(reg, verdicts)  # 缺环境的后果（息壤减产 → 壤晶链空闲 → 粉碎机阻塞）降为待确认
    verdicts += group_source_roots(reg, verdicts, source_roots(reg, source_short))
    fold_blocked_feeders(reg, verdicts)  # 下游因根因空闲 → 上游产物积压阻塞：并进根因，别说「传送带断了」
    # 来源供不上 / 缺设备 是地区级事实：本分区没有能归到它名下的设备时，在主分区仍单独报一条问题
    # （受影响设备可能被别的规则先拿走——如空闲的天有洪炉被「可能缺环境」收走——或根本在别的分区）
    # 2026-11-06 教训：景玉谷一根断开的清水暗管，简报里 清水 1124/2610 从 14:44 起每份都在，但整天只扫应龙关、
    # 应龙关没有用清水的设备，这条就被压掉了。现在不是主分区也列出来（待确认，注明地区级、主分区计为问题），单扫一区也能看到
    for f in source_short or []:
        if f.get("kind") not in ("来源供不上", "缺设备"):
            continue
        label = f["item"]
        if any(v.key == f"root:{label}" for v in verdicts):
            continue
        is_main = bool(area and area == main_area)
        down_reason, down_advice = downstream_summary(reg, label)
        counts, idle_devs = fluid_consumer_status(reg, label)
        # 本分区里已经有自己结论、且结论没提到这种流体的空闲设备（如池内缺蓝铁粉末的反应池），空闲另有原因，不算因缺它而停
        explained = {
            f"{v.device.name} {v.device.where} @{area}"
            for v in verdicts
            if v.device.name != UNLABELED and label not in v.reason and label not in (v.title or "")
        }
        idle_devs = [x for x in idle_devs if x not in explained]
        busy = counts.get("阻塞", 0) + counts.get("生产中", 0) + counts.get("运行中", 0)
        if f["kind"] == "来源供不上" and not idle_devs and busy >= 3:
            # 最近扫描里吃它的设备没有一台空闲、都在阻塞/生产：当前产量低于能力是下游用得少（爆仓回堵），不是源头断了
            # 2026-11-06 20:52：景玉谷暗管接回后清水仍 1142/2610，武陵城 13 台阻塞 4 台生产中 0 空闲 → 正常
            verdicts.append(
                Verdict(
                    "正常",
                    Device(UNLABELED, "?", "?", 0, 0, "", None, None),
                    f"{f['text']}；但最近扫描里吃它的设备 {busy} 台在阻塞/生产中，没有一台是因缺它而空闲的（{'、'.join(f'{k} {v}' for k, v in counts.items())}"
                    + ("；无环境 = 散布机没气，另算" if counts.get("无环境") else "")
                    + (f"；空闲的 {counts.get('空闲中', 0) + counts.get('未激活', 0)} 台本分区已另有结论" if counts.get("空闲中") or counts.get("未激活") else "")
                    + "）：用量由下游决定，不是源头断了"
                    + down_reason,
                    "",
                    title=f"{label} 当前低于能力：用量由下游决定（上次扫描无空闲消费设备）",
                    key=f"root:{label}",
                )
            )
            continue
        verdicts.append(
            Verdict(
                "问题" if is_main else "待确认",
                Device(UNLABELED, "?", "?", 0, 0, "", None, None),
                f"{f['text']}；本分区巡检没有能归到它名下的空闲设备"
                + ("" if is_main else f"（地区级事实，在主分区{main_area or ''}的报告里计为问题；受影响设备多半在别的分区）")
                + down_reason
                + (f"；最近扫描里因它空闲的设备：{'、'.join(idle_devs[:6])}" if idle_devs else ""),
                f.get("advice", "") + down_advice,
                title=f"根因：{label} {f['kind']}（见 sources）" + ("" if is_main else "（地区级）"),
                key=f"root:{label}",
            )
        )
    # 源头设备阻塞（如水泵）产的正是根因流体：并进根因条目作为可能的断点，不再单独一条
    for v in [v for v in verdicts if v.key.startswith("source:")]:
        root = next((r for r in verdicts if r.key == "root:" + v.key.removeprefix("source:")), None)
        if root is None:
            continue
        verdicts.remove(v)
        d = v.device
        root.reason += f"；可能的断点：{d.name} {d.where} 后面的暗管"
        root.members.insert(0, d)
        if root.device.name == UNLABELED:
            root.device = d  # 根因条目原本没有设备：断点设备就是它的代表设备（定位图、回归按它找）
        if root.level != "问题":  # 非主分区的地区级根因：源头设备就在本分区，升成问题（2026-11-06 景玉谷 水泵 → 清水）
            root.level = "问题"
            root.title = root.title.replace("（地区级）", "")
            root.reason = root.reason.split("（地区级事实")[0]
    for v in verdicts:
        if pipe_hint and bucket(v.level) == "待确认" and v.reason.startswith("流体"):
            v.advice = (v.advice + "；" if v.advice else "") + pipe_hint  # 流体供给待确认附上暗管线索
        if v.device.near:
            v.reason += NEAR_NOTE
        elif v.device.guessed:
            v.reason += GUESS_NOTE
        if v.device.deep:
            v.reason += DEEP_NOTE
        if v.device.off_overridden:
            v.reason += f"（俯瞰标记是关闭，面板显示{v.device.status}，按面板）"
    n_check = sum(bucket(v.level) == "待确认" for v in verdicts) + (1 if unlabeled else 0)
    counts = {s: sum(bucket(v.level) == s for v in verdicts) for s in ORDER}
    counts["待确认"] = n_check
    lines = [
        f"# 巡检 + 简报合并诊断：{reg.name}{f' / {area}' if area else ''}（仓库上限 {reg.cap}）",
        f"简报 {brief_name}，巡检 {patrol_name}，共 {len(devices)} 台生产设备，"
        f"闪烁 {flicker} 台已忽略，供电件 {power} 个已忽略",
        "　".join(f"{s} {counts[s]}" for s in ORDER)
        + f"　物流件 {len(parts)}　快速模式 {fast} 台 / 深入模式 {deep} 台（其中定点深查覆盖 {deep_checked} 台）",
    ]
    lines += capacity_check_lines(reg)
    hints = {h["product"]: h["text"] for h in reg.blueprint.playbook.get("hints", [])}
    for section in ORDER:
        group = [v for v in verdicts if bucket(v.level) == section]
        if section == "待确认" and unlabeled:
            lines.append("\n## 待确认")
            lines += unlabeled_lines(unlabeled)
        if not group:
            continue
        if not (section == "待确认" and unlabeled):
            lines.append(f"\n## {section}" + ("（含原因）" if section == "正常" else ""))
        for v in group:
            d = v.device
            tag = (f"[{v.level}] " if v.level != section else "") + ("[兜底] " if v.fallback else "")
            if v.title:
                lines.append(f"- {tag}**{v.title}**")
            elif v.partner:
                b = v.partner
                lines.append(
                    f"- {tag}**{d.name} {d.where} 阻塞 → {b.name} {b.where} 空闲**："
                    f"上游 {d.recipe or '配方：?'}；下游 {b.recipe or '配方：?'}"
                )
            else:
                lines.append(f"- {tag}**{d.name} {d.where}** 面板「{d.status}」/标记「{d.badge}」：{d.recipe}")
            lines.append(f"  - 依据：{v.reason}")
            if v.advice:
                lines.append(f"  - 建议：{v.advice}")
            if section == "待确认" or v.title:
                # 错题本：待确认条目和根因条目涉及的产物（标题或依据里提到的）有排查链就附上；
                # 设备级的「问题」已经有明确原因和建议（如池子断料、准入口改错），不再挂整条链
                for prod, text in hints.items():
                    if prod in (v.title or "") or f"源头 {prod}" in v.reason or f"原料 {prod}" in v.reason or f"产物 {prod}" in v.reason:
                        lines.append(f"  - 错题本：{text}")
            lines += [f"    {s}" for s in v.chain]

    info = [{"device": asdict(d), "text": logistics_info(d)} for d in parts]
    if info:
        lines.append("\n## 信息（物流件状态）")
        for d, x in zip(parts, info):
            lines.append(f"- **{d.name} {d.where}** 面板「{d.status}」/标记「{d.badge}」：{x['text']}")

    findings = reg.diagnose()
    # P2 的「提醒」（在耗库存、预计多久耗尽）：只靠简报就能看出的趋势，原样带出来
    reminders = [f for f in findings if f.level == "提醒"]
    if reminders:
        lines.append("\n## 提醒（简报）")
        for f in reminders:
            lines.append(f"- **{f.text}**")
            lines += [f"    {s}" for s in f.chain]
    if unlocated:
        lines.append("\n## 简报：理论消耗下降（本分区巡检没找到相关设备，可能在别的分区）")
        lines += [f"- **{text}**" for text in unlocated]

    # P2 待确认项：巡检点开的设备产物在影响列表里的，附上巡检结论
    lines.append("\n## P2 待确认项的核实结果")
    checks = []
    for f in findings:
        if f.level != "待确认":
            continue
        hits = [v for v in verdicts if any(label_of(reg, o) in f.affected for o in v.device.outputs)]
        checks.append({"finding": f.text, "affected": f.affected, "verified": [v.as_dict() for v in hits]})
        mark = "已核实" if hits else "本次巡检没覆盖"
        lines.append(f"- **{f.text}**（{mark}）")
        lines.append(f"  - P2 影响：{'、'.join(f.affected)}")
        # P2 的流体待确认项（疑似 X 供给不足、源头流体产量低）附上暗管出口线索
        fluid = "供给不足" in f.text or any("（流体" in s for s in f.chain[:1])
        if fluid and pipe_hint:
            lines.append(f"  - {pipe_hint}")
            checks[-1]["pipe_hint"] = pipe_hint
        for v in hits:
            d = v.device
            made = " 或 ".join(label_of(reg, o) for o in d.outputs)
            lines.append(f"  - 巡检：{d.name} {d.where} 产 {made} → {v.level}：{v.reason}")
    if not checks:
        lines.append("（P2 没有待确认项）")
    lines.append("")

    data = {
        "region": reg.name,
        "area": area,
        "cap": reg.cap,
        "brief": brief_name,
        "patrol": patrol_name,
        "verdicts": {s: [v.as_dict() for v in verdicts if bucket(v.level) == s] for s in ORDER},
        "logistics": info,
        "unlabeled": [asdict(d) for d in unlabeled],
        "mode": {"fast": fast, "deep": deep, "deep_checked": deep_checked},
        # 所有待确认条目涉及的坐标（含无标签），给 UI 和深入模式用
        "deep_check": [
            {"x": d.x, "y": d.y, "name": d.name, "status": d.status, "reason": v.reason}
            for v in verdicts
            if bucket(v.level) == "待确认"
            for d in (v.members or [v.device])
        ]
        + [{"x": d.x, "y": d.y, "name": d.name, "status": d.status, "reason": "没认出设备"} for d in unlabeled],
        "ignored": {"flicker": flicker, "power": power},
        "p2_checks": checks,
        "reminders": [{"text": f.text, "affected": f.affected, "chain": f.chain} for f in reminders],
        "theo_drops_unlocated": unlocated,
        # 本分区定位到的理论消耗下降（物品显示名）：full_patrol 传给之后的分区，主分区就不再按地区级重复计
        "theo_drops_located": sorted(located | {v.key.removeprefix("starved:") for v in verdicts if v.key.startswith("starved:")}),
    }
    return "\n".join(lines), data


def latest(pattern: str) -> Path:
    return max(DEBUG.glob(pattern), key=lambda p: p.stat().st_mtime)


def patrol_meta(path: Path) -> dict | None:
    """新格式巡检文件的 region / area / time / 是否含快速、深查条目；旧格式（列表）返回 None。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    devs = raw.get("devices", [])
    return {
        "region": raw.get("region"),
        "area": raw.get("area"),
        "time": raw.get("time") or path.stem.removeprefix("patrol_"),
        "fast": any(d.get("guessed") for d in devs),
        "deep": any(d.get("guessed") is False for d in devs),
    }


def latest_fast_patrol() -> Path:
    """最新一份快速模式的巡检（含 guessed 条目）；一份都没有时退回最新的任意巡检。"""
    files = sorted(DEBUG.glob("patrol_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return next((f for f in files if (m := patrol_meta(f)) and m["fast"]), files[0])


def deep_files_after(main: Path, region: str | None, area: str | None, folder: Path = DEBUG) -> list[Path]:
    """主文件之后生成的、同一分区（或同一地区且分区为空）的深查巡检，按时间从早到晚。默认在 debug 里找。"""
    m0 = patrol_meta(main)
    t0 = m0["time"] if m0 else main.stem.removeprefix("patrol_")
    out = []
    for f in Path(folder).glob("patrol_*.json"):
        m = patrol_meta(f)
        if f == main or not m or not m["deep"] or m["time"] <= t0:
            continue
        same = (area and m["area"] == area) or (not m["area"] and (m["region"] == region or not region))
        if same:
            out.append((m["time"], f))
    return [f for _, f in sorted(out)]


def merge_deep(devices: list[Device], deep_path: Path) -> int:
    """用深查结果（guessed 为 false 的条目）覆盖 25 像素内最近的快速条目；匹配不到的追加。返回覆盖/追加数。"""
    _, _, deep = load_patrol(deep_path)
    n = 0
    for dd in deep:
        if dd.guessed:
            continue
        near = [d for d in devices if abs(d.x - dd.x) <= MERGE_PX and abs(d.y - dd.y) <= MERGE_PX]
        if near:
            d = min(near, key=lambda d: abs(d.x - dd.x) + abs(d.y - dd.y))
            d.name, d.status, d.recipe, d.output, d.recipe_id, d.env = dd.name, dd.status, dd.recipe, dd.output, dd.recipe_id, dd.env
            d.foreign = dd.foreign
            d.guessed, d.deep = False, True  # 深查判为「（闪烁）」的，名字一换就按闪烁处理
        else:
            dd.deep = True
            devices.append(dd)
        n += 1
    return n


def run(
    brief_path: Path,
    patrol_path: Path,
    blueprint: "str | bp.Blueprint | None" = None,
    merge: bool = True,
    deep_dir: Path | None = None,
    drops_elsewhere: dict[str, str] | None = None,
) -> tuple[str, dict]:
    """合并一份简报和一份巡检，返回（报告文字, 数据）；不打印、不保存。地区在简报里没有数据时抛 ValueError。
    merge=True 时把这份巡检之后、同一分区的定点深查结果覆盖进来；深查文件默认在 debug 里找，
    deep_dir 给了就只在那个目录找（离线回归用，不碰 debug）。
    drops_elsewhere：别的分区已定位到的理论消耗下降（物品 → 分区），这里不再报（full_patrol 用来避免主分区重复计）。"""
    plan = blueprint if isinstance(blueprint, bp.Blueprint) else bp.load(blueprint)
    brief_path, patrol_path = Path(brief_path), Path(patrol_path)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    region, area, devices = load_patrol(patrol_path)
    patrol_name = patrol_path.name
    merged = []
    if merge:
        for f in deep_files_after(patrol_path, region, area, deep_dir or DEBUG):
            merged.append(f"{f.name}（{merge_deep(devices, f)} 条）")
        if merged:
            patrol_name += " + 定点深查 " + "、".join(merged)
    name = pick_region(brief, region, devices, plan, area)
    if not brief.get(name):
        raise ValueError(f"简报里没有「{name}」的数据，没法合并")
    # 巡检点开面板确认在跑的配方：可逆配方（固气转化机 重息壤气→重息壤 等）光看简报分不出方向，确认过的一律进图
    confirmed = {d.recipe_id for d in devices if d.recipe_id and not d.guessed and not d.recipe_id.startswith("pool:")}
    # 并上最近各分区扫描确认过的配方：本次没点开的设备（如 重息壤气→重息壤 的固气转化机）也算在跑，可逆配方方向才稳
    # （2026-11-06 一次扫描没点开它，重息壤气 在图里没有消费者，息壤气 的爆仓回堵链就断了，流速核查解释不了）
    for _area, (_f, devs) in latest_patrol_by_area().items():
        confirmed |= {r.get("recipe_id") for r in devs if r.get("recipe_id") and not r.get("guessed") and not str(r.get("recipe_id")).startswith("pool:")}
    reg = Region(name, brief[name], plan, confirmed=confirmed)
    reg.area = area  # 这次巡检的分区（两种植物轮换按分区登记、历史面板按分区查）
    import sources  # sources 依赖 diagnose，放这里避免启动时多读一遍参考简报

    try:
        short = [f for f in sources.compare(brief, plan) if f["kind"] in ("来源供不上", "缺设备", "库存在降")]
    except FileNotFoundError:  # 蓝图没有参考简报：只用 diagnose 的源头判断
        short = []
    text, data = report(reg, devices, brief_path.name, patrol_name, area, short, drops_elsewhere)
    data["source_short"] = short
    data["deep_merged"] = merged
    data["patrol_path"] = str(patrol_path)
    return text, data


def problem_sheet(data: dict, out: Path) -> list[str]:
    """给「问题」里的每台设备出定位图（src/tools/locate.py），拼成一张存到 out。返回没出图的设备。
    用户看不懂坐标，报告里的问题设备一律配图（2026-11-04）。"""
    sys.path.insert(0, str(Path(__file__).resolve().parent / "tools"))
    import locate  # noqa: E402

    patrol = json.loads(Path(data["patrol_path"]).read_text(encoding="utf-8"))
    shots = locate.shots_dir(patrol)
    seen, panels, missed = set(), [], []
    for v in data["verdicts"].get("问题", []):
        for d in [v["device"], *v.get("members", [])]:
            key = (d["x"], d["y"])
            if key in seen or d["name"] == UNLABELED or key == (0, 0):
                continue
            seen.add(key)
            im = locate.locate_one(shots, d["x"], d["y"], d["name"].split("|")[0], small=False,
                                   shot=d.get("shot") or None, sx=d.get("sx"), sy=d.get("sy")) if shots else None
            if im is None:
                missed.append(f"{d['name']} ({d['x']}, {d['y']})")
            else:
                panels.append(im)
    if panels:
        locate.save_sheet(panels, out)
    return missed


def save(text: str, data: dict) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    sheet = DEBUG / f"combined_{stamp}_where.png"
    try:
        missed = problem_sheet(data, sheet)
    except Exception as e:  # 出图失败不影响报告
        missed, sheet = [f"出图失败：{e}"], None
    if sheet is not None and sheet.exists():
        text += f"\n\n定位图：{sheet.name}" + (f"（没出图：{'、'.join(missed)}）" if missed else "")
    elif missed:
        text += "\n\n定位图：没出图（" + "、".join(missed) + "）"
    (DEBUG / f"combined_{stamp}.md").write_text(text, encoding="utf-8")
    (DEBUG / f"combined_{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return f"debug\\combined_{stamp}.md / .json"


def main(argv: list[str]) -> int:
    name = None  # --blueprint 名字，默认 1.6皇帝产线
    if "--blueprint" in argv:
        k = argv.index("--blueprint")
        name = argv[k + 1]
        argv = argv[:k] + argv[k + 2 :]
    merge = "--no-merge" not in argv  # 默认把之后的定点深查结果覆盖进来
    argv = [a for a in argv if a != "--no-merge"]
    brief_path = Path(argv[0]) if len(argv) > 0 else latest("brief_*.json")
    if len(argv) > 1:
        patrol_path = Path(argv[1])
    else:  # 合并时主文件取最新的快速模式巡检，深查结果再覆盖上去
        patrol_path = latest_fast_patrol() if merge else latest("patrol_*.json")
    try:
        text, data = run(brief_path, patrol_path, name, merge)
    except ValueError as e:
        print(e)
        return 1
    print(text)
    print(f"已保存：{save(text, data)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
