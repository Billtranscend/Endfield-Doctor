"""跑全部测试：逐个跑 tests/test_*.py，再跑 tests/regress.py；每个脚本一行结果，最后报失败数和跳过数，有失败退出码 1。

  .venv/Scripts/python.exe tests/run_all.py                      全部
  .venv/Scripts/python.exe tests/run_all.py tests/test_bpa.py …   只跑这几个（给了文件就不跑 regress.py，除非也点名它）

判失败（2026-11-19）：退出码非 0；没有输出；最后一行不是「a/b …」；a≠b；有以 FAIL 开头的行。
起因：tests/test_bpa.py 没有运行入口，直接运行什么都不做、退出码 0，原来的 bash 脚本把空输出记成「过」，20 条空跑了好几个版本；
再早（2026-11-14）test_pool_channels 5/6 退出码 0，也一直被当成过。
跳过数 = 输出里以「跳过 」开头的行（tests/_testkit.skip：本机没有那份实测数据）。
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
    bad = skipped = 0
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
        skipped += sk
        tail = f"（跳过 {sk} 条）" if sk else ""
        if ok:
            print(f"过   {name} | {why}{tail} | {time.time() - t0:.0f}s", flush=True)
        else:
            bad += 1
            print(f"失败 {name} | {why}{tail} | {time.time() - t0:.0f}s", flush=True)
            for ln in out.splitlines()[-15:]:
                print("     " + ln, flush=True)
    print(f"共 {len(files)} 个脚本，失败 {bad}，跳过 {skipped} 条，用时 {time.time() - t_all:.0f}s")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
