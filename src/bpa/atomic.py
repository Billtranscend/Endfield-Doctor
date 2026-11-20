"""原子写：先写到同目录的临时文件，再 os.replace 换上去。读的一方看到的要么是旧文件、要么是新文件，不会是写了一半的。

2026-11-19 审计实测：design.json 原来用 Path.write_text 写（先清空再写），界面另一个线程（api.bpa_design）同时读，
一边反复写 107 KB 的 design.json、一边反复读，10 秒里 5923 次读有 911 次读到空文件、json 解析失败。
界面的每次接口调用都在单独的线程里跑（pywebview），拼合任务写文件时界面照样会来读，所以蓝图分析项目里会被界面读的 json 都走这里。

Windows 上目标文件正被别的线程/进程打开着读时，os.replace 会抛 PermissionError（拒绝访问）：读一个 json 很快，短等几次再换。
读的一方用 read_text / read_json：换文件的那一下去打开也会拿到 PermissionError，同样短等再读。
换行和原来的 Path.write_text 一样按文本模式写（Windows 上是 CRLF），换过来以后文件内容逐字节不变。
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

RETRY_WAITS = (0.01, 0.02, 0.05, 0.1, 0.2, 0.4, 0.8, 1.0, 1.0, 1.0)  # 合计约 4.6 秒：写设计分析结果宁可多等，也别算完了存不下


def write_text(p: Path | str, text: str, encoding: str = "utf-8") -> None:
    p = Path(p)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.{threading.get_ident()}.tmp")  # 同目录（os.replace 要同一个盘）；带线程号，两个线程同时写同一个文件也不会共用临时文件
    try:
        with open(tmp, "w", encoding=encoding) as f:
            f.write(text)
        for wait in RETRY_WAITS + (None,):
            try:
                os.replace(tmp, p)
                return
            except PermissionError:
                if wait is None:
                    raise
                time.sleep(wait)
    finally:
        if tmp.exists():  # 写失败或换不上：临时文件不留在项目目录里
            try:
                tmp.unlink()
            except OSError:
                pass


def read_text(p: Path | str, encoding: str = "utf-8") -> str:
    """读的一方也要短等：os.replace 换文件的那一下，Windows 上同时去打开会拿到 PermissionError（不是读到半个文件）。
    tests/test_concurrency.py 的压测里 3 秒 5000 次读撞上过 1 次。"""
    p = Path(p)
    for wait in RETRY_WAITS + (None,):
        try:
            return p.read_text(encoding=encoding)
        except PermissionError:
            if wait is None:
                raise
            time.sleep(wait)


def read_json(p: Path | str):
    return json.loads(read_text(p))


def write_json(p: Path | str, obj, mkdir: bool = False, **kw) -> None:
    """json.dumps(obj, ensure_ascii=False, **kw) 原子写到 p。kw 默认 indent=1（项目里的 json 都这么存）。"""
    p = Path(p)
    if mkdir:
        p.parent.mkdir(parents=True, exist_ok=True)
    kw.setdefault("indent", 1)
    write_text(p, json.dumps(obj, ensure_ascii=False, **kw))
