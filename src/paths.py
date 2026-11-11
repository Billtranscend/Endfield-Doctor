"""项目根目录：开发期是仓库目录（src 的上一级）；PyInstaller 打包后是解包目录（sys._MEIPASS，onedir 时为 exe 旁的 _internal），
data/ resource/ .cache/ 都随包放在那里，debug/ 也写在那里。所有模块的 ROOT 都从这里取，别再各自用 __file__ 推。"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT = Path(__file__).resolve().parent.parent


# ---- 读写图片：cv2.imread / cv2.imwrite 在 Windows 上不认中文路径（静默返回 None / False）----
# 2026-11-12 群友把程序放进中文目录后不能用：基地图标、状态标记模板一张都读不进来。程序里读写图片一律走这两个。
def imread(path, flags: int = 1):
    """同 cv2.imread（flags 默认彩色），中文路径也能读；文件不存在或读不了返回 None。"""
    import cv2
    import numpy as np
    try:
        data = np.fromfile(str(path), dtype=np.uint8)
    except OSError:
        return None
    if data.size == 0:
        return None
    return cv2.imdecode(data, flags)


def imwrite(path, img, params=None) -> bool:
    """同 cv2.imwrite，中文路径也能写。格式按扩展名（没有扩展名按 .png）。"""
    import cv2
    ext = Path(str(path)).suffix or ".png"
    ok, buf = cv2.imencode(ext, img, list(params or []))
    if ok:
        buf.tofile(str(path))
    return bool(ok)
