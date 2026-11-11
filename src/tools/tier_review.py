"""备路/优先级核对：把一份巡检报告里所有「备路相关」设备（产物是 tier_rules / overflow / design_limits 里的，
或用户点名的低容、赤铜零件、蓝铁瓶、灌装瓶类）按分区列出来：状态、配方、结论等级和理由，
旁边附简报里该产物的 产/耗/理论/库存 和基准值。用来和蓝图的优先级设计逐台对。

用法：.venv\\Scripts\\python.exe src\\tools\\tier_review.py debug\\app_report_<stamp>.json [--all]
  --all  列出全部设备（默认只列备路相关的）
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

import blueprint as bp  # noqa: E402

WATCH = {"低容武陵电池", "赤铜零件", "蓝铁瓶(液化息壤)", "蓝铁瓶(液化重息壤)", "锦草软饮", "芽针针剂", "优质锦草软饮", "优质芽针针剂",
         "灼铜装备原件", "赤铜瓶(锦草溶液)", "赤铜瓶(芽针溶液)", "赤铜耐压罐", "赤铜耐压罐(惰气)", "中容武陵电池", "重息壤", "灼铜零件"}


def main(argv: list[str]) -> int:
    show_all = "--all" in argv
    argv = [a for a in argv if a != "--all"]
    rep = json.loads(Path(argv[0]).read_text(encoding="utf-8"))
    plan = bp.load(rep.get("blueprint"))
    tier = {r["product"] for r in plan.tier_rules} | set(plan.overflow_products) | set(plan.design_limits) | WATCH
    brief_path = Path("debug") / rep["brief"]
    brief = json.loads(brief_path.read_text(encoding="utf-8")) if brief_path.exists() else {}
    rows = {}
    for region, rs in brief.items():
        for r in rs:
            label = r["name"] + (f"({r['fluid']})" if r.get("fluid") else "")
            rows.setdefault(r["name"], r)
    ref = {}
    for region, rs in (plan.reference_brief or {}).items():
        for r in rs:
            ref.setdefault(r["name"], r)

    def fmt(r):
        return f"产{r['cur_prod']}/耗{r['cur_cons']} 理论{r['theo_prod']}/{r['theo_cons']} 库存{r['stock']}" if r else "-"

    print(f"报告 {argv[0]}  蓝图 {plan.name}  简报 {rep['brief']}")
    print("\n## 备路/限速产物在简报里的现状（新基准对比）")
    for p in sorted(tier):
        base = p.split("(")[0]
        if base in rows or base in ref:
            print(f"  {p:14s} 现在 {fmt(rows.get(base)):48s} 基准 {fmt(ref.get(base))}")
    for a in rep["areas"]:
        if not a.get("ok"):
            print(f"\n## {a['area']}：失败 {a.get('error')}")
            continue
        print(f"\n## {a['area']}  设备 {a['devices']}  {a['counts']}")
        for lv in ("问题", "待确认", "设计内", "正常"):
            for v in a["data"]["verdicts"].get(lv, []):
                devs = [v["device"]] + v.get("members", [])
                for d in devs:
                    recipe = d.get("recipe") or ""
                    hit = show_all or any(t.split("(")[0] in recipe for t in tier) or d["name"] in ("配件机", "封装机", "灌装机", "塑形机")
                    if not hit:
                        continue
                    print(f"  [{lv}] {d['name']} ({d['x']},{d['y']}) {d['status']}/{d['badge']} | {recipe[:48]}")
                    print(f"        → {(v.get('title') or '')[:60]} {v['reason'][:150]}")
                    break
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
