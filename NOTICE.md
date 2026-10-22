# Third-party notices / 第三方归属

本仓库的脚本与文档为原创，以 MIT 发布（见 `LICENSE`）。以下项目/资料被**依赖**或**参考**：

## 1. easyeda2kicad.py — AGPL-3.0（运行时外部依赖，不随仓库分发）

- 仓库：<https://github.com/uPesy/easyeda2kicad.py>
- 用途：下载 EasyEDA/LCSC 元件数据并转换成 KiCad 库；本仓库的
  `scripts/build_kicad_lib.py` 通过命令行调用它，`scripts/fix_symbol_pin_names.py`
  在 `--fetch` / `--easyeda-json` 模式下导入其解析模块。
- 许可证：GNU Affero General Public License v3.0。
- 本仓库**不包含、也不重新分发** easyeda2kicad 的源代码；使用者需自行
  `pip install easyeda2kicad`。如需再分发包含它的组合包，请遵守 AGPL-3.0。

## 2. LCKiConverter — MIT（格式参考）

- 仓库：<https://github.com/xtoolbox/lckiconverter>
- 用途：`references/easyeda-kicad-mapping.md` 中的 EasyEDA → KiCad 单位/图层/焊盘/3D
  映射结论，部分来自阅读其源码（`src/jlc/*.ts`）后的实测归纳；本仓库未复制其代码。
- 许可证：MIT。原项目版权声明：

  > Copyright (c) 2021 XToolBox — admin@xtoolbox.org — http://lckicad.xtoolbox.org

## 3. 资料链接

- EasyEDA 文件格式：<https://docs.easyeda.com/en/DocumentFormat/2-EasyEDA-Schematic-File-Format/>
- KiCad 库文件格式（legacy）：<https://www.compuphase.com/electronics/LibraryFileFormats.pdf>

## 4. 商标

EasyEDA、嘉立创 / LCSC、KiCad 等名称与商标归各自所有者所有。本仓库与这些厂商
无隶属关系，也未获其背书。转换结果来自社区公开数据，非官方库。
