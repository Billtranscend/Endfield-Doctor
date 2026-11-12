"""项目根目录：开发期是仓库目录（src 的上一级）；PyInstaller 打包后是解包目录（sys._MEIPASS，onedir 时为 exe 旁的 _internal），
data/ resource/ .cache/ 都随包放在那里，debug/ 也写在那里。所有模块的 ROOT 都从这里取，别再各自用 __file__ 推。"""

import sys
from pathlib import Path

if getattr(sys, "frozen", False):
    ROOT = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
else:
    ROOT = Path(__file__).resolve().parent.parent


# ---- 解除「来自网络」的锁定 ----
# 2026-11-12 群友从 GitHub 用浏览器下载 zip、系统自带解压后启动报错：
#   Failed to resolve Python.Runtime.Loader.Initialize from …/_internal/pythonnet/runtime/Python.Runtime.dll
# 浏览器给 zip 打「来自网络」标记（NTFS 备用数据流 Zone.Identifier），资源管理器解压时带到每个文件上，
# .NET 不加载带这个标记的程序集，界面（pywebview → pythonnet）就起不来。本机复现：给这个 DLL 加上标记就报同样的错，去掉就好。
# 启动时把程序自己目录下文件的标记去掉（等同于在属性里勾「解除锁定」），必须在加载 pythonnet 之前做。
MARK = ":Zone.Identifier"
SKIP_DIRS = {"debug"}  # 程序自己写的数据（报告、截图），不会带标记，文件又多


def unblock_downloaded(base=None) -> int:
    """去掉 base（默认 exe 所在目录）下所有文件的「来自网络」标记，返回去掉了几个。没有标记、不是 NTFS、没权限都静默跳过。"""
    import os
    base = Path(base) if base else Path(sys.executable).resolve().parent
    count = 0
    for folder, dirs, files in os.walk(base):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for name in files:
            try:
                os.remove(os.path.join(folder, name) + MARK)
                count += 1
            except OSError:
                pass
    return count


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
