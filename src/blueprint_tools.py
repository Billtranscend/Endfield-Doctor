"""蓝图档案的导入 / 导出 / 新建 / 更新基准（给桌面程序用，不用手改 JSON）。

档案 = data/blueprints/<名>/ 目录：
  areas.json            地区 → 分区（哪个是主分区），深入巡检按它传送
  playbook.json         targets 最终产物与目标产量（产能自检）、doc 说明
  tier_rules.json       备路规则，可以为空：没有规则时诊断按参考简报「和基准比」
  reference_brief.json  按蓝图搭好、跑正常时的一份简报（缺矿缺气液、备路基准都靠它）

新建流程（Api.start_blueprint_survey → save_archive）：
  1. 读全部地区的简报 → 哪些地区在生产（regions_in_use）、最终产物和目标产量（derive_targets）
  2. 每个在用地区：打开地图 → 地区总览 → 逐个分区点进去找基地（survey_region），协议核心所在的分区记为主分区
  3. 用户在界面上勾选确认后 save_archive 写出四个文件，简报原样存为 reference_brief.json
"""

from __future__ import annotations

import json
import re
import shutil
import time
import zipfile
from pathlib import Path

import blueprint as bp
from diagnose import ID_NAME

FILES = ("areas.json", "playbook.json", "tier_rules.json", "reference_brief.json")
NAME_RE = re.compile(r"[\\/:*?\"<>|]")
TARGET_RATIO = 0.95  # 目标产量 = 基准当前产量；低于理论 5% 以内按理论算（基准那会儿轻微波动）


def archive_dir(name: str) -> Path:
    return bp.BLUEPRINTS / name


def clean_name(name: str) -> str:
    name = NAME_RE.sub("", (name or "").strip())
    if not name:
        raise ValueError("蓝图名不能为空")
    return name


# ---------------------------------------------------------------- 导入 / 导出

def import_archive(src: str, rename: str | None = None) -> str:
    """把一个档案目录或 .zip 复制进 data/blueprints。目录里至少要有 reference_brief.json 或 areas.json 之一，
    缺的文件补成空的。返回档案名（已存在同名时在后面加日期）。"""
    p = Path(src)
    if not p.exists():
        raise FileNotFoundError(f"找不到 {src}")
    if p.is_file() and p.suffix.lower() == ".zip":
        from bpa import importer
        if importer.is_bpa_zip(p):  # 作者用「蓝图分析」导出的 zip：按设计分析 + 作者确认生成档案（任务计划第 2 项）
            return importer.import_zip(p, rename)
    tmp = None
    if p.is_file() and p.suffix.lower() == ".zip":
        tmp = bp.BLUEPRINTS / f"_import_{int(time.time())}"
        tmp.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(p) as z:
            for info in z.infolist():  # 只解出我们认识的四个文件，忽略目录层级
                base = Path(info.filename).name
                if base in FILES:
                    (tmp / base).write_bytes(z.read(info))
        folder = tmp
        default_name = p.stem
    elif p.is_dir():
        folder = p
        default_name = p.name
    else:
        raise ValueError("请选择档案文件夹或 .zip 压缩包")
    try:
        present = [f for f in FILES if (folder / f).exists()]
        if not any(f in present for f in ("reference_brief.json", "areas.json")):
            found = "、".join(x.name for x in folder.iterdir()) or "空目录"
            raise ValueError(f"这不是蓝图档案：里面没有 {' / '.join(FILES)}（找到：{found}）")
        for f in present:  # 先验证 JSON 能读
            json.loads((folder / f).read_text(encoding="utf-8"))
        name = clean_name(rename or default_name)
        dest = archive_dir(name)
        if dest.exists():
            name = f"{name}-{time.strftime('%m%d%H%M')}"
            dest = archive_dir(name)
        dest.mkdir(parents=True)
        for f in present:
            shutil.copy2(folder / f, dest / f)
        fill_missing(dest)
        return name
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)


def set_rotating_plants(name: str, pairs: list[dict]) -> list[dict]:
    """两种植物轮换（任务计划第 13 项）写进档案的 tier_rules.json → rotating_plants。
    pairs：[{plants: [植物A, 植物B], area?: 分区, note?: 说明}]，植物名写显示名（锦草、芽针…，不带「种子」）。"""
    d = archive_dir(name)
    p = d / "tier_rules.json"
    if not p.exists():
        raise FileNotFoundError(f"蓝图档案「{name}」缺少 tier_rules.json")
    known = set(ID_NAME.values())
    clean = []
    for x in pairs:
        plants = [str(v).strip().removesuffix("种子") for v in (x.get("plants") or []) if str(v).strip()]
        if len(plants) != 2 or plants[0] == plants[1]:
            raise ValueError(f"一组要写两种不同的植物：{x.get('plants')}")
        bad = [v for v in plants if v not in known]
        if bad:
            raise ValueError(f"不认识的物品名：{'、'.join(bad)}（写游戏里的显示名，如 锦草、芽针）")
        e = {"plants": plants}
        if (x.get("area") or "").strip():
            e["area"] = x["area"].strip()
        if (x.get("note") or "").strip():
            e["note"] = x["note"].strip()
        clean.append(e)
    tier = json.loads(p.read_text(encoding="utf-8"))
    tier["rotating_plants_doc"] = "两种植物轮换：一对种植机/采种机在两种植物之间来回切，某一刻两台可能错开；搭档正在做另一种时，这台空闲/阻塞是设计内"
    tier["rotating_plants"] = clean
    p.write_text(_dump(tier), encoding="utf-8")
    return clean


def set_transfers(name: str, rows: list[dict], regions: list[str] | None = None) -> list[dict]:
    """跨地区传输（可选设置）整张表覆盖写进档案的 playbook.json → transfers。
    rows：[{item, from, to, mode: 超库存|库存, note?}]；物品写游戏里的显示名，起点终点写地区名（武陵、四号谷地）。"""
    d = archive_dir(name)
    if not d.is_dir():
        raise FileNotFoundError(f"没有蓝图档案「{name}」")
    known = set(ID_NAME.values())
    clean = []
    for x in rows:
        item = str(x.get("item") or "").strip()
        src, dst = str(x.get("from") or "").strip(), str(x.get("to") or "").strip()
        mode = str(x.get("mode") or "超库存").strip().removesuffix("传输")
        if item not in known:
            raise ValueError(f"不认识的物品名：{item or '（空）'}（写游戏里的显示名，如 蓝铁块）")
        if not src or not dst or src == dst:
            raise ValueError(f"「{item}」的起点和终点要写两个不同的地区")
        bad = [r for r in (src, dst) if regions and r not in regions]
        if bad:
            raise ValueError(f"不认识的地区：{'、'.join(bad)}（可选：{'、'.join(regions)}）")
        if mode not in bp.TRANSFER_MODES:
            raise ValueError(f"模式只能是 {' / '.join(bp.TRANSFER_MODES)}：{mode}")
        if any(c["item"] == item and c["to"] == dst for c in clean):
            raise ValueError(f"「{item}」送到{dst}的传输写了两条")
        e = {"item": item, "from": src, "to": dst, "mode": mode}
        if (x.get("note") or "").strip():
            e["note"] = x["note"].strip()
        clean.append(e)
    p = d / "playbook.json"
    pb = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"doc": "", "targets": [], "hints": [], "normal": []}
    pb["transfers_doc"] = ("跨地区传输（可选）：协议核心 → 协议管理里开着的传输。库存传输扣起点仓库；超库存传输不扣。"
                           "简报里传进来的量不算产量，所以终点地区这些物品消耗大于产出是正常的")
    pb["transfers"] = clean
    p.write_text(json.dumps(pb, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")  # playbook 一直是 indent=1
    return clean


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=4) + "\n"


def fill_missing(dest: Path) -> None:
    if not (dest / "tier_rules.json").exists():
        (dest / "tier_rules.json").write_text(_dump({"doc": "没有备路规则：诊断按参考简报和基准比", "rules": []}), encoding="utf-8")
    if not (dest / "playbook.json").exists():
        (dest / "playbook.json").write_text(_dump({"doc": "", "targets": [], "hints": [], "normal": []}), encoding="utf-8")
    if not (dest / "areas.json").exists():
        (dest / "areas.json").write_text(_dump({"_note": "导入时没有分区信息：深入巡检前请在蓝图页重新识别分区"}), encoding="utf-8")


def export_archive(name: str, dest_zip: str) -> str:
    d = archive_dir(name)
    if not d.is_dir():
        raise FileNotFoundError(f"没有蓝图档案「{name}」")
    out = Path(dest_zip)
    if out.suffix.lower() != ".zip":
        out = out.with_suffix(".zip")
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            if (d / f).exists():
                z.write(d / f, f"{name}/{f}")
    return str(out)


def delete_archive(name: str) -> None:
    d = archive_dir(name)
    if not d.is_dir():
        raise FileNotFoundError(f"没有蓝图档案「{name}」")
    shutil.rmtree(d)


# ---------------------------------------------------------------- 从简报推

def regions_in_use(brief: dict) -> list[str]:
    """简报里有生产（理论生产 > 0）的地区。"""
    return [r for r, rows in brief.items() if any((row.get("theo_prod") or 0) > 0 for row in rows or [])]


def derive_targets(brief: dict, regions: list[str] | None = None) -> list[dict]:
    """最终产物 = 简报里产大于耗的固体物品（理论生产 > 0，理论消耗 < 理论生产 × 0.8：中间产物几乎都是产多少吃多少）。
    目标产量：当前接近理论 → 理论；基准时爆仓（当前产量被回堵压到 0）→ 填理论但默认不勾（实际产量未知，让人改）；
    其他 → 基准当前产量；当前为 0 且没爆仓 → 基准时没在产，默认不勾。按地区归类。"""
    out = []
    for region, rows in brief.items():
        if regions and region not in regions:
            continue
        stocks = sorted((r.get("stock") or 0 for r in rows or [] if r.get("stock") is not None), reverse=True)
        if len(stocks) >= 2 and stocks[0] > 2 * stocks[1]:
            stocks = stocks[1:]  # 孤立的超大值是 OCR 多认了一位（同 diagnose.Region）
        cap = max(10000, -(-max(stocks, default=0) // 10000) * 10000)
        for row in rows or []:
            tp, tc = row.get("theo_prod") or 0, row.get("theo_cons") or 0
            stock = row.get("stock")
            if tp <= 0 or stock is None or tc >= tp * 0.8:
                continue
            cur = row.get("cur_prod") or 0
            full = stock >= cap * 0.995
            label = row["name"] + (f"({ID_NAME.get(row['fluid'], row['fluid'])})" if row.get("fluid") else "")  # 和 diagnose.Item.label 一致
            if cur >= tp * TARGET_RATIO:
                goal, sel, why = tp, True, "基准满速"
            elif full:
                goal, sel, why = tp, False, "基准时已爆仓，实际产量未知：按理论填的，请核对后再勾"
            elif cur > 0:
                goal, sel, why = cur, True, f"基准当前 {cur}/理论 {tp}（备路或限速产物按基准产量）"
            else:
                goal, sel, why = tp, False, "基准时没在产，按理论填的，请核对后再勾"
            out.append({"product": label, "per_min": goal, "note": f"{region}；{why}", "region": region,
                        "cur": cur, "theo": tp, "stock": stock, "full": full, "selected": sel})
    out.sort(key=lambda t: (not t["selected"], -t["per_min"]))
    return out


def produced_items(brief: dict, regions: list[str] | None = None) -> list[dict]:
    """简报里理论生产 > 0 的固体物品（手动往目标里加一行时的候选）。"""
    out = []
    for region, rows in brief.items():
        if regions and region not in regions:
            continue
        for row in rows or []:
            tp = row.get("theo_prod") or 0
            if tp <= 0 or row.get("stock") is None:
                continue
            label = row["name"] + (f"({ID_NAME.get(row['fluid'], row['fluid'])})" if row.get("fluid") else "")
            out.append({"product": label, "region": region, "cur": row.get("cur_prod") or 0, "theo": tp})
    return out


# ---------------------------------------------------------------- 找基地（要游戏）

def survey_region(nav, region: str, log=print) -> list[dict]:
    """地区总览里的每个分区点进去找基地，返回 [{name, has_base, title, main}]。
    默认视野找不到的分区（基地在视野外、图标和别的设备重叠）记 has_base=False，用户可以手动勾上。"""
    import navigate as nv

    out = []
    title = nav.open_map()
    if title is None:
        raise RuntimeError("按 M 后没读到地图标题：请确认游戏在大世界里")
    boxes = nav.open_overview(region, title[0])
    if boxes is None:
        raise RuntimeError(f"不认识的地区「{region}」，程序目前只认：{'、'.join(nv.REGION_TABS)}")
    ocr_names = nav.area_names(boxes)
    known = list(nv.OVERVIEW_POS.get(region, {}))  # 登记过固定位置的分区先按名单点（总览字小，OCR 常漏首墩/雪松林/藏剑谷）
    names = known + [n for n in ocr_names if not any(n in k or k in n for k in known)]
    log(f"  {region} 的分区：{'、'.join(names) or '（没认到）'}（OCR 认到 {'、'.join(ocr_names) or '无'}）")
    for name in names:
        entry = {"name": name, "has_base": False, "selected": False, "title": "", "main": False}
        if nav.click_area(boxes, name):
            real = nav.map_title()
            if real and real[1] != name:
                log(f"  总览里的「{name}」地图标题是「{real[1]}」，按标题记")
                entry["name"] = name = real[1]
            if nav.click_core():
                card = nav.ocr_text(nv.CARD_TITLE_ROI)
                entry.update(has_base=True, selected=True, title=card, main="协议核心" in card)
                log(f"  ✓ {name}：{card}")
                nav.key("esc")  # 关卡片
                time.sleep(0.8)
            else:
                known = inherited_config(name)
                if known:
                    log(f"  - {name}：默认视野里没找到基地图标；别的档案里这个分区有特例配置（{'、'.join(k for k in known if k != 'main')}），保存时会沿用")
                    entry.update(has_base=True, selected=True, title="（沿用已有档案的配置）", main=bool(known.get("main")))
                else:
                    log(f"  - {name}：默认视野里没找到基地图标")
        else:
            log(f"  - {name}：点不进去（地图标题不符）")
        out.append(entry)
        boxes = nav.back_to_overview(region)
        if boxes is None:
            log("  回不到地区总览，剩下的分区没看")
            break
    close_map(nav)
    return out


def close_map(nav) -> None:
    for _ in range(4):
        if not nav.map_open():
            break
        nav.key("esc")
        time.sleep(0.8)
    if nav.menu_open():
        nav.click(1217, 38)
        time.sleep(1.0)


def inherited_config(area: str) -> dict | None:
    """别的档案 areas.json 里同名分区的特例配置（drag / icon / menu_pick / single_teleport / extent / main）。
    同一个分区的基地位置、图标重叠情况和蓝图无关，新档案可以直接沿用。"""
    for name in bp.available():
        try:
            plan = bp.load(name)
        except Exception:
            continue
        cfg = plan.area_options(area)
        if cfg:
            return cfg
    return None


# ---------------------------------------------------------------- 写档案

def save_archive(draft: dict, brief: dict) -> str:
    """draft：{name, regions: {地区: [{name, main, selected}]}, targets: [{product, per_min, note, selected}], doc, overwrite}。
    只写 selected 为真的分区；主分区最多一个，没有就不标。"""
    name = clean_name(draft.get("name"))
    dest = archive_dir(name)
    if dest.exists() and not draft.get("overwrite"):
        raise FileExistsError(f"已有蓝图档案「{name}」")
    dest.mkdir(parents=True, exist_ok=True)
    areas: dict = {
        "_note": f"{time.strftime('%Y-%m-%d')} 由程序识别生成。分区写成对象可加特例：drag 先拖地图、icon 图标模板、"
                 "menu_pick 重叠菜单、single_teleport 单按钮卡片、extent 扫描范围。主分区放最后。"
    }
    for region, items in (draft.get("regions") or {}).items():
        chosen = [a for a in items if a.get("selected", a.get("has_base"))]
        if not chosen:
            continue
        chosen.sort(key=lambda a: bool(a.get("main")))  # 主分区放最后（地区级问题只在它那里计一次）
        items_out = []
        for a in chosen:
            cfg = {k: v for k, v in (inherited_config(a["name"]) or {}).items() if k != "main"}  # 沿用已有档案里同名分区的特例配置
            if a.get("main"):
                cfg["main"] = True
            items_out.append({"name": a["name"], **cfg} if cfg else a["name"])
        areas[region] = items_out
    if len(areas) == 1:
        raise ValueError("至少要有一个分区")
    targets = [
        {"product": t["product"], "per_min": t["per_min"], "note": t.get("note", "")}
        for t in draft.get("targets") or []
        if t.get("selected", True)
    ]
    playbook = {
        "doc": draft.get("doc") or f"{time.strftime('%Y-%m-%d')} 由程序从基准简报生成：targets 是只产不耗的最终产物和基准产量。",
        "targets": targets,
        "hints": [],
        "normal": [],
    }
    (dest / "areas.json").write_text(_dump(areas), encoding="utf-8")
    (dest / "playbook.json").write_text(_dump(playbook), encoding="utf-8")
    if not (dest / "tier_rules.json").exists():
        (dest / "tier_rules.json").write_text(
            _dump({"doc": "没有备路规则：诊断按参考简报和基准比（某产物在基准里就只跑部分理论值，现在不低于基准就算正常）", "rules": []}),
            encoding="utf-8",
        )
    regions = set(areas) - {"_note"}
    (dest / "reference_brief.json").write_text(
        json.dumps({r: rows for r, rows in brief.items() if r in regions}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    return name


def update_reference(name: str, brief: dict, merge: bool = False) -> int:
    """把一份新简报存为档案的参考简报（只存 areas.json 里有的地区）。返回存了几行。
    merge=True：和现有参考简报合并——同一物品取 理论产/耗、当前产/耗 的最大值，库存取新的。
    备路产物（低容、蓝铁瓶…）只在主路爆仓时才跑，单张快照常把它们抓成 0；同一蓝图多抓几次合并，基准才是「跑起来最好的样子」。
    换了蓝图要用替换（merge=False），旧蓝图的理论值混进来会被当成「消费者被断开」。"""
    d = archive_dir(name)
    if not d.is_dir():
        raise FileNotFoundError(f"没有蓝图档案「{name}」")
    plan = bp.load(name)
    regions = set(plan.areas) or set(brief)
    data = {r: rows for r, rows in brief.items() if r in regions}
    if merge and plan.reference_brief:
        data = merge_briefs(plan.reference_brief, data)
    (d / "reference_brief.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    return sum(len(v) for v in data.values())


def merge_briefs(old: dict, new: dict) -> dict:
    """按地区 + (名称, 灌装物) 对齐；数值字段取最大，库存和 OCR 辅助字段取新的；只在一边出现的行照抄。"""
    out: dict = {}
    for region in dict.fromkeys([*old, *new]):
        o = {(r["name"], r.get("fluid")): r for r in old.get(region) or []}
        n = {(r["name"], r.get("fluid")): r for r in new.get(region) or []}
        rows = []
        for key in dict.fromkeys([*o, *n]):
            a, b = o.get(key), n.get(key)
            if a and b:
                m = dict(b)
                for f in ("cur_prod", "cur_cons", "theo_prod", "theo_cons"):
                    m[f] = max(a.get(f) or 0, b.get(f) or 0)
                m["theo_cons_red"] = bool(a.get("theo_cons_red")) or bool(b.get("theo_cons_red"))
                rows.append(m)
            else:
                rows.append(dict(a or b))
        out[region] = rows
    return out
