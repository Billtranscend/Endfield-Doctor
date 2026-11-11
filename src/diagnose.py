"""P2：诊断引擎。

输入 P1 读出的简报（debug/brief_*.json），按分级生产规则从产物往上游一步步推断：
  1. 产量低于理论的产物，先看能不能被解释：自己爆仓、下游爆仓回堵、备路未触发、蓝图设计限速 → 正常。
  2. 解释不了的，沿原料往上游追，直到遇到：
     - 有大量库存的原料：原料有，这条线却没吃饱 → 送不到或准入口限速（待确认，去俯瞰查进料口）；
     - 流体原料：固体原料都够，剩下的限制是流体 → 流体供给不足（待确认，去俯瞰查泵/暗管/准入口）；
     - 产量低于理论的源头（矿、泵、采集）→ 来源不足（待确认）；
     - 已按理论满产、但设计上就不够分的环节 → 设计内。
     同一个根因影响到的产物合并成一条。
  3. 库存在被持续消耗的产物，算出还能撑几分钟（提醒）。

注意：库存为 0 不代表缺料。很多中间产物走传送带直送下一台设备，不进仓库。
所以不单独拿「库存为 0」报问题，而是看产量和上下游。

用法：.venv\\Scripts\\python.exe src\\diagnose.py [--blueprint 1.6皇帝产线] [debug\\brief_xxx.json]
不给参数时用 debug 目录里最新的一份简报。备路规则、设计限速按蓝图档案读（默认 1.6皇帝产线）；
给了 --blueprint 时，报告最前面加一节「源头物品对照蓝图」（src/sources.py）。
"""

import json
import math
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import blueprint as bp

from paths import ROOT  # noqa: E402,F401
DATA = ROOT / "data"
DEBUG = ROOT / "debug"

NAME_IDS: dict[str, list[str]] = json.loads((DATA / "item_names_cn.json").read_text(encoding="utf-8"))
ID_NAME: dict[str, str] = json.loads((DATA / "item_id_names.json").read_text(encoding="utf-8"))
RECIPES: list[dict] = json.loads((DATA / "recipes.json").read_text(encoding="utf-8"))
# 备路规则、设计限速按所选蓝图读（src/blueprint.py），见 Region 的 blueprint 参数

FULL_RATIO = 0.995  # 库存 ≥ 上限 × 这个比例算爆仓（满仓时库存会在上限附近小幅波动）
PLENTY_RATIO = 0.01  # 库存 > 上限 × 这个比例算「原料充足」
BLOCKED = "输入端/准入口显示「阻塞」= 液体是满的、不缺，"  # 流体待确认的建议：先教用户分「满」和「缺」（2026-11-12 群友反馈）
UNDER_RATIO = 0.95  # 当前产量 < 理论 × 这个比例算低于理论
BASELINE_RATIO = 0.9  # 当前产量 ≥ 参考简报里的产量 × 这个比例 = 和基准一致（基准里就没跑满的备路/限速产物不报）
DRAIN_MIN = 2  # 消耗比生产多出这么多（/分钟）以上才算在耗库存
XFER_LOW = 0.01  # 跨地区传输送进来的物品，库存 ≤ 上限 × 这个比例算见底（传输停了 / 单批不够）

LEVELS = ("待确认", "提醒", "设计内", "闲置", "正常")

# 主要来自矿机、水泵、采集的物料：追到这里就停，不再顺着配方往上找
SOURCE_NAMES = {"源矿", "蓝铁矿", "赤铜矿", "紫晶矿", "清水", "沉积酸", "惰气"}


# ---------------------------------------------------------------- 物品

@dataclass
class Item:
    id: str
    label: str  # 显示名，灌装瓶带上灌装物，如「蓝铁瓶(液化息壤)」
    stock: int | None  # 流体没有库存
    cur_prod: int
    cur_cons: int
    theo_prod: int
    theo_cons: int
    theo_red: bool

    @property
    def fluid(self) -> bool:
        return self.stock is None

    @property
    def active(self) -> bool:
        return any((self.cur_prod, self.cur_cons, self.theo_prod, self.theo_cons))


RECIPE_ITEMS = {i for r in RECIPES for i in (*r["inputs"], *r["outputs"])}


def item_id(name: str, fluid: str | None) -> str | None:
    """简报里的（名称, 灌装物）→ 物品 id。同名的还有科技、配方书之类，只保留实物。"""
    ids = [
        i
        for i in NAME_IDS.get(name, [])
        if i.startswith("item_") and "formula" not in i and not i.startswith("item_factech_")
    ]
    if len(ids) > 1 and fluid is None:
        in_recipes = [i for i in ids if i in RECIPE_ITEMS and not i.startswith(("item_fbottle_", "item_gasjar_"))]
        if len(in_recipes) == 1:
            return in_recipes[0]
    if len(ids) == 1:
        return ids[0]
    if fluid is None:
        plain = [i for i in ids if not i.startswith(("item_fbottle_", "item_gasjar_"))]
        return plain[0] if len(plain) == 1 else None
    if fluid.startswith("item_liquid_"):
        suffix = "_" + fluid.removeprefix("item_liquid_").replace("plant_", "")
        cands = [i for i in ids if i.startswith("item_fbottle_") and i.endswith(suffix)]
    else:
        suffix = "_gas_" + fluid.removeprefix("item_gas_")
        cands = [i for i in ids if i.startswith("item_gasjar_") and i.endswith(suffix)]
    return cands[0] if len(cands) == 1 else None


def load_items(rows: list[dict]) -> tuple[dict[str, Item], list[str]]:
    items: dict[str, Item] = {}
    unknown = []
    for r in rows:
        iid = item_id(r["name"], r["fluid"])
        label = r["name"] + (f"({ID_NAME.get(r['fluid'], r['fluid'])})" if r["fluid"] else "")
        if iid is None:
            unknown.append(label)
            continue
        if iid in items:
            # 简报两页没对齐时同一行会出现两次；其中库存多了一位的那条是 OCR 错（见 brief_reader.digit_slip），取小的
            old, new = items[iid].stock, r.get("stock")
            if old is not None and new is not None and old != new and max(old, new) // 10 == min(old, new) > 0:
                items[iid].stock = min(old, new)
            continue
        items[iid] = Item(
            id=iid,
            label=label,
            stock=r["stock"],
            cur_prod=r["cur_prod"] or 0,
            cur_cons=r["cur_cons"] or 0,
            theo_prod=r["theo_prod"] or 0,
            theo_cons=r["theo_cons"] or 0,
            theo_red=r["theo_cons_red"],
        )
    return items, unknown


# ---------------------------------------------------------------- 诊断

@dataclass
class Root:
    key: str  # 同一个 key 的产物合并成一条
    level: str
    text: str
    advice: str = ""


@dataclass
class Finding:
    level: str
    text: str
    advice: str = ""
    affected: list[str] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)  # 一步步的推断（取影响链最长的那个产物）



class _CycleMarker:
    """blocked_path 的「只绕回环」标记：假值，和 None（没堵）区分开。"""
    def __bool__(self):
        return False

    def __repr__(self):
        return "_CYCLE"


_CYCLE = _CycleMarker()


class Region:
    def __init__(self, name: str, rows: list[dict], blueprint: bp.Blueprint | None = None, confirmed: set[str] | None = None):
        """confirmed：巡检点开面板确认在跑的配方 id。可逆配方（固气/液气转化、灌装/拆解）光看简报分不出方向，
        确认过的一律保留、它的逆向（没确认的）一律丢掉。"""
        self.name = name
        self.blueprint = blueprint or bp.load(None)
        self.confirmed = set(confirmed or ())
        self.items, self.unknown = load_items(rows)
        stocks = sorted((i.stock for i in self.items.values() if i.stock is not None), reverse=True)
        if len(stocks) >= 2 and stocks[0] > 2 * stocks[1]:
            # 仓库上限是全地区共用的，真到上限的物品不会只有一个且比第二名高一倍以上；这种孤值是 OCR 多认了一位
            stocks = stocks[1:]
        self.cap = max(10000, math.ceil(max(stocks, default=0) / 10000) * 10000)
        self.by_label = {i.label: i for i in self.items.values()}
        self._build_graph()

    # 只保留这个地区正在用的配方：输入都在被消耗，且至少一个输出在被生产
    def _build_graph(self):
        it = self.items
        consumed = {k for k, v in it.items() if v.theo_cons or v.cur_cons}
        produced = {k for k, v in it.items() if v.theo_prod or v.cur_prod}
        cands = [
            r
            for r in RECIPES
            if r["inputs"]
            and all(i in consumed for i in r["inputs"])
            and any(o in produced for o in r["outputs"])
        ]
        # 互为逆向的配方（灌装/拆解、液气双向转化）只靠「在被消耗、在被生产」分不出哪个方向在用。
        # 某个方向的产物另有正常来源时，就认为这个方向没在用（例如拆解机拆出的空瓶，塑形机本来就在做）。
        def key(r):
            return frozenset(r["inputs"]), frozenset(r["outputs"])

        reverse_of = {}
        by_key = {key(r): r for r in cands}
        for r in cands:
            rev = by_key.get((frozenset(r["outputs"]), frozenset(r["inputs"])))
            if rev is not None:
                reverse_of[r["id"]] = rev["id"]

        def has_other_source(r) -> bool:
            # 「另有来源」只算方向明确（本身不可逆）的配方：2026-11-06 武陵 息壤→息壤气（固气转化机）和
            # 液化息壤→息壤气（液气转化机）都可逆，互相把对方当成另有来源，两条全被丢掉，息壤气就成了没有生产者的「源头」，
            # 报出「源头 息壤气 产量低于理论」；其实是下游重息壤爆仓回堵
            return all(
                any(o in x["outputs"] and x["id"] not in (r["id"], reverse_of.get(r["id"])) and x["id"] not in reverse_of for x in cands)
                for o in r["outputs"]
            )

        confirmed = self.confirmed
        for r in RECIPES:  # 确认在跑的配方即使简报上看不出产耗（刚起步、数值被四舍五入）也进图
            if r["id"] in confirmed and all(r["id"] != c["id"] for c in cands):
                cands.append(r)
        by_key = {key(r): r for r in cands}
        for r in cands:
            rev = by_key.get((frozenset(r["outputs"]), frozenset(r["inputs"])))
            if rev is not None:
                reverse_of[r["id"]] = rev["id"]
        self.recipes = [
            r for r in cands
            if r["id"] in confirmed
            or not (
                (r["id"] in reverse_of and has_other_source(r))
                or reverse_of.get(r["id"]) in confirmed  # 逆向已被巡检确认：这个方向没在用
            )
        ]
        self.producers: dict[str, list[dict]] = {}
        self.consumers: dict[str, list[dict]] = {}
        for r in self.recipes:
            for o in r["outputs"]:
                self.producers.setdefault(o, []).append(r)
            for i in r["inputs"]:
                self.consumers.setdefault(i, []).append(r)

    # ---- 基本状态 ----
    def full(self, i: Item) -> bool:
        return i.stock is not None and i.stock >= self.cap * FULL_RATIO

    def tier_full(self, i: Item) -> bool:
        """备路触发用的严格满仓：库存到了上限、入库被拒、传送带才会回堵到溢流段。
        89967/90000 这种「产耗平衡、差一点没满」入库照收，溢流段没料，备路设备空着是正常（用户 2026-11-04 指正）。"""
        return i.stock is not None and i.stock >= self.cap

    def plenty(self, i: Item) -> bool:
        return i.stock is not None and i.stock > self.cap * PLENTY_RATIO

    def under(self, i: Item) -> bool:
        return i.theo_prod > 0 and i.cur_prod < i.theo_prod * UNDER_RATIO

    def draining(self, i: Item) -> float | None:
        """在耗库存时返回预计耗尽的分钟数。"""
        if i.fluid or self.full(i) or not i.stock:
            return None
        gap = i.cur_cons - i.cur_prod
        if gap >= max(DRAIN_MIN, 0.03 * i.cur_cons):
            return i.stock / gap
        return None

    def fmt(self, i: Item) -> str:
        stock = "流体" if i.fluid else f"库存 {i.stock}"
        red = "，理论消耗标红" if i.theo_red else ""
        return f"{i.label}（{stock}，当前 产{i.cur_prod}/耗{i.cur_cons}，理论 产{i.theo_prod}/耗{i.theo_cons}{red}）"

    def machines_of(self, i: Item) -> str:
        names = sorted({r["machine_name"] for r in self.producers.get(i.id, [])})
        return "、".join(names) or "采集/矿机/水泵"

    def inputs_of(self, i: Item) -> list[Item]:
        seen, out = set(), []
        for r in self.producers.get(i.id, []):
            for iid in r["inputs"]:
                if iid in self.items and iid not in seen:
                    seen.add(iid)
                    out.append(self.items[iid])
        return out

    # ---- 能解释「低于理论」的正常原因 ----
    def blocked_path(self, iid: str, seen: frozenset = frozenset()) -> list[str] | None:
        """iid 爆仓，或它的所有下游都爆仓/回堵时，返回到爆仓产物的路径。
        下游只绕回环（芽针 ⇄ 芽针种子）的消费者不算出口，忽略；所有消费者都只是环时返回 _CYCLE（假值）。2026-11-07"""
        i = self.items.get(iid)
        if i is None:
            return None
        if iid in seen:
            return _CYCLE
        if self.full(i):
            return [i.label]
        cons = self.consumers.get(iid)
        if not cons:
            return None
        paths = []
        for r in cons:
            sub, only_cycle = None, True
            for o in r["outputs"]:
                res = self.blocked_path(o, seen | {iid})
                if res is _CYCLE:
                    continue
                only_cycle = False
                sub = res
                if sub:
                    break
            if only_cycle:
                continue  # 这条消费路只绕回环，不是出口
            if sub is None:
                return None  # 有一路下游没堵，iid 还能被消耗
            paths.append(sub)
        if not paths:
            return _CYCLE
        return [i.label] + min(paths, key=len)

    def excuse(self, i: Item) -> str | None:
        if self.full(i):
            return "仓库爆仓，生产自动降速"
        path = self.blocked_path(i.id)
        if path:
            return f"下游 {path[-1]} 爆仓回堵（{' → '.join(path)}）"
        rule = next((r for r in self.blueprint.tier_rules if r["product"] == i.label), None)
        if rule:
            missing = [n for n in rule["requires_full"] if not (n in self.by_label and self.full(self.by_label[n]))]
            if missing:
                return f"备路未触发：{'、'.join(missing)} 未爆仓（{rule['note']}）"
        limits = self.blueprint.design_limits
        if i.label in limits:
            return f"蓝图设计限速：{limits[i.label]}"
        return self.baseline_excuse(i)

    @property
    def ref_items(self) -> dict[str, Item]:
        """蓝图参考简报里本地区的物品（按蓝图搭好、跑正常时抓的）；没有参考简报时为空。"""
        if not hasattr(self, "_ref_items"):
            rows = (self.blueprint.reference_brief or {}).get(self.name) or []
            self._ref_items = load_items(rows)[0] if rows else {}
        return self._ref_items

    def baseline_excuse(self, i: Item) -> str | None:
        """没有备路规则的蓝图靠这个：参考简报里这个产物本来就没跑满（备路、限速、轮换），
        现在的产量不低于基准 × BASELINE_RATIO 就算正常。基准里是满速的不在此列（现在低就是真的低）。2026-11-07"""
        ref = self.ref_items.get(i.id)
        if ref is None or ref.theo_prod <= 0 or ref.cur_prod >= ref.theo_prod * UNDER_RATIO:
            return None
        if i.cur_prod >= ref.cur_prod * BASELINE_RATIO:
            return f"与基准一致：按蓝图正常跑时 {i.label} 就是 {ref.cur_prod}/{ref.theo_prod}（备路或设计限速），现在 {i.cur_prod}/{i.theo_prod} 不低于基准"
        return None

    # ---- 往上游追根因 ----
    def find_root(self, i: Item, seen: frozenset = frozenset(), depth: int = 0) -> tuple[Root, list[str]]:
        pad = "  " * depth
        chain = [pad + self.fmt(i)]
        seen = seen | {i.id}

        if depth > 0:
            ex = self.excuse(i)
            if ex:
                chain.append(pad + f"→ {ex}")
                return Root(f"excused:{i.id}", "设计内", f"{i.label}：{ex}"), chain

        if not self.producers.get(i.id) or i.label in SOURCE_NAMES:
            if i.theo_prod == 0 and i.cur_prod == 0:
                chain.append(pad + "→ 没有任何来源（理论生产也是 0）")
                return Root(f"idle:{i.id}", "闲置", f"{i.label} 没有来源，用它的设备闲置", "不打算生产可以忽略"), chain
            chain.append(pad + f"→ 源头产量低于理论（{i.cur_prod}/{i.theo_prod}）")
            key = f"fluid:{i.id}" if i.fluid else f"source:{i.id}"
            return (
                Root(key, "待确认", f"源头 {i.label} 产量低于理论" + ("（流体按用量供给，低于理论多半是用得少）" if i.fluid else ""),
                     (f"去俯瞰看用 {i.label} 的设备：{BLOCKED}这条可以不管；设备空闲、原料格里缺 {i.label} 才是真缺" if i.fluid
                      else f"去俯瞰查 {i.label} 的采集设备、泵或供给准入口")),
                chain,
            )

        inputs = [x for x in self.inputs_of(i) if x.id not in seen]
        tight = [x for x in inputs if not x.fluid and not self.plenty(x)]
        for x in tight:
            if self.under(x) or not self.producers.get(x.id):
                chain.append(pad + f"→ {self.machines_of(i)} 没跑满，原料 {x.label} 没有存货，往上追：")
                root, sub = self.find_root(x, seen, depth + 1)
                return root, chain + sub
        if tight:
            x = tight[0]
            red = "（理论消耗标红）" if x.theo_red else ""
            chain.append(pad + f"→ 原料 {x.label} 已按理论满产（{x.cur_prod}/{x.theo_prod}），仍不够分{red}")
            return Root(f"design:{x.id}", "设计内", f"{x.label} 已满产但供不应求{red}", "设计上按优先级分配"), chain

        fluids = [x for x in inputs if x.fluid]
        machines = self.machines_of(i)
        if fluids and all(self.excuse(x) for x in fluids):
            names = "、".join(x.label for x in fluids)
            chain.append(pad + f"→ 固体原料都有库存，流体 {names} 是蓝图设计限速的")
            return Root(f"excused-fluid:{names}", "设计内", f"受 {names} 的设计限速影响"), chain
        if fluids:
            names = "、".join(x.label for x in fluids)
            chain.append(pad + f"→ 固体原料都有库存，剩下的限制是流体 {names}")
            key = "fluid:" + "+".join(sorted(x.id for x in fluids))
            return (
                Root(key, "待确认", f"疑似 {names} 供给不足（输入端显示阻塞就不缺）",
                     f"去俯瞰看 {machines} 的 {names} 输入端：{BLOCKED}是别的原因（产物爆仓、被关）；"
                     f"设备空闲、原料格缺液体才查泵/暗管/准入口限速、单根管道 120/分上限"),
                chain,
            )
        if inputs:
            have = "、".join(f"{x.label} {x.stock}" for x in inputs)
            chain.append(pad + f"→ 原料都有库存（{have}），但 {machines} 没跑满")
        else:
            chain.append(pad + f"→ 原料就是上游刚追过的物品（循环），{machines} 没跑满")
        return (
            Root(f"deliver:{i.id}", "待确认", f"{i.label}：原料有，但{machines}没吃饱（送不到或限速）", f"去俯瞰查 {machines} 的进料口：取货口、传送带、准入口限速"),
            chain,
        )

    def diagnose(self) -> list[Finding]:
        groups: dict[str, Finding] = {}

        def add(root: Root, label: str, chain: list[str]):
            f = groups.setdefault(root.key, Finding(root.level, root.text, root.advice))
            if label not in f.affected:
                f.affected.append(label)
            if len(chain) > len(f.chain):
                f.chain = chain

        xfers = bp.transfers(self.blueprint.playbook)
        inbound = {x["item"]: x for x in xfers if x["to"] == self.name}
        outbound = {x["item"]: x for x in xfers if x.get("from") == self.name and x.get("mode") == "库存"}
        for i in self.items.values():
            if not i.active:
                continue
            minutes = self.draining(i)
            x = inbound.get(i.label)
            if x and i.stock is not None and i.stock <= self.cap * XFER_LOW:
                # 跨地区传输送进来的物品库存见底：传输停了或单批不够（简报看不到传输量，只能看库存）
                add(Root(f"xfer:{i.id}", "提醒", f"{i.label} 靠跨地区传输补（{bp.transfer_label(x)}），库存只剩 {i.stock}",
                         "到协议核心 → 协议管理看这条跨地区传输是否还开着、单批数量够不够（程序不会去点）"), i.label, [self.fmt(i)])
                continue
            if x and minutes is not None:
                minutes = None  # 消耗大于产出的差额由传输补，简报里传进来的量不算产量
            if minutes is not None and i.label in outbound:
                y = outbound[i.label]
                add(Root(f"xfer:{i.id}", "提醒", f"{i.label} 在耗库存：在用库存传输送往{y['to']}，库存下降是预期的",
                         "库存传输会一直扣到起点仓库见底；不想耗光就改成超库存传输（起点建设等级够时）或调小单批数量"),
                    i.label, [self.fmt(i), f"→ 消耗比生产多 {i.cur_cons - i.cur_prod}/分，约 {minutes:.0f} 分钟后耗尽"])
                continue
            if minutes is not None:
                chain = [self.fmt(i), f"→ 消耗比生产多 {i.cur_cons - i.cur_prod}/分，约 {minutes:.0f} 分钟后耗尽"]
                advice = ""
                if self.under(i) and not self.excuse(i):
                    root, sub = self.find_root(i)
                    chain += sub[1:]
                    text = f"{i.label} 在耗库存；生产受限于：{root.text}"
                    advice = root.advice
                elif self.under(i):
                    text = f"{i.label} 在耗库存；生产低于理论但有设计内原因：{self.excuse(i)}"
                else:
                    text = f"{i.label} 在耗库存（生产没低于理论，是消耗变多了）"
                add(Root(f"drain:{i.id}", "提醒", text, advice), i.label, chain)
                continue
            if not self.under(i):
                continue
            ex = self.excuse(i)
            if ex:
                reason = ex.split("（")[0]  # 「下游 X 爆仓回堵（路径）」按「下游 X 爆仓回堵」合并；设计限速各自保留说明
                add(Root(f"ok:{reason}", "正常", reason), i.label, [])
                continue
            root, chain = self.find_root(i)
            add(root, i.label, chain)

        return sorted(groups.values(), key=lambda f: LEVELS.index(f.level))

    def full_items(self) -> list[Item]:
        return [i for i in self.items.values() if self.full(i) and i.active]


# ---------------------------------------------------------------- 报告

def report(regions: list[Region]) -> tuple[str, dict]:
    lines, data = [], {}
    for reg in regions:
        findings = reg.diagnose()
        count = {lv: sum(f.level == lv for f in findings) for lv in LEVELS}
        lines.append(f"# {reg.name}（仓库上限 {reg.cap}）")
        lines.append("　".join(f"{lv} {count[lv]}" for lv in LEVELS[:-1]))
        full = reg.full_items()
        lines.append(f"\n爆仓（{len(full)}）：" + "、".join(i.label for i in full))
        for level in LEVELS[:-1]:
            group = [f for f in findings if f.level == level]
            if not group:
                continue
            lines.append(f"\n## {level}")
            for f in group:
                lines.append(f"- **{f.text}**")
                if f.advice:
                    lines.append(f"  - 建议：{f.advice}")
                lines.append(f"  - 影响：{'、'.join(f.affected)}")
                lines += [f"    {s}" for s in f.chain]
        normal = [f for f in findings if f.level == "正常"]
        if normal:
            lines.append("\n## 低于理论但正常")
            for f in normal:
                lines.append(f"- {f.text}：{'、'.join(f.affected)}")
        if reg.unknown:
            lines.append(f"\n（简报里有 {len(reg.unknown)} 行没对上物品 id：{'、'.join(reg.unknown)}）")
        lines.append("")
        data[reg.name] = {
            "cap": reg.cap,
            "full": [i.label for i in full],
            "findings": [f.__dict__ for f in findings],
            "unknown": reg.unknown,
        }
    return "\n".join(lines), data


def main(argv: list[str]) -> int:
    blueprint = None  # 可选：--blueprint 名字，在报告最前面加「源头物品对照蓝图」一节
    if "--blueprint" in argv:
        k = argv.index("--blueprint")
        blueprint = argv[k + 1]
        argv = argv[:k] + argv[k + 2 :]
    path = Path(argv[0]) if argv else max(DEBUG.glob("brief_*.json"), key=lambda p: p.stat().st_mtime)
    brief = json.loads(path.read_text(encoding="utf-8"))
    plan = bp.load(blueprint)  # 不给 --blueprint 时用默认蓝图的备路规则
    # 只诊断蓝图 areas.json 里列出的地区（没有 areas.json 时诊断全部）
    skipped = [n for n, rows in brief.items() if rows and plan.areas and n not in plan.areas]
    regions = [Region(name, rows, plan) for name, rows in brief.items() if rows and name not in skipped]
    text, data = report(regions)
    print(f"简报：{path.name}\n")
    for n in skipped:
        print(f"跳过地区「{n}」：不在蓝图「{plan.name}」的 areas.json 里")
    if blueprint:
        import sources  # 放这里避免循环导入（sources 依赖本模块）

        src_text, _ = sources.report(brief, plan)
        print(src_text)
    print(text)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (DEBUG / f"diagnosis_{stamp}.md").write_text(text, encoding="utf-8")
    (DEBUG / f"diagnosis_{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已保存：debug\\diagnosis_{stamp}.md / .json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
