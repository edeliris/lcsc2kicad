# EasyEDA / 立创 → KiCad 映射表与格式事实

> 口径：easyeda2kicad 1.0.1（本机 `site-packages` 源码）、kicad-cli 10.0.6、Python 3.13.5。
> 实测日期：2026-10-18（转换/注册）、2026-10-21～22（格式事实、逐引脚、项目本地）。
> 数据来源两处，均可在本机或公网复核：
> * easyeda2kicad：`%APPDATA%\Python\Python313\site-packages\easyeda2kicad\`
> * LCKiConverter（MIT）：<https://github.com/xtoolbox/lckiconverter> 的 `src/jlc/*.ts`

---

## 一、单位与坐标

| 量 | EasyEDA | KiCad | 换算 | 出处 |
|---|---|---|---|---|
| 符号坐标 | px，5 px = 1.27 mm | mm | `mm = px × 10 × 0.0254` | easyeda2kicad `export_kicad_symbol.py` `px_to_mm` |
| 符号坐标（legacy `.lib`） | px | mil | `mil = px × 10` | LCKiConverter `std_symbol.ts` `ratio = 10` |
| 符号文字字号 | `"7pt"` 或数字 | mm | `mm = pt × 0.3528` | easyeda2kicad `easyeda_importer.py` `add_easyeda_text` |
| 符号线宽（legacy） | px | mil | `× 6`（0.254 mm ≈ 6 mil） | LCKiConverter `j2k_line_width` |
| 符号角度（legacy） | 度 | 0.1° | `× 10` | LCKiConverter `j2k_angle` |
| 封装坐标 | 0.254 mm 单位 | mm | `mm = 单位 × 0.254` | LCKiConverter `std_footprint.ts` `toMM` |
| 封装文字字号 | 0.254 mm 单位 | mm | 高 = 字号 × 0.75 | LCKiConverter `fontHeightRatio` |

> 符号与封装的基本单位是同一个：0.254 mm（10 mil）。KiCad 的 .kicad_sym 用 mm，legacy `.lib` 用 mil。

---

## 二、引脚（std 格式）

引脚数据段共 7 段，`^^` 分隔，每段内 `~` 分隔：

| 段 | 内容 | 字段 |
|---|---|---|
| 0 | 设置 | `is_displayed`（show/hide）、`type`、`spice_pin_number`、`pos_x`、`pos_y`、`rotation`、`id`、`is_locked` |
| 1 | 圆点 | `dot_x`、`dot_y` |
| 2 | 引脚线 | `path`、`color` |
| 3 | **引脚名** | **`is_displayed`**、`pos_x`、`pos_y`、`rotation`、`text`、`text_anchor`、`font`、`font_size` |
| 4 | 引脚号 | `show`、`x`、`y`、`rotation`、`number`、`text_anchor`、`font`、`font_size` |
| 5 | 反相圈 | `is_displayed`、`circle_x`、`circle_y` |
| 6 | 时钟 | `is_displayed`、`path` |

* **`name.is_displayed` 就是嘉立创的"显示引脚名"开关**；easyeda2kicad 解析到了它，但 `convert_ee_pins()` 只取了 `name.text`，开关被丢掉。
* 布尔解析（easyeda2kicad `_safe_bool`）：`"0"` / `"false"` / `"hide"` → False；`"1"` / `"true"` / `"show"` → True；空 → 该字段的默认值（引脚名默认 True）。
* 引脚号以第 4 段第 5 个字段为准（`spice_pin_number` 会被覆盖）。
* 电气类型：`0=unspecified`、`1=input`、`2=output`、`3=bidirectional`、`4=power` → KiCad `unspecified/input/output/bidirectional/power_in`。
* 反相圈 → KiCad `inverted`；时钟 → `clock`；两者都有 → `inverted_clock`。
* 引脚长度：easyeda2kicad 从 `pin_path` 的 `split("h")[-1]` 取，再对齐 1.27 mm 网格。

---

## 三、封装图层映射（std 格式）

| EasyEDA id | KiCad 图层 | 说明 |
|---|---|---|
| 1 / 2 | `F.Cu` / `B.Cu` | 焊盘时同时给 `F.Paste`/`B.Paste` + `F.Mask`/`B.Mask` |
| 3 / 4 | `F.SilkS` / `B.SilkS` | |
| 5 / 6 | `F.Paste` / `B.Paste` | |
| 7 / 8 | `F.Mask` / `B.Mask` | |
| 9 | `Eco2.User` | 原 `JLC.RateLines`，无对应层 |
| 10 | `Edge.Cuts` | 外框/开槽 |
| 11 | `*.Cu` + `*.Mask` | 通孔焊盘 |
| 12 | `Dwgs.User` | |
| 13 / 14 | `F.Fab` / `B.Fab` | |
| 15 | `M` | 多铜层标记 |
| 16 / 19 / 100 / 101 | `Eco2.User` | 机械、3D、元件外形等专有层 |
| 21–52 | `In(n-20).Cu` | 内层 1–32 |
| 99 | `F.CrtYd` | 元件外形 → courtyard |
| `"Hole"` | `JLC.Hole` | 非 KiCad 图层名，落缺省层 |

> 未知层统一落 `Eco2.User`，**不丢图元**——这是稳妥降级，值得抄。

---

## 四、焊盘与孔

字段顺序（`parse_PAD`）：`shape~x~y~w~h~layerId~net~number~holeRadius~points~rotate~id~holeLength~holePoints~plated~locked~u1~u2`。

| 判定 | KiCad 结果 |
|---|---|
| `holeRadius > 0` | `thru_hole`，drill = 直径 × 2 |
| `plated == "N"` | `np_thru_hole` |
| `holeLength > 0` | 槽孔：`drill oval`，用 `(w<h) != (drillX<drillY)` 判长轴 |
| `ELLIPSE` | `circle`（长宽不等时退化为椭圆近似） |
| `RECT` | `rect` |
| `OVAL` | `oval` |
| `POLYGON` | `custom` + `gr_poly` + `zone_connect 0` + `anchor circle` |
| `VIA` / `HOLE` | 独立 `pad`：`thru_hole` / `np_thru_hole` |

> 槽孔轴向判断与 `oval drill` 用例见 LCKiConverter `test_data/test_std_footprint_oval_drill.js`（46 KB 真实样本）。

---

## 五、3D 模型

3D 模型不是独立附件，而是封装 `shape` 里的 `SVGNODE~<json>`；JSON 关键字段：

| 字段 | 含义 |
|---|---|
| `attrs.title` | 模型文件名（不含扩展名） |
| `attrs.c_origin` | `"x,y"`，模型在封装里的平面位置 |
| `attrs.c_rotation` | `"rx,ry,rz"`，度 |
| `attrs.z` | 高度；通孔器件强制 0 |
| `attrs.c_witdh` / `c_height` | 包围盒宽高 |

KiCad 侧：

```
(model "${KIPRJMOD}/<库名>.3dshapes/<title>.step"
  (offset (xyz <x_mm> <-y_mm> <z_mm>))
  (scale (xyz 1 1 1))
  (rotate (xyz -rx -ry -rz))
)
```

* 路径模板：LCKiConverter 默认 `${KIPRJMOD}`；装全局库时可用 `${KICAD10_3DMODEL_DIR}`。
* 同一模型同时写 `.step` 与 `.wrl` 两条 `(model)`；KiCad 优先 STEP。
* 通孔器件把 z 置 0，否则模型会浮在板面上方。

---

## 六、文本与非 ASCII 丝印

* 封装文本：`T~type~x~y~penWidth~rotate~mirror~layerId~net~fontSize~text~path~display~...`；`display == "none"` → 隐藏。
* **非 ASCII 文字（中文丝印）**：KiCad footprint 文本对 UTF-8 支持不可靠，LCKiConverter 的做法是把 `path`（字形轮廓）拆成折线绘制，代价是不可编辑。
* 符号文本：`j2k_string` 会把 `~` 转义成 `~~`、空白转 `_`、空串写 `~`（legacy 语义）；.kicad_sym 里直接用 `""` 即可。

---

## 七、KiCad 格式事实（本机实测，2026-10-21）

| # | 事实 | 验证方法 | 结果 |
|---|---|---|---|
| 1 | 符号级 `(pin_names (offset x) hide)` 有效 | 生成库 + `kicad-cli sym upgrade` | exit=0，KiCad 认可 |
| 2 | 引脚级 `(hide yes)` 写在 name 的 effects 里**无效** | 同上，观察 upgrade 输出 | `(hide yes)` 被**静默丢弃**，名字照常显示 |
| 3 | 引脚名写 `"~"` | `kicad-cli sym export svg` | SVG 里出现字面 `~` |
| 4 | 引脚名写 `""` | 同上 | 该引脚只显示编号，名字不显示 |
| 5 | UTF-8 BOM 会让库无法加载 | 同一文件加/去 BOM 各跑一次 `sym upgrade` | 有 BOM：`无法加载库`，exit=2；无 BOM：exit=0 |
| 6 | 空 `.pretty` 目录不能作为封装库验证依据 | `kicad-cli fp upgrade -o` | 见 `pitfalls.md` §八 |
| 7 | 封装元数据存放位置（KiCad 10） | 对照自带库与升级输出 | 描述用 `(descr ...)`、关键词用 `(tags ...)`，另有 `Datasheet` / `Description` 属性；easyeda2kicad **都不写** |

> 结论：**逐引脚隐藏只能"清空名字文本"**；`fix_symbol_pin_names.py --per-pin blank` 即此策略。

---

## 八、LCKiConverter 值得抄 / 不要抄

值得抄：

1. **数据获取路线**：页面内按 uuid 取数，绕开批量 API 限流。
   `https://lceda.cn/api/components/<uuid>`（专业版 `pro.lceda.cn`）、
   `https://lceda.cn/analyzer/api/3dmodel/<uuid>`、`https://modules.lceda.cn/3dmodel/<uuid>`；
   Datasheet 从 `item.szlcsc.com/<id>.html` 正则提取 PDF 链接。
2. **项目本地库表**：`${KIPRJMOD}` 相对路径，跨机器可迁移、不碰全局配置。
3. **真实抓包做测试夹具**：`test_data/*.js` 直接存页面原始 shape 字符串，覆盖槽孔、负坐标、中文斜体等边角。
4. **降级策略**：无 courtyard 用 BBox 生成；bezier 拆 polyline；>180° 圆弧拆两段；未知 shape 输出注释而非崩溃。
5. **纯 JS 生成 STEP**：AP214 文本拼装、边/顶点去重、颜色 `STYLED_ITEM`；输出 `SHELL_BASED_SURFACE_MODEL`（只有面、没有实体）。

不要照抄：

| 问题 | 位置 | 说明 |
|---|---|---|
| 输出 KiCad 5 legacy | `convert.ts`、`std_symbol.ts` | `EESchema-LIBRARY 2.4`、`DEF/F0/DRAW`、`module`；今天应直出 v6+ S 表达式 |
| 圆弧求解在 `xRotate≠0` 时错误 | `svg.ts` `svg_solve_arc` / `svg_calc_arc` | 旋转椭圆弧圆心公式写错（`sinφ·cx'` 应为 `sinφ·cy'` 等） |
| `rotatePoint` 丢弃中间结果 | `std_footprint.ts` | 绕非原点旋转会错位（POLYGON 焊盘旋转会中招） |
| 模块级可变状态 | `svg.ts` `g_x`/`g_y` | 非重入，连续调用互相污染 |
| 贝塞尔当直线 | `svg.ts` `c/C` | 曲线轮廓退化为折线 |
| 3 年未更新 | 仓库 | 最后 push 2023-02-27，API 端点需重新验证 |

---

## 九、参考

* EasyEDA 文件格式：<https://docs.easyeda.com/en/DocumentFormat/2-EasyEDA-Schematic-File-Format/>
* KiCad 库文件格式（legacy）：<https://www.compuphase.com/electronics/LibraryFileFormats.pdf>
* easyeda2kicad：<https://github.com/uPesy/easyeda2kicad.py>
* LCKiConverter：<https://github.com/xtoolbox/lckiconverter>
