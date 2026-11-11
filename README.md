# 终末地基建诊断

Endfield Doctor 是一个帮你检查《明日方舟：终末地》基建产线哪里出了问题的小工具。

产线一大，哪台设备断了料、哪根管子被拆了、哪个准入口被改了，靠肉眼一个个点很难找。它会替你翻游戏里的简报，和蓝图该有的产量对一遍，告诉你哪里不对、该去哪个分区看哪类设备；需要的话还能自己传送过去，进俯瞰把设备挨个看一遍。

目前支持 1.6「皇帝产线」蓝图（武陵：景玉谷、应龙关、武陵城）。

## 下载使用

到 [Releases](https://github.com/Billtranscend/Endfield-Doctor/releases) 下载最新版：

- `EndfieldFactoryDoctor-<版本>.zip`：程序，解压后右键 `EndfieldFactoryDoctor.exe` →「以管理员身份运行」。
- `emperor-line-1.6-author.bpa.zip`：作者分析包，不用解压，在程序「蓝图」页导入一次。
- `tutorial.mp4`、`guide.html`：上手视频和图文说明，第一次用先看这个。

游戏设置：画面 16:9（全屏或无边框最稳）、简体中文、关掉 HDR。程序运行时别碰键盘鼠标。

## 能做什么

- **快速诊断**：读一遍简报（约 2 分钟），按人工排查的顺序列出「排查树」，全是 ✓ 就没事，哪一步打 ✗ 就告诉你去哪看。
- **深度巡检**：自动传送到各分区、进俯瞰逐屏扫，点开有「阻塞 / 空闲 / 关闭」标记的设备核对面板，再和简报合起来下结论。
- **定向巡检**：只去排查树指出的分区、只看那几类设备，比全基地巡检快得多。
- **蓝图分析**：把一套蓝图的设备、传送带、管道、准入口限速整理成分析包，别人导入后就能按这套蓝图诊断。

程序只看游戏画面、模拟键鼠，不读游戏内存、不改游戏文件；巡检只点开面板读数，不按开关、不收取、不换物品。自动操作游戏可能违反游戏的用户协议，风险自负。

## 反馈

遇到报错、判断错了，或者不知道怎么用，到 [Issues](https://github.com/Billtranscend/Endfield-Doctor/issues/new/choose) 选模板提交。

记得附上数据目录（程序里「设置」→「打开数据目录」）里出问题那次的报告 `app_report_*.json`、进度日志 `app_log_*.log` 和简报 `brief_*.json`，没有这些基本没法查。

## 本地开发

需要 Windows 和 Python 3.12。

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install MaaFw opencv-python numpy pywebview pyinstaller
.venv\Scripts\python.exe src\app\main.py      # 运行程序
.venv\Scripts\python.exe tests\regress.py     # 跑回归测试
.venv\Scripts\python.exe build\build_exe.py   # 打包成 exe
```

设计和开发记录在 `docs/`。

## 许可证

源代码按 [AGPL-3.0](LICENSE) 发布。游戏里的名称、图标、截图和数据版权归鹰角网络 / GRYPHLINE 所有，不在授权范围内，详见 [NOTICE.md](NOTICE.md)。
