"""桌面程序的后端接口（pywebview js_api）。

界面只做展示，所有动作都在这里：读简报 → 诊断（快速），传送 + 俯瞰巡检 → 读简报 → 合并（深入）。
一次只跑一个任务（Job），任务在后台线程里跑，界面轮询 job() 拿日志和进度。
现有模块都用 print 报进度，任务期间把 stdout 接到任务日志里。
"""

from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field, asdict
from pathlib import Path

if getattr(sys, "frozen", False):  # 打包后所有模块都在解包目录里，是顶层模块
    SRC = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
else:
    SRC = Path(__file__).resolve().parent.parent
for _p in (SRC, SRC / "tools"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import blueprint as bp  # noqa: E402
from diagnose import DEBUG, DATA  # noqa: E402

REPORT_PREFIX = "app_report_"
REPORT_KIND = {"quick": "快速", "patrol": "深度"}  # 报告文件名里的类型（定向巡检也是 patrol → 深度）
LOG_PREFIX = "app_log_"  # 任务进度日志：app_log_<类型>_<开始时间>.log，边跑边写（2026-11-12：群友反馈时界面右边的进度栏只能截图）


@dataclass
class Job:
    kind: str = ""  # quick / patrol
    state: str = "idle"  # idle / running / done / error / cancelled
    step: str = ""
    started: float = 0.0
    finished: float = 0.0
    log: list[str] = field(default_factory=list)
    areas: dict[str, str] = field(default_factory=dict)  # 分区 → 等待 / 进行中 / 完成 / 失败
    report_id: str | None = None
    error: str = ""
    cancel: bool = False
    result: dict | None = None  # 非报告类任务（蓝图识别、更新基准）的结果
    log_file: str = ""  # 这次任务的进度日志文件名（在数据目录里）

    def snapshot(self) -> dict:
        d = asdict(self)
        d["log"] = self.log[-400:]
        d["elapsed"] = (self.finished or time.time()) - self.started if self.started else 0
        return d


class _LogWriter(io.TextIOBase):
    """把 print 的内容逐行塞进任务日志，同时照常写到原来的 stdout。"""

    def __init__(self, job: Job, orig, file=None):
        self._job, self.orig, self.buf, self.file = job, orig, "", file

    def add(self, line: str):
        self._job.log.append(line)
        if self.file is not None:
            try:
                self.file.write(f"[{time.strftime('%H:%M:%S')}] {line}\n")
                self.file.flush()  # 边跑边写：程序中途被关掉也留得下
            except Exception:
                pass

    def write(self, s):
        if self.orig is not None:
            try:
                self.orig.write(s)
            except Exception:
                pass
        self.buf += s
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            if line.strip():
                self.add(line.rstrip())
        return len(s)

    def flush(self):
        if self.orig is not None:
            try:
                self.orig.flush()
            except Exception:
                pass


def _leave_overhead() -> None:
    """出错后尽量回到大世界：在俯瞰里就按 CapsLock 退出（开着面板先 Esc）。只按这两个键，确认不了就算了。"""
    try:
        import full_patrol
        import navigate
        nav = navigate._get()
        if nav.scene() != "Scene_Overhead":
            return
        print("  出错时停在俯瞰里，先退出俯瞰，后面的分区才能传送")
        if not full_patrol.exit_overhead():
            nav.key("esc")  # 多半开着面板：关掉再退
            time.sleep(1.0)
            full_patrol.exit_overhead()
    except Exception as e:  # noqa: BLE001
        print(f"  退出俯瞰没成功：{type(e).__name__}: {e}")


def _ake_icon_b64(item_id: str, size: int = 40) -> str:
    """AKEData 物品图标 → 深色底小图（data URL），给「准入口图标分组」的候选用。"""
    import json
    import cv2
    import numpy as np
    import panel_recipe as PR
    from paths import ROOT
    icons = json.loads((ROOT / "data" / "item_icons.json").read_text(encoding="utf-8"))
    a = PR.icon(icons.get(item_id, item_id))
    if a is None:
        return ""
    h, w = a.shape[:2]
    sc = size / max(h, w)
    a = cv2.resize(a, (max(1, round(w * sc)), max(1, round(h * sc))), interpolation=cv2.INTER_AREA)
    bg = np.full((size, size, 3), 40, np.float32)
    al = a[..., 3:4] / 255.0
    y0, x0 = (size - a.shape[0]) // 2, (size - a.shape[1]) // 2
    bg[y0:y0 + a.shape[0], x0:x0 + a.shape[1]] = a[..., :3] * al + bg[y0:y0 + a.shape[0], x0:x0 + a.shape[1]] * (1 - al)
    ok, buf = cv2.imencode(".png", bg.astype(np.uint8))
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")


class Api:
    def __init__(self):
        self._job = Job()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------ 查询
    def version(self) -> dict:
        from paths import ROOT
        return {"name": "终末地基建诊断", "version": "0.10.7", "root": str(ROOT), "debug": str(DEBUG), "frozen": bool(getattr(sys, "frozen", False))}

    def blueprints(self) -> list[dict]:
        out = []
        for d in sorted((DATA / "blueprints").iterdir()):
            if not d.is_dir() or d.name.startswith("_"):
                continue  # _ 开头的是回归测试用的旧档案，不给界面
            plan = bp.load(d.name)
            regions = {
                r: [{"name": a, "main": bool(plan.area_options(a).get("main"))} for a in areas]
                for r, areas in plan.areas.items()
            }
            out.append({
                "name": d.name,
                "regions": regions,
                "targets": plan.playbook.get("targets", []),
                "has_reference": (d / "reference_brief.json").exists(),
                "notes": plan.playbook.get("doc", ""),
                "rotating_plants": plan.rotating_plants,
                "transfers": bp.transfers(plan.playbook),
                "core_ports": self._core_summaries(d.name, [a for areas in plan.areas.values() for a in areas]),
            })
        return out

    @staticmethod
    def _core_summaries(name: str, areas: list[str]) -> dict:
        """各分区上次读到的核心取货口（任务计划第 12 项），一句话。"""
        try:
            import core_ports
            return {a: s for a in areas if (s := core_ports.summary(name, a))}
        except Exception:
            return {}

    def save_rotating_plants(self, name: str, pairs: list[dict]) -> dict:
        """两种植物轮换（任务计划第 13 项）：整张表覆盖写进档案的 tier_rules.json。"""
        import blueprint_tools as bt
        try:
            return {"ok": True, "pairs": bt.set_rotating_plants(name, pairs)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def save_transfers(self, name: str, rows: list[dict]) -> dict:
        """跨地区传输（可选设置）：整张表覆盖写进档案的 playbook.json。"""
        import blueprint_tools as bt
        try:
            return {"ok": True, "transfers": bt.set_transfers(name, rows, self.known_regions())}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}" if not isinstance(e, ValueError) else str(e)}

    def known_regions(self) -> list[str]:
        """程序认识的地区（地图地区总览的页签）。"""
        try:
            import navigate
            return list(navigate.REGION_TABS)
        except Exception:
            return ["武陵", "四号谷地"]

    def game_status(self) -> dict:
        """游戏窗口有没有、当前在哪个界面。找不到窗口不抛异常，界面显示「未连接」。"""
        try:
            import game
            game.init_toolkit()
            w = game.find_game_window()
            if w is None:
                return {"connected": False, "scene": "", "hint": "没找到游戏窗口（Endfield）。请先启动游戏并进入大世界。"}
            bad = game.window_problem(w.hwnd)
            if bad:
                return {"connected": False, "scene": "", "hint": bad, "window": w.window_name}
            import navigate
            if self._job.state != "running":
                game.reset_sizes()  # 用户改过窗口（如窗口化改全屏）后，常驻的连接按现在的尺寸重新记
            try:
                scene = navigate._get().scene()
            except game.Cancelled:  # 任务正在取消：界面状态这一轮先不读
                scene = ""
            names = {"Scene_World": "大世界", "Scene_Overhead": "俯瞰", "Scene_Brief": "简报"}
            out = {"connected": True, "scene": names.get(scene, scene or "未知界面"), "scene_id": scene, "window": w.window_name}
            if game.hdr_on(w.hwnd):
                out["hint"] = game.HDR_HINT
            return out
        except Exception as e:
            return {"connected": False, "scene": "", "hint": f"连接失败：{e}"}

    def job_status(self) -> dict:
        return self._job.snapshot()

    def cancel(self) -> bool:
        """请求取消：下一次给游戏发点击/按键/截图时就停（game.Cancelled），游戏停在当时的画面。"""
        self._job.cancel = True
        try:
            import game
            if self._job.state == "running":
                game.request_stop()
        except ImportError:
            pass
        self._job.log.append("（已请求取消，下一步操作前停止）")
        return True

    # ------------------------------------------------------------ 任务
    def start_quick(self, blueprint: str, regions: list[str] | None = None) -> dict:
        return self._start("quick", self._quick, blueprint, regions or None)

    def start_patrol(self, blueprint: str, areas: list[str] | None = None, read_brief: bool = True) -> dict:
        return self._start("patrol", self._patrol, blueprint, areas or None, read_brief)

    def start_targeted(self, blueprint: str, targets: list[dict], read_brief: bool = True) -> dict:
        """定向巡检：targets = [{"area": 分区, "machines": [设备类型…], "checks": bool}]。
        每个分区只扫一遍、只点这些设备类型的面板，不做两遍对照、不点物流件；checks 为真时跑错题本定点核查。
        排查树 ✗ 步骤的 where 直接就是一条 target（可能含多个分区）。"""
        areas = list(dict.fromkeys(t["area"] for t in targets))
        return self._start("patrol", self._patrol, blueprint, areas, read_brief, targets)

    def near_baselines(self, blueprint: str, areas: list[str]) -> dict:
        """各分区近景巡检的结构基线：几台设备、什么时候存的。"""
        try:
            import near_patrol
            return {"ok": True, "areas": [near_patrol.baseline_info(blueprint, a) for a in areas]}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def reset_near_baseline(self, blueprint: str, area: str) -> dict:
        """重设基线（任务计划第 8 项）：把这个分区的旧基线挪进备份目录，下次近景巡检重新存。基地改过布局后用。"""
        if self._job.state == "running":
            return {"ok": False, "error": "有任务在跑，等它结束再重设"}
        try:
            import near_patrol
            return {"ok": True, **near_patrol.reset_baseline(blueprint, area)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def start_gate_limits(self, blueprint: str, areas: list[str] | None = None) -> dict:
        """读准入口限速：每个分区进俯瞰近景，逐个点开准入口面板读限速（只读，不点开关）。结果 job.result = {分区: {rows, error}}。"""
        return self._start("gates", self._gate_limits, blueprint, areas)

    def gate_limits(self, blueprint: str, area: str) -> dict:
        """上次读到的准入口限速（档案 ledger/gate_limits/<分区>.json）。"""
        import near_patrol
        p = near_patrol.archive_dir(blueprint) / "ledger" / "gate_limits" / f"{area}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _gate_limits(self, job: Job, blueprint: str, areas: list[str] | None) -> None:
        import full_patrol
        import gate_limits
        import near_patrol
        plan = bp.load(blueprint)
        out = {}
        for i, (region, area) in enumerate(full_patrol.plan_areas(plan, areas), 1):
            if job.cancel:
                break
            ok, why = near_patrol.ledger_ready(plan.name, area)
            if not ok:
                print(f"[{area}] {why}，跳过")
                out[area] = {"rows": [], "error": why}
                continue
            job.step = f"读准入口限速 {area}（{i}）"
            r = gate_limits.read_area(region, area, plan.name, log=print, cancelled=lambda: job.cancel)
            out[area] = {"rows": r["rows"], "error": r["error"], "path": str(r["path"] or "")}
            if r["error"]:
                print(f"  {area}：{r['error']}")
        job.result = out
        return None

    def start_core_ports(self, blueprint: str, areas: list[str] | None = None) -> dict:
        """读协议核心/次级核心 6 个取货口设的物品（任务计划第 12 项）：传送到基地、进俯瞰，逐个点三角开面板读、悬停读物品名、关掉。"""
        return self._start("cores", self._core_ports, blueprint, areas)

    def core_ports(self, blueprint: str, area: str) -> dict:
        """上次读到的核心取货口（档案 ledger/core_ports/<分区>.json）。"""
        import core_ports
        return core_ports.load(blueprint, area)

    def _core_ports(self, job: Job, blueprint: str, areas: list[str] | None) -> None:
        import core_ports
        import full_patrol
        plan = bp.load(blueprint)
        out = {}
        for i, (region, area) in enumerate(full_patrol.plan_areas(plan, areas), 1):
            if job.cancel:
                break
            job.step = f"读核心取货口 {area}（{i}）"
            r = core_ports.read_area(region, area, plan.name, log=print)
            out[area] = {"rows": r["rows"], "error": r["error"], "path": str(r["path"] or "")}
            if r["error"]:
                print(f"  {area}：{r['error']}")
        job.result = out
        return None

    # ---- 蓝图档案
    def start_blueprint_survey(self, name: str, regions: list[str] | None = None) -> dict:
        """新建档案第一步：读全部地区的简报（当成基准）、推最终产物，再到每个在用地区的总览里逐分区找基地。
        结果在 job_status().result：{name, brief, regions: {地区: [{name, has_base, selected, main, title}]}, targets, products}。"""
        return self._start("survey", self._survey, name, regions or None)

    def start_update_reference(self, name: str, merge: bool = False) -> dict:
        """把当前简报存为档案的参考简报（基地改过、源头设备数量变了之后用）。merge=True 和现有基准合并取最大值（同一蓝图多抓几次）。"""
        return self._start("reference", self._reference, name, merge)

    def save_blueprint(self, draft: dict) -> dict:
        """向导确认后写档案。draft 来自 survey 的 result，分区/产物的 selected、main、per_min 可被用户改过。"""
        import blueprint_tools as bt
        try:
            brief = json.loads(Path(draft["brief"]).read_text(encoding="utf-8"))
            name = bt.save_archive(draft, brief)
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def import_blueprint(self, kind: str = "folder") -> dict:
        """弹文件对话框选档案文件夹或 .zip，复制进 data/blueprints。"""
        import blueprint_tools as bt
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return {"ok": False, "error": "浏览器调试模式没有文件对话框，请用 exe"}
            if kind == "zip":
                picked = win.create_file_dialog(webview.FileDialog.OPEN, file_types=("蓝图档案 (*.zip)", "所有文件 (*.*)"))
            else:
                picked = win.create_file_dialog(webview.FileDialog.FOLDER)
            if not picked:
                return {"ok": False, "error": "没有选择"}
            from bpa import importer
            if kind == "zip" and importer.is_bpa_zip(picked[0]):
                # 作者的蓝图分析 zip：有分区盖得住它的现有档案时先问合并还是新建（界面再调 import_bpa）
                info = importer.peek(picked[0])
                cands = importer.merge_candidates(info["groups"])
                if cands:
                    return {"ok": True, "choose": True, "path": picked[0], "info": info, "candidates": cands}
            name = bt.import_archive(picked[0])
            return {"ok": True, "name": name}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def import_bpa(self, path: str, into: str = "") -> dict:
        """导入作者的蓝图分析 zip：into 非空 = 合并进这个现有档案（保留它的错题本、参考简报、备路规则），空 = 新建档案。"""
        from bpa import importer
        try:
            return {"ok": True, "name": importer.import_zip(path, None, into=into or None), "merged": bool(into)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def export_blueprint(self, name: str) -> dict:
        import blueprint_tools as bt
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            if win is None:
                return {"ok": False, "error": "浏览器调试模式没有文件对话框，请用 exe"}
            picked = win.create_file_dialog(webview.FileDialog.SAVE, save_filename=f"{name}.zip", file_types=("蓝图档案 (*.zip)",))
            if not picked:
                return {"ok": False, "error": "没有选择"}
            dest = picked if isinstance(picked, str) else picked[0]
            return {"ok": True, "path": bt.export_archive(name, dest)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def delete_blueprint(self, name: str) -> dict:
        import blueprint_tools as bt
        try:
            bt.delete_archive(name)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    # ---- 蓝图分析（docs/蓝图分析器-设计.md）
    def bpa_projects(self) -> list[dict]:
        from bpa import project as P
        return P.list_projects()

    def bpa_delete_info(self, name: str) -> dict:
        from bpa import project as P
        try:
            return {"ok": True, **P.delete_info(name)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_delete(self, name: str) -> dict:
        """删除蓝图分析项目：挪进备份目录（不真删）。有任务在跑时不删（读蓝图 / 拼合正写着这个目录）。"""
        if self._job.state == "running":
            return {"ok": False, "error": "有任务在跑，等它结束再删"}
        from bpa import project as P
        try:
            return {"ok": True, **P.delete(name)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_create(self, name: str, groups_text: str, archive: str = "") -> dict:
        """groups_text：每行「据点：蓝图1，蓝图2…」（中英文冒号/逗号都行）。archive：底账所在的蓝图档案。"""
        import re
        from bpa import project as P
        try:
            name = name.strip()
            if not name or re.search(r'[\\/:*?"<>|]', name):
                return {"ok": False, "error": "项目名不能为空，也不能含 \\ / : * ? \" < > |"}
            groups = {}
            for line in groups_text.splitlines():
                if not line.strip():
                    continue
                m = re.match(r"\s*([^:：]+)[:：](.*)", line)
                if not m:
                    return {"ok": False, "error": f"这一行看不懂：{line}（格式：据点：蓝图1，蓝图2）"}
                bps = [b.strip() for b in re.split(r"[,，、\s]+", m.group(2)) if b.strip()]
                if bps:
                    groups[m.group(1).strip()] = bps
            if not groups:
                return {"ok": False, "error": "至少写一个据点和它的蓝图"}
            P.create(name, groups, archive)
            return {"ok": True, "status": P.status(name)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_status(self, name: str) -> dict:
        from bpa import project as P
        try:
            return {"ok": True, "status": P.status(name)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def start_bpa_read(self, name: str, only: list[str] | None = None) -> dict:
        """进游戏读蓝图：逐张打开预览，读尺寸/设备一览、采集拼接、认暗管、单击配对。游戏要停在 F1 共享蓝图列表页。"""
        return self._start("bpa", self._bpa_read, name, only or None)

    def start_bpa_ledger(self, name: str, base: str) -> dict:
        """进游戏扫某个据点的远景底账（人要在这个据点的俯瞰里）。只按方向键、滚轮和 Ctrl+R（转视角补拍），不点任何东西。"""
        return self._start("bpa", self._bpa_ledger, name, base)

    def start_bpa_fit(self, name: str) -> dict:
        """离线：按底账自动锚点把蓝图拼到基地上，出核对图、暗管连线图、输入端候选。"""
        return self._start("bpa", self._bpa_fit, name)

    def bpa_image(self, name: str, base: str, which: str = "check", scale: float = 0.6) -> dict:
        import cv2
        from bpa import project as P, vision as V
        if which == "gates":  # 全项目的准入口图标分组图，不分据点、不缩放
            f, scale = P.pdir(name) / "gate_icons.png", 1.0
        else:
            f = P.pdir(name) / "base" / base / {"check": "check.jpg", "flow": "flow.jpg", "links": "links.jpg", "logistics": "logistics.jpg", "placement": "placement.jpg",
                                                "design": "design.jpg", "composite": "composite.png"}.get(which, "check.jpg")
        if not f.exists():
            return {"ok": False, "error": "还没生成（先跑「据点拼合」）"}
        im = cv2.resize(V.rd(f), None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", im, [cv2.IMWRITE_JPEG_QUALITY, 85])
        return {"ok": True, "image": "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode("ascii")}

    def bpa_design(self, name: str, base: str) -> dict:
        """设计分析计算器的结果（base/<据点>/design.json）：每台设备的配方、开工率、状态、卡在哪、依据；推断连线清单；汇总。"""
        from bpa import project as P
        f = P.pdir(name) / "base" / base / "design.json"
        if not f.exists():
            return {"ok": False, "error": "还没算（先跑「据点拼合」）"}
        d = json.loads(f.read_text(encoding="utf-8"))
        conf = P.load_confirm(name).get(base, {})
        rows = []
        for r in d.get("devices", []):
            row = {k: r.get(k) for k in ("device", "c", "recipe", "recipe_inferred", "env_required", "via_inferred", "rate", "state",
                                         "limit_by", "got", "need", "basis", "if_main_blocked")}
            row["key"] = P.device_key(r)
            row["confirm"] = conf.get("devices", {}).get(row["key"])
            rows.append(row)
        links = [dict(lk, key=P.link_key(lk), confirm=conf.get("links", {}).get(P.link_key(lk))) for lk in d.get("inferred_links", [])]
        return {"ok": True, "summary": d.get("summary") or {}, "devices": rows, "inferred_links": links, "actuals": list(P.ACTUALS),
                "progress": P.confirm_progress(name, base), "recipe_fixes": d.get("recipe_fixes", []), "gates": d.get("gates", [])}

    def bpa_confirm(self, name: str, base: str, kind: str, entries: dict) -> dict:
        """作者确认（任务计划第 1 项）：kind = devices / links；entries = {键: {verdict: ok/wrong, actual, note} 或 None 撤销}。"""
        from bpa import project as P
        try:
            return {"ok": True, "progress": P.save_confirm(name, base, kind, entries)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_gate_groups(self, name: str) -> dict:
        """准入口图标分组：每组的图标、个数、形态（液/气/固/未知）、名字、定出来的管道/物品。"""
        import cv2
        from bpa import project as P, vision as V
        d = P.pdir(name)
        gj = P._jload(d / "gates.json")
        if not gj:
            return {"ok": False, "error": "还没生成（先跑「据点拼合」）"}
        rows = []
        for k, v in sorted(gj["groups"].items(), key=lambda kv: int(kv[0])):
            f = d / "gate_groups" / f"G{k}.png"
            img = ""
            if f.exists():
                ok, buf = cv2.imencode(".png", cv2.resize(V.rd(f), (46, 46), interpolation=cv2.INTER_NEAREST))
                img = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
            cands = []
            for c in v.get("candidates", []):
                ci = _ake_icon_b64(c["id"])
                cands.append(dict(c, icon=ci))
            rows.append(dict(v, k=k, icon=img, candidates=cands))
        return {"ok": True, "groups": rows}

    def bpa_label_gates(self, name: str, labels: dict) -> dict:
        """作者标图标组的形态/名字：{组号: {state: 液/气/固, item: 名字}}。存进项目图标库，重算据点拼合的图。"""
        from bpa import project as P
        try:
            P.label_gate_groups(name, labels)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_inputs(self, name: str, base: str) -> dict:
        from bpa import project as P
        try:
            return {"ok": True, **P.inputs_view(name, base)}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_save_inputs(self, name: str, base: str, marks: list[dict]) -> dict:
        from bpa import project as P
        try:
            P.save_inputs(name, base, marks)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def bpa_export(self, name: str, lite: bool = True) -> dict:
        """lite：给玩家的精简包（约 30 MB，只带导入后巡检要用的）；否则完整包（含原始截图，约 200+ MB，给开发核对）。"""
        from bpa import project as P
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            fn = f"{name}{'' if lite else '-完整'}.bpa.zip"
            if win is None:  # 浏览器调试：直接导出到 debug/bpa/<项目>/
                return {"ok": True, "path": str(P.export(name, P.pdir(name) / fn, lite=lite))}
            picked = win.create_file_dialog(webview.FileDialog.SAVE, save_filename=fn, file_types=("蓝图分析包 (*.zip)",))
            if not picked:
                return {"ok": False, "error": "没有选择"}
            return {"ok": True, "path": str(P.export(name, picked if isinstance(picked, str) else picked[0], lite=lite))}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _bpa_read(self, job: Job, name: str, only: list[str] | None) -> None:
        from bpa import game_ops, project as P
        job.step = "连接游戏"
        g = game_ops.Game(log=print)
        try:
            if not g.on_list():
                raise RuntimeError("请先在游戏里按 F1 打开蓝图列表（「共享蓝图」或「我的蓝图」页签），并让要分析的蓝图卡片显示在屏幕上")
            g.check_tab()  # 在「系统蓝图」页签时直接说明，不去点（共享蓝图、我的蓝图都能读）
            job.step = "读蓝图"
            P.read_blueprints(name, g, only, log=print, cancelled=lambda: job.cancel)
        finally:
            g.close()
        if not job.cancel:
            job.step = "据点拼合"
            P.fit_bases(name, log=print)
        job.result = P.status(name)

    def _bpa_ledger(self, job: Job, name: str, base: str) -> None:
        from bpa import project as P
        import patrol as patrol_mod
        job.step = "连接游戏"
        try:
            pt = patrol_mod.Patrol()
        except SystemExit as e:
            raise RuntimeError(str(e))
        sc = pt.scene()
        if sc not in ("Scene_Overhead", "Scene_World"):
            raise RuntimeError(f"游戏不在大地图/俯瞰（当前 {sc}）：请先走到「{base}」据点里，再点扫底账")
        job.step = f"扫底账：{base}"
        P.scan_ledger(name, base, pt, log=print, cancelled=lambda: job.cancel)
        if not job.cancel:
            job.step = "据点拼合"
            P.fit_bases(name, log=print)
        job.result = P.status(name)

    def _bpa_fit(self, job: Job, name: str) -> None:
        from bpa import project as P
        job.step = "据点拼合"
        P.fit_bases(name, log=print)
        job.result = P.status(name)

    def _survey(self, job: Job, name: str, regions: list[str] | None) -> None:
        import blueprint_tools as bt
        import navigate

        job.step = "读简报（当基准）"
        print("== 第 1 步：读全部地区的简报，当作这套蓝图的正常基准")
        brief_path = self._read_brief(job, None, regions, all_regions=True)
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        in_use = bt.regions_in_use(brief)
        wanted = [r for r in (regions or in_use) if r in in_use]
        print(f"在生产的地区：{'、'.join(in_use) or '无'}；要识别分区的：{'、'.join(wanted) or '无'}")
        result = {"name": name, "brief": str(brief_path), "regions": {}, "targets": bt.derive_targets(brief, wanted),
                  "products": bt.produced_items(brief, wanted)}
        job.result = result
        nav = navigate._get()
        for i, region in enumerate(wanted, 1):
            if job.cancel:
                break
            job.step = f"找基地：{region}（{i}/{len(wanted)}）"
            print(f"== 第 2 步：{region} 的地区总览里逐个分区找基地")
            try:
                result["regions"][region] = bt.survey_region(nav, region)
            except Exception as e:
                print(f"  {region}：{type(e).__name__}: {e}")
                bt.close_map(nav)
                result["regions"][region] = []
            job.result = dict(result)
        job.step = "识别完成，等待确认"
        print("== 识别完成：请在界面上核对分区和最终产物，然后保存")

    def _reference(self, job: Job, name: str, merge: bool = False) -> None:
        import blueprint_tools as bt

        plan = bp.load(name)
        brief_path = self._read_brief(job, plan, None)
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        n = bt.update_reference(name, brief, merge=merge)
        job.result = {"name": name, "brief": str(brief_path), "rows": n, "merge": merge}
        print(f"已把 {brief_path.name} {'合并进' if merge else '存为'}「{name}」的参考简报（{n} 行）")

    def _start(self, kind, fn, *args) -> dict:
        with self._lock:
            if self._job.state == "running":
                return {"ok": False, "error": "已有任务在跑"}
            self._job = Job(kind=kind, state="running", started=time.time(), step="准备")
            job = self._job
        if kind == "patrol":
            plan = bp.load(args[0])
            wanted = args[1]
            for r, areas in plan.areas.items():
                for a in areas:
                    if wanted is None or a in wanted:
                        job.areas[a] = "等待"

        def run():
            orig = sys.stdout
            stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(job.started))
            log_path = DEBUG / f"{LOG_PREFIX}{REPORT_KIND.get(kind, kind)}_{stamp}.log"
            n = 2
            while log_path.exists():  # 同一秒开了两个任务：别覆盖
                log_path = DEBUG / f"{LOG_PREFIX}{REPORT_KIND.get(kind, kind)}_{stamp}_{n}.log"
                n += 1
            try:
                DEBUG.mkdir(parents=True, exist_ok=True)
                fh = open(log_path, "w", encoding="utf-8")
                job.log_file = log_path.name
            except OSError:
                fh = None
            writer = _LogWriter(job, orig, fh)
            sys.stdout = writer
            try:
                import json as _json
                writer.add(f"任务 {kind}，参数 {_json.dumps(args, ensure_ascii=False, default=str)}，版本 {self.version()['version']}")
            except Exception:
                pass
            try:
                try:
                    import game
                    game.reset_sizes()  # 每个任务按开始时的窗口尺寸算「运行中变了」
                    game.clear_stop()
                except ImportError:
                    pass
                job.report_id = fn(job, *args)
                job.state = "cancelled" if job.cancel else "done"
            except BaseException as e:
                if type(e).__name__ == "Cancelled":
                    print("已取消。游戏停在取消时的画面（可能还在俯瞰或开着面板），需要的话自己按 Esc / CapsLock 退出")
                    job.state = "cancelled"
                    return
                if not isinstance(e, (Exception, SystemExit)):
                    raise  # 老模块连不上游戏时 raise SystemExit("…")：不接住的话线程静默退出、任务一直显示在跑
                job.error = str(e) if isinstance(e, (SystemExit,)) or type(e).__name__ == "WindowShapeError" else f"{type(e).__name__}: {e}"
                writer.add(traceback.format_exc())
                job.state = "error"
            finally:
                job.finished = time.time()
                sys.stdout = orig
                writer.add(f"结束：{job.state}，用时 {job.finished - job.started:.0f} 秒" + (f"，报告 {job.report_id}" if job.report_id else "")
                           + (f"，错误 {job.error}" if job.error else ""))
                if fh is not None:
                    fh.close()
                try:
                    import game
                    game.clear_stop()  # 任务结束：后面读界面状态等不受影响
                except ImportError:
                    pass

        self._thread = threading.Thread(target=run, daemon=True)
        self._thread.start()
        return {"ok": True}

    # ---- 简报
    def _read_brief(self, job: Job, plan: bp.Blueprint | None, regions: list[str] | None, all_regions: bool = False) -> Path:
        """进简报界面（大世界里按 F10）、读指定地区、退出简报。返回 brief_*.json 路径。
        regions 为空时读蓝图涉及的地区；all_regions=True（新建档案）时读下拉里的全部地区。"""
        import navigate
        import brief_reader

        nav = navigate._get()
        job.step = "打开简报"
        scene = nav.scene()
        if scene == "Scene_Overhead":
            nav.key("capslock")
            time.sleep(2.0)
        reader = brief_reader.BriefReader()
        if not reader.is_brief():
            nav.key("f10")
            for _ in range(6):
                time.sleep(0.8)
                if reader.is_brief():
                    break
        if not reader.is_brief():
            raise RuntimeError("打不开简报界面：请确认游戏在大世界里（不在菜单/对话/加载中），再试一次")
        job.step = "读简报"
        if not regions:
            if all_regions or plan is None:
                regions = [t for t, _ in reader.region_options()]
                nav.click(*brief_reader.REGION_DROPDOWN)  # 收起下拉
                time.sleep(0.5)
            else:
                regions = list(plan.areas.keys())
        out, empty = {}, []
        for region in regions:
            if job.cancel:
                break
            print(f"== 读简报：{region}")
            if not reader.select_region(region):
                print(f"  没找到地区「{region}」，跳过")
                continue
            rows = reader.read_region()
            out[region] = [asdict(r) for r in rows]
            print(f"  {region}：{len(rows)} 行")
            if not rows:
                shot = reader.save_page(region)
                print(f"  {region} 一行都没读到，截图存在 {shot.name}")
                empty.append((region, shot.name))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = DEBUG / f"brief_{stamp}.json"
        path.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"简报已保存：{path.name}")
        if navigate.caps_on():
            nav.key("capslock")
        # 关简报：按一次 Esc 游戏偶尔不理（2026-11-12 实测：快速诊断后简报一直开着，接着的深度巡检按 M 读不到地图、景玉谷被跳过），关上为止最多 3 次
        for _ in range(3):
            if not reader.is_brief():
                break
            nav.key("esc")
            time.sleep(1.5)
        else:
            if reader.is_brief():
                print("  简报按了 3 次 Esc 还开着，请手动关掉")
        if empty and not all_regions:
            # 蓝图要的地区一行都没读到：往下诊断只会得出「简报里没有这一行 / 没有这个地区」的误导结论（2026-11-11 灰度群友）
            names = "、".join(r for r, _ in empty)
            raise RuntimeError(f"简报里「{names}」一行都没读到（截图：{'、'.join(s for _, s in empty)}，在数据目录里）。"
                               "常见原因：游戏画面不是 16:9、简报没停在默认的列表页。请把截图和 maa\\debug\\maafw.log 发给作者")
        return path

    def _quick(self, job: Job, blueprint: str, regions: list[str] | None) -> str:
        import diagnose
        import sources

        import expert

        plan = bp.load(blueprint)
        brief_path = self._read_brief(job, plan, regions)
        job.step = "诊断"
        brief = json.loads(brief_path.read_text(encoding="utf-8"))
        tree = expert.run(brief, plan)
        reread = None
        if any(s["status"] == "bad" for s in tree) and not job.cancel:
            # 排查树有 ✗：复读一次简报，两次都 ✗ 的才算（expert.confirm）。复读失败或被取消就按第一次的出报告
            bad = "、".join(s["title"] for s in tree if s["status"] == "bad")
            print(f"排查树有 ✗（{bad}）：再读一次简报确认是不是一时的波动，约 1–2 分钟")
            try:
                again_path = self._read_brief(job, plan, regions)
                again = json.loads(again_path.read_text(encoding="utf-8"))
            except RuntimeError as e:
                again = None
                print(f"  复读没成功（{e}），按第一次读到的出报告")
            if again is not None and not job.cancel:
                tree, cleared = expert.confirm(tree, expert.run(again, plan))
                reread = {"first_brief": brief_path.name, "cleared": cleared}
                brief_path, brief = again_path, again
                print("  复读后恢复正常：" + "、".join(cleared) if cleared else "  复读结果和第一次一样")
            job.step = "诊断"
        regs = [diagnose.Region(name, rows, plan) for name, rows in brief.items() if rows and (not plan.areas or name in plan.areas)]
        text, data = diagnose.report(regs)
        try:
            short = sources.compare(brief, plan)
            src_text, _ = sources.report(brief, plan)
        except FileNotFoundError:
            short, src_text = [], "（蓝图没有参考简报，不比缺矿缺气液）"
        report = {
            "kind": "quick",
            "reread": reread,
            "stamp": time.strftime("%Y%m%d-%H%M%S"),
            "blueprint": plan.name,
            "brief": brief_path.name,
            "tree": tree,
            "regions": data,
            "sources": short,
            "sources_text": "\n".join(expert.to_text(tree)) + "\n\n" + src_text,
            "text": text,
            "elapsed": time.time() - job.started,
        }
        return self._save_report(report)

    # ---- 巡检
    def _patrol(self, job: Job, blueprint: str, areas: list[str] | None, read_brief: bool, targets: list[dict] | None = None) -> str:
        import combine
        import full_patrol

        plan = bp.load(blueprint)
        todo = full_patrol.plan_areas(plan, areas)
        results = [full_patrol.AreaResult(r, a) for r, a in todo]
        by_area: dict[str, dict] = {}
        near_rows: dict[str, list] = {}  # 近景定向巡检每台目标设备的观察（含缺气）
        for t in targets or []:
            cur = by_area.setdefault(t["area"], {"machines": set(), "checks": False})
            cur["machines"] |= set(t.get("machines") or [])
            cur["checks"] = cur["checks"] or bool(t.get("checks"))
        for i, res in enumerate(results, 1):
            if job.cancel:
                job.areas[res.area] = "已取消"
                continue
            job.step = f"{'定向' if targets else ''}巡检 {res.area}（{i}/{len(results)}）"
            job.areas[res.area] = "进行中"
            tgt = by_area.get(res.area)
            print(f"\n[{i}/{len(results)}] {res.region} / {res.area}：开始" + (f"（定向：{'、'.join(sorted(tgt['machines'])) or '按错题本'}）" if tgt else ""))
            try:
                near_ok, why = (False, "")
                if tgt and tgt["machines"]:
                    import near_patrol
                    near_ok, why = near_patrol.ledger_ready(plan.name, res.area)
                    if not near_ok:
                        print(f"  {why}，改用远景扫描的定向巡检")
                if near_ok:
                    # 近景定向巡检：按档案底账直接看目标设备，不点面板；错题本 7.1 核查要点开暗管面板，这里不跑
                    job.step = f"近景定向巡检 {res.area}（{i}/{len(results)}）"
                    r = near_patrol.scan_area(res.region, res.area, plan.name, sorted(tgt["machines"]), log=print,
                                              cancelled=lambda: job.cancel)
                    res.patrol_path, res.devices, res.step, res.error = r["patrol_path"], r["devices"], r["step"], r["error"]
                    res.ok = r["patrol_path"] is not None and not r["error"]
                    near_rows[res.area] = r["rows"]
                    if tgt["checks"]:
                        print("  （错题本 7.1 核查要点开暗管面板，近景定向巡检不做；需要时跑深入巡检）")
                elif tgt:
                    full_patrol.scan_area(res, False, plan.name, only=sorted(tgt["machines"]) or None, single=True, checks=tgt["checks"])
                else:
                    full_patrol.scan_area(res, False, plan.name)
            except Exception as e:
                res.error = f"{type(e).__name__}: {e}"
                print(traceback.format_exc())
                _leave_overhead()  # 扫描中途出错多半停在俯瞰里：不退出的话后面的分区都「还在俯瞰里」传送不了（2026-11-12 群友）
            except BaseException as e:
                if type(e).__name__ != "Cancelled":
                    raise
                res.error = "已取消"
                res.patrol_path = None
                print(f"  {res.area}：已取消，这个分区没扫完，不进报告")
            if res.area != todo[i - 1][1] and todo[i - 1][1] in job.areas:  # 自动探测到真实分区名
                job.areas[res.area] = job.areas.pop(todo[i - 1][1])
            job.areas[res.area] = "完成" if res.ok else "已取消" if res.error == "已取消" else f"失败（{res.step}：{res.error}）"
            print(f"[{i}/{len(results)}] {res.area}：{'扫描完成，' + str(res.devices) + ' 台设备' if res.ok else '已取消' if res.error == '已取消' else '失败在「' + res.step + '」—— ' + res.error}")
        scanned = [r for r in results if r.patrol_path is not None]
        if not scanned:
            if job.cancel:
                print("已取消，没有扫完的分区，不出报告。游戏停在取消时的画面，需要的话自己按 Esc / CapsLock 退出")
                return None
            raise RuntimeError("没有一个分区扫描成功")
        if read_brief and not job.cancel:
            brief_path = self._read_brief(job, plan, None)
        else:
            brief_path = combine.latest("brief_*.json")
            print(f"用最近一份简报：{brief_path.name}")
        job.step = "合并诊断"
        items = [dict(region=r.region, area=r.area, patrol=r.patrol_path, devices=r.devices, error="" if r.ok else f"{r.step}：{r.error}") for r in results]
        report = build_patrol_report(plan, items, brief_path, elapsed=time.time() - job.started, cancelled=job.cancel)
        if targets:
            report["targeted"] = targets
        if near_rows:
            attach_near(report, near_rows, plan)
        return self._save_report(report)

    # ------------------------------------------------------------ 报告
    def _save_report(self, report: dict) -> str:
        """文件名带类型：app_report_快速_<时间>.json / app_report_深度_<时间>.json（2026-11-11 用户定）。
        群友反馈时「发最新的 app_report」容易发错（先跑了深度巡检、又跑了一次快速诊断）。0.10.2 之前的文件没有类型，照样能看。"""
        rid = f"{REPORT_PREFIX}{REPORT_KIND.get(report.get('kind'), '其他')}_{report['stamp']}"
        (DEBUG / f"{rid}.json").write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")
        return rid

    def reports(self) -> list[dict]:
        out = []
        # 按文件名末尾的时间排（带类型以后按整个文件名排会先按类型分组）
        for f in sorted(DEBUG.glob(f"{REPORT_PREFIX}*.json"), key=lambda p: p.stem[-15:], reverse=True):
            try:
                r = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                continue
            summary = {"id": f.stem, "kind": r.get("kind"), "stamp": r.get("stamp"), "blueprint": r.get("blueprint"), "elapsed": r.get("elapsed")}
            if r.get("kind") == "patrol":
                summary["areas"] = [{"area": a["area"], "ok": a["ok"], "counts": a.get("counts", {})} for a in r.get("areas", [])]
                summary["problems"] = sum(a.get("counts", {}).get("问题", 0) for a in r.get("areas", []))
            else:
                summary["problems"] = sum(1 for s in r.get("sources", []) if s.get("kind") in ("缺设备", "来源供不上", "缺地区", "缺物品"))
                summary["tree_bad"] = sum(1 for s in r.get("tree") or [] if s.get("status") == "bad")
                summary["pending"] = sum(len([x for x in reg.get("findings", []) if x["level"] == "待确认"]) for reg in r.get("regions", {}).values())
            out.append(summary)
        return out

    def report(self, rid: str) -> dict | None:
        f = DEBUG / f"{rid}.json"
        if not f.exists() or not rid.startswith(REPORT_PREFIX):
            return None
        return json.loads(f.read_text(encoding="utf-8"))

    def open_folder(self, which: str = "debug") -> bool:
        import os
        from paths import ROOT
        path = DEBUG if which == "debug" else ROOT
        os.startfile(str(path))  # noqa: S606
        return True


def build_patrol_report(plan: bp.Blueprint, items: list[dict], brief_path: Path, elapsed: float = 0.0, cancelled: bool = False) -> dict:
    """items：[{region, area, patrol(Path|None), devices, error}]（按蓝图顺序）。扫到的分区按主分区最后的顺序合并，
    前面分区定位到的理论消耗下降传给主分区。每条问题/待确认设备配 base64 定位图。"""
    import combine

    scanned = [it for it in items if it.get("patrol")]
    combine.set_context_patrols([it["patrol"] for it in scanned])
    drops: dict[str, str] = {}
    by_area: dict[str, dict] = {}
    for it in sorted(scanned, key=lambda x: bool(plan.area_options(x["area"]).get("main"))):
        try:
            text, data = combine.run(brief_path, it["patrol"], plan, merge=False, drops_elsewhere=dict(drops))  # 不合并 debug 里之后的「定点深查」文件（开发期功能）
            for label in data.get("theo_drops_located", []):
                drops.setdefault(label, it["area"])
            attach_images(data)
            counts = {s: len(data["verdicts"][s]) for s in combine.ORDER}
            counts["物流件"] = len(data.get("logistics", []))
            by_area[it["area"]] = {"region": it["region"], "area": it["area"], "ok": True, "devices": it.get("devices", 0),
                                   "counts": counts, "text": text, "data": data}
            print(f"{it['area']}：问题 {counts['问题']}　待确认 {counts['待确认']}　正常 {counts['正常']}")
        except Exception as e:
            print(traceback.format_exc())
            by_area[it["area"]] = {"region": it["region"], "area": it["area"], "ok": False, "devices": it.get("devices", 0),
                                   "error": f"{type(e).__name__}: {e}", "counts": {}, "text": "", "data": None}
    areas = []
    for it in items:
        if it["area"] in by_area:
            areas.append(by_area[it["area"]])
        else:
            areas.append({"region": it["region"], "area": it["area"], "ok": False, "devices": 0, "error": it.get("error") or "未扫描",
                          "counts": {}, "text": "", "data": None})
    try:
        import expert
        power = None  # 巡检读到的顶部电量框，任一分区读到就用（地区级数字）
        for it in scanned:
            try:
                power = json.loads(Path(it["patrol"]).read_text(encoding="utf-8")).get("power") or power
            except Exception:
                pass
        tree = expert.run(json.loads(Path(brief_path).read_text(encoding="utf-8")), plan, power=power)
    except Exception:
        print(traceback.format_exc())
        tree = []
    return {
        "kind": "patrol",
        "stamp": time.strftime("%Y%m%d-%H%M%S"),
        "blueprint": plan.name,
        "brief": Path(brief_path).name,
        "tree": tree,
        "areas": areas,
        "elapsed": elapsed,
        "cancelled": cancelled,
    }


def attach_near(report: dict, near_rows: dict[str, list], plan: bp.Blueprint) -> None:
    """近景定向巡检的逐台观察并进报告：每个分区 area["near"] = {rows, counts}。
    缺气（combine 不管）和「没认出」在这里下结论，计入分区的问题/待确认；阻塞/空闲/关闭的结论在合并诊断里，这里只列观察。"""
    import cv2
    import near_patrol
    from paths import ROOT
    order = {"问题": 0, "待确认": 1, "观察": 2, "设计内": 3, "正常": 4}
    for a in report.get("areas", []):
        rows = near_rows.get(a["area"])
        if rows is None:
            continue
        shots = None
        try:
            pj = json.loads(Path((a.get("data") or {}).get("patrol_path", "")).read_text(encoding="utf-8")) if a.get("data") else None
            shots = ROOT / pj["shots"] if pj else None
        except Exception:
            shots = None
        near_patrol.dedupe_struct(rows)
        reg = None
        try:
            import diagnose
            b = json.loads((DEBUG / report["brief"]).read_text(encoding="utf-8"))
            reg = diagnose.Region(a["region"], b.get(a["region"]) or next(iter(b.values())), plan)
        except Exception:
            reg = None
        for r in rows:
            r["level"], r["text"] = near_patrol.judge_near(r, plan.playbook)
            if (r["level"] == "问题" and (r.get("struct") or {}).get("at_far") and r["name"] in near_patrol.SOURCE_FLUID) or                     (r.get("gate_no") is not None and r["level"] in ("问题", "待确认")):
                try:
                    txt, img, imp = near_patrol.describe_impact(plan.name, a["area"], r, rows, reg)
                except Exception as ex:
                    txt, img, imp = f"影响范围：算不出（{type(ex).__name__}: {ex}）", None, None
                if txt:
                    r["text"] += "。" + txt
                    r["impact"] = imp
                if img is not None:
                    ok, buf = cv2.imencode(".png", img)
                    r["impact_image"] = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
            if r.get("struct_dup_of"):
                r["text"] += f"（旁边的断开处已算在 {r['struct_dup_of']} 上）"
            sti = (r.get("struct") or {}).get("image") if not r.get("struct_dup_of") else None
            if sti and shots is not None and (shots / sti).exists():  # 结构变了：基线 | 这次 对照图
                ok, buf = cv2.imencode(".png", near_patrol.imread(shots / sti))
                r["image"] = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
            elif r.get("gate_no") is not None and r["level"] in ("问题", "待确认", "观察") and shots is not None and r.get("u") and r.get("view"):
                f = shots / f"v{r['view']:02d}_f00.png"  # 输入端：圈出图标和外侧那段管子
                im = near_patrol.imread(f) if f.exists() else None
                if im is not None:
                    x, y = int(r["u"][0]), int(r["u"][1])
                    x0, y0 = max(0, x - 200), max(0, y - 110)
                    crop = im[y0:y0 + 220, x0:x0 + 400].copy()
                    cv2.circle(crop, (x - x0, y - y0), 24, (0, 0, 255), 3)
                    ok, buf = cv2.imencode(".png", crop)
                    r["image"] = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
            elif r["level"] in ("问题", "待确认") and shots is not None:
                k = "strike" if "strike" in r.get("shots", {}) else next(iter(r.get("shots", {})), None)
                if k:
                    f, x, y = r["shots"][k]
                    im = near_patrol.imread(shots / f) if (shots / f).exists() else None
                    if im is not None:
                        x0, y0 = max(0, x - 200), max(0, y - 90)
                        crop = im[y0:y0 + 220, x0:x0 + 400].copy()
                        cv2.circle(crop, (x - x0, y - y0), 22, (0, 0, 255), 3)
                        ok, buf = cv2.imencode(".png", crop)
                        r["image"] = "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode("ascii")
        rows.sort(key=lambda r: (order.get(r["level"], 9), r["name"], r["far"][1], r["far"][0]))
        cnt = {k: sum(1 for r in rows if r["level"] == k) for k in order}
        a["near"] = {"rows": rows, "counts": cnt}
        # 合并诊断里近景设备的空闲/缺料结论：用户确认过的单台设计（playbook.device_designs）改判正常
        data = a.get("data")
        if data and a.get("counts") is not None:
            for lv in ("问题", "待确认"):
                keep = []
                for v in data["verdicts"].get(lv, []):
                    dv = v.get("device") or {}
                    note = near_patrol.device_design(plan.playbook, a["area"], dv.get("name", ""), (dv.get("x", 0), dv.get("y", 0)),
                                                     "idle" if dv.get("badge") == "空闲中" else "blocked") if dv.get("near") and not v.get("title") else None
                    if note:
                        v["level"], v["reason"] = "设计内", f"{note}（原判：{v['reason'][:80]}…）"
                        data["verdicts"].setdefault("正常", []).append(v)
                        a["counts"][lv] -= 1
                        a["counts"]["正常"] = a["counts"].get("正常", 0) + 1
                    else:
                        ex = near_patrol.design_expect(plan.playbook, a["area"], dv.get("name", ""), (dv.get("x", 0), dv.get("y", 0)))                             if dv.get("near") and not v.get("title") else None
                        if ex is not None and float(ex.get("rate", 1)) < 0.995:  # 导入的蓝图分析算出它设计上就跑不满，但作者没确认：只加说明、不改判
                            v["reason"] = f"{v['reason']}（蓝图分析算的设计开工率 {float(ex['rate']):.0%}「{ex.get('state', '')[:12]}」，作者没确认，供参考）"
                        keep.append(v)
                data["verdicts"][lv] = keep
            # 持续阻塞、没断开的水泵：合并诊断按全地区清水总量判「供大于求」，改按它自己那张管网判（用户 2026-11-08）
            moved2 = []
            for v in data["verdicts"].get("正常", []):
                dv = v.get("device") or {}
                if not dv.get("near") or dv.get("name") not in near_patrol.SOURCE_FLUID or "供大于求" not in v.get("reason", ""):
                    continue
                row = next((r for r in rows if r["name"] == dv["name"] and abs(r["far"][0] - dv["x"]) <= 3 and abs(r["far"][1] - dv["y"]) <= 3), None)
                if row is None or ((row.get("struct") or {}).get("boxes") and not row.get("struct_dup_of")):
                    continue  # 自己的管子断了的那台走下面「结构断开」；只是看到邻居断口的照常按自己的网判
                try:
                    res = near_patrol.own_network_note(plan.name, a["area"], row, rows, reg)
                except Exception as ex:
                    res = ("待确认", f"按自己那张管网判时出错（{type(ex).__name__}: {ex}）")
                if res is None:
                    ok_, why_ = near_patrol.network_trusted(plan.name, a["area"], near_patrol.SOURCE_FLUID[dv["name"]])
                    if not ok_:  # 管网追踪不可信：保留全地区的判断，但说清楚没按自己那张网核对
                        v["reason"] += f"（没按它自己那张管网核对：{why_}）"
                    continue
                lv2, txt2 = res
                v["reason"] = f"{dv['name']} ({dv['x']}, {dv['y']}) 一直阻塞：{txt2}" + v["reason"][v["reason"].find("（近景定向巡检"):] if "（近景定向巡检" in v["reason"] else txt2
                row["text"] = f"{row['text']}；{txt2}"
                if lv2 != "正常":
                    v["level"] = lv2
                    moved2.append(v)
            if moved2:
                data["verdicts"]["正常"] = [v for v in data["verdicts"]["正常"] if v not in moved2]
                data["verdicts"].setdefault("待确认", []).extend(moved2)
                a["counts"]["正常"] = a["counts"].get("正常", 0) - len(moved2)
                a["counts"]["待确认"] = a["counts"].get("待确认", 0) + len(moved2)
            link_cut_victims(data, rows)
            # 近景发现结构断开（管道/传送带比基线少了）的设备：合并诊断按标记判成正常的（如水泵「供大于求」），改成问题
            broken = {(r["name"], r["far"][0], r["far"][1]): r for r in rows if r["level"] == "问题" and (r.get("struct") or {}).get("boxes")}
            moved = []
            for v in data["verdicts"].get("正常", []):
                dv = v.get("device") or {}
                hit = next((r for (nm, x, y), r in broken.items() if dv.get("name") == nm and abs(dv.get("x", 0) - x) <= 3 and abs(dv.get("y", 0) - y) <= 3), None)
                if hit:
                    v["level"], v["reason"] = "问题", f"{hit['text']}（合并诊断原判：{v['reason'][:100]}…）"
                    v["advice"] = "去俯瞰看对照图红框处：管道/传送带被拆或断开，接回去"
                    if hit.get("image"):
                        dv["image"] = hit["image"]
                    if hit.get("impact_image"):
                        v["impact_image"] = hit["impact_image"]
                    moved.append(v)
            if moved:
                data["verdicts"]["正常"] = [v for v in data["verdicts"]["正常"] if v not in moved]
                data["verdicts"].setdefault("问题", []).extend(moved)
                a["counts"]["正常"] = a["counts"].get("正常", 0) - len(moved)
                a["counts"]["问题"] = a["counts"].get("问题", 0) + len(moved)
        dup = len(moved) if data and a.get("counts") is not None else 0  # 同一台已在合并诊断里计成问题，近景一节不再加
        if a.get("counts") is not None:
            a["counts"]["问题"] = a["counts"].get("问题", 0) + cnt["问题"] - dup
            a["counts"]["待确认"] = a["counts"].get("待确认", 0) + cnt["待确认"]


def link_cut_victims(data: dict, rows: list[dict]) -> int:
    """近景找到断开的流体输入端、按分流优先级算出断开后会减产的设备：合并诊断里涉及这些设备的「问题/待确认」注明是缺这种流体停的。
    2026-11-10 断水实测：断了武陵城清水输入端 13，扩容反应池 (1489,1055) 停了，合并诊断按简报写成「息壤 理论消耗 390→360：
    有消费者被断开」，建议查传送带/准入口——它其实是缺清水（池子同时跑 清水 + 息壤 → 液化息壤），该先接回水管。返回改了几条。"""
    victims = []
    for r in rows:
        imp = r.get("impact") or {}
        for u in imp.get("cut_lost") or []:
            victims.append((u, r, imp.get("fluid") or r.get("fluid") or "清水"))
    if not victims:
        return 0
    n = 0
    for lv in ("问题", "待确认"):
        for v in data["verdicts"].get(lv, []):
            devs = [v.get("device") or {}, *(v.get("members") or [])]
            hits = []
            for u, r, fl in victims:
                if any(d.get("name") == u["name"] and abs(d.get("x", 0) - u["x"]) <= 3 and abs(d.get("y", 0) - u["y"]) <= 3 for d in devs):
                    hits.append(f"{u['name']} ({u['x']}, {u['y']}) 在断开的 {r['name']} 下游，按分流优先级算断开后{fl} {u['before']:g}→{u['after']:g}/分")
            if hits:
                fl = victims[0][2]
                v["reason"] += "；近景巡检：" + "；".join(hits) + f"——是缺{fl}停的，不是这条说的原料没送到"
                v["advice"] = f"先接回 {'、'.join(sorted({r['name'] for _, r, _ in victims}))} 外面断开的那段管子（见近景定向巡检的对照图），再看这条还在不在"
                n += 1
    return n


def attach_images(data: dict) -> None:
    """给「问题」「待确认」里的每台设备配定位图（base64 PNG），界面直接显示。出不了图就没有 image 字段。"""
    try:
        import locate
        patrol = json.loads(Path(data["patrol_path"]).read_text(encoding="utf-8"))
        shots = locate.shots_dir(patrol)
    except Exception:
        shots = None
    if shots is None:
        return
    cache: dict[tuple, str | None] = {}
    for level in ("问题", "待确认"):
        for v in data["verdicts"].get(level, []):
            for d in [v["device"], *v.get("members", []), *([v["partner"]] if v.get("partner") else [])]:
                key = (d["x"], d["y"])
                if d["name"].startswith("（无标签") or key == (0, 0):
                    continue
                if key not in cache:
                    try:
                        im = locate.locate_one(shots, d["x"], d["y"], d["name"].split("|")[0], small=True,
                                               shot=d.get("shot") or None, sx=d.get("sx"), sy=d.get("sy"))
                    except Exception:
                        im = None
                    cache[key] = _png_b64(im) if im is not None else None
                if cache[key]:
                    d["image"] = cache[key]


def _png_b64(im) -> str:
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
