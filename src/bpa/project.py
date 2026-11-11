"""蓝图分析项目：一套蓝图（若干据点 × 若干张共享蓝图）的分析过程和结果。

目录 debug/bpa/<项目>/：
  project.json                 名称、分组 {据点: [蓝图…]}、底账来源档案、各步状态
  bp/<蓝图>/devices.json       设备一览；size.json 蓝图尺寸；frames/ 预览帧；mosaic.png 拼图
           anchors.json        预览里认出的暗管、息壤供电桩（及数量自检）
           links.json          暗管连接对 + 自检状态；link_shots/ 单击截图
           logistics.json      物流件（分流器/汇流器/桥/准入口）+ 和设备一览的种类核对
  base/<据点>/transform.json   每张蓝图在基地里的旋转、平移（自动锚点）
             composite.png    据点拼合图；check.jpg 底账核对图；links.jpg 暗管连线图；logistics.jpg 物流件标注图
             placement.json   放置完整性检查（每张蓝图的设备是否都摆了）；placement.jpg 放置核对图
             logistics.json   物流件换算到拼合图坐标
             inputs_auto.json 输入端候选；inputs.jpg 带编号截图
  inputs.json                  作者按编号填的输入端 {据点: {"marks": [...]}}
  ledger/<据点>.json           这个项目自己扫的远景底账（据点拼合优先用它，没有再用蓝图档案里的）；ledger_scan/ 扫描截图
  gates.json                   全项目的准入口：图标分组（同组 = 同一种物品）、管道/物品（推断）；gate_icons.png 分组图

流程见 docs/蓝图分析器-设计.md 第 3 节。
"""

from __future__ import annotations

import colorsys
import json
import shutil
import time
import zipfile
from collections import Counter
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from paths import ROOT
from . import inputs as INP
from . import logistics as LG
from . import vision as V

BPA = ROOT / "debug" / "bpa"
FORMAT = "bpa-1"


def pdir(name: str) -> Path:
    return BPA / name


def _jload(p: Path, default=None):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def _jsave(p: Path, obj) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


# ------------------------------------------------------------------ 项目
def create(name: str, groups: dict[str, list[str]], archive: str = "") -> dict:
    """groups：{据点: [共享蓝图名…]}；archive：底账所在的蓝图档案（data/blueprints/<archive>/ledger/<据点>.json）。"""
    d = pdir(name)
    proj = _jload(d / "project.json", {}) or {}
    proj.update({"name": name, "groups": groups, "archive": archive, "format": FORMAT,
                 "created": proj.get("created") or time.strftime("%Y-%m-%d %H:%M:%S"), "updated": time.strftime("%Y-%m-%d %H:%M:%S")})
    proj.setdefault("steps", {})
    _jsave(d / "project.json", proj)
    return proj


def load(name: str) -> dict:
    proj = _jload(pdir(name) / "project.json")
    if proj is None:
        raise FileNotFoundError(f"没有蓝图分析项目「{name}」")
    return proj


BACKUP = ROOT / "debug" / "bpa_备份"  # 删除的项目挪到这里（不在 BPA 下：项目列表和巡检找拼合结果都不会再读到它）


def delete_info(name: str) -> dict:
    """删除前要提醒的：哪个蓝图档案在用这个项目的拼合结果，删了以后它的近景巡检还有没有别的来源。
    近景巡检的管网追踪 / 断水影响按 near_patrol.bpa_base_dir 找拼合结果：同一档案的其他项目 → 档案里导入的作者 zip。"""
    proj = load(name)
    archive = proj.get("archive") or ""
    bases = [b for b in proj.get("groups", {}) if (pdir(name) / "base" / b / "composite.png").exists()]
    others, imported, lost = [], [], []
    if archive:
        for p in list_projects():
            if p["name"] != name and (_jload(pdir(p["name"]) / "project.json", {}) or {}).get("archive") == archive:
                others.append(p["name"])
        from . import importer
        for b in bases:
            if importer.base_dir(archive, b) is not None:
                imported.append(b)
            elif not any((pdir(o) / "base" / b / "composite.png").exists() for o in others):
                lost.append(b)
    return {"name": name, "archive": archive, "bases": bases, "other_projects": others, "imported": imported, "lost": lost}


def delete(name: str) -> dict:
    """「删除」项目：整个目录挪进 debug/bpa_备份/<项目>_<时间>/，不真删（挪回 debug/bpa/ 就恢复）。→ {backup}"""
    import shutil
    d = pdir(name)
    if not (d / "project.json").exists():
        raise FileNotFoundError(f"没有蓝图分析项目「{name}」")
    BACKUP.mkdir(parents=True, exist_ok=True)
    dest = BACKUP / f"{name}_{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.move(str(d), str(dest))
    return {"backup": str(dest)}


def list_projects() -> list[dict]:
    out = []
    if BPA.exists():
        for d in sorted(BPA.iterdir()):
            p = _jload(d / "project.json")
            if p:
                out.append(status(p["name"]))
    return out


def status(name: str) -> dict:
    """每张蓝图、每个据点做到哪一步了（界面显示用）。"""
    proj = load(name)
    d = pdir(name)
    bps = {}
    for base, names in proj["groups"].items():
        for b in names:
            bd = d / "bp" / b
            anc = _jload(bd / "anchors.json", {}) or {}
            lk = _jload(bd / "links.json", {}) or {}
            st = lk.get("status", {})
            lgj = _jload(bd / "logistics.json", {}) or {}
            bps[b] = {"base": base, "devices": (bd / "devices.json").exists(), "mosaic": (bd / "mosaic.png").exists(),
                      "size_check": anc.get("size_check"), "pipe_check": anc.get("pipe_check"),
                      "pipes": anc.get("pipe_count", 0),
                      "links": {k: sum(1 for s in st.values() if s == k) for k in ("双向", "单向", "矛盾", "未配")} if st else None,
                      "logistics": {t: sum(1 for o in lgj.get("items", []) if o["type"] == t) for t in LG.LG_TYPES + [LG.GATE]} if lgj else None,
                      "logistics_bad": lgj.get("presence_bad") if lgj else None}
    bases = {}
    inputs = _jload(d / "inputs.json", {}) or {}
    for base in proj["groups"]:
        bd = d / "base" / base
        tf = _jload(bd / "transform.json", {}) or {}
        lp = ledger_path(name, base)
        lj = _jload(lp, {}) if lp else {}
        bases[base] = {"composite": (bd / "composite.png").exists(), "fit": tf.get("fit", {}),
                       "ledger": ({"source": "项目扫描" if lp.parent.parent == d else "蓝图档案", "devices": len(lj.get("devices", [])),
                                   "updated": lj.get("updated", "")} if lj else None),
                       "inputs_auto": len((_jload(bd / "inputs_auto.json", []) or [])),
                       "inputs_filled": len((inputs.get(base) or {}).get("marks", [])),
                       "placement": (_jload(bd / "placement.json", {}) or {}).get("summary"),
                       "error": tf.get("error", "")}
    return {"name": name, "archive": proj.get("archive", ""), "groups": proj["groups"], "blueprints": bps, "bases": bases,
            "updated": proj.get("updated")}


def import_preview(name: str, bp: str, src: Path) -> None:
    """把已有的预览采集（frames/、devices.json）拷进项目，免得重新进游戏（皇帝产线用）。"""
    dst = pdir(name) / "bp" / bp
    (dst / "frames").mkdir(parents=True, exist_ok=True)
    for f in sorted((src / "frames").glob("*.png")):
        shutil.copy2(f, dst / "frames" / f.name)
    if (src / "devices.json").exists():
        shutil.copy2(src / "devices.json", dst / "devices.json")


# ------------------------------------------------------------------ 第 ①–④ 步：读蓝图（离线部分）
def process_blueprint(name: str, bp: str, size: tuple[int, int] | None = None, log=print) -> dict:
    """拼接 → 尺寸自检 → 认暗管/供电桩 → 数量自检。frames/ 和 devices.json 已就位。"""
    bd = pdir(name) / "bp" / bp
    frames = [V.rd(p) for p in sorted((bd / "frames").glob("*.png"))]
    if not frames:
        raise RuntimeError(f"{bp}：没有预览帧")
    mos = V.stitch(frames)
    V.wr(bd / "mosaic.png", mos)
    if size is None:
        s = _jload(bd / "size.json")
        size = tuple(s) if s else None
    ok_size, msg_size = V.size_ok(mos, size)
    found = V.detect(mos)
    devices = _jload(bd / "devices.json", {}) or {}
    pipes = [f for f in found if f["name"] in V.PIPE_NAMES]
    ok_pipe, got, exp = V.pipe_count_check(pipes, devices)
    res = {"size": size, "size_check": ok_size, "size_msg": msg_size, "pipe_check": ok_pipe, "pipe_got": got, "pipe_expected": exp,
           "pipe_count": len(pipes), "pylons": sum(1 for f in found if f["name"] == "息壤供电桩"), "anchors": found}
    _jsave(bd / "anchors.json", res)
    items = LG.detect(mos)
    bad = LG.presence_check(items, devices)
    _jsave(bd / "logistics.json", {"items": items, "presence_bad": bad})
    log(f"  {bp}：{msg_size}{'' if ok_size else '  ← 尺寸不对，拼接可能错格'}；暗管 {len(pipes)} 个"
        f"{'，和设备一览一致' if ok_pipe else f'，和设备一览不一致：识别 {got}，一览 {exp}'}；息壤供电桩 {res['pylons']} 个")
    cnt = {t: sum(1 for o in items if o["type"] == t) for t in LG.LG_TYPES + [LG.GATE]}
    log("  物流件：" + "，".join(f"{t} {n}" for t, n in cnt.items() if n)
        + ("；种类和设备一览一致" if not bad else "；和设备一览不一致：" + "；".join(bad)))
    return res


def read_blueprints(name: str, game, only: list[str] | None = None, log=print, cancelled=lambda: False) -> None:
    """游戏内：逐张打开预览 → 读尺寸、设备一览 → 采集 → 离线处理 → 暗管逐个单击配对 → 关预览。"""
    proj = load(name)
    todo = [b for names in proj["groups"].values() for b in names if not only or b in only]
    for i, bp in enumerate(todo, 1):
        if cancelled():
            break
        log(f"== [{i}/{len(todo)}] {bp}")
        bd = pdir(name) / "bp" / bp
        bd.mkdir(parents=True, exist_ok=True)
        game.open_preview(bp)
        try:
            img = game.shot()
            size = game.read_size(img)
            _jsave(bd / "size.json", list(size) if size else None)
            devs = game.read_devices(img)
            _jsave(bd / "devices.json", devs)
            log(f"  蓝图尺寸 {size}；设备一览 {len(devs)} 种")
            if (bd / "frames").exists():
                shutil.rmtree(bd / "frames")
            game.capture(bd)
            res = process_blueprint(name, bp, size, log)
            pipes = [a for a in res["anchors"] if a["name"] in V.PIPE_NAMES]
            if pipes and not cancelled():
                log(f"  暗管 {len(pipes)} 个：逐个单击确认连接")
                mos = V.rd(bd / "mosaic.png")
                f0 = V.rd(sorted((bd / "frames").glob("*.png"))[0])
                part, note = game.link_pipes(mos, f0, pipes, bd / "link_shots", cancelled)
                st = V.link_status(len(pipes), part)
                _jsave(bd / "links.json", {"pipes": pipes, "partner": {str(k): v for k, v in part.items()},
                                           "status": {str(k): v for k, v in st.items()}, "note": {str(k): v for k, v in note.items()}})
        finally:
            game.close_preview()


# ------------------------------------------------------------------ 第 ⓪ 步：扫底账（游戏内）
def ledger_path(name: str, base: str) -> Path | None:
    """据点拼合用的底账：项目自己扫的优先，没有再用蓝图档案里的（巡检用的那份，只读）。"""
    own = pdir(name) / "ledger" / f"{base}.json"
    if own.exists():
        return own
    arch = load(name).get("archive")
    p = ROOT / "data" / "blueprints" / (arch or "_") / "ledger" / f"{base}.json"
    return p if arch and p.exists() else None


def _extent(name: str, base: str):
    """据点范围：蓝图档案 areas.json 登记过就用，没有返回 None（扫描按「画面不动 / 没标签」判到头）。"""
    arch = load(name).get("archive")
    if not arch:
        return None
    try:
        import blueprint as bpm
        e = bpm.load(arch).area_options(base).get("extent")
        return tuple(e) if e else None
    except Exception:
        return None


def scan_ledger(name: str, base: str, patrol, log=print, cancelled=lambda: False) -> Path:
    """游戏内扫这个据点的远景底账（人要在这个据点的俯瞰里），离线识别后存进项目 ledger/<据点>.json。"""
    from . import ledger_scan as LS
    out = pdir(name) / "ledger_scan" / f"{base}_{time.strftime('%Y%m%d-%H%M%S')}"
    ext = _extent(name, base)
    log(f"== 扫底账：{base}" + (f"（档案登记范围 {ext[0]}×{ext[1]}）" if ext else "（档案里没登记范围，扫到画面不动 / 没有标签为止）"))
    LS.scan(patrol, base, out, ext, log, cancelled)
    if cancelled():
        return out
    log("== 识别标签（离线）")
    near = LS.recognize(out, log)
    return LS.save_ledger("", base, near, out, None, dest=pdir(name) / "ledger" / f"{base}.json", log=log)


# ------------------------------------------------------------------ 第 ⑤ 步：据点拼合（离线）
def group_gates(name: str, log=print) -> list[dict]:
    """全项目的准入口按图标分组（同组 = 同一种物品），按图标形态定管道/物品。写 gates.json、gate_icons.png。
    形态（液/气/固）先查项目图标库 <项目>/items/（作者标过的），再查全局 resource/bpa/items/。"""
    proj = load(name)
    d = pdir(name)
    gates, icons = [], []
    for names in proj["groups"].values():
        for bp in names:
            b = d / "bp" / bp
            lgj = _jload(b / "logistics.json")
            if not lgj or not (b / "mosaic.png").exists():
                continue
            mos = V.rd(b / "mosaic.png")
            for i, o in enumerate(lgj["items"]):
                if o["type"] == LG.GATE:
                    gates.append({"bp": bp, "i": i, "p": o["p"], "kind_local": o.get("kind_local", "?"),
                                  "kind_basis": o.get("kind_basis", ""), "belt_touch": o.get("belt_touch", False)})
                    icons.append(LG.gate_icon(mos, o["p"]))
    grp = LG.group_icons(icons)
    reps = {}
    for g, k, ic in zip(gates, grp, icons):
        g["icon_group"] = k
        reps.setdefault(k, ic)
    meta = {}
    for k, ic in reps.items():
        # 名字/形态的来源，先到先得：作者标的（项目库）> 全局库里有名字的（用户确认过）> AKEData 比对有把握的 > 全局库只有形态的
        cands = LG.ake_candidates(ic)
        pst, pnm = LG.lookup_item(ic, d / "items") if (d / "items").exists() else (None, "")
        gst, gnm = LG.lookup_item(ic)
        if pst:
            m = {"state": pst, "item": pnm, "source": "作者标注"}
        elif gst and gnm:
            m = {"state": gst, "item": gnm, "source": "图标库"}
        elif LG.ake_confident(cands):
            m = {"state": cands[0]["state"], "item": cands[0]["name"], "source": "AKEData 比对"}
        elif gst:
            m = {"state": gst, "item": "", "source": "图标库（只有形态）"}
        else:
            m = {"state": None, "item": "", "source": ""}
        m["candidates"] = cands
        meta[k] = m
    LG.decide_kinds(gates, meta)
    groups = {}
    for g in gates:
        groups.setdefault(g["icon_group"], []).append(g)
    _jsave(d / "gates.json", {"gates": gates, "groups": {
        str(k): {"n": len(v), "state": meta[k]["state"], "item": meta[k]["item"], "source": meta[k]["source"],
                 "candidates": meta[k]["candidates"], "kind": v[0]["kind"],
                 "kind_how": v[0]["kind_how"], "conflict": v[0]["kind_conflict"],
                 "maybe_item_below": sum(1 for x in v if x["maybe_item_below"])} for k, v in groups.items()}})
    if (d / "gate_groups").exists():
        shutil.rmtree(d / "gate_groups")
    (d / "gate_groups").mkdir(parents=True)
    for k, ic in reps.items():
        V.wr(d / "gate_groups" / f"G{k}.png", ic)
    by_bp = {}
    for g in gates:
        by_bp.setdefault(g["bp"], []).append(g)
    for bp, gs in by_bp.items():
        f = d / "bp" / bp / "logistics.json"
        lgj = _jload(f)
        for g in gs:
            lgj["items"][g["i"]].update({k: g[k] for k in ("icon_group", "kind", "kind_conflict", "kind_how", "maybe_item_below")})
        _jsave(f, lgj)
    if icons:
        _gate_sheet(gates, icons, meta).save(d / "gate_icons.png")
    nunk = sum(1 for k in groups if meta[k]["state"] is None)
    log(f"== 准入口 {len(gates)} 个，按图标分 {len(groups)} 种物品；管道 {sum(1 for g in gates if g['kind'] == '管道')}、"
        f"物品 {sum(1 for g in gates if g['kind'] == '物品')}、未定 {sum(1 for g in gates if g['kind'] == '?')}"
        + (f"；{nunk} 种图标还不知道是液体/气体/固体，请在「准入口图标分组」里标" if nunk else "")
        + f"；底下可能叠着物品准入口的 {sum(1 for g in gates if g['maybe_item_below'])} 个（到俯瞰里点开看）")
    return gates


def label_gate_groups(name: str, labels: dict) -> None:
    """作者按图标组标形态/名字：{组号: {state: 液/气/固, item: 名字}}。存进项目图标库后重算。"""
    d = pdir(name)
    for k, v in labels.items():
        st = v.get("state")
        f = d / "gate_groups" / f"G{k}.png"
        if st not in LG.STATES or not f.exists():
            continue
        ic = V.rd(f)
        nm = (v.get("item") or "").strip()
        LG.forget_item(ic, d / "items")
        if LG.lookup_item(ic)[0:2] != (st, nm):  # 和全局图标库一样的不用存进项目库（免得以后全局库补了名字，项目库还压着旧的）
            LG.save_item(ic, st, nm, d / "items")
    fit_bases(name, log=lambda *_: None)


def _gate_sheet(gates, icons, meta) -> Image.Image:
    groups = {}
    for g, ic in zip(gates, icons):
        groups.setdefault(g["icon_group"], []).append((g, ic))
    S, W = 46, 16
    im = Image.new("RGB", (190 + S * W, S * len(groups)), "white")
    dr = ImageDraw.Draw(im)
    F = _font(14)
    for r, (k, mem) in enumerate(sorted(groups.items())):
        g0, m = mem[0][0], meta[k]
        st = {"液": "液体", "气": "气体", "固": "固体"}.get(m["state"], "形态未知")
        dr.text((4, r * S + 4), f"G{k} {m['item'] or ''}（{len(mem)} 个）", fill=(0, 0, 0), font=F)
        dr.text((4, r * S + 24), f"{st} → {g0['kind'] if g0['kind'] != '?' else '未定'}{'准入口' if g0['kind'] != '?' else ''}",
                fill=(200, 0, 0) if m["state"] is None else (60, 60, 60), font=F)
        for c, (g, ic) in enumerate(mem[:W]):
            t = Image.fromarray(cv2.cvtColor(cv2.resize(ic, (S - 2, S - 2), interpolation=cv2.INTER_NEAREST), cv2.COLOR_BGR2RGB))
            im.paste(t, (190 + c * S, r * S + 1))
            if g["maybe_item_below"]:
                ImageDraw.Draw(im).rectangle([190 + c * S, r * S + 1, 190 + c * S + S - 3, r * S + S - 2], outline=(230, 120, 0), width=2)
    return im


def fit_bases(name: str, log=print) -> None:
    proj = load(name)
    group_gates(name, log)
    for base, names in proj["groups"].items():
        bd = pdir(name) / "base" / base
        bd.mkdir(parents=True, exist_ok=True)
        led_p = ledger_path(name, base)
        led = _jload(led_p) if led_p else None
        if not led:
            msg = "没有底账：请先在游戏里对这个据点做「扫底账」，才能把蓝图拼到基地上"
            _jsave(bd / "transform.json", {"error": msg})
            log(f"== {base}：{msg}")
            continue
        items, fit = [], {}
        for bp in names:
            b = pdir(name) / "bp" / bp
            anc = _jload(b / "anchors.json")
            if not anc or not (b / "mosaic.png").exists():
                log(f"  {bp}：还没读这张蓝图，跳过")
                continue
            r = V.fit_blueprint(anc["anchors"], led["devices"])
            if r is None:
                log(f"  {bp}：认不出锚点（没有暗管和息壤供电桩），跳过")
                continue
            sure = r["inliers"] >= max(3, 0.7 * r["total"]) and r["inliers"] >= r["second"] + 2
            fit[bp] = {k: r[k] for k in ("rot", "inliers", "total", "second", "resid")} | {"ok": sure}
            log(f"  {bp}：旋转 {r['rot']}°，锚点 {r['inliers']}/{r['total']} 对上（次优 {r['second']}），残差 {r['resid']:.1f} 远景 px"
                + ("" if sure else "  ← 不确定，请看核对图"))
            items.append({"bp": bp, "mosaic": V.rd(b / "mosaic.png"), "rot": r["rot"], "t": r["t"]})
        if not items:
            continue
        canvas, tf = V.composite(items)
        tf["fit"] = fit
        _jsave(bd / "transform.json", tf)
        V.wr(bd / "composite.png", canvas)
        _check_image(canvas, tf, led["devices"]).save(bd / "check.jpg", quality=88)
        _links_image(name, canvas, tf, items).save(bd / "links.jpg", quality=88)
        lg_canvas = _logistics_on_canvas(name, tf, items)
        _jsave(bd / "logistics.json", lg_canvas)
        _logistics_image(canvas, lg_canvas).save(bd / "logistics.jpg", quality=88)
        ginfo = {int(k): v for k, v in ((_jload(pdir(name) / "gates.json", {}) or {}).get("groups") or {}).items()}
        pipes_canvas = []
        for it in items:
            anc = _jload(pdir(name) / "bp" / it["bp"] / "anchors.json", {}) or {}
            for a in anc.get("anchors", []):
                if a["name"] in V.PIPE_NAMES:
                    px, py = V.preview_to_canvas(tf, it["bp"], it["mosaic"].shape, *a["p"])
                    pipes_canvas.append({"name": a["name"], "p": [px, py]})
        try:  # 物流件方向和连接（任务计划第 4 项）→ flow.json / flow.jpg
            from . import flow as FL
            fr = FL.run_base(bd, led)
            log(f"   物流连接 {len(fr['links'])} 条（有方向 {sum(1 for l in fr['links'] if l['dir'])}），要核对 {len(fr['issues'])} 处")
        except Exception as e:  # 不影响后面的输入端/放置检查
            log(f"   物流连接分析出错：{e}")
            fr = None
        if fr is not None:
            try:  # 设计分析计算器（任务计划第 3 项）→ design.json / design.jpg
                from . import design as DS
                arch = load(name).get("archive")
                arch_dir = ROOT / "data" / "blueprints" / arch if arch else None
                dr = DS.run_base(bd, arch_dir if arch_dir and arch_dir.exists() else None, base, ledger=led_p)
                sm = dr["summary"]
                log(f"   设计分析：{sm['devices']} 台，" + "、".join(f"{k} {v}" for k, v in sm["states"].items() if v)
                    + f"；推断连线 {sm['inferred_links']} 条，来料经过推断连线的 {sm['via_inferred']} 台")
            except Exception as e:
                log(f"   设计分析出错：{type(e).__name__}: {e}")
        marks, dropped = INP.from_gates(canvas, lg_canvas, ginfo, pipes_canvas)
        _jsave(bd / "inputs_dropped.json", dropped)
        _jsave(bd / "inputs_auto.json", marks)
        log(f"== {base}：拼合 {len(items)} 张，画布 {canvas.shape[1] / V.CELL:.0f}×{canvas.shape[0] / V.CELL:.0f} 格；"
            f"物流件 {len(lg_canvas)} 个；输入端候选 {len(marks)} 个（边上的准入口另有 {len(dropped)} 个不算输入）")
    check_placement(name, log)


def check_placement(name: str, log=print) -> dict:
    """第 4.9 节放置完整性检查（离线、只读）：每个据点写 base/<据点>/placement.json、placement.jpg。返回 {据点: 结果摘要}。"""
    from . import placement as PL
    proj = load(name)
    out = {}
    for base, names in proj["groups"].items():
        bd = pdir(name) / "base" / base
        tf = _jload(bd / "transform.json")
        led_p = ledger_path(name, base)
        if not tf or "bps" not in tf or not led_p:
            log(f"== {base}：还没拼合或没有底账，跳过放置检查")
            continue
        led = _jload(led_p)
        bps = {}
        for bp in names:
            b = pdir(name) / "bp" / bp
            if bp not in tf["bps"] or not (b / "mosaic.png").exists():
                continue
            bps[bp] = {"mosaic": V.rd(b / "mosaic.png"), "devices": _jload(b / "devices.json", {}) or {},
                       "anchors": (_jload(b / "anchors.json", {}) or {}).get("anchors", []),
                       "logistics": (_jload(b / "logistics.json", {}) or {}).get("items", [])}
        canvas = V.rd(bd / "composite.png")
        res = PL.check_base(bps, tf, led, canvas)
        res_saved = {k: v for k, v in res.items() if k != "marks"} | {"ledger": str(led_p.relative_to(ROOT)) if led_p.is_relative_to(ROOT) else str(led_p)}
        _jsave(bd / "placement.json", res_saved)
        PL.draw(canvas, tf, res).save(bd / "placement.jpg", quality=88)
        log(f"== {base}：放置检查 {res['summary']}")
        for bp, r in res["bps"].items():
            if r["diff"]:
                log(f"  {bp}：" + "、".join(f"{k} {'+' if v > 0 else ''}{v}" for k, v in sorted(r["diff"].items()))
                    + f"（预览图标 {r['marks_matched']}/{r['marks']} 配上底账）")
            else:
                log(f"  {bp}：设备一览 {sum(r['expected'].values())} 台全部在底账里（预览图标 {r['marks_matched']}/{r['marks']} 配上底账）")
        if res["orphans"]:
            log(f"  不属于任何蓝图的底账设备 {len(res['orphans'])} 台：" + "、".join(f"{k}×{v}" for k, v in Counter(o['name'] for o in res['orphans']).most_common()))
        out[base] = res_saved
    return out


def _logistics_on_canvas(name, tf, items) -> list[dict]:
    """各蓝图的物流件换算到据点拼合图坐标。"""
    out = []
    for it in items:
        lgj = _jload(pdir(name) / "bp" / it["bp"] / "logistics.json")
        if not lgj:
            continue
        for o in lgj["items"]:
            x, y = V.preview_to_canvas(tf, it["bp"], it["mosaic"].shape, *o["p"])
            out.append(dict(o, bp=it["bp"], p_bp=o["p"], p=[int(round(x)), int(round(y))]))
    return out


LG_COLORS = {"分流器": (230, 130, 0), "汇流器": (0, 150, 60), "物流桥": (120, 120, 120),
             "管道分流器": (200, 60, 160), "管道汇流器": (20, 110, 220), "管道桥": (80, 80, 80)}
LG_SHORT = {"分流器": "分", "汇流器": "汇", "物流桥": "桥", "管道分流器": "管分", "管道汇流器": "管汇", "管道桥": "管桥"}


def _logistics_image(canvas, lg) -> Image.Image:
    pil = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = _font(13)
    for o in lg:
        x, y = o["p"]
        if o["type"] == LG.GATE:
            k = o.get("kind", "?")
            col = (30, 90, 230) if k == "管道" else (230, 120, 0) if k == "物品" else (150, 150, 150)
            dr.ellipse([x - 19, y - 19, x + 19, y + 19], outline=col, width=4)
            if o.get("maybe_item_below"):  # 底下可能还叠着物品准入口：外面再套一圈橙色虚圈
                for a0 in range(0, 360, 30):
                    dr.arc([x - 26, y - 26, x + 26, y + 26], a0, a0 + 15, fill=(230, 120, 0), width=4)
            t = f"G{o.get('icon_group', '?')}" + ("" if k != "?" else "?")
            w = dr.textlength(t, font=F)
            dr.rounded_rectangle([x + 10, y - 30, x + 16 + w, y - 12], 4, fill=col)
            dr.text((x + 13, y - 30), t, fill="white", font=F)
        else:
            col = LG_COLORS[o["type"]]
            dr.rectangle([x - 17, y - 17, x + 17, y + 17], outline=col, width=3)
            t = LG_SHORT[o["type"]]
            w = dr.textlength(t, font=F)
            dr.rectangle([x - 17, y + 17, x - 13 + w, y + 35], fill=col)
            dr.text((x - 15, y + 17), t, fill="white", font=F)
    return pil


_F = None


def _font(sz=15, bold=True):
    return ImageFont.truetype(r"C:\Windows\Fonts\msyhbd.ttc" if bold else r"C:\Windows\Fonts\msyh.ttc", sz)


def _check_image(canvas, tf, ledger_devs) -> Image.Image:
    pil = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB))
    dr = ImageDraw.Draw(pil)
    F = _font(15)
    for dv in ledger_devs:
        cx, cy = V.far_to_canvas(tf, dv["x"], dv["y"])
        dr.ellipse([cx - 6, cy - 6, cx + 6, cy + 6], fill=(230, 0, 0))
        dr.text((cx + 7, cy - 9), dv["name"][:3], fill=(200, 0, 0), font=F)
    return pil


def _links_image(name, canvas, tf, items) -> Image.Image:
    pil = Image.fromarray(cv2.cvtColor(canvas, cv2.COLOR_BGR2RGB)).convert("RGBA")
    ov = Image.new("RGBA", pil.size, (0, 0, 0, 0))
    dr = ImageDraw.Draw(ov)
    F = _font(22)
    n = 0
    for it in items:
        lk = _jload(pdir(name) / "bp" / it["bp"] / "links.json")
        if not lk:
            continue
        pipes, part, st = lk["pipes"], {int(k): v for k, v in lk["partner"].items()}, lk["status"]
        seen = set()
        for i, j in part.items():
            key = tuple(sorted((i, j)))
            if key in seen:
                continue
            seen.add(key)
            n += 1
            both = st.get(str(i)) == "双向"
            r, g, b = [int(255 * v) for v in colorsys.hsv_to_rgb((n * 0.137) % 1, 0.85, 0.85)]
            p1 = V.preview_to_canvas(tf, it["bp"], it["mosaic"].shape, *pipes[i]["p"])
            p2 = V.preview_to_canvas(tf, it["bp"], it["mosaic"].shape, *pipes[j]["p"])
            dr.line([*p1, *p2], fill=(r, g, b, 230), width=6 if both else 3)
            for p in (p1, p2):
                dr.ellipse([p[0] - 14, p[1] - 14, p[0] + 14, p[1] + 14], outline=(r, g, b, 255), width=5)
            m = ((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2)
            t = str(n)
            w = dr.textlength(t, font=F)
            dr.rounded_rectangle([m[0] - 4, m[1] - 14, m[0] + w + 6, m[1] + 16], 6, fill=(r, g, b, 255))
            dr.text((m[0] + 1, m[1] - 14), t, fill="white", font=F)
    return Image.alpha_composite(pil, ov).convert("RGB")


# ------------------------------------------------------------------ 输入端（作者按编号填写）
def inputs_view(name: str, base: str, scale: float = 1.0) -> dict:
    """返回 {marks, image(base64 jpg), width, height, scale}。已填过的用已填的，没填过的用自动候选。"""
    import base64
    import io
    bd = pdir(name) / "base" / base
    canvas = V.rd(bd / "composite.png")
    saved = ((_jload(pdir(name) / "inputs.json", {}) or {}).get(base) or {}).get("marks")
    marks = saved if saved is not None else (_jload(bd / "inputs_auto.json", []) or [])
    # 贴边但判断不算输入的准入口：已经被作者加回（和某个标记重合）的不再显示
    dropped = [d for d in (_jload(bd / "inputs_dropped.json", []) or [])
               if not any(abs(m["p"][0] - d["p"][0]) < 15 and abs(m["p"][1] - d["p"][1]) < 15 for m in marks)]
    for i, d in enumerate(dropped):
        d["tag"] = chr(ord("A") + i % 26)
    im = INP.numbered_image(canvas, marks, scale, dropped)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return {"marks": marks, "dropped": dropped, "image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            "width": im.size[0], "height": im.size[1], "scale": scale}


def save_inputs(name: str, base: str, marks: list[dict]) -> None:
    """marks：[{no, p:[x,y], item, ports?, rate, merged, via, source, note}]，p 是拼合图坐标。"""
    p = pdir(name) / "inputs.json"
    allin = _jload(p, {}) or {}
    allin[base] = {"marks": marks, "saved": time.strftime("%Y-%m-%d %H:%M:%S")}
    _jsave(p, allin)
    bd = pdir(name) / "base" / base
    if (bd / "composite.png").exists():
        INP.numbered_image(V.rd(bd / "composite.png"), marks).save(bd / "inputs.jpg", quality=88)


# ------------------------------------------------------------------ 作者确认（任务计划第 1 项）
ACTUALS = ("满速", "间歇", "平时空闲（设计如此）", "没在用")  # 作者说「不对」时选的实际情况


def device_key(row: dict) -> str:
    """设计分析一行 → 确认记录的键：设备名@底账远景坐标（底账重扫后坐标会变，那时要重新确认）。"""
    return f"{row['device']}@{row['x']},{row['y']}"


def link_key(lk: dict) -> str:
    return f"{lk['from']}→{lk['to']}"


def load_confirm(name: str) -> dict:
    """confirm.json：{据点: {devices: {键: {verdict: ok/wrong, actual, note, at}}, links: {键: {...}}}}。"""
    return _jload(pdir(name) / "confirm.json", {}) or {}


def save_confirm(name: str, base: str, kind: str, entries: dict) -> dict:
    """kind = devices / links；entries = {键: {verdict, actual?, note?} 或 None（撤销）}。返回这个据点的确认进度。"""
    if kind not in ("devices", "links"):
        raise ValueError(f"不认识的确认类别：{kind}")
    allc = load_confirm(name)
    sect = allc.setdefault(base, {}).setdefault(kind, {})
    now = time.strftime("%Y-%m-%d %H:%M:%S")
    for k, v in entries.items():
        if v is None or not v.get("verdict"):
            sect.pop(k, None)
            continue
        if v["verdict"] not in ("ok", "wrong"):
            raise ValueError(f"确认结果只能是 ok / wrong：{v['verdict']}")
        if v["verdict"] == "wrong" and kind == "devices" and v.get("actual") not in ACTUALS:
            raise ValueError(f"选「不对」时要选实际情况（{' / '.join(ACTUALS)}）")
        sect[k] = {"verdict": v["verdict"], "actual": v.get("actual") or "", "note": (v.get("note") or "").strip(), "at": now}
    allc["updated"] = now
    _jsave(pdir(name) / "confirm.json", allc)
    return confirm_progress(name, base)


def confirm_progress(name: str, base: str) -> dict:
    """这个据点设计分析里多少台设备、多少条推断连线确认过（只数现在 design.json 里还有的）。"""
    dj = _jload(pdir(name) / "base" / base / "design.json", {}) or {}
    c = load_confirm(name).get(base, {})
    dkeys = [device_key(r) for r in dj.get("devices", [])]
    lkeys = [link_key(lk) for lk in dj.get("inferred_links", [])]
    dv, lv = c.get("devices", {}), c.get("links", {})
    return {"devices": [sum(1 for k in dkeys if k in dv), len(dkeys)], "links": [sum(1 for k in lkeys if k in lv), len(lkeys)],
            "wrong": sum(1 for k in dkeys if dv.get(k, {}).get("verdict") == "wrong") + sum(1 for k in lkeys if lv.get(k, {}).get("verdict") == "wrong")}


# ------------------------------------------------------------------ 导出 zip
def export(name: str, dest: str | Path, raw_quality: int = 92, log=print, lite: bool = False) -> Path:
    """打包 <产线>.bpa.zip（docs/蓝图分析器-设计.md 第 5 节）。原始截图默认 jpg，校准关键帧（每张第 00 帧）存 png。
    lite：给玩家的精简包（2026-11-10 灰度包 235 MB 太大）——只带导入后巡检要用的：底账、配方表、准入口限速、拼合结果、近景大图、
    扫描的 scan.json 和两张角落图（远景↔近景配准）。不带建底账的逐屏截图（有近景大图就用不上）、读蓝图的预览原始帧（导入时本来就跳过）、
    核对用的标注图。完整包留给开发核对 / 纠错。"""
    proj = load(name)
    d = pdir(name)
    dest = Path(dest)
    checks = status(name)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        arch = proj.get("archive", "")
        arch_dir = ROOT / "data" / "blueprints" / arch if arch else None
        # 据点所在地区和巡检特例配置（导入后深入巡检要按地区传送）：从底账所在的蓝图档案的 areas.json 抄这几个据点的
        areas = {}
        araw = _jload(arch_dir / "areas.json", {}) if arch_dir else {}
        for region, items in (araw or {}).items():
            if region.startswith("_"):
                continue
            for a in items:
                nm = a["name"] if isinstance(a, dict) else a
                if nm in proj["groups"]:
                    areas.setdefault(region, []).append(a)
        z.writestr("manifest.json", json.dumps({"format": FORMAT, "name": name, "generated": time.strftime("%Y-%m-%d %H:%M:%S"), "lite": lite,
                                                "groups": proj["groups"], "archive": arch, "areas": areas, "checks": checks,
                                                "confirm": {b: confirm_progress(name, b) for b in proj["groups"]}},
                                               ensure_ascii=False, indent=1))
        z.writestr("confirm.json", json.dumps(load_confirm(name), ensure_ascii=False, indent=1))
        for base in proj["groups"]:  # 档案里游戏内读到的配方表、准入口限速（导入后巡检和设计分析都要用）
            for src, dst in ((arch_dir / "ledger" / f"{base}_recipes.json" if arch_dir else None, f"{base}/recipes.json"),
                             (arch_dir / "ledger" / "gate_limits" / f"{base}.json" if arch_dir else None, f"{base}/gate_limits.json")):
                if src is not None and src.exists():
                    z.write(src, dst)
        z.writestr("inputs.json", json.dumps(_jload(d / "inputs.json", {}) or {}, ensure_ascii=False, indent=1))
        for f in ("gates.json", "gate_icons.png"):
            if (d / f).exists():
                z.write(d / f, f)
        for base, names in proj["groups"].items():
            bd = d / "base" / base
            lp = ledger_path(name, base)
            if lp:
                z.write(lp, f"{base}/ledger.json")
            scans = sorted((d / "ledger_scan").glob(f"{base}_*")) if (d / "ledger_scan").exists() else []
            own = (_jload(lp, {}) or {}).get("scan", {}).get("dir") if lp else None  # 底账自己记的那次扫描（远景↔近景配准要用同一次的角落图）
            if own and (ROOT / own).is_dir():
                scans = [ROOT / own]
            if scans:  # 底账对应的那次扫描（没记就取最近一次）的原始截图（jpg），校准/纠错用
                sd = scans[-1]
                try:  # 近景大图（无损 png + 配准）：导入方近景巡检核实断口、认污水管直接用它（任务计划第 2 项）
                    import tempfile
                    from . import nearmap as NM
                    import near_patrol as NP
                    with tempfile.TemporaryDirectory() as td:
                        NM.save(NM.build(sd, scale=0.5), NP.calib(sd), Path(td) / "nearmap.png")
                        z.write(Path(td) / "nearmap.png", f"{base}/nearmap.png")
                        z.write(Path(td) / "nearmap.json", f"{base}/nearmap.json")
                except Exception as e:  # noqa: BLE001
                    log(f"  {base}：近景大图没导出（{type(e).__name__}: {e}），导入方按 jpg 截图现拼")
                for f in ("scan.json", "ledger_near.json"):
                    if (sd / f).exists():
                        z.write(sd / f, f"{base}/raw/ledger_scan/{f}")
                for fr in ([sd / "far_corner.png", sd / "near_corner.png"] if lite else sorted(sd.glob("*.png")) + sorted((sd / "near").glob("*.png"))):
                    if not fr.exists():
                        continue
                    if lite:  # 角落图原样存 png（配准用，不压 jpg）
                        z.write(fr, f"{base}/raw/ledger_scan/{fr.name}")
                        continue
                    # 导入方核实断口、认污水管用上面那张无损近景大图；这些 jpg 只做校准/纠错（jpg 里 6 px 细管的颜色会糊，
                    # 2026-11-10 导入演练只用 jpg 现拼时武陵城管网闭合 24/24 → 21/24）
                    ok, buf = cv2.imencode(".jpg", V.rd(fr), [cv2.IMWRITE_JPEG_QUALITY, raw_quality])
                    z.writestr(f"{base}/raw/ledger_scan/{fr.relative_to(sd).with_suffix('.jpg').as_posix()}", buf.tobytes())
            for f in ("transform.json", "inputs_auto.json", "logistics.json", "placement.json", "flow.json", "design.json"):
                if (bd / f).exists():
                    z.write(bd / f, f"{base}/{f}")
            for f in ("composite.png", "check.jpg", "links.jpg", "logistics.jpg", "placement.jpg", "inputs.jpg", "flow.jpg", "design.jpg"):
                if (bd / f).exists() and not (lite and f.endswith(".jpg")):
                    z.write(bd / f, f"{base}/annotated/{f}" if f.endswith(".jpg") else f"{base}/{f}")
            for bp in names:
                b = d / "bp" / bp
                for f in ("devices.json", "size.json", "anchors.json", "links.json", "logistics.json"):
                    if (b / f).exists():
                        z.write(b / f, f"{base}/blueprints/{bp}/{f}")
                if (b / "mosaic.png").exists():
                    z.write(b / "mosaic.png", f"{base}/blueprints/{bp}/mosaic.png")
                for i, fr in enumerate([] if lite else sorted((b / "frames").glob("*.png"))):
                    if i == 0:
                        z.write(fr, f"{base}/raw/{bp}/frames/{fr.name}")  # 滚动条标定用，保留 png
                    else:
                        ok, buf = cv2.imencode(".jpg", V.rd(fr), [cv2.IMWRITE_JPEG_QUALITY, raw_quality])
                        z.writestr(f"{base}/raw/{bp}/frames/{fr.stem}.jpg", buf.tobytes())
                for fr in sorted((b / "link_shots").glob("*")) if (b / "link_shots").exists() and not lite else []:
                    z.write(fr, f"{base}/raw/{bp}/link_shots/{fr.name}")
    log(f"已导出 {dest}（{dest.stat().st_size / 1e6:.1f} MB）")
    return dest
