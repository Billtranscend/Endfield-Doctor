"""全基地巡检：按蓝图档案 areas.json 里的分区依次「传送 → 进俯瞰 → 全基地扫描 → 退出俯瞰」，
最后对每个分区跑合并诊断（combine.run），汇总成 debug/full_<时间>.md / .json。

用法：.venv\\Scripts\\python.exe src\\full_patrol.py [--blueprint 名] [--areas 武陵城,景玉谷,应龙关] [--brief 简报.json] [--skip-nav]
  --areas    只巡检这些分区（默认 areas.json 里的全部，按文件里写的顺序）
  --brief    合并用的简报（默认 debug 里最新的 brief_*.json）
  --skip-nav 调试用：角色已经在目标分区并在俯瞰里，只扫描（只扫第一个分区，扫完不退出俯瞰）
退出码：全部成功 0，部分失败 2，参数错误 1。
"""

import json
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import blueprint as bp
import combine
import navigate
import patrol
from diagnose import DEBUG


@dataclass
class AreaResult:
    region: str
    area: str
    ok: bool = False
    step: str = ""  # 失败在哪一步
    error: str = ""
    patrol_path: Path | None = None
    devices: int = 0
    text: str = ""  # 合并报告
    counts: dict = field(default_factory=dict)  # 问题 / 待确认 / 正常 / 物流件 条数


def plan_areas(plan: bp.Blueprint, only: list[str] | None) -> list[tuple[str, str]]:
    """（地区, 分区）列表，完全按 areas.json 里写的顺序（地区、分区都是）。"""
    todo = [(r, a) for r in plan.areas for a in plan.areas[r]]  # 按 areas.json 里写的顺序
    if only:
        unknown = [a for a in only if a not in {a for _, a in todo}]
        if unknown:
            raise SystemExit(f"蓝图「{plan.name}」的 areas.json 里没有分区：{'、'.join(unknown)}")
        todo = [t for t in todo if t[1] in only]
    return todo


def exit_overhead() -> bool:
    """按一次 CapsLock 退出俯瞰（进出各按一次，系统大写锁定回到原状），再确认已经不在俯瞰。"""
    nav = navigate._get()
    nav.key("capslock")
    for _ in range(4):
        time.sleep(1.0)
        if nav.scene() != "Scene_Overhead":
            return True
    return False


def scan_area(res: AreaResult, skip_nav: bool, blueprint: str | None = None, only: list[str] | None = None, single: bool = False, checks: bool = True):
    """一个分区：传送 → 进俯瞰 → 扫描 → 保存 → 退出俯瞰。失败时设置 res.step / res.error。
    定向模式（排查树指到的分区）：single=True 只扫一遍（不做两遍对照），only=设备类型名单时只点这些设备的面板，
    checks=False 不跑错题本定点核查。"""
    if not skip_nav:
        res.step = "传送"
        if not navigate.goto_area(res.region, res.area, blueprint):
            res.error = navigate.last_error() or "navigate.goto_area 返回 False"
            return
        if navigate.discovered():  # 自动探测到真实分区名（已写回 areas.json），后面按真实名记录
            print(f"  {res.area} → 实际分区 {navigate.discovered()}")
            res.area = navigate.discovered()
        res.step = "进俯瞰"  # goto_area 只传送并等加载，不进俯瞰，所以这里要单独调
        if not navigate.enter_overhead():
            # 已在本分区但角色不在基地旁（没传送）：强制传送一次再试
            print("  进俯瞰失败，强制传送到基地再试一次")
            if not navigate.goto_area(res.region, res.area, blueprint, force_teleport=True) or not navigate.enter_overhead():
                res.error = navigate.last_error() or "navigate.enter_overhead 返回 False"
                return
    res.step = "扫描"
    p = patrol.Patrol()
    p.area = res.area  # 让扫描起点按这个分区的参考图校准，不同次扫描的坐标才能对上
    stamp = time.strftime("%Y%m%d-%H%M%S")
    p.shot_dir = DEBUG / "shots" / f"{stamp}_{res.area}"  # 每屏截图/面板图按分区分目录：定位图裁的就是这些（2026-11-06 没设时裁到了别区的图）
    p.panel_dir = DEBUG / "panels" / f"{stamp}_{res.area}"
    if p.scene() != "Scene_Overhead":
        res.error = "扫描前确认场景：不在俯瞰"
        return
    extent = bp.load(blueprint).area_options(res.area).get("extent")
    # 固定网格 + 两遍对照（第二遍当场点开确认；按坐标回头点的第三遍会点错邻居，不再跑）。
    # 没登记 extent 的分区按 6 列 × 7 行封顶（地图硬边界靠 ORB 里程计自动停），扫完提示可登记的 extent
    if only is not None:
        p.only_names = set(only)
    if single:
        p.sweep_once(tuple(extent) if extent else (2900, 2850))
    else:
        p.sweep_twice(tuple(extent) if extent else (2900, 2850))
    if checks:
        p.run_checks()  # 错题本定点核查（playbook.checks）
    if not extent:
        xs = [d.world_x for d in p.devices]; ys = [d.world_y for d in p.devices]
        if xs:
            print(f"== {res.area} 设备范围 x {min(xs)}–{max(xs)}，y {min(ys)}–{max(ys)}：可登记 extent [{max(xs) + 300}, {max(ys) + 300}]")
    if p.region is None:
        p.region = res.region  # 面板里没读到地区名时，用 areas.json 里的
    res.step = "保存"
    res.patrol_path = p.save(res.area)
    res.devices = len(p.devices)
    if not skip_nav:
        res.step = "退出俯瞰"
        if not exit_overhead():
            res.error = "按了 CapsLock 仍在俯瞰"
            return
    res.ok = True
    res.step = "扫描完成"


def summarize(results: list[AreaResult], plan: bp.Blueprint, brief_path: Path) -> tuple[str, dict]:
    lines = [
        f"# 全基地巡检：蓝图「{plan.name}」",
        f"简报 {brief_path.name}，{time.strftime('%Y-%m-%d %H:%M')}",
        "",
        "| 地区 | 分区 | 结果 | 设备 | 问题 | 待确认 | 正常 | 物流件 |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in results:
        c = r.counts
        state = "完成" if r.ok and r.text else f"失败（{r.step}：{r.error}）"
        lines.append(
            f"| {r.region} | {r.area} | {state} | {r.devices} | {c.get('问题', '-')} | {c.get('待确认', '-')} "
            f"| {c.get('正常', '-')} | {c.get('物流件', '-')} |"
        )
    for r in results:
        if r.text:
            lines += ["", "---", "", r.text]
    lines.append("")
    data = {
        "blueprint": plan.name,
        "brief": brief_path.name,
        "areas": [
            {
                "region": r.region,
                "area": r.area,
                "ok": r.ok and bool(r.text),
                "step": r.step,
                "error": r.error,
                "patrol": r.patrol_path.name if r.patrol_path else None,
                "devices": r.devices,
                "counts": r.counts,
            }
            for r in results
        ],
    }
    return "\n".join(lines), data


def main(argv: list[str]) -> int:
    def opt(name: str) -> str | None:
        return argv[argv.index(name) + 1] if name in argv and argv.index(name) + 1 < len(argv) else None

    plan = bp.load(opt("--blueprint"))
    only = [a.strip() for a in opt("--areas").split(",") if a.strip()] if opt("--areas") else None
    skip_nav = "--skip-nav" in argv
    brief_path = Path(opt("--brief")) if opt("--brief") else combine.latest("brief_*.json")
    if not plan.areas:
        print(f"蓝图「{plan.name}」没有 areas.json，或里面没有分区")
        return 1
    todo = plan_areas(plan, only)
    if skip_nav:
        todo = todo[:1]  # 不传送就只能扫当前所在的那个分区
        print(f"--skip-nav：只扫描当前画面所在的分区，按「{todo[0][1]}」记")

    results = [AreaResult(r, a) for r, a in todo]
    n = len(results)
    for i, res in enumerate(results, 1):
        print(f"\n[{i}/{n}] {res.region} / {res.area}：开始")
        try:
            scan_area(res, skip_nav, plan.name)
        except Exception as e:  # 任何异常都记下来，继续下一个分区
            res.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        if res.ok:
            print(f"[{i}/{n}] {res.area}：扫描完成，{res.devices} 台设备")
        else:
            print(f"[{i}/{n}] {res.area}：失败在「{res.step}」—— {res.error}")

    # 全部扫完后逐个分区合并。主分区（areas.json 里 "main": true）放最后：前面分区已定位到的理论消耗下降
    # 传给它，主分区就不再把它们当地区级问题重复计（例：应龙关并进「赤铜块送不到」的息壤气 324→318）
    drops_elsewhere: dict[str, str] = {}
    # 「最近各分区扫描」只用本次跑出来的文件（下游设备在哪个分区、可逆配方确认、流体消费设备状态），不碰 debug 里的历史
    combine.set_context_patrols([r.patrol_path for r in results if r.patrol_path is not None])
    for res in sorted(results, key=lambda r: bool(plan.area_options(r.area).get("main"))):
        if res.patrol_path is None:
            continue
        try:
            res.text, data = combine.run(brief_path, res.patrol_path, plan, drops_elsewhere=dict(drops_elsewhere))
            for label in data.get("theo_drops_located", []):
                drops_elsewhere.setdefault(label, res.area)
            counts = {s: len(data["verdicts"][s]) for s in combine.ORDER}
            counts["物流件"] = len(data.get("logistics", []))
            res.counts = counts
        except Exception as e:
            res.ok = False
            res.step, res.error = "合并", f"{type(e).__name__}: {e}"
            traceback.print_exc()
            print(f"{res.area}：合并失败 —— {res.error}")

    text, data = summarize(results, plan, brief_path)
    print("\n" + text)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (DEBUG / f"full_{stamp}.md").write_text(text, encoding="utf-8")
    (DEBUG / f"full_{stamp}.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"已保存：debug\\full_{stamp}.md / .json")
    return 0 if all(a["ok"] for a in data["areas"]) else 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
