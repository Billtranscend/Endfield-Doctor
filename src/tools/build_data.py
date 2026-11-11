"""从 AKEData 下载数据表，生成诊断用的精简数据。

输出：
  data/item_names_cn.json  中文名 → 物品 id 列表（简报名称纠错用）
  data/item_id_names.json  物品 id → 中文名
  data/recipes.json        配方：设备、每轮秒数、输入、输出

用法：.venv\\Scripts\\python.exe src\\tools\\build_data.py
"""

import json
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
CACHE = ROOT / ".cache" / "akedata"
DATA = ROOT / "data"
BASE = "https://data.akedata.wiki/public/1.6.4/11343132-9/TableCfg"
TABLES = ["ItemTable", "I18nTextTable_CN", "FactoryMachineCraftTable", "FactoryBuildingTable"]


def load(table: str):
    path = CACHE / f"{table}.json"
    if not path.exists():
        CACHE.mkdir(parents=True, exist_ok=True)
        print(f"下载 {table} …")
        # AKEData 会拒绝 Python 默认的 User-Agent（403），用通用的浏览器标识
        req = urllib.request.Request(f"{BASE}/{table}.json", headers={"User-Agent": "Mozilla/5.0"})
        path.write_bytes(urllib.request.urlopen(req, timeout=120).read())
    return json.loads(path.read_text(encoding="utf-8"))


def building_ports(buildings: dict, building_name: dict) -> dict:
    """设备中文名 → 默认朝向下的占地和端口格（bpa/ports.py 用）。
    坐标换算按 IndustrialPlanner 在游戏里实测校准的规则（2026-11-10 记）：格 X = position.x，格 Y = depth-1-position.z，Y=0 是北边。
    同名多个建筑（精炼炉 furnance_1 / furnance_nop_1）取端口最多的那个（带管口的是超集）。
    端口所在的边：带子口取同一角色所有口共有的那条边（角上的口两条边都挨着），管口取左右边。"""
    out = {}
    for bid, b in sorted(buildings.items()):
        name = building_name.get(bid)
        rng = b.get("range") or {}
        w, d = rng.get("width"), rng.get("depth")
        ins, outs = b.get("inputPorts") or [], b.get("outputPorts") or []
        if not name or not w or not d or not (ins or outs):
            continue
        ports = []
        for role, ps in (("in", ins), ("out", outs)):
            cells = []
            for p in ps:
                pos = p["trans"]["position"]
                x, y = int(round(pos["x"])), int(round(d - 1 - pos["z"]))
                sides = {s for s, ok in (("N", y == 0), ("S", y == d - 1), ("W", x == 0), ("E", x == w - 1)) if ok}
                cells.append((x, y, bool(p.get("isPipe")), sides))
            for pipe in (False, True):
                grp = [c for c in cells if c[2] == pipe]
                common = set.intersection(*(c[3] for c in grp)) if grp else set()
                for x, y, _, sides in grp:
                    pick = common if len(common) == 1 else sides
                    if pipe and len(pick) > 1:
                        pick = pick & {"W", "E"} or pick
                    if len(pick) != 1:
                        continue  # 不在边上 / 说不清哪条边：不收
                    ports.append({"x": x, "y": y, "side": next(iter(pick)), "role": role, "pipe": pipe})
        if name not in out or len(ports) > len(out[name]["ports"]):
            out[name] = {"id": bid, "w": w, "d": d, "ports": ports}
    return out


def main() -> int:
    items = load("ItemTable")
    i18n = load("I18nTextTable_CN")
    craft = load("FactoryMachineCraftTable")
    buildings = load("FactoryBuildingTable")
    building_name = {
        bid: i18n.get(str(v.get("name", {}).get("id")))
        for bid, v in buildings.items()
        if i18n.get(str(v.get("name", {}).get("id")))
    }

    id_name = {}
    for iid, v in items.items():
        name = i18n.get(str(v.get("name", {}).get("id")))
        if name:
            id_name[iid] = name
    name_ids: dict[str, list[str]] = {}
    for iid, name in id_name.items():
        name_ids.setdefault(name, []).append(iid)

    recipes = []
    for rid, v in sorted(craft.items()):
        machine = v["machineId"]
        recipes.append(
            {
                "id": rid,
                "machine": machine,
                "machine_name": building_name.get(machine) or id_name.get(f"item_port_{machine}", machine),
                "seconds": v["progressRound"],  # totalProgress = 6000 × 秒
                "env": v.get("gasEnv", 0),
                "inputs": {g["id"]: g["count"] for grp in v["ingredients"] for g in grp["group"]},
                "outputs": {g["id"]: g["count"] for grp in v["outcomes"] for g in grp["group"]},
            }
        )

    DATA.mkdir(exist_ok=True)
    dump = lambda name, obj: (DATA / name).write_text(  # noqa: E731
        json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8"
    )
    dump("item_names_cn.json", name_ids)
    dump("item_id_names.json", id_name)
    dump("recipes.json", recipes)
    dump("building_names.json", sorted(set(building_name.values())))
    dump("item_icons.json", {iid: v["iconId"] for iid, v in items.items() if v.get("iconId")})
    dump("building_ports.json", building_ports(buildings, building_name))
    print(f"物品名 {len(name_ids)}，物品 id {len(id_name)}，配方 {len(recipes)}，建筑名 {len(set(building_name.values()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
