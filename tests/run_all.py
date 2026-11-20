"""跑全部测试：逐个跑 tests/test_*.py，再跑 tests/regress.py；每个脚本一行结果，最后报失败数和跳过数，有失败退出码 1。

  .venv/Scripts/python.exe tests/run_all.py                      全部
  .venv/Scripts/python.exe tests/run_all.py tests/test_bpa.py …   只跑这几个（给了文件就不跑 regress.py，除非也点名它）

判失败（2026-11-19）：退出码非 0；没有输出；最后一行不是「a/b …」；a≠b；有以 FAIL 开头的行。
起因：tests/test_bpa.py 没有运行入口，直接运行什么都不做、退出码 0，原来的 bash 脚本把空输出记成「过」，20 条空跑了好几个版本；
再早（2026-11-14）test_pool_channels 5/6 退出码 0，也一直被当成过。
跳过数 = 输出里以「跳过 」开头的行（tests/_testkit.skip：本机没有那份实测数据）。
已知缺陷数 = 输出里以「已知缺陷 」开头的行（tests/_testkit.known_defect：严格预期失败，缺陷还在时不算失败）。
失败时除了最后 15 行，另打印输出里以「差异 」开头的行（最多 60 行）：设计分析整份快照（tests/test_design_snapshot.py）的 diff
最要紧的几行（各段计数、状态迁移直方图、✗ 清单）都以「差异 」开头、排在最前面；_testkit.run 把 AssertionError 截到 300 字，不另打印就看不全。
"""
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TESTS = ROOT / "tests"
LAST = re.compile(r"^(\d+)/(\d+) ")
DIFF_PREFIX = "差异 "
DIFF_MAX = 60
DEFECT_PREFIX = "已知缺陷 "


def defects(out: str) -> int:
    return sum(1 for ln in out.splitlines() if ln.startswith(DEFECT_PREFIX))


def diff_lines(out: str) -> list[str]:
    """输出里以「差异 」开头的行，最多 DIFF_MAX 行（多了另起一行说还有几行）。"""
    ds = [ln for ln in out.splitlines() if ln.startswith(DIFF_PREFIX)]
    return ds[:DIFF_MAX] + ([f"…另有 {len(ds) - DIFF_MAX} 行「差异 」没打印（全文见输出里的 diff 全文路径）"] if len(ds) > DIFF_MAX else [])


def python() -> str:
    for p in (ROOT / ".venv" / "Scripts" / "python.exe", ROOT / ".venv" / "bin" / "python"):
        if p.exists():
            return str(p)
    return sys.executable


def judge(rc: int, out: str) -> tuple[bool, str, int]:
    """→ (过没过, 原因或最后一行, 跳过条数)。"""
    lines = [ln for ln in out.splitlines() if ln.strip()]
    skips = sum(1 for ln in lines if ln.startswith("跳过 "))
    if not lines:
        return False, f"没有输出（退出码 {rc}）", skips
    last = lines[-1].strip()
    if rc != 0:
        return False, f"退出码 {rc}：{last}", skips
    m = LAST.match(last)
    if not m:
        return False, f"最后一行不是「a/b …」：{last}", skips
    if m.group(1) != m.group(2):
        return False, last, skips
    fails = [ln for ln in lines if ln.startswith("FAIL")]
    if fails:
        return False, f"{last}，但有 {len(fails)} 行 FAIL", skips
    return True, last, skips


def main(argv: list[str]) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # 输出接到管道时（bash 脚本转调）默认是 GBK，中文会乱
    except (AttributeError, ValueError):
        pass
    if argv:
        files = [Path(a) if Path(a).is_absolute() else ROOT / a for a in argv]
    else:
        files = sorted(TESTS.glob("test_*.py")) + [TESTS / "regress.py"]
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    py = python()
    bad = skipped = known = 0
    t_all = time.time()
    for f in files:
        name = f.relative_to(ROOT).as_posix() if f.is_relative_to(ROOT) else str(f)
        if not f.exists():
            bad += 1
            print(f"失败 {name} | 没有这个文件", flush=True)
            continue
        t0 = time.time()
        p = subprocess.run([py, str(f)], cwd=str(ROOT), env=env, capture_output=True, text=True, encoding="utf-8", errors="replace")
        out = (p.stdout or "") + (p.stderr or "")
        ok, why, sk = judge(p.returncode, out)
        kd = defects(out)
        skipped += sk
        known += kd
        tail = (f"（跳过 {sk} 条）" if sk else "") + (f"（已知缺陷 {kd} 条）" if kd else "")
        if ok:
            print(f"过   {name} | {why}{tail} | {time.time() - t0:.0f}s", flush=True)
        else:
            bad += 1
            print(f"失败 {name} | {why}{tail} | {time.time() - t0:.0f}s", flush=True)
            for ln in diff_lines(out):
                print("     " + ln, flush=True)
            for ln in out.splitlines()[-15:]:
                print("     " + ln, flush=True)
    print(f"共 {len(files)} 个脚本，失败 {bad}，跳过 {skipped} 条，已知缺陷 {known} 条，用时 {time.time() - t_all:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
