"""地图标题识别：只有第一级是已知地区才算地图（2026-11-10 角色「装备」页的「祀/装备」被当成地图标题）。"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "src" / "tools")]

from navigate import parse_map_title  # noqa: E402


def test_parse_map_title():
    assert parse_map_title("//武陵/景玉谷") == ("武陵", "景玉谷")
    assert parse_map_title("// 武陵 /应龙关") == ("武陵", "应龙关")
    assert parse_map_title("陵/武陵城") == ("陵", "武陵城")          # OCR 漏字
    assert parse_map_title("四号谷地") == ("四号谷地", "四号谷地")
    # 不是地图的画面
    assert parse_map_title("祀/装备") is None                         # 角色装备页（实测 OCR）
    assert parse_map_title("洁尔佩塔/装备") is None
    assert parse_map_title("//简报") is None
    assert parse_map_title("③工业品心") is None                       # 大世界左上角模式框
    assert parse_map_title("") is None


if __name__ == "__main__":
    test_parse_map_title()
    print("1/1 通过")
