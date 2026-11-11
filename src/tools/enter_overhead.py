"""传送到基地后，走几步进入协议区域并打开俯瞰。

主体已搬到 src/navigate.py（Navigator.enter_overhead），这里只是薄包装，保留原来的用法。

用法：python src\\tools\\enter_overhead.py [--restore-caps]
--restore-caps：之前在区域外按过 CapsLock、系统大写锁定被切到开启时，先按一次复原。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import navigate  # noqa: E402

if __name__ == "__main__":
    sys.exit(0 if navigate.enter_overhead("--restore-caps" in sys.argv[1:]) else 2)
