"""设备底账：每张蓝图、每个分区里每台设备的 {类型, 坐标}。巡检靠它直接去点名，不再只跟着状态标记走。

坐标系：和 patrol.Device.world_x/world_y 一样（最远缩放、720p、以分区地图左上角硬边界为原点）。跨次复现误差 1–3 px（2026-11-07 用
景玉谷/应龙关/武陵城多次巡检核对）。

文件：data/blueprints/<蓝图>/ledger/<分区>.json
  {"area": 分区, "zoom": "far", "updated": 时间, "devices": [{"name", "x", "y", "source", "seen", "last"}]}
  source：panel = 面板标题确认；ocr = 固定缩放下读到全名；history = 历史巡检并集（只作补充，用户 2026-11-07：不能全靠它）。

设计见 docs/设备识别方案.md。底账为空时巡检回退到原来的「标记 + 首字猜测」，行为不变。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path

import blueprint as bp

MATCH_TOL = 30  # 同一台设备两次坐标的容差（px）：复现误差 1–3，留足余量但小于相邻设备间距（≥ 40）


@dataclass
class Entry:
    name: str
    x: int
    y: int
    source: str = "history"  # panel / ocr / history
    seen: int = 1  # 被看到的次数（并集时累加）
    last: str = ""  # 最近一次看到的时间戳
    note: str = ""


@dataclass
class Ledger:
    area: str
    devices: list[Entry] = field(default_factory=list)
    zoom: str = "far"
    updated: str = ""

    # ---- 查询 ----
    def by_type(self, names: set[str] | list[str] | None) -> list[Entry]:
        if not names:
            return list(self.devices)
        want = set(names)
        return [d for d in self.devices if d.name in want]

    def near(self, x: int, y: int, tol: int = MATCH_TOL) -> Entry | None:
        best = None
        for d in self.devices:
            dist = abs(d.x - x) + abs(d.y - y)
            if abs(d.x - x) <= tol and abs(d.y - y) <= tol and (best is None or dist < best[0]):
                best = (dist, d)
        return best[1] if best else None

    def types(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.devices:
            out[d.name] = out.get(d.name, 0) + 1
        return dict(sorted(out.items(), key=lambda kv: -kv[1]))

    # ---- 更新 ----
    def add(self, name: str, x: int, y: int, source: str, stamp: str = "") -> Entry:
        """并入一台：同位置已有 → 累加 seen，来源按可靠性升级（panel > ocr > history），名字以更可靠的来源为准。"""
        rank = {"panel": 3, "ocr": 2, "history": 1}
        hit = self.near(x, y)
        if hit is None:
            e = Entry(name, int(x), int(y), source, 1, stamp)
            self.devices.append(e)
            return e
        hit.seen += 1
        hit.last = stamp or hit.last
        if rank.get(source, 0) > rank.get(hit.source, 0):
            if hit.name != name:
                hit.note = f"原 {hit.name}({hit.source})"
            hit.name, hit.source = name, source
        elif rank.get(source, 0) == rank.get(hit.source, 0) and hit.name != name:
            hit.note = f"两次不一致：{hit.name} / {name}"
        return hit


# ---- 读写 ----

def _path(blueprint: str, area: str) -> Path:
    return bp.BLUEPRINTS / blueprint / "ledger" / f"{area}.json"


def load(blueprint: str, area: str) -> Ledger:
    p = _path(blueprint, area)
    if not p.exists():
        return Ledger(area)
    raw = json.loads(p.read_text(encoding="utf-8"))
    return Ledger(raw.get("area", area), [Entry(**d) for d in raw.get("devices", [])], raw.get("zoom", "far"), raw.get("updated", ""))


def save(blueprint: str, ledger: Ledger) -> Path:
    p = _path(blueprint, ledger.area)
    p.parent.mkdir(parents=True, exist_ok=True)
    ledger.updated = time.strftime("%Y%m%d-%H%M%S")
    body = {"area": ledger.area, "zoom": ledger.zoom, "updated": ledger.updated,
            "devices": [asdict(d) for d in sorted(ledger.devices, key=lambda d: (d.y, d.x))]}
    p.write_text(json.dumps(body, ensure_ascii=False, indent=1), encoding="utf-8")
    return p


def from_history(blueprint: str, area: str, patrols: list[Path]) -> Ledger:
    """用历史巡检文件（patrol_*.json）的并集做底账补充：只收面板确认过名字的设备（guessed=False、名字唯一），
    标记为 source=history。只作补充，不替代固定缩放扫描。"""
    led = load(blueprint, area)
    for p in patrols:
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(raw, dict) or raw.get("area") != area:
            continue
        for d in raw.get("devices", []):
            if d.get("guessed") or "|" in d["name"] or d["name"].startswith("（"):
                continue
            led.add(d["name"], d["world_x"], d["world_y"], "history", raw.get("time", ""))
    return led


def text(ledger: Ledger) -> str:
    lines = [f"底账 {ledger.area}：{len(ledger.devices)} 台（更新 {ledger.updated or '—'}）"]
    for name, n in ledger.types().items():
        lines.append(f"  {name} ×{n}")
    uncertain = [d for d in ledger.devices if d.note]
    if uncertain:
        lines.append(f"  待确认 {len(uncertain)} 台：" + "；".join(f"{d.name}({d.x},{d.y}) {d.note}" for d in uncertain[:8]))
    return "\n".join(lines)


if __name__ == "__main__":  # 开发期：python src\ledger.py <蓝图> <分区>  → 用 debug 里的历史巡检建补充底账并打印
    import sys
    blueprint, area = sys.argv[1], sys.argv[2]
    led = from_history(blueprint, area, sorted((Path(__file__).resolve().parent.parent / "debug").glob("patrol_*.json")))
    print(text(led))
    if "--save" in sys.argv:
        print("已存", save(blueprint, led))
