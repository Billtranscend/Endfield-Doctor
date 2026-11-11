"""蓝图分析项目「删除」（任务计划第 11 项打包前，用户 2026-11-10）：
- 挪进 debug/bpa_备份/<项目>_<时间>/，不真删；项目列表不再有它。
- 删除前提醒：档案的近景巡检在用这个项目的拼合结果、又没有别的来源（同档案其他项目 / 导入的作者 zip）的据点。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from bpa import project as P  # noqa: E402


def _base(proj_dir: Path, area: str):
    d = proj_dir / "base" / area
    d.mkdir(parents=True)
    (d / "composite.png").write_bytes(b"x")


def test_delete_moves_and_warns():
    with tempfile.TemporaryDirectory() as td:
        old = (P.BPA, P.BACKUP)
        P.BPA, P.BACKUP = Path(td) / "bpa", Path(td) / "bpa_备份"
        try:
            P.create("甲", {"应龙关": ["a"], "武陵城": ["b"]}, "某档案")
            P.create("乙", {"武陵城": ["b"]}, "某档案")
            _base(P.pdir("甲"), "应龙关")
            _base(P.pdir("甲"), "武陵城")
            _base(P.pdir("乙"), "武陵城")
            info = P.delete_info("甲")
            assert info["archive"] == "某档案" and info["other_projects"] == ["乙"], info
            assert info["lost"] == ["应龙关"], info  # 武陵城还有「乙」的拼合结果
            r = P.delete("甲")
            assert Path(r["backup"]).parent == P.BACKUP and (Path(r["backup"]) / "project.json").exists()
            assert [p["name"] for p in P.list_projects()] == ["乙"]
            try:
                P.delete("甲")
                raise AssertionError("删了不存在的项目")
            except FileNotFoundError:
                pass
        finally:
            P.BPA, P.BACKUP = old


if __name__ == "__main__":
    test_delete_moves_and_warns()
    print("1/1 通过")
