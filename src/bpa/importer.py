"""导入作者导出的蓝图分析 zip（<产线>.bpa.zip），生成一个蓝图档案 data/blueprints/<产线名>/（任务计划第 2 项）。

导入后巡检怎么按设计判断（都走原有的入口，不另起一套判定）：
- playbook.json 的 device_designs：作者确认过的「平时空闲 / 间歇 / 溢流位」设备，巡检看到它空闲按设计如此、不报问题。
  没确认过的设计分析结果只放在 bpa_expect 里当参考，不压问题（作者没核对过，可能是识别错）。
- tier_rules.json 的 overflow_products（溢流位设备做的东西）和 design_limits（准入口限量的产物）：合并诊断按设计内处理。
- ledger/<据点>.json 底账、<据点>_recipes.json 配方表、gate_limits/<据点>.json 准入口限速：近景定向巡检用。
- bpa/：拼合结果（composite、logistics、flow、design、输入端、暗管连接），近景巡检的管网追踪、断水影响范围用。
- scan/<据点>/：建底账那次扫描的角落图和 scan.json（远景 ↔ 近景配准）。

zip 格式见 docs/蓝图分析器-设计.md 第 5 节（project.export）。
"""
from __future__ import annotations

import json
import shutil
import time
import zipfile
from pathlib import Path, PurePosixPath

import cv2
import numpy as np

IDLE_ACTUALS = ("间歇", "平时空闲（设计如此）", "没在用")


def _j(z: zipfile.ZipFile, name: str, default=None):
    try:
        return json.loads(z.read(name).decode("utf-8"))
    except KeyError:
        return default


def _dump(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def is_bpa_zip(path: str | Path) -> bool:
    try:
        with zipfile.ZipFile(path) as z:
            m = _j(z, "manifest.json", {}) or {}
        return str(m.get("format", "")).startswith("bpa")
    except (zipfile.BadZipFile, OSError, ValueError):
        return False


def _outputs(recipe: str) -> list[str]:
    from .design import outputs_of
    return [k for k in outputs_of(recipe) if k not in ("清水", "污水")]


def expectations(design: dict, confirm: dict, area: str) -> dict:
    """一个据点的设计分析 + 作者确认 → {device_designs, overflow, limits, unconfirmed, wrong}。
    只有作者确认过的才进 device_designs / overflow / limits（会压掉巡检的问题）；没确认的进 unconfirmed（只当参考）。"""
    from .project import device_key
    dev_c = (confirm or {}).get("devices", {})
    out = {"device_designs": [], "overflow": {}, "limits": {}, "unconfirmed": [], "wrong": []}
    for r in design.get("devices", []):
        c = dev_c.get(device_key(r))
        rate, state = float(r.get("rate") or 0), r.get("state", "")
        base = {"area": area, "name": r["device"], "x": r["x"], "y": r["y"]}
        if c is None:
            out["unconfirmed"].append(dict(base, rate=rate, state=state, recipe=r.get("recipe", "")))
            continue
        why = next((b for b in r.get("basis", []) if not b.startswith("来料路径里有推断")), "")
        if c["verdict"] == "ok":
            rot = _rotation_of(r)
            if rot:  # 作者确认过的两种植物轮换（计算器 _plant_alternation 推出来的）→ 档案登记轮换对
                out.setdefault("rotations", []).append({"plants": rot, "area": area, "note": f"{r['device']} ({r['x']},{r['y']}) 蓝图分析推断、作者确认"})
            if rate >= 0.995:
                continue  # 设计满速：巡检看到空闲照常报
            note = f"蓝图分析：设计开工率 {rate:.0%}（{state}）" + (f"；{why}" if why else "") + "；作者确认" + (f"：{c['note']}" if c.get("note") else "")
            out["device_designs"].append(dict(base, kinds=["idle"] + (["strike"] if "燃料" in state or "罢工" in state else []), note=note,
                                              rate=rate, source="bpa"))
            if state.startswith("溢流位"):
                for p in _outputs(r.get("recipe", "")):
                    out["overflow"].setdefault(p, f"{area} {r['device']} ({r['x']},{r['y']}) 是溢流位：主路堵了才转（蓝图分析，作者确认）")
            if any("准入口限速" in b for b in r.get("basis", [])):
                for p in _outputs(r.get("recipe", "")):
                    out["limits"].setdefault(p, f"{area} {r['device']} ({r['x']},{r['y']}) 设计开工率 {rate:.0%}：{why}（蓝图分析，作者确认）")
        else:
            out["wrong"].append(dict(base, computed_rate=rate, computed_state=state, actual=c.get("actual"), note=c.get("note", "")))
            if c.get("actual") in IDLE_ACTUALS:
                note = f"作者确认：实际{c['actual']}" + (f"（{c['note']}）" if c.get("note") else "") + f"；蓝图分析原来算的是 {rate:.0%}（{state}）"
                out["device_designs"].append(dict(base, kinds=["idle"], note=note, source="bpa"))
    return out


def _rotation_of(row: dict) -> list[str] | None:
    """设计分析一行的依据里有「两种植物轮换」的交替配方 → [植物A, 植物B]。"""
    from .design import _plant_of
    b0 = next((b for b in row.get("basis", []) if b.startswith("交替配方：") and "两种植物轮换" in b), None)
    if not b0:
        return None
    recs = b0.removeprefix("交替配方：").split("；")[0].split(" / ")
    plants = [_plant_of(row["device"], rc.strip()) for rc in recs[:2]]
    return plants if len(plants) == 2 and all(plants) and plants[0] != plants[1] else None


def _recipes_from_design(design: dict) -> list[dict]:
    """zip 里没带游戏内读到的配方表时，用设计分析里每台设备的配方（推断的照样标出来）。"""
    out = []
    for r in design.get("devices", []):
        if not r.get("recipe"):
            continue
        outs = _outputs(r["recipe"])
        out.append({"name": r["device"], "x": r["x"], "y": r["y"], "recipe": r["recipe"], "output": None,
                    "votes": "蓝图分析" + ("（推断配方）" if r.get("recipe_inferred") else ""), "product": outs[0] if outs else ""})
    return out


def peek(path: str | Path) -> dict:
    """导入前看一眼：作者项目名、据点分组（界面用来问「合并进现有档案还是新建」）。"""
    with zipfile.ZipFile(path) as z:
        man = _j(z, "manifest.json", {}) or {}
    return {"name": man.get("name") or Path(path).stem.replace(".bpa", ""), "groups": list((man.get("groups") or {}).keys()),
            "generated": man.get("generated")}


def merge_candidates(groups: list[str]) -> list[str]:
    """分区名能盖住 zip 里全部据点的现有档案（areas.json 里登记过这些分区）。"""
    import blueprint as bp
    out = []
    for d in sorted(bp.BLUEPRINTS.iterdir()) if bp.BLUEPRINTS.exists() else []:
        if not d.is_dir() or d.name.startswith("_"):
            continue
        try:
            areas = json.loads((d / "areas.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        names = {a if isinstance(a, str) else (a or {}).get("name") for k, v in areas.items() if not k.startswith("_") and isinstance(v, list) for a in v}
        if groups and set(groups) <= names:
            out.append(d.name)
    return out


def import_zip(path: str | Path, rename: str | None = None, log=print, into: str | None = None) -> str:
    """→ 档案名。已有同名档案时在名字后加日期，不覆盖。
    into：合并进这个现有档案（灰度包 2026-11-10：包里的档案带错题本、参考简报、备路规则但没有底账，作者 zip 带底账和设计分析，
    分开导入会变成两套都不全的档案）。合并时写入 bpa/、scan/、ledger/ 和作者确认的设计内设备，保留档案原有的分区、错题本、
    参考简报和备路规则；中途出错整套档案恢复原样。"""
    import blueprint as bp
    import blueprint_tools as bt
    from paths import ROOT
    path = Path(path)
    with zipfile.ZipFile(path) as z:
        man = _j(z, "manifest.json", {}) or {}
        if not str(man.get("format", "")).startswith("bpa"):
            raise ValueError("这不是蓝图分析导出的 zip（没有 manifest.json 或格式不对）")
        groups: dict[str, list[str]] = man.get("groups") or {}
        if not groups:
            raise ValueError("zip 里没有据点分组")
        backup = None
        if into:
            name, dest = into, bp.BLUEPRINTS / into
            if not (dest / "areas.json").exists():
                raise FileNotFoundError(f"没有蓝图档案「{into}」")
            backup = bp.BLUEPRINTS / f"_merge_backup_{int(time.time())}"
            shutil.copytree(dest, backup)
        else:
            name = bt.clean_name(rename or man.get("name") or path.stem.replace(".bpa", ""))
            dest = bp.BLUEPRINTS / name
            if dest.exists():
                name = f"{name}-{time.strftime('%m%d%H%M')}"
                dest = bp.BLUEPRINTS / name
            dest.mkdir(parents=True)
        try:
            names = z.namelist()
            confirm = _j(z, "confirm.json", {}) or {}
            inputs = _j(z, "inputs.json", {}) or {}
            _dump(dest / "bpa" / "inputs.json", inputs)
            _dump(dest / "bpa" / "project.json", {"name": name, "groups": groups, "archive": name, "imported_from": path.name,
                                                  "author_project": man.get("name"), "generated": man.get("generated")})
            tier = {"doc": f"导入自蓝图分析 zip「{path.name}」（{man.get('generated', '')}）：备路规则来自作者确认过的设计分析",
                    "rules": [], "design_limits": [], "overflow_products": [], "rotating_products": [], "rotating_plants": []}
            playbook = {"doc": f"导入自蓝图分析 zip「{path.name}」", "targets": [], "hints": [], "normal": [],
                        "device_designs_doc": "蓝图分析里作者确认过「平时空闲 / 间歇 / 溢流位」的设备：巡检看到它空闲按设计如此",
                        "device_designs": [], "bpa_expect": [], "bpa_wrong": []}
            if into:  # 合并：在档案原有的备路规则 / 错题本上补，作者 zip 那几项（设计内设备、没确认的、说不对的）按这次的换掉
                old_t = _jfile(dest / "tier_rules.json") or {}
                old_p = _jfile(dest / "playbook.json") or {}
                for k, v in tier.items():
                    old_t.setdefault(k, v)
                for k in ("design_limits", "overflow_products", "rotating_products", "rotating_plants"):
                    if isinstance(old_t.get(k), dict):  # 老格式：{产物: 说明}
                        old_t[k] = [{"product": a, "note": b} for a, b in old_t[k].items()]
                tier = old_t
                for k, v in playbook.items():
                    old_p.setdefault(k, v)
                old_p["device_designs"] = [x for x in old_p.get("device_designs", []) if not x.get("from_bpa")]
                old_p["bpa_expect"], old_p["bpa_wrong"] = [], []
                playbook = old_p
            playbook["bpa"] = {"zip": path.name, "author_project": man.get("name"), "generated": man.get("generated"),
                               "imported": time.strftime("%Y-%m-%d %H:%M:%S"), "confirm": man.get("confirm", {}), "merged": bool(into)}
            for base, bps in groups.items():
                bd = dest / "bpa" / "base" / base
                bd.mkdir(parents=True, exist_ok=True)
                pre = f"{base}/"
                for n in names:
                    if not n.startswith(pre) or n.endswith("/"):
                        continue
                    rel = PurePosixPath(n[len(pre):])
                    parts = rel.parts
                    if parts[0] == "blueprints" and len(parts) >= 3:  # 蓝图：暗管配对、锚点、拼图（管网追踪要用）
                        tgt = dest / "bpa" / "bp" / parts[1] / Path(*parts[2:])
                    elif parts[0] == "raw" and len(parts) >= 3 and parts[1] == "ledger_scan":  # 建底账那次扫描
                        tgt = dest / "scan" / base / Path(*parts[2:])
                    elif parts[0] == "raw":
                        continue  # 预览原始帧：校准/纠错用，巡检不需要
                    elif parts[0] == "annotated":
                        tgt = bd / parts[-1]
                    elif rel.name in ("ledger.json", "recipes.json", "gate_limits.json"):
                        continue  # 下面单独放
                    else:
                        tgt = bd / Path(*parts)
                    tgt.parent.mkdir(parents=True, exist_ok=True)
                    tgt.write_bytes(z.read(n))
                # 远景↔近景配准用的两张角落图：导出时转成了 jpg，巡检按 png 读
                sd = dest / "scan" / base
                for stem in ("far_corner", "near_corner"):
                    j = sd / f"{stem}.jpg"
                    if j.exists() and not (sd / f"{stem}.png").exists():
                        im = cv2.imdecode(np.fromfile(str(j), np.uint8), cv2.IMREAD_COLOR)
                        cv2.imencode(".png", im)[1].tofile(str(sd / f"{stem}.png"))
                led = _j(z, f"{base}/ledger.json")
                if led is None:
                    log(f"  {base}：zip 里没有底账，近景巡检用不了这个据点")
                else:
                    if led.get("scan") and sd.exists():
                        try:
                            led["scan"]["dir"] = str(sd.relative_to(ROOT))
                        except ValueError:  # 档案不在程序目录下：写绝对路径（ROOT / 绝对路径 = 绝对路径）
                            led["scan"]["dir"] = str(sd)
                    _dump(dest / "ledger" / f"{base}.json", led)
                design = json.loads((bd / "design.json").read_text(encoding="utf-8")) if (bd / "design.json").exists() else {}
                rec = _j(z, f"{base}/recipes.json")
                _dump(dest / "ledger" / f"{base}_recipes.json", rec if rec is not None else {"devices": _recipes_from_design(design),
                                                                                         "doc": "导入时 zip 里没有游戏内读到的配方表，用设计分析的配方"})
                gl = _j(z, f"{base}/gate_limits.json")
                if gl is not None:
                    _dump(dest / "ledger" / "gate_limits" / f"{base}.json", gl)
                ex = expectations(design, confirm.get(base, {}), base)
                playbook["device_designs"] += [dict(x, from_bpa=True) for x in ex["device_designs"]]
                playbook["bpa_expect"] += ex["unconfirmed"]
                playbook["bpa_wrong"] += ex["wrong"]
                for p, note in ex["overflow"].items():
                    if p not in {x["product"] for x in tier["overflow_products"]}:
                        tier["overflow_products"].append({"product": p, "note": note})
                for rt in ex.get("rotations", []):
                    if not any(set(x["plants"]) == set(rt["plants"]) and x.get("area") == rt["area"] for x in tier["rotating_plants"]):
                        tier["rotating_plants"].append(rt)
                for p, note in ex["limits"].items():
                    if p not in {x["product"] for x in tier["design_limits"]}:
                        tier["design_limits"].append({"product": p, "note": note})
                log(f"  {base}：作者确认的设计内设备 {len(ex['device_designs'])} 台，没确认的 {len(ex['unconfirmed'])} 台（只当参考），"
                    f"说不对的 {len(ex['wrong'])} 处")
            if not into:  # 合并时保留档案原有的分区登记（主分区、锚点标签都在里面）
                areas = man.get("areas") or {}
                if not areas:
                    areas = {"_note": "zip 里没有地区信息：深入巡检前请在「蓝图」页重新识别分区（或手改这里，地区名要和游戏地区总览一致）",
                             "未知地区": list(groups)}
                _dump(dest / "areas.json", areas)
            _dump(dest / "tier_rules.json", tier)
            _dump(dest / "playbook.json", playbook)
            bt.fill_missing(dest)
        except Exception:
            if backup is not None:  # 合并失败：整套档案恢复原样
                shutil.rmtree(dest, ignore_errors=True)
                shutil.move(str(backup), str(dest))
                backup = None
            else:
                shutil.rmtree(dest, ignore_errors=True)
            raise
        finally:
            if backup is not None:
                shutil.rmtree(backup, ignore_errors=True)
    log(f"== 已{'合并进' if into else '导入为'}蓝图档案「{name}」（{dest}）")
    return name


def _jfile(p: Path):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def base_dir(archive: str, area: str) -> Path | None:
    """导入的档案里这个据点的拼合结果目录（近景巡检的管网追踪用）。"""
    import blueprint as bp
    d = bp.BLUEPRINTS / archive / "bpa" / "base" / area
    return d if (d / "composite.png").exists() and (d / "inputs_auto.json").exists() else None
