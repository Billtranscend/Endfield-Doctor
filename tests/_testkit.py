"""测试脚本共用：依赖本机数据（debug/ 下的实测项目、巡检记录，.cache 里的物品图标）的几条，数据不在时显式报跳过。

2026-11-19 以前这些测试缺数据就直接 return，脚本照样打印「n/n 通过」，换一台机器（或数据被清掉）什么都没测也看不出来。
现在缺数据时 skip(原因) → 打印「跳过 <测试名>：<原因>」，不算进通过数；最后一行「a/b 通过（跳过 k 条）」里 b 只数真跑了的。
tests/run_all.py 按「跳过 」开头的行统计跳过数。

还有一个：设计分析、物流连接的 run_base 会写 design.json / flow.json，测试不能对 debug/bpa/ 下的真实项目直接跑
（2026-11-19 审计：test_design 一直在改写界面显示的那份 design.json）。copy_project 把项目复制到临时目录再跑。

known_defect(tag, check)：已知缺陷用「严格预期失败」锁住现状（现在不挂，修好以后自动报失败，提醒改成正式断言）。
"""
import atexit
import shutil
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Skip(Exception):
    """这条测试要的本机数据不在。"""


def skip(why: str):
    raise Skip(why)


def run(tests) -> int:
    """逐条跑，返回退出码（有失败 1）。跳过的不算通过、也不算进总数。"""
    ok = skipped = 0
    for f in tests:
        try:
            f(); ok += 1
        except Skip as e:
            skipped += 1
            print(f"跳过 {f.__name__}：{e}")
        except AssertionError as e:
            print("FAIL", f.__name__, str(e)[:300])
    n = len(tests) - skipped
    print(f"{ok}/{n} 通过" + (f"（跳过 {skipped} 条）" if skipped else ""))
    return 0 if ok == n else 1


def known_defect(tag: str, check) -> None:
    """已知缺陷（严格预期失败，2026-11-20 审计第 5 项）：check() 写的是「修好以后应该成立」的断言。
    - check() 抛 AssertionError：缺陷还在，打印「已知缺陷 <tag>：仍在（原因前 80 字）」，不算失败；
    - check() 不抛错：缺陷已经不在了，报失败，提醒把它改成正式断言、去掉 known_defect（免得修好了还一直挂着「已知」）。
    别的异常照常往外抛（测试本身写错了）。打印的行不以 FAIL 开头；tests/run_all.py 按「已知缺陷 」开头的行计数。"""
    try:
        check()
    except AssertionError as e:
        why = " ".join(str(e).split())[:80] or "断言不成立"
        print(f"已知缺陷 {tag}：仍在（{why}）")
        return
    raise AssertionError(f"已知缺陷 {tag} 已不再出现：改成正式断言、去掉 known_defect")


_COPIES: dict[str, Path] = {}


def copy_project(name: str) -> Path:
    """debug/bpa/<name> 整个复制到临时目录（同一个脚本里只复制一次），返回副本的项目目录；项目不在就跳过。
    设计分析除了据点目录还读 <项目>/inputs.json、fixes.json、bp/<蓝图>/links.json，只复制据点目录结果会变，所以整个复制；
    去掉 ledger_scan（底账扫描的原始截图，900 多 MB，分析不读）和 bp/*/frames（读蓝图的原始帧，分析读的是拼好的 mosaic.png）。
    和 scratchpad/simtools/snap.py 一样的做法（它还复制 frames，结果不受影响）。副本约 100 MB，脚本退出时删掉。"""
    if name in _COPIES:
        return _COPIES[name]
    src = ROOT / "debug" / "bpa" / name
    if not (src / "project.json").exists():
        skip(f"本机没有实测项目 debug/bpa/{name}")
    tmp = Path(tempfile.mkdtemp(prefix="bpa_test_"))
    atexit.register(shutil.rmtree, tmp, True)
    dst = tmp / name
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns("ledger_scan", "frames"))
    _COPIES[name] = dst
    return dst


def project_ledger(proj: Path, base: str) -> Path | None:
    """和界面（project.rerun_design）一样取底账：直接调 project.ledger_path，项目目录临时指到副本。
    项目自己扫的 <项目>/ledger/<据点>.json 优先，没有再用蓝图档案里的（皇帝产线：景玉谷、应龙关有自己的，武陵城用档案的）。"""
    from bpa import project as P
    old, P.BPA = P.BPA, proj.parent
    try:
        return P.ledger_path(proj.name, base)
    finally:
        P.BPA = old
