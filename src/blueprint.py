"""蓝图档案：data/blueprints/<名>/ 下的备路规则（tier_rules.json）和参考简报（reference_brief.json）。

用户预选蓝图后，诊断（diagnose）、合并（combine）、源头对照（sources）都按这里读到的规则判断。
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from paths import ROOT  # noqa: E402,F401
BLUEPRINTS = ROOT / "data" / "blueprints"
DEFAULT = "1.6皇帝产线"


@dataclass
class Blueprint:
    name: str
    dir: Path
    tier_rules: list[dict]  # 备路规则：product、requires_full、note
    design_limits: dict[str, str]  # 蓝图设计限速：产物显示名 → 说明
    overflow_products: dict[str, str]  # 溢流位产物（汇流器/分流后段的设备做的东西）：显示名 → 说明；这些设备空闲/未激活是设计内
    rotating_products: dict[str, str]  # 轮换产物：多配方反应池（自动处理复数配方阻塞）轮着做的东西，理论产量 0/30 来回切；吃它的设备间歇空闲是设计内
    reference_brief: dict | None  # 按蓝图搭好的基地的简报，没有时为 None
    playbook: dict = field(default_factory=dict)  # 用户的排查手册（playbook.json）：targets 产能目标、hints 按产物挂的排查链、normal 设计内说明
    areas: dict[str, list[str]] = field(default_factory=dict)  # 地区 → 分区名（areas.json），没有时为空
    area_configs: dict[str, dict] = field(default_factory=dict)  # 分区名 → 特例配置（anchor_label、icon、card）
    rotating_plants: list[dict] = field(default_factory=list)  # 两种植物轮换（任务计划第 13 项）：[{plants: [锦草, 芽针], area?, note}]；种植机/采种机在两种之间来回切

    def area_options(self, name: str) -> dict:
        """分区的特例配置（areas.json 里写成对象的分区）；普通分区返回空字典。"""
        return dict(self.area_configs.get(name, {}))


TRANSFER_MODES = ("超库存", "库存")  # 跨地区传输的两种模式


def transfers(playbook: dict) -> list[dict]:
    """跨地区传输（playbook.json → transfers）：[{item, from, to, mode, note?}]。
    游戏里在协议核心 → 协议管理设置（程序不读、也绝不点），开启后一批批持续传输：
    - 库存传输：从起点地区仓库扣掉、等量送到终点；
    - 超库存传输：起点建设等级够高才解锁，不扣起点库存，终点直接拿到（每批受「传输总值」限制）。
    简报里传进来的量不算产量：终点地区这个物品「消耗大于产出」、理论消耗标红是正常的。"""
    return [x for x in playbook.get("transfers", []) if x.get("item") and x.get("to")]


def transfer_label(x: dict) -> str:
    return f"{x.get('from') or '?'}→{x['to']}，{x.get('mode') or '超库存'}传输"


def normal_notes(playbook: dict, region: str | None = None) -> dict[str, str]:
    """设计内产物的说明：错题本 normal，加上跨地区传输送进来的物品（region 给了时只算送到这个地区的）。"""
    out = {x["product"]: x["note"] for x in playbook.get("normal", [])}
    for x in transfers(playbook):
        if region is None or x["to"] == region:
            out.setdefault(x["item"], f"跨地区传输补货（{transfer_label(x)}），消耗大于产出是正常" + (f"；{x['note']}" if x.get("note") else ""))
    return out


def available() -> list[str]:
    return sorted(p.name for p in BLUEPRINTS.iterdir() if p.is_dir()) if BLUEPRINTS.exists() else []


def load(name: str | None = None) -> Blueprint:
    name = name or DEFAULT
    d = BLUEPRINTS / name
    if not d.is_dir():
        raise FileNotFoundError(f"没有蓝图档案「{name}」（{d}）；可选：{'、'.join(available()) or '无'}")
    tier_path = d / "tier_rules.json"
    if not tier_path.exists():
        raise FileNotFoundError(f"蓝图档案「{name}」缺少 tier_rules.json（{tier_path}）")
    tier = json.loads(tier_path.read_text(encoding="utf-8"))
    pb_path = d / "playbook.json"
    playbook = json.loads(pb_path.read_text(encoding="utf-8")) if pb_path.exists() else {}
    ref_path = d / "reference_brief.json"
    ref = json.loads(ref_path.read_text(encoding="utf-8")) if ref_path.exists() else None
    areas_path = d / "areas.json"
    raw = json.loads(areas_path.read_text(encoding="utf-8")) if areas_path.exists() else {}
    # 分区可以是字符串，或带特例配置的对象 {"name": ..., "anchor_label": ...}；_note 之类的说明字段跳过
    areas: dict[str, list[str]] = {}
    configs: dict[str, dict] = {}
    for region, items in raw.items():
        if region.startswith("_"):
            continue
        areas[region] = []
        for a in items:
            if isinstance(a, dict):
                areas[region].append(a["name"])
                configs[a["name"]] = {k: v for k, v in a.items() if k != "name"}
            else:
                areas[region].append(a)
    return Blueprint(
        name=name,
        dir=d,
        tier_rules=tier["rules"],
        design_limits={x["product"]: x["note"] for x in tier.get("design_limits", [])},
        overflow_products={x["product"]: x["note"] for x in tier.get("overflow_products", [])},
        rotating_products={x["product"]: x["note"] for x in tier.get("rotating_products", [])},
        rotating_plants=[x for x in tier.get("rotating_plants", []) if len(x.get("plants") or []) == 2],
        reference_brief=ref,
        playbook=playbook,
        areas=areas,
        area_configs=configs,
    )
