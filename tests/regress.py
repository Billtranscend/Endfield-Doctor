"""故障用例离线回归：对 tests/faults/<用例名>/ 下存档的简报和巡检跑 combine，对照 expect.json。

每个用例目录：
  brief.json            简报
  patrol_fast*.json     快速巡检（可能多份，取 JSON 里 time 最新的；expect.json 里写 "patrol" 可指定文件）
  patrol_deep*.json     定点深查（可选；按 combine 现有规则合并：主文件之后、同一分区的才合并）
  expect.json           {"problems": [{"name": "精炼炉", "status": "关闭"}, {"title_contains": "清水"}],
                         "max_problems": 1, "exact_problems": 1, "must_not_problem": ["封装机", ...],
                         "uncertain_contains": ["分离芯"],
                         可选 "blueprint": 蓝图名, "patrol": 主巡检文件名, "merge_deep": false}
  problems 每条必须出现在「问题」里：单台设备按设备名 + status 或 badge 匹配（设备名是「A|B」候选时任一候选相同即可）；
    合并条目（根因、这条线断了、送不到……）用 title_contains 按标题关键字匹配；
  问题总数 ≤ max_problems、= exact_problems；must_not_problem 里的设备名不能单独成条出现在「问题」里
  （被并进合并条目不算）；uncertain_contains 每个关键字都要在某条待确认的标题或依据里出现。

只读用例目录，不读也不写 debug。用例都在 2026-11-04 版蓝图上录的，expect.json 的 blueprint 指向 data/blueprints/_回归-1.6皇帝产线-旧基准1104（界面隐藏）；
正式档案 1.6皇帝产线 的参考简报 2026-11-07 已换成新蓝图（传送带优先级）的。
用法：.venv\\Scripts\\python.exe tests\\regress.py [用例名 ...]      退出码：全部通过 0，有失败 1
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import combine  # noqa: E402

FAULTS = Path(__file__).resolve().parent / "faults"


def newest(files: list[Path]) -> Path | None:
    """按巡检 JSON 里的 time 取最新；没有 time 的按文件修改时间。"""
    def key(f: Path):
        m = combine.patrol_meta(f)
        return (m["time"] if m else "", f.stat().st_mtime)

    return max(files, key=key) if files else None


def names_of(device: dict) -> list[str]:
    return [n for n in device["name"].split("|") if n]


def check(case: Path) -> list[str]:
    """跑一个用例，返回失败原因（空列表 = 通过）。"""
    expect = json.loads((case / "expect.json").read_text(encoding="utf-8"))
    brief = case / "brief.json"
    if not brief.exists():
        return ["缺 brief.json"]
    if expect.get("patrol"):
        patrol = case / expect["patrol"]
    else:
        patrol = newest(sorted(case.glob("patrol_fast*.json")))
    if patrol is None or not patrol.exists():
        return ["缺快速巡检 patrol_fast*.json"]
    merge = expect.get("merge_deep", True)
    # combine 的深查合并只认 patrol_*.json，用例目录里的 patrol_deep*.json 正好符合；deep_dir 指向用例目录，不碰 debug
    _, data = combine.run(brief, patrol, expect.get("blueprint"), merge=merge, deep_dir=case)
    verdicts = data["verdicts"]["问题"]
    print(f"  主巡检 {patrol.name}，合并深查 {data.get('deep_merged') or '无'}，问题 {len(verdicts)} 条：")
    for v in verdicts:
        d = v["device"]
        if v.get("title"):  # 合并条目（根因、这条线断了、送不到……）：设备名是第一台，按标题显示
            print(f"    - 【{v['title']}】（{len(v.get('members') or [])} 台设备）")
        else:
            print(f"    - {d['name']} ({d['x']}, {d['y']}) 面板「{d['status']}」/标记「{d['badge']}」")

    errors = []
    for want in expect.get("problems", []):
        if "title_contains" in want:  # 合并条目按标题关键字匹配
            hit = [v for v in verdicts if want["title_contains"] in (v.get("title") or "")]
            what = f"标题含「{want['title_contains']}」"
        else:
            hit = [
                v for v in verdicts
                if not v.get("title")
                and want["name"] in names_of(v["device"])
                and ("status" not in want or want["status"] in (v["device"]["status"], v["device"]["badge"]))
            ]
            what = f"{want['name']}（{want.get('status', '任意状态')}）"
        if not hit:
            errors.append(f"应报但没报：{what}")
    limit = expect.get("max_problems")
    if limit is not None and len(verdicts) > limit:
        errors.append(f"问题 {len(verdicts)} 条，超过上限 {limit}")
    exact = expect.get("exact_problems")
    if exact is not None and len(verdicts) != exact:
        errors.append(f"问题 {len(verdicts)} 条，应恰好 {exact} 条")
    # must_not_problem 只管单独成条的：设备被并进合并条目（根因的受影响设备等）不算
    for name in expect.get("must_not_problem", []):
        bad = [v["device"] for v in verdicts if not v.get("title") and name in names_of(v["device"])]
        if bad:
            errors.append("不应报却报了：" + "、".join(f"{d['name']} ({d['x']}, {d['y']})" for d in bad))
    # uncertain_contains：待确认里必须有标题或依据含这些关键字的条目
    uncertain = data["verdicts"]["待确认"]
    for word in expect.get("uncertain_contains", []):
        if not any(word in (v.get("title") or "") or word in v["reason"] for v in uncertain):
            errors.append(f"待确认里没有含「{word}」的条目")
    return errors


def main(argv: list[str]) -> int:
    cases = sorted(p for p in FAULTS.iterdir() if p.is_dir() and (p / "expect.json").exists())
    if argv:
        cases = [c for c in cases if c.name in argv]
    if not cases:
        print("没有找到带 expect.json 的用例")
        return 1
    failed = 0
    for case in cases:
        print(f"== {case.name}")
        try:
            errors = check(case)
        except Exception as e:  # 用例本身跑崩也算失败，继续下一个
            errors = [f"运行出错：{type(e).__name__}: {e}"]
        if errors:
            failed += 1
            print("  FAIL")
            for e in errors:
                print(f"    × {e}")
        else:
            print("  PASS")
    print(f"\n{len(cases) - failed}/{len(cases)} 通过")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
