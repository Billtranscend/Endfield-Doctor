"""启动时解除「来自网络」的锁定（2026-11-12 群友从 GitHub 下载后启动报 Failed to resolve Python.Runtime.Loader.Initialize）。"""
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src")]

import paths as P  # noqa: E402


def mark(f: Path) -> bool:
    try:
        with open(str(f) + P.MARK, "w") as h:
            h.write("[ZoneTransfer]\nZoneId=3\n")
        return True
    except OSError:
        return False  # 不是 NTFS


def marked(f: Path) -> bool:
    try:
        open(str(f) + P.MARK).close()
        return True
    except OSError:
        return False


def test_unblock_marked_files():
    with tempfile.TemporaryDirectory() as tmp:
        base = Path(tmp) / "中文 目录"
        (base / "_internal" / "pythonnet" / "runtime").mkdir(parents=True)
        (base / "_internal" / "debug").mkdir()
        files = [base / "App.exe", base / "_internal" / "pythonnet" / "runtime" / "Python.Runtime.dll", base / "_internal" / "plain.txt"]
        for f in files:
            f.write_bytes(b"data-" + f.name.encode())
        skipped = base / "_internal" / "debug" / "shot.png"
        skipped.write_bytes(b"x")
        if not mark(files[0]):
            return
        mark(files[1])
        mark(skipped)
        assert P.unblock_downloaded(base) == 2
        assert not any(marked(f) for f in files)
        assert all(f.read_bytes() == b"data-" + f.name.encode() for f in files)  # 文件内容不动
        assert marked(skipped)  # debug 目录不扫
        assert P.unblock_downloaded(base) == 0  # 再跑一次什么都不做


def test_missing_dir_is_quiet():
    assert P.unblock_downloaded(Path(tempfile.gettempdir()) / "没有这个目录-unblock") == 0


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
    print(f"{len(tests)}/{len(tests)} 通过")
