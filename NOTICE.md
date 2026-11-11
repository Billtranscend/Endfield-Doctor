# 第三方软件与素材声明

本项目的**源代码**按 `LICENSE` 文件中的许可证发布。下面这些不属于本项目，各自按其原有许可或权利归属处理。

## 游戏数据与素材

《明日方舟：终末地》的游戏名称、物品名称与图标、界面截图、游戏数据表等，版权归上海鹰角网络科技有限公司（Hypergryph）/ GRYPHLINE 所有。本项目使用它们只是为了识别游戏画面，**不在本项目许可证的授权范围内**：

- `resource/icons/`、`resource/badges/`、`resource/anchors/`、`resource/bpa/`：从游戏截图或游戏数据中截取的图标、模板和参考画面；
- `data/*.json` 中的物品名、配方、建筑数据：由 `src/tools/build_data.py` 从 [AKEData](https://akedata.wiki) 提供的游戏数据表生成；
- `tests/` 中的游戏截图和简报样本。

如果权利人认为某些内容不应出现在这里，请提 issue，我们会尽快移除。

本程序只读取游戏画面、模拟键盘鼠标，不读写游戏内存、不修改游戏文件。自动操作游戏可能违反游戏的用户协议，风险由使用者自行承担。

## 运行依赖

| 组件 | 用途 | 许可证 |
|---|---|---|
| [MaaFramework](https://github.com/MaaXYZ/MaaFramework) | 截图、键鼠输入、OCR | LGPL-3.0 |
| `resource/model/ocr/`：[MaaCommonAssets](https://github.com/MaaXYZ/MaaCommonAssets) 整理的 PaddleOCR 模型 | 文字识别 | MIT（MaaCommonAssets）/ Apache-2.0（PaddleOCR） |
| [pywebview](https://github.com/r0x0r/pywebview) | 程序界面 | BSD-3-Clause |
| [OpenCV](https://github.com/opencv/opencv-python) | 图像匹配 | Apache-2.0 |
| [NumPy](https://numpy.org) | 数值计算 | BSD-3-Clause 等 |
| [PyInstaller](https://pyinstaller.org)（仅打包时） | 打包 exe | GPL-2.0+，附打包例外 |

## 参考与致谢

- [IndustrialPlanner](https://github.com/hsyhhssyy/IndustrialPlanner)（MIT）：建筑端口方向、传送带优先级等规则的参考。
- [AKEData](https://akedata.wiki)：游戏数据表来源。
- 界面风格参考了 MaaEnd、OEA 等社区工具。
