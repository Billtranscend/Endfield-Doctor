"""从最新（或指定）的巡检 JSON 里取出需要深查的坐标，拼成 patrol.py --at 用的参数串。

  python src\\tools\\deep_targets.py [patrol.json] [--all]
默认只取「（无标签）」的标记；--all 取全部快速模式条目。坐标按行（每 300 像素一行）、再按 x 排序，减少镜头来回。
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent


def main(argv):
    files = [a for a in argv if not a.startswith("--")]
    name = argv[argv.index("--name") + 1] if "--name" in argv else None
    if name:
        files = [f for f in files if f != name]
    path = Path(files[0]) if files else max((ROOT / "debug").glob("patrol_*.json"), key=lambda p: p.stat().st_mtime)
    data = json.loads(path.read_text(encoding="utf-8"))
    devices = data["devices"] if isinstance(data, dict) else data
    if name:  # --name 反应池：取名字含这个词的全部设备
        picked = [d for d in devices if name in d["name"]]
    else:
        picked = [d for d in devices if d["name"] == "（无标签）" or ("--all" in argv and d.get("guessed"))]
    picked.sort(key=lambda d: (d["world_y"] // 300, d["world_x"]))
    kinds = {"阻塞": "blocked", "空闲中": "idle", "关闭": "closed"}
    print(";".join(f"{d['world_x']},{d['world_y']},{kinds.get(d['badge'], 'blocked')}" for d in picked))


if __name__ == "__main__":
    main(sys.argv[1:])
