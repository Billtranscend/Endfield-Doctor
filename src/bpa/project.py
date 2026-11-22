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
             design.json      设计分析；gates_view.json 准入口表（设计分析顺带写，0.10.13）
  inputs.json                  作者按编号填的输入端 {据点: {"marks": [...]}}
  fixes.json                   作者在「设计分析」页做的修正（进料、配方、池子配方、准入口限速），见 load_fixes / save_fixes
  confirm.json                 作者对设计分析逐台说的对 / 不对
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
from . import atomic
from . import inputs as INP
from . import logistics as LG
from . import vision as V

BPA = ROOT / "debug" / "bpa"
FORMAT = "bpa-1"


def pdir(name: str) -> Path:
    return BPA / name


def _jload(p: Path, default=None):
    return atomic.read_json(p) if p.exists() else default  # 撞上别的线程正在换文件时短等再读（见 atomic.read_text）


def _jsave(p: Path, obj) -> None:
    # 原子写（2026-11-19）：拼合任务写 inputs.json、gates.json、logistics.json 时界面别的线程照样在读，
    # 原来先清空再写，读到一半会拿到空文件（见 atomic.py）
    atomic.write_json(p, obj, mkdir=True)


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
                      "pipes": anc.get("pipe_count", 0), "pipes_open": anc.get("pipe_open", 0),
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
    n_open = sum(1 for f in pipes if f.get("open"))  # 外接的暗管（带红标，对面的口不在这张蓝图里，见 vision.OPEN_TPLS）：计数、当锚点，不去点
    res = {"size": size, "size_check": ok_size, "size_msg": msg_size, "pipe_check": ok_pipe, "pipe_got": got, "pipe_expected": exp,
           "pipe_count": len(pipes), "pipe_open": n_open, "pylons": sum(1 for f in found if f["name"] == "息壤供电桩"), "anchors": found}
    _jsave(bd / "anchors.json", res)
    items = LG.detect(mos)
    bad = LG.presence_check(items, devices)
    _jsave(bd / "logistics.json", {"items": items, "presence_bad": bad})
    log(f"  {bp}：{msg_size}{'' if ok_size else '  ← 尺寸不对，拼接可能错格'}；暗管 {len(pipes)} 个{f'（其中 {n_open} 个外接）' if n_open else ''}"
        f"{'，和设备一览一致' if ok_pipe else f'，和设备一览不一致：识别 {got}，一览 {exp}'}；息壤供电桩 {res['pylons']} 个")
    cnt = {t: sum(1 for o in items if o["type"] == t) for t in LG.LG_TYPES + [LG.GATE]}
    log("  物流件：" + "，".join(f"{t} {n}" for t, n in cnt.items() if n)
        + ("；种类和设备一览一致" if not bad else "；和设备一览不一致：" + "；".join(bad)))
    return res


def devices_changed(old: dict | None, new: dict) -> list[str]:
    """两次读到的设备一览哪里不一样（只比有数量的类型）→ ["仓库取货口 15 → 1", "封装机 没有 → 3", ...]；第一次读或完全一样返回 []。
    2026-11-14 issue #1：同一张蓝图第三次读时「仓库取货口 15」读成 1，自检表上看不出来，放置检查报成基地多出 14 台。"""
    if not old:
        return []
    num = lambda d: {k: v for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}  # noqa: E731
    a, b = num(old), num(new)
    show = lambda v: "没有" if v is None else f"{v:g}"  # noqa: E731
    return [f"{k} {show(a.get(k))} → {show(b.get(k))}" for k in sorted(set(a) | set(b)) if a.get(k) != b.get(k)]


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
            changed = devices_changed(_jload(bd / "devices.json"), devs)
            _jsave(bd / "devices.json", devs)
            log(f"  蓝图尺寸 {size}；设备一览 {len(devs)} 种")
            if changed:
                log("  设备一览和上次读到的不一样：" + "、".join(changed)
                    + "。蓝图没改过的话，是这次或上次有一格读错了，请对着游戏里的设备一览核对一下")
            if (bd / "frames").exists():
                shutil.rmtree(bd / "frames")
            game.capture(bd)
            res = process_blueprint(name, bp, size, log)
            # 外接的（红标）不点：预览里它没有放大镜，点了也不出连接线；配对候选里也不该有它
            pipes = [a for a in res["anchors"] if a["name"] in V.PIPE_NAMES and not a.get("open")]
            if (bd / "links.json").exists() and not pipes:
                (bd / "links.json").unlink()  # 重读后一个可点的都没有了：旧的配对结果作废
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


def label_gate_groups(name: str, labels: dict, log=lambda *_: None) -> None:
    """作者按图标组标形态/名字：{组号: {state: 液/气/固, item: 名字}}。存进项目图标库后重算。
    log：重新拼合的进度（界面里走任务时接到任务日志；默认不打印，和原来一样）。"""
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
    fit_bases(name, log=log)


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
        # 输入端候选要在设计分析前面写好：设计分析靠它知道哪些准入口是输入端。原来排在后面，新项目第一次拼合时文件还没有，
        # 设计分析直接报 FileNotFoundError，再拼一次才有结果，而且用的一直是上一次拼合的候选（2026-11-14 读代码发现）
        marks, dropped = INP.from_gates(canvas, lg_canvas, ginfo, pipes_canvas)
        _jsave(bd / "inputs_dropped.json", dropped)
        _jsave(bd / "inputs_auto.json", marks)
        # 作者存的标记跟着这次的拼合图挪（也要在设计分析前面）；标在暗管出口上的吸到暗管自己的锚点（0.10.13 功能二）
        _rebase_inputs(name, base, canvas, tf, lg_canvas, marks, log, INP.tunnel_anchors(pdir(name), tf))
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
                for w in sm.get("warnings", []):
                    log(f"   注意：{w}")
            except Exception as e:
                log(f"   设计分析出错：{type(e).__name__}: {e}")
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
    entry = (_jload(pdir(name) / "inputs.json", {}) or {}).get(base) or {}
    if any(m.get("p_bp") is None for m in entry.get("marks") or []):
        # 老版本存的（没记蓝图里的位置）：升级后还没重新拼合就打开这一页。先按现在的拼合图核一遍——不然作者一点保存，
        # save_inputs 就把对不上的旧位置当成现在的位置记下来，以后再也认不出它是旧的
        _rebase_inputs_from_disk(name, base)
        entry = (_jload(pdir(name) / "inputs.json", {}) or {}).get(base) or {}
    # 和 pipenet.load_inputs 同一个规矩：存的标记是空的就用候选（原来这里空表也算「填过」，页面是空的、分析用的却是候选）
    marks = entry.get("marks") or (_jload(bd / "inputs_auto.json", []) or [])
    # 贴边但判断不算输入的准入口：已经被作者加回（和某个标记重合）的不再显示
    dropped = [d for d in (_jload(bd / "inputs_dropped.json", []) or [])
               if not any(abs(m["p"][0] - d["p"][0]) < 15 and abs(m["p"][1] - d["p"][1]) < 15 for m in marks)]
    for i, d in enumerate(dropped):
        d["tag"] = chr(ord("A") + i % 26)
    im = INP.numbered_image(canvas, marks, scale, dropped)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=85)
    return {"marks": marks, "dropped": dropped, "image": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii"),
            "width": im.size[0], "height": im.size[1], "scale": scale,
            "stale": entry.get("stale") or [], "bad_items": INP.check_items(marks), "items": INP.item_names()}


def save_inputs(name: str, base: str, marks: list[dict], stale: list[dict] | None = None) -> list[dict]:
    """marks：[{no, p:[x,y], item, ports?, rate, merged, via, source, note, bp, p_bp}]，p 是拼合图坐标；bp、p_bp 是它在哪张蓝图
    预览里的什么位置（这里补记，重新拼合后靠它跟着挪，见 inputs.rebase）。stale：对不上的旧标记，不给就保留原来存的。
    返回物品名认不出来的标记（inputs.check_items）。"""
    p = pdir(name) / "inputs.json"
    allin = _jload(p, {}) or {}
    bd = pdir(name) / "base" / base
    tf = _jload(bd / "transform.json", {}) or {}
    marks = INP.anchor(marks, tf, _jload(bd / "logistics.json", []) or [], INP.tunnel_anchors(pdir(name), tf))
    entry = {"marks": marks, "saved": time.strftime("%Y-%m-%d %H:%M:%S"), "seen": INP.seen_of(_jload(bd / "inputs_auto.json", []) or [])}
    keep = stale if stale is not None else (allin.get(base) or {}).get("stale")
    if keep:
        entry["stale"] = keep
    allin[base] = entry
    _jsave(p, allin)
    if (bd / "composite.png").exists():
        INP.numbered_image(V.rd(bd / "composite.png"), marks).save(bd / "inputs.jpg", quality=88)
    return INP.check_items(marks)


def _rebase_inputs_from_disk(name: str, base: str, log=lambda *_: None) -> None:
    """同 _rebase_inputs，用据点目录里上次拼合的结果（不重新拼合）。老版本拼合出的候选没有 p_bp，这里补上。"""
    bd = pdir(name) / "base" / base
    tf, lg = _jload(bd / "transform.json"), _jload(bd / "logistics.json")
    if not tf or "bps" not in tf or lg is None or not (bd / "composite.png").exists():
        return
    auto = INP.anchor(_jload(bd / "inputs_auto.json", []) or [], tf, lg)
    _rebase_inputs(name, base, V.rd(bd / "composite.png"), tf, lg, auto, log, INP.tunnel_anchors(pdir(name), tf))


def _rebase_inputs(name: str, base: str, canvas, tf: dict, lg_canvas: list[dict], auto: list[dict], log=print,
                   tunnels: list[dict] | None = None) -> None:
    """据点拼合时：作者存的输入端按这次的拼合图重新定位（inputs.rebase），结果写回项目 inputs.json。
    tunnels：这次拼合图上的暗管出口锚点（inputs.tunnel_anchors），标在外接暗管出口上的标记吸到它上面。
    有对不上的旧标记时，改之前的文件另存一份 inputs.json.bak（只存第一次）。"""
    p = pdir(name) / "inputs.json"
    allin = _jload(p, {}) or {}
    entry = allin.get(base)
    if not entry or not (entry.get("marks") or entry.get("stale")):
        return
    marks, stale, info = INP.rebase(entry.get("marks") or [], tf, lg_canvas, auto, entry.get("seen"), tunnels)
    if stale and not (p.parent / "inputs.json.bak").exists():
        shutil.copyfile(p, p.parent / "inputs.json.bak")
    new = dict(entry, marks=marks, seen=INP.seen_of(auto))
    if stale or entry.get("stale"):
        new["stale"] = (entry.get("stale") or []) + stale
    if new != entry:
        allin[base] = new
        _jsave(p, allin)
        if marks:
            INP.numbered_image(canvas, marks).save(pdir(name) / "base" / base / "inputs.jpg", quality=88)
    if info["moved"]:
        log(f"   输入端：拼合图变了，作者存的 {info['moved']} 个标记跟着挪到了新位置")
    if info["stale"]:
        log(f"   输入端：作者存的标记有 {info['stale']} 个在现在的拼合图上对不上（存的是旧拼合图上的位置），已经挪到「输入端」页的"
            f"「对不上的旧标记」里，不参加分析" + (f"；改用程序这次认出来的候选 {info['added']} 个" if info["added"] else "") + "。请到「输入端」页核对")
    elif info["added"]:
        log(f"   输入端：程序新认出 {info['added']} 个候选，已经加进作者存的标记里，请到「输入端」页核对")
    for b in INP.check_items(marks):
        log(f"   输入端：{b['no']} 号填的物品「{b['item']}」认不出来" + (f"（是不是「{'」「'.join(b['suggest'])}」？）" if b["suggest"] else "")
            + "，分析里这个口送的东西没有设备吃，请到「输入端」页改")


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


def load_fixes(name: str) -> dict:
    """fixes.json：作者在「设计分析」页做的修正，每个据点一段：
    {据点: {feeds: {设备键: to:<设备键> / none / unused}, recipes: {设备键: 配方}, pool_recipes: {池子的设备键: [原始配方…]},
            gates: {准入口键: {bp, p_bp, kind, item, limit, count, at}},
            pool_outlets: {池子的设备键: {belt: [固体，最多 1 个], pipe: [液体，最多 2 个], at}}}, updated: 时间}（格式见 design.load_fixes）。
    和 confirm.json 的区别：确认只是记下作者的判断；修正会改变设计分析的算法输入（下次算就按它来）。"""
    return _jload(pdir(name) / "fixes.json", {}) or {}


# fixes.json 每个据点下的几段。pool_outlets：池子出口送什么（0.10.13 功能三）
FIX_KINDS = ("feeds", "recipes", "pool_recipes", "gates", "pool_outlets")
GATE_STEP = 6          # 准入口限制速度游戏里每档 6（设计文档「准入口：…每档 6」）：界面只给下拉，这里也只收 6 的倍数
GATE_TOP = {"物品": 30}  # 物品准入口最多 30/分；管道准入口、类型没定的口最多 60/分
GATE_COUNT = (1, 5000)  # 限制通过数量的范围（整数）
# 外接暗管出口的来料（0.10.13 功能二）：和别的修正走同一个保存接口、同一个任务，但不进 fixes.json，存成 inputs.json 里的输入端标记
TUN_KIND = "tunnel_feeds"


def save_fix(name: str, base: str, kind: str, key: str, value: str | list | None) -> None:
    """只存一项的 save_fixes（老的入口，测试和别的调用照旧能用）。各类别的值见 save_fixes。"""
    save_fixes(name, base, [{"kind": kind, "key": key, "value": value}])


def save_fixes(name: str, base: str, changes: list[dict]) -> list[dict]:
    """一次存几项修正：changes = [{kind, key, value}]，先全部校验（check_fixes，一项不对整批不存），再改 fixes.json、原子写一次。
    返回规整过的改动（check_fixes 的结果）。各类别：
    - feeds / recipes：value 是字符串，空串或 None = 撤销；
    - pool_recipes（池子同时跑的几条原始配方，2026-11-19 审计第 3 项）：value 是配方列表，[] = 这台没在用（合法值，不是撤销），
      只有 None 才撤销。池子的两段互斥、后存的为准：给池子存 recipes 一律转存成 pool_recipes=[这一条]，存 pool_recipes 时删掉
      同一台老的 recipes[键]，撤销时两段一起删（一台池子只认一种说法）；
    - gates（0.10.13 作者填的准入口）：value = {limit, count}（都可以是 None：两项都是 None = 作者说都没开），None = 撤销
      （回到游戏里读的，没有读数就回到没说）。键、位置、物品从准入口表 gates_view.json 补，不信界面传的。
    - pool_outlets（0.10.13 功能三，池子出口送什么）：value = {belt?: [固体，最多 1 个], pipe?: [液体，最多 2 个、不重复]}，
      None = 撤销（校验见 _norm_outlets，存的时候加 at）。撤销这台的池子配方（pool_recipes None）或说这台没在用（[]）时，
      同一台的出口回答一起删（这一项改动带 outlets_dropped=True）；一批里同时有这台的出口回答时按批里的为准，不删。
    配方（recipes / pool_recipes）和原来存的不一样时，这台原来点的「对」撤掉（针对的是旧配方算出的结果），这一项改动带 revoked=True；
    原来说「不对」的留着（作者说的实际情况不随配方变），带 kept_wrong=True。出口回答（不算 at）变了的池子照这个办，标记打在
    pool_outlets 那一项上。任务日志照这两个标记点名。
    别的设备结果变了要不要撤「对」，由重算前后比对决定（revoke_changed，在界面的修正任务里做）。
    - tunnel_feeds（0.10.13 外接暗管出口的来料）：不进 fixes.json，记成 inputs.json 里标在这台暗管出口上的输入端标记（见 save_tunnel_feed）。
    一批里两种都有时先全部校验，再两处都存（fixes.json、inputs.json），界面的修正任务只重算一次。"""
    done = check_fixes(name, base, changes)
    fx = [c for c in done if c["kind"] != TUN_KIND]
    if fx:
        _write_fixes(name, base, fx)
    tun = [c for c in done if c["kind"] == TUN_KIND]
    if tun:
        try:
            _apply_tunnel_feeds(name, base, tun)
        except Exception as e:  # noqa: BLE001
            if not fx:
                raise
            # 两个文件没法一起原子写：fixes.json 已经落盘、inputs.json 没写成时照实说，不然作者只看到报错，以为什么都没存
            raise RuntimeError(f"另外 {len(fx)} 项修正已经存下了，暗管来料没存上（{e}）。再点一次暗管来料卡上的按钮；"
                               "已经存下的那几项要等下次重算才生效（可以点「重算」）") from e
    return done


def _write_fixes(name: str, base: str, done: list[dict]) -> None:
    """规整过的 fixes.json 改动写进去（原子写一次）；撤销池子配方、说没在用时一起删出口回答；改了配方、出口回答的设备撤「对」
    （见 save_fixes）。"""
    allf = load_fixes(name)
    bf = allf.setdefault(base, {})
    keys = {c["key"] for c in done if c["kind"] in ("recipes", "pool_recipes")}
    okeys = {c["key"] for c in done if c["kind"] in ("pool_recipes", "pool_outlets")}  # 配方撤销、没在用会连带删出口回答
    before = {k: _recipe_of(bf, k) for k in keys}
    before_o = {k: _outlets_of(bf, k) for k in okeys}
    for c in done:
        _apply_fix(bf, c)
    said = {c["key"] for c in done if c["kind"] == "pool_outlets"}
    for c in done:
        # 撤销池子配方、说这台没在用：出口回答跟着配方走，一起删；这一批里也说了这台的出口就按批里的（不管先后）
        if c["kind"] == "pool_recipes" and not c["value"] and c["key"] not in said:
            if (bf.get("pool_outlets") or {}).pop(c["key"], None) is not None:
                c["outlets_dropped"] = True
    allf["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _jsave(pdir(name) / "fixes.json", allf)
    moved_r = {k for k in keys if _recipe_of(bf, k) != before[k]}
    moved_o = {k for k in okeys if _outlets_of(bf, k) != before_o[k]}
    moved = moved_r | moved_o
    if moved:
        # 2026-11-19：表格上「改配方」就在「✓ 对」旁边。皇帝武陵城扩容反应池 (1346,1362) 先点「对」、再改配方，重算成没料 0%，
        # 「对」还留着的话导入会把这个作者没看过的新结果当成设计如此写进 device_designs，巡检就不报了。只作废这一台的「对」
        # （0.10.13：「不对」留着，和重算前后比对的规矩一样；出口回答变了的池子也一样，功能三）
        allc = load_confirm(name)
        dv = (allc.get(base) or {}).get("devices", {})
        verdict = {k: (dv.get(k) or {}).get("verdict") for k in moved}
        for c in done:
            hit = (c["kind"] in ("recipes", "pool_recipes") and c["key"] in moved_r
                   or (c["kind"] == "pool_outlets" or c.get("outlets_dropped")) and c["key"] in moved_o)
            if hit:
                c["revoked"], c["kept_wrong"] = verdict[c["key"]] == "ok", verdict[c["key"]] == "wrong"
        if any(v == "ok" for v in verdict.values()):
            for k, v in verdict.items():
                if v == "ok":
                    dv.pop(k, None)
            allc["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
            _jsave(pdir(name) / "confirm.json", allc)


def check_fixes(name: str, base: str, changes: list[dict]) -> list[dict]:
    """校验一批修正，不写文件 → 规整过的改动 [{kind, key, value}]：池子的 recipes 转成 pool_recipes；准入口的 value 换成要存的整条
    记录（多带一个 row = 准入口表里那一行，日志说是哪个口用）；暗管来料多带一个 card = design.json 里那张「暗管来料」卡；
    池子出口回答、池子配方多带一个 row = design.json 里这台池子那一行（日志写拼合图坐标用；池子配方找不到这台是 None，照常存）。
    不对的抛 ValueError（界面当场报，不开任务）。"""
    if not isinstance(changes, list):
        raise ValueError("修正要是一组改动")
    out, gv, saved, cards, pools, saved_o = [], None, None, None, None, None
    for ch in changes:
        if not isinstance(ch, dict):
            raise ValueError("看不懂的修正（不是一项改动）")
        kind, key, value = ch.get("kind"), ch.get("key"), ch.get("value")
        if kind not in FIX_KINDS + (TUN_KIND,):
            raise ValueError(f"不认识的修正类别：{kind}")
        if not isinstance(key, str) or not key:
            raise ValueError("修正里缺了是哪台设备 / 哪个准入口")
        if kind in ("pool_outlets", "pool_recipes", "recipes") and pools is None:
            pools = pool_rows(name, base)
        if kind == "gates":
            if gv is None:
                gv = gates_view(name, base)
                saved = (load_fixes(name).get(base) or {}).get("gates") or {}
            out.append(_norm_gate(key, value, gv, saved))
        elif kind == TUN_KIND:
            if cards is None:
                cards = tunnel_cards(name, base)
            out.append(_norm_tunnel(key, value, cards))
        elif kind == "pool_outlets":
            if saved_o is None:
                saved_o = (load_fixes(name).get(base) or {}).get("pool_outlets") or {}
            out.append(_norm_outlets(key, value, pools, saved_o))
        else:
            c = _norm_fix(kind, key, value)
            if c["kind"] == "pool_recipes":
                c["row"] = (pools or {}).get(key)
            out.append(c)
    return out


def pool_rows(name: str, base: str) -> dict | None:
    """现在 design.json 里的池子行 → {设备键: 行}；还没算过（没有 design.json）→ None。"""
    from . import pool_model as PM
    dj = _jload(pdir(name) / "base" / base / "design.json")
    if dj is None:
        return None
    return {device_key(r): r for r in dj.get("devices") or [] if r.get("device") in PM.SLOTS}


def _norm_outlets(key: str, value, pools: dict | None, saved: dict) -> dict:
    """池子出口送什么一项（0.10.13 功能三）的校验：键要是这个据点 design.json 里的反应池 / 扩容反应池（撤销时也可以是 fixes.json 里
    已经存了的：底账重扫后对不上的旧回答要能删）；value = None（撤销）或只有 belt、pipe 两项的 dict：
      belt  带出口送什么，物品名列表，最多 1 个（带出口只有 1 个选择，推断；PM.OUT_SOLID），都得是固体；
      pipe  管出口送什么，物品名列表，最多 2 个（官方：2 个管道取货口；PM.OUT_FLUID）、不重复，都得是液体或气体。
    没有这一项（或是 None）= 那种出口没说；空列表 = 什么都不送；两项都没说（{}）= 撤销。在不在这台池子的配方里这里不查
    （面板、推断来的池子这里拿不到配方），算的时候查、写说明。→ {kind, key, value: {belt?, pipe?, at} | None, row}。"""
    from . import pool_model as PM
    machine = key.split("@")[0]
    if machine not in PM.SLOTS:
        raise ValueError(f"只有反应池、扩容反应池能说出口送什么（这台是{machine}）")
    if pools is None:
        raise ValueError("这个据点还没算设计分析（先跑「据点拼合」）")
    row = pools.get(key)
    if isinstance(value, dict) and all(value.get(f) is None for f in value) and set(value) <= {"belt", "pipe"}:
        value = None  # 两种出口都没说：和没回答一样，当撤销（不留一条什么都没说的记录）
    if value is None:
        if row is None and key not in saved:
            raise ValueError(f"设计分析结果里没有这台{machine}（重算过的话刷新一下）")
        return {"kind": "pool_outlets", "key": key, "value": None, "row": row}
    if row is None:
        raise ValueError(f"设计分析结果里没有这台{machine}（重算过的话刷新一下）")
    if not isinstance(value, dict):
        raise ValueError("出口送什么要按带出口、管出口分开说")
    if set(value) - {"belt", "pipe"}:
        raise ValueError("出口送什么只能说带出口、管出口两样")
    rec, names = {}, set(INP.item_names())
    for layer, what, most, fluid in (("belt", "带出口", PM.OUT_SOLID, False), ("pipe", "管出口", PM.OUT_FLUID, True)):
        lst = value.get(layer)
        if lst is None:
            continue
        if not isinstance(lst, list) or not all(isinstance(x, str) and x for x in lst):
            raise ValueError(f"{what}送什么要选物品")
        if len(lst) > most:
            raise ValueError(f"{what}最多选 {most} 种（选了 {len(lst)} 种）")
        if len(set(lst)) < len(lst):
            raise ValueError(f"{what}选的物品重复了")
        for x in lst:
            if x not in names:
                raise ValueError(f"{what}选的「{x}」认不出来")
            if PM.is_fluid(x) != fluid:
                raise ValueError(f"「{x}」是{'固体，走不了管出口' if fluid else '液体，走不了带出口'}")
        rec[layer] = list(lst)
    rec["at"] = time.strftime("%Y-%m-%d %H:%M:%S")
    return {"kind": "pool_outlets", "key": key, "value": rec, "row": row}


def _outlets_of(bf: dict, key: str):
    """这台池子现在存的出口回答（只比 belt、pipe，不比 at）；没说 → None。"""
    rec = (bf.get("pool_outlets") or {}).get(key)
    return {f: rec[f] for f in ("belt", "pipe") if f in rec} if isinstance(rec, dict) else None


def _norm_fix(kind: str, key: str, value) -> dict:
    """feeds / recipes / pool_recipes 一项的校验和规整（原来 save_fix 里的那几条规矩）。"""
    from . import pool_model as PM
    pool = key.split("@")[0] in PM.SLOTS
    if kind == "recipes" and pool:
        # 2026-11-19：「没有进料」卡的单条下拉以前存进 recipes[键]，而 design 只要有 pool_recipes 就跳过它——作者选了、卡上显示已选，
        # 结果一点不变；反过来存 recipes 时也不删 pool_recipes。转存成 pool_recipes 后两段只剩一段，后存的说了算
        if value is not None and not isinstance(value, str):
            raise ValueError(f"{kind} 修正只能是字符串：{value!r}")
        kind, value = "pool_recipes", ([value] if value else None)
    if kind == "pool_recipes":
        if value is not None:
            _check_pool_fix(key, value)
            value = list(value)
        return {"kind": kind, "key": key, "value": value}
    if value is not None and not isinstance(value, str):
        raise ValueError(f"{kind} 修正只能是字符串：{value!r}")
    if kind == "feeds" and value and not (value in ("none", "unused") or value.startswith("to:")):
        raise ValueError(f"进料修正只能是 to:<设备键> / none / unused：{value}")
    return {"kind": kind, "key": key, "value": value or None}


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _shown(v) -> str:
    """出错提示里填的值：数字照写，别的写「不是数字」（不把程序里的写法弹给作者看）。"""
    return f"{v:g}" if _is_num(v) else "不是数字"


def _norm_gate(key: str, value, gv: dict | None, saved: dict) -> dict:
    """准入口一项：键要在准入口表里（撤销时也可以是「对不上的旧设置」或 fixes.json 里有的）；限制速度是 6 的倍数，物品准入口 0–30、
    管道准入口和类型没定的口 0–60；限制通过数量是 1–5000 的整数。存的记录里 bp、p_bp 用这条记录原来的（同一个键改了还存回同一条），
    新填的用准入口表这一行的（来自 logistics.json）；kind、item 是存的时候的快照，只在「对不上的旧设置」里给人看。"""
    if gv is None:
        raise ValueError("这个据点的设计分析是旧版本算的，还没有准入口表：先点「重算」")
    rows = {r["key"]: r for r in gv.get("gates") or [] if r.get("key")}
    row = rows.get(key)
    if value is None:
        if row is None and key not in saved and not any(s.get("key") == key for s in gv.get("stale") or []):
            raise ValueError("这个准入口在准入口表里找不到了（重算过的话刷新一下）")
        return {"kind": "gates", "key": key, "value": None, "row": row}
    if row is None:
        raise ValueError("这个准入口在准入口表里找不到了（重算过的话刷新一下；对不上的旧设置只能删）")
    if not isinstance(value, dict):
        raise ValueError("准入口的设置要写限制速度和限制通过数量")
    lim, cnt = value.get("limit"), value.get("count")
    top = GATE_TOP.get(row.get("kind"), 60)
    what = f"{row.get('kind') or ''}准入口"
    if lim is not None:
        if not _is_num(lim) or lim % GATE_STEP or not 0 <= lim <= top:
            raise ValueError(f"{what}的限制速度只能是 0–{top} 里 {GATE_STEP} 的倍数（游戏里每档 {GATE_STEP}），或者不限速（填的是 {_shown(lim)}）")
        lim = int(lim)
    if cnt is not None:
        if not _is_num(cnt) or (isinstance(cnt, float) and not cnt.is_integer()) or not GATE_COUNT[0] <= cnt <= GATE_COUNT[1]:
            raise ValueError(f"限制通过数量只能是 {GATE_COUNT[0]}–{GATE_COUNT[1]} 的整数，或者不限量（填的是 {_shown(cnt)}）")
        cnt = int(cnt)
    # 这个口上认出的就是作者原来那条记录（表上 src 是作者）：位置用记录的；不然（新填的，或同名的记录其实没对上）用表上这一行的
    old = saved.get(key) if row.get("src") == "作者" and isinstance(saved.get(key), dict) else {}
    bp, p_bp = (old["bp"], old["p_bp"]) if old.get("bp") and old.get("p_bp") is not None else (row["bp"], row["p_bp"])
    rec = {"bp": bp, "p_bp": list(p_bp), "kind": row.get("kind") or "", "item": gate_item(row), "limit": lim, "count": cnt,
           "at": time.strftime("%Y-%m-%d %H:%M:%S")}
    return {"kind": "gates", "key": key, "value": rec, "row": row}


def _recipe_of(bf: dict, key: str):
    """这台现在按作者说的是哪几条配方（池子：pool_recipes 优先，再看老的 recipes）。"""
    from . import pool_model as PM
    pool = key.split("@")[0] in PM.SLOTS
    if pool and key in (bf.get("pool_recipes") or {}):
        return bf["pool_recipes"][key]
    old = (bf.get("recipes") or {}).get(key)
    return ([old] if pool else old) if old else None


def _apply_fix(bf: dict, c: dict) -> None:
    """一项规整过的改动写进这个据点那一段（撤销也会建出空的那一段，和原来一样）。"""
    kind, key, value = c["kind"], c["key"], c["value"]
    sect = bf.setdefault(kind, {})
    if value is None:
        sect.pop(key, None)
    else:
        sect[key] = value
    if kind == "pool_recipes":
        (bf.get("recipes") or {}).pop(key, None)


def _check_pool_fix(key: str, value) -> None:
    """池子配方的服务端校验（界面上也挡了一遍，这里再挡：手改的请求、旧界面）。设备键「扩容反应池@x,y」里 @ 前面是机器名，
    必须是反应池 / 扩容反应池；每条都得是这种池子的配方、不重复、物品种类不超格（pool_model.check）。"""
    from . import pool_model as PM
    machine = key.split("@")[0]
    if machine not in PM.SLOTS:
        raise ValueError(f"只有反应池、扩容反应池能多选配方：{key}")
    if not isinstance(value, list) or not all(isinstance(x, str) for x in value):
        raise ValueError(f"池子配方要是配方列表：{value!r}")
    if value:
        ck = PM.check(machine, value)
        if not ck["ok"]:
            raise ValueError("；".join(ck["why"]))


# ------------------------------------------------------------------ 外接暗管出口的来料（0.10.13 功能二）
def tunnel_cards(name: str, base: str) -> dict:
    """现在 design.json 待确认里的「暗管来料」卡 → {设备键: 卡}（design._tunnel_card 写的：c、cls、bp、p_bp、ports、marks…）。"""
    dj = _jload(pdir(name) / "base" / base / "design.json", {}) or {}
    return {p["key"]: p for p in dj.get("pending") or [] if p.get("kind") == "暗管来料" and p.get("key")}


def _norm_tunnel(key: str, value, cards: dict) -> dict:
    """暗管来料一项的校验：key 要是 design.json 里一张「暗管来料」卡；value = {item, rate} 或 None（撤销）。rate 是 0 到 120 × 口数的数
    （building_ports.json：暗管出口 1 个口、多口暗管出口 2 个口）；item 是认得的液体、气体（暗管只走管子），只有 rate 为 0
    （这台平时不来料）时可以空着。→ {kind, key, value: {item, rate} | None, card}。"""
    from . import design as DS, pool_model as PM
    card = cards.get(key)
    if card is None:
        raise ValueError("这台暗管出口不在设计分析的「暗管来料」待确认里（重算过的话刷新一下）")
    if value is None:
        return {"kind": TUN_KIND, "key": key, "value": None, "card": card}
    if not isinstance(value, dict):
        raise ValueError("暗管来料要写送进来的物品和每分钟多少")
    ports = DS.tunnel_ports(key.split("@")[0])
    cap = DS.PIPE_CAP * ports
    rate, item = value.get("rate"), value.get("item")
    if not _is_num(rate) or not 0 <= rate <= cap:
        raise ValueError(f"每分钟多少只能是 0–{cap:g} 的数（{'多口暗管出口 2 个口合计' if ports > 1 else '一根管子'}最多 {cap:g}，"
                         f"填的是 {_shown(rate)}）")
    item = (item or "").strip() if isinstance(item, str) or item is None else None
    if item is None:
        raise ValueError("送进来的物品要选一种液体或气体")
    if not item and rate != 0:
        raise ValueError("要选送进来的是哪种液体或气体（只有填 0、这台平时不来料时可以不选）")
    if item and not (item in INP.item_names() and PM.is_fluid(item)):
        raise ValueError(f"送进来的物品只能是液体或气体（暗管只走管子），「{item}」" + ("是固体" if item in INP.item_names() else "认不出来"))
    return {"kind": TUN_KIND, "key": key, "value": {"item": item, "rate": int(rate) if float(rate).is_integer() else float(rate)}, "card": card}


def save_tunnel_feed(name: str, base: str, key: str, value: dict | None) -> dict:
    """作者在「暗管来料」卡上填的（0.10.13 功能二）：value = {item, rate}（rate 0 = 这台平时不来料）或 None（撤销）。
    记成 inputs.json 里标在这台暗管出口上的输入端标记（「输入端」页也能看到、能改），不另开 fixes.json 段：作者在应龙关已经这样手标过，
    只放一处就不会两处说法打架。位置、蓝图、锚点、单口还是多口都从 design.json 那张卡取，不信界面传的。
    只存一项的 save_fixes（校验见 _norm_tunnel，存法见 _apply_tunnel_feeds）→ 规整过的改动（带 no：标记编号；撤销的带 removed）。"""
    return save_fixes(name, base, [{"kind": TUN_KIND, "key": key, "value": value}])[0]


def _apply_tunnel_feeds(name: str, base: str, changes: list[dict]) -> None:
    """校验过的暗管来料写进 inputs.json（走 save_inputs：吸到暗管锚点、记 seen、重画 inputs.jpg）。
    - 存的标记是空的：从候选 inputs_auto.json 起步（和 inputs_view、pipenet.load_inputs 同一个规矩）；有老版本存的（没有 p_bp）先按
      现在的拼合图核一遍（同 inputs_view：不然存的时候会把对不上的旧位置当成现在的位置记下来）。
    - 标在这台上的标记认设计分析认的（卡上的 marks：analyze 只看位置绑的，第一个是它用的那个），再核一下还在这台暗管上
      （inputs.mark_on_tunnel：不在准入口上、离它 45 px 内；「输入端」页存过、还没重算时可能挪走或删了）：改第一个，位置放到
      这台暗管自己的锚点上；没有就追加一个，编号取最大号 + 1，位置是锚点；撤销 = 这几个都删掉（编号不重排），卡回到没填。
      不在存的时候按位置重新猜：旁边还有没卡的暗管出口（蓝图外、缺文件、配上对的）时，猜出来的和设计分析认的不是同一台，
      会改掉、删掉隔壁那台的标记（0.10.13 功能二审查）。
    每项改动上记 no（标记编号）、added（新加的）、removed（撤销删掉的编号），日志用。"""
    bd = pdir(name) / "base" / base
    entry = (_jload(pdir(name) / "inputs.json", {}) or {}).get(base) or {}
    if any(m.get("p_bp") is None for m in entry.get("marks") or []):
        _rebase_inputs_from_disk(name, base)
        entry = (_jload(pdir(name) / "inputs.json", {}) or {}).get(base) or {}
    marks = [dict(m) for m in entry.get("marks") or (_jload(bd / "inputs_auto.json", []) or [])]
    lg = _jload(bd / "logistics.json", []) or []
    tf = _jload(bd / "transform.json", {}) or {}
    for c in changes:
        card, key, v = c["card"], c["key"], c["value"]
        dev = key.split("@")[0]
        nos = card.get("marks")
        if nos is None:  # 早一点的 design.json 卡上没有 marks：只认用的那个
            nos = [card["mark"].get("no")] if card.get("mark") else []
        near = set(INP.mark_on_tunnel(marks, card["c"], lg))
        hit = []
        for no in nos:
            hit += [k for k, m in enumerate(marks) if no is not None and m.get("no") == no and k in near and k not in hit]
        if v is None:
            c["removed"] = [marks[k].get("no") for k in hit]
            marks = [m for k, m in enumerate(marks) if k not in hit]
            continue
        rec = {"item": v["item"], "rate": v["rate"], "via": "多口暗管" if dev.startswith("多口") else "暗管", "on": dev}
        at = card.get("bp") in (tf.get("bps") or {}) and card.get("p_bp") is not None
        pos = {"p": INP.from_bp(tf, card["bp"], card["p_bp"]), "bp": card["bp"], "p_bp": list(card["p_bp"])} if at else {}
        if hit:
            marks[hit[0]].update(rec, **pos)
            c["no"], c["added"] = marks[hit[0]].get("no"), False
            continue
        no = max([int(m.get("no") or 0) for m in marks] + [0]) + 1
        what = "外接暗管出口" if card.get("cls") == "外接" else "没配上对的暗管出口"
        new = dict(rec, no=no, p=list(card["c"]), merged=False, source="manual", note=f"{what}（设计分析「待确认」里填的）",
                   bp=card.get("bp"), p_bp=card.get("p_bp"))
        marks.append(dict(new, **pos))
        c["no"], c["added"] = no, True
    save_inputs(name, base, marks)


def archive_dir(name: str) -> Path | None:
    """项目登记的蓝图档案目录（data/blueprints/<档案>）：配方表、游戏里读到的准入口限速在这里。没登记或不在本机 → None。"""
    arch = load(name).get("archive")
    d = ROOT / "data" / "blueprints" / arch if arch else None
    return d if d is not None and d.exists() else None


def rerun_design(name: str, base: str) -> dict:
    """只重算这个据点的设计分析（不重新拼合）：作者改了修正以后用。返回汇总。"""
    from . import design as DS
    bd = pdir(name) / "base" / base
    dr = DS.run_base(bd, archive_dir(name), base, ledger=ledger_path(name, base))
    return dr["summary"]


def design_state(name: str, base: str) -> dict:
    """现在 design.json 里每台设备 → {设备键: {device, c, rate, state}}：按作者修正重算前后各取一次，比哪几台结果变了。"""
    dj = _jload(pdir(name) / "base" / base / "design.json", {}) or {}
    return {device_key(r): {"device": r.get("device"), "c": r.get("c"), "rate": r.get("rate"), "state": r.get("state") or ""}
            for r in dj.get("devices", [])}


def revoke_changed(name: str, base: str, before: dict, after: dict) -> dict:
    """按作者修正重算以后：开工率或状态变了的设备，原来点的「对」撤掉（留着的话，导入时会把作者没看过的新结果当成设计如此，
    巡检就不报了；2026-11-19 save_fix 里只撤改了配方的那一台，0.10.13 推广到所有结果变了的设备）；「不对」留着（作者说的实际情况
    不随我们算的变），只让日志提醒；没变的不动。前后有一边没有的设备不算变（这台不在了 / 新冒出来，没有可撤的）。
    → {changed: [{key, device, c, before, after}], revoked: [设备键], wrong: [设备键]}（before / after 是 {rate, state}）。"""
    changed = []
    for k, a in after.items():
        b = before.get(k)
        if b is None:
            continue
        if abs((a.get("rate") or 0) - (b.get("rate") or 0)) > 1e-6 or a.get("state") != b.get("state"):
            changed.append({"key": k, "device": a.get("device"), "c": a.get("c"),
                            "before": {"rate": b.get("rate"), "state": b.get("state")}, "after": {"rate": a.get("rate"), "state": a.get("state")}})
    allc = load_confirm(name)
    dv = (allc.get(base) or {}).get("devices") or {}
    revoked = [c["key"] for c in changed if (dv.get(c["key"]) or {}).get("verdict") == "ok"]
    wrong = [c["key"] for c in changed if (dv.get(c["key"]) or {}).get("verdict") == "wrong"]
    if revoked:
        for k in revoked:
            dv.pop(k, None)
        allc["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
        _jsave(pdir(name) / "confirm.json", allc)
    return {"changed": changed, "revoked": revoked, "wrong": wrong}


# ------------------------------------------------------------------ 准入口限速表（0.10.13 功能一）
def gates_view(name: str, base: str) -> dict | None:
    """设计分析顺带写的准入口表 base/<据点>/gates_view.json（格式见 design._gate_view）；0.10.13 以前算的据点没有 → None。"""
    return _jload(pdir(name) / "base" / base / "gates_view.json")


def gate_read_path(name: str, base: str) -> Path | None:
    """档案里游戏读到的准入口限速（深入巡检「读准入口限速」写的 ledger/gate_limits/<据点>.json）；没有 → None。"""
    d = archive_dir(name)
    p = d / "ledger" / "gate_limits" / f"{base}.json" if d else None
    return p if p is not None and p.exists() else None


UNK_FLOW = ("没看到的物品", "没看到的液体")  # design.UNK_NAMES 的写法：准入口表里过口的物品没看到


def gate_item(row: dict) -> str:
    """准入口表一行给人看的物品：口上认出来的；没认出来、推料时只过了一种料的用那种料（作者武陵城 #45 图标没认出，过的是息壤）；都不知道是空串。
    开了限制通过数量的口，被挡在口上的料（shut：不算出料堵死时（正的背压照算）真有料在动着送到这个口上的物品，设计分析主分析和两遍 probe 的并集，
    见 design._gate_view）也算「过了」（0.10.13 A2 审查）。"""
    if row.get("item"):
        return row["item"]
    seen = [k for k in {**(row.get("flow") or {}), **(row.get("held") or {}), **dict.fromkeys(row.get("shut") or [])} if k not in UNK_FLOW]
    return seen[0] if len(seen) == 1 else ""


def gate_table(name: str, base: str) -> dict | None:
    """准入口表按界面要的样子排好（不含图）→ {rows, counts, stale, read_time, has_read}；没有 gates_view.json → None。
    每行在 gates_view 那一行上多几个字段：
      group   有料（不是输入端、设计分析里有料经过或被挡下；开了限制通过数量的口，上游要往它送料也算，见 gates_view 的 shut）/
              输入端 / 没追到（不是输入端、没有料经过）
      seg     排序的段：0 = 作者填过的、游戏里读到开着的；1 = 其余有料经过的；2 = 其余（输入端、没追到料的）
      no      行号（1 起），总览图上的编号和它一样
      item_name  gate_item；show  默认筛选（有料经过的）显示不显示：有料的、作者填过的都显示；fix  作者存的那条记录（src 是作者时）
    每段里按物品分组，同物品按位置从上到下、从左到右排。counts：{有料, 已填（作者存过的，含说都没开的）, 说没开（其中两项都是 None 的）,
    游戏读到（读失败的不算）, 输入端, 没追到, 填不了（有料经过、但 logistics.json 里没有位置数据、没有键的）}。"""
    gv = gates_view(name, base)
    if gv is None:
        return None
    saved = (load_fixes(name).get(base) or {}).get("gates") or {}
    rows = []
    for r in gv.get("gates") or []:
        r = dict(r)
        # 开了限制通过数量的口（0.10.13 A2）：上游是生产设备时背压把它压成 0，flow、held 都是空的，按 shut 归「有料」（审查 2026-11-21：
        # 原来这种口从「通过 30」变成「没追到」，默认筛选里看不到、有料的口数也少一个）。上游设备自己没料的口 shut 是空的，照旧归「没追到」（复审）
        r["group"] = "输入端" if r.get("input_no") is not None else "有料" if (r.get("flow") or r.get("held") or r.get("shut")) else "没追到"
        on = r.get("limit") is not None or r.get("count") is not None
        r["seg"] = 0 if r.get("src") == "作者" or (r.get("src") == "游戏" and on) else 1 if r["group"] == "有料" else 2
        r["item_name"] = gate_item(r)
        r["show"] = r["group"] == "有料" or r.get("src") == "作者"
        r["fix"] = saved.get(r.get("key")) if r.get("src") == "作者" else None
        rows.append(r)
    rows.sort(key=lambda r: (r["seg"], r["item_name"] or "￿", r["c"][1], r["c"][0]))
    for i, r in enumerate(rows, 1):
        r["no"] = i
    mine = [r for r in rows if r.get("src") == "作者"]
    counts = {"有料": sum(r["group"] == "有料" for r in rows), "已填": len(mine),
              "说没开": sum(r.get("limit") is None and r.get("count") is None for r in mine),
              "游戏读到": sum(r.get("src") == "游戏" for r in rows), "输入端": sum(r["group"] == "输入端" for r in rows),
              "没追到": sum(r["group"] == "没追到" for r in rows), "填不了": sum(r["group"] == "有料" and not r.get("key") for r in rows)}
    return {"rows": rows, "counts": counts, "stale": list(gv.get("stale") or []), "read_time": gv.get("read_time"),
            "has_read": gate_read_path(name, base) is not None}


def gate_limits_export(name: str, base: str) -> dict | None:
    """导出 zip 里的 <据点>/gate_limits.json：档案里游戏读到的，加上作者在准入口表里填的（盖过同一个口的游戏读数）。
    导入的玩家巡检、断水影响照它算，自己进游戏读一次就会覆盖它。行格式照 gate_limits.read_area 的 rows（p 是拼合图坐标、
    far = F0 + K·p 远景坐标，设计分析按 far 认口）；作者填的行多 src="作者"、time = 填的时间。限制通过数量没有累计数（passed 是 None），
    只有和游戏里读到的同一个数时带上读到的累计数（和 design.analyze 一样）。
    值取 fixes.json 里存的记录（作者存了、还没等到重算就取消了任务，导出照样带上刚存的；撤销了的不带），位置取准入口表里认上的那一行
    （键相同的行：作者填过的口行上的键就是记录的键，刚存还没重算的口行上的键是按位置现算的、存的时候用的就是它）。
    认不上的记录（对不上的旧设置）不带。没有作者填的（或还没有准入口表）→ None：照旧原样带档案文件。"""
    gv = gates_view(name, base)
    saved = (load_fixes(name).get(base) or {}).get("gates") or {}
    rows = {r["key"]: r for r in (gv or {}).get("gates") or [] if r.get("key")}
    mine = [(rows[k], rec) for k, rec in saved.items() if k in rows and isinstance(rec, dict)
            and all(rec.get(f) is None or _is_num(rec.get(f)) for f in ("limit", "count"))]
    if not mine:
        return None
    tf = _jload(pdir(name) / "base" / base / "transform.json", {}) or {}
    rp = gate_read_path(name, base)
    arch = (_jload(rp, {}) or {}) if rp else {}

    def same(row, c):  # 档案这一行是不是这个口（和 design.analyze 一样：far 换到拼合图上差 15 px 以内；没有 far 的按 p）
        p = V.far_to_canvas(tf, *row["far"]) if row.get("far") and tf.get("F0") else row.get("p")
        return p is not None and abs(p[0] - c[0]) <= INP.SNAP and abs(p[1] - c[1]) <= INP.SNAP
    keep = [row for row in arch.get("rows") or [] if not any(same(row, r["c"]) for r, _ in mine)]
    add = []
    for r, rec in mine:
        x, y = r["c"]
        lim, cnt, rd = rec.get("limit"), rec.get("count"), r.get("read") or {}
        add.append({"p": [int(round(x)), int(round(y))],
                    "far": [round(tf["F0"][0] + tf["K"] * x, 2), round(tf["F0"][1] + tf["K"] * y, 2)] if tf.get("F0") else None,
                    "kind": r.get("kind") or "", "bp": r.get("bp"), "p_bp": r.get("p_bp"), "gate_no": r.get("input_no"),
                    "item": gate_item(r) or rec.get("item") or "", "rate": r.get("input_rate"),
                    "limit_on": lim is not None, "limit": lim, "count_on": cnt is not None, "count_limit": cnt,
                    "passed": rd.get("passed") if cnt is not None and rd.get("count_on") and rd.get("count_limit") == cnt else None,
                    "src": "作者", "time": rec.get("at") or ""})
    out = dict(arch) if arch else {"area": base, "time": time.strftime("%Y%m%d-%H%M%S")}
    out["rows"] = keep + add
    out["author"] = f"含蓝图作者在「准入口限速」表里填的 {len(add)} 个口"
    return out


def export_fixes(name: str) -> dict:
    """导出 zip 里的 fixes.json：作者的修正，去掉每个据点的准入口段（gates：已经合进 <据点>/gate_limits.json，导入方按游戏读数用）。"""
    allf = load_fixes(name)
    return {b: ({k: v for k, v in sect.items() if k != "gates"} if isinstance(sect, dict) else sect) for b, sect in allf.items()}


def confirm_progress(name: str, base: str) -> dict:
    """这个据点设计分析里多少台设备、多少条推断连线确认过（只数现在 design.json 里还有的）。"""
    dj = _jload(pdir(name) / "base" / base / "design.json", {}) or {}
    c = load_confirm(name).get(base, {})
    dkeys = [device_key(r) for r in dj.get("devices", []) if not r.get("recipe_unknown")]  # 配方未知的行没有可确认的结果（2026-11-19），不算进分母
    lkeys = [link_key(lk) for lk in dj.get("inferred_links", [])]
    dv, lv = c.get("devices", {}), c.get("links", {})
    return {"devices": [sum(1 for k in dkeys if k in dv), len(dkeys)], "links": [sum(1 for k in lkeys if k in lv), len(lkeys)],
            "wrong": sum(1 for k in dkeys if dv.get(k, {}).get("verdict") == "wrong") + sum(1 for k in lkeys if lv.get(k, {}).get("verdict") == "wrong")}


# ------------------------------------------------------------------ 反馈包
FEEDBACK_LIMIT = 24 * 1024 * 1024  # GitHub issue 附件单个 25 MB：留 1 MB 余量
_FB_IMAGES = (".png", ".jpg", ".jpeg")
# 每张蓝图读出来的结构文件（bp/<蓝图>/ 下）：设备一览、尺寸、暗管和供电桩、暗管配对、物流件。据点拼合、设计分析、管网都要读，
# 导出 .bpa.zip 和反馈包共用这一份清单。mosaic.png（拼好的预览）另算：它是图，只有蓝图格子，没有游戏界面
BP_JSON = ("devices.json", "size.json", "anchors.json", "links.json", "logistics.json")


def feedback_groups(name: str, raw: bool = False, logs: list[Path] | None = None) -> list[tuple[str, list[tuple[Path, str]]]]:
    """反馈包里放什么：[(组名, [(源文件, 包内路径)])]。
    默认只放重算要用的：项目级的 json（输入端、修正、确认、准入口分组）、每个据点的底账、拼合图和全部 json、
    每张蓝图的结构文件（BP_JSON）和拼好的预览 mosaic.png、最近的任务日志。
    标注图（*.jpg）都能从这些重新生成，不放。**不放游戏原始截图**：读蓝图的帧、点暗管的截图、扫底账的逐屏图很大（皇帝产线 1.1 GB），
    而且截图里可能带 UID；raw=True 才放（排查读图 / 拼接问题时用）。
    2026-11-14 issue #1：来回四轮里三轮是在跟作者要文件；用户定：做成设置里一键导出。
    2026-11-19：原来 bp/ 整个算「原始截图」不放，可设计分析和管网的暗管配对要读 bp/<蓝图>/links.json——别人的武陵城反馈包里没有，
    本地重算时暗管配对是 0 对，作者机器上有没有也看不出来。结构文件很小（皇帝产线 9 张蓝图共 0.1 MB），mosaic 共 13 MB，默认带上；
    frames/、link_shots/ 这些游戏截图仍然只在 raw 时带。"""
    proj = load(name)
    d = pdir(name)
    g0 = [(d / f, f) for f in ("project.json", "inputs.json", "fixes.json", "confirm.json", "gates.json", "gate_icons.png") if (d / f).exists()]
    for base in proj["groups"]:
        lp = ledger_path(name, base)
        if lp:
            g0.append((lp, f"ledger/{base}.json"))
    for lg in logs or []:
        g0.append((lg, f"logs/{lg.name}"))
    groups = [("项目", g0)]
    for base in proj["groups"]:
        bd = d / "base" / base
        if bd.is_dir():
            fs = [(f, f"base/{base}/{f.name}") for f in sorted(bd.iterdir()) if f.suffix == ".json" or f.name == "composite.png"]
            if fs:
                groups.append((base, fs))
    bpd = d / "bp"
    fs = [(one / f, f"bp/{one.name}/{f}") for one in (sorted(p for p in bpd.iterdir() if p.is_dir()) if bpd.is_dir() else [])
          for f in (*BP_JSON, "mosaic.png") if (one / f).is_file()]
    if fs:
        groups.append(("蓝图", fs))
    took = {f for _, g in groups for f, _ in g}
    if raw:
        for sub, label in (("bp", "读蓝图原图"), ("ledger_scan", "扫底账原图")):
            root = d / sub
            if root.is_dir():
                for one in sorted(p for p in root.iterdir() if p.is_dir()):
                    fs = [(f, f"{sub}/{one.name}/{f.relative_to(one).as_posix()}") for f in sorted(one.rglob("*")) if f.is_file() and f not in took]
                    if fs:
                        groups.append((f"{label}-{one.name}", fs))
    return groups


def _fb_est(f: Path) -> int:
    """压缩后大概多大：图片压不动，文本按四分之一算。"""
    n = f.stat().st_size
    return n if f.suffix.lower() in _FB_IMAGES else n // 4 + 200


def _fb_split(files: list[tuple[Path, str]], limit: int) -> list[list[tuple[Path, str]]]:
    parts, cur, size = [], [], 0
    for f, arc in files:
        e = _fb_est(f)
        if cur and size + e > limit:
            parts.append(cur); cur, size = [], 0
        cur.append((f, arc)); size += e
    if cur:
        parts.append(cur)
    return parts


def export_feedback(name: str, out_dir: str | Path, raw: bool = False, logs: list[Path] | None = None, version: str = "",
                    limit: int = FEEDBACK_LIMIT, log=print) -> list[dict]:
    """打反馈包到 out_dir：每个压缩包都能单独解压、都不超过 limit（只有单个文件自己就超了的那一包例外，说明里会写）。
    返回 [{path, size, files, note}]。"""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    todo = []  # (组名, 这一包的文件)
    for gname, files in feedback_groups(name, raw, logs):
        parts = _fb_split(files, limit)
        for k, part in enumerate(parts, 1):
            todo.append((gname if len(parts) == 1 else f"{gname}-{k}", part))
    merged = []  # 挨着的小包并成一个（皇帝产线三个据点加起来才 9 MB：一个文件比四个好拖）
    for gname, part in todo:
        est = sum(_fb_est(f) for f, _ in part)
        if merged and merged[-1][2] + est <= limit:
            merged[-1] = (merged[-1][0] + [gname], merged[-1][1] + part, merged[-1][2] + est)
        else:
            merged.append(([gname], part, est))
    todo = [("全部" if len(merged) == 1 else "+".join(ns) if len(ns) <= 3 else f"{ns[0]}等{len(ns)}组", part) for ns, part, _ in merged]
    done = []
    while todo:
        gname, part = todo.pop(0)
        tmp = out_dir / "_写入中.zip"
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
            for f, arc in part:
                z.write(f, arc)
        size = tmp.stat().st_size
        if size > limit and len(part) > 1:  # 估小了：对半拆开重打
            tmp.unlink()
            h = len(part) // 2
            todo[:0] = [(f"{gname}a", part[:h]), (f"{gname}b", part[h:])]
            continue
        done.append({"group": gname, "tmp": tmp.rename(out_dir / f"_{len(done)}.zip"), "size": size, "files": [arc for _, arc in part],
                     "note": "这一个文件自己就超过 24 MB，GitHub 传不上，要走别的方式发" if size > limit else ""})
    n = len(done)
    res = []
    for k, it in enumerate(done, 1):
        dest = out_dir / f"反馈包_{name}_{stamp}_{k}of{n}_{it['group']}.zip"
        readme = "\n".join([f"终末地基建诊断 反馈包（第 {k} 个，共 {n} 个；每个都能单独解压）", f"项目：{name}", f"程序版本：{version or '未知'}",
                            f"导出时间：{time.strftime('%Y-%m-%d %H:%M:%S')}", f"这一包：{it['group']}，{len(it['files'])} 个文件",
                            "内容：" + ("含游戏原始截图（可能带 UID，发之前请自己看一眼）" if raw else "不含游戏原始截图，只有拼合图、蓝图预览拼图（mosaic.png）和分析数据"),
                            "", *it["files"]])
        with zipfile.ZipFile(it["tmp"], "a", zipfile.ZIP_DEFLATED) as z:
            z.writestr("说明.txt", readme)
        it["tmp"].rename(dest)
        res.append({"path": str(dest), "name": dest.name, "size": dest.stat().st_size, "files": len(it["files"]), "note": it["note"]})
        log(f"  {dest.name}  {dest.stat().st_size / 1048576:.1f} MB，{len(it['files'])} 个文件" + (f"（{it['note']}）" if it["note"] else ""))
    return res


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
        # 作者的修正（池子配方、进料…）：导入方的断水影响（near_patrol → design.cut_impact）按它重算，导入时放到 档案/bpa/fixes.json。
        # 0.10.13 以前不带，导入方不认作者勾的池子配方。准入口段去掉，合进下面的 gate_limits.json
        z.writestr("fixes.json", json.dumps(export_fixes(name), ensure_ascii=False, indent=1))
        for base in proj["groups"]:  # 档案里游戏内读到的配方表、准入口限速（导入后巡检和设计分析都要用）
            src = arch_dir / "ledger" / f"{base}_recipes.json" if arch_dir else None
            if src is not None and src.exists():
                z.write(src, f"{base}/recipes.json")
            gl = gate_limits_export(name, base)  # 作者在准入口表里填过的：和游戏读数合成一份；没填过的照旧原样带档案文件
            src = arch_dir / "ledger" / "gate_limits" / f"{base}.json" if arch_dir else None
            if gl is not None:
                z.writestr(f"{base}/gate_limits.json", json.dumps(gl, ensure_ascii=False, indent=1))
            elif src is not None and src.exists():
                z.write(src, f"{base}/gate_limits.json")
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
                for f in BP_JSON:
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
