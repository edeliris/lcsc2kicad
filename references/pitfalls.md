# 实测踩坑清单（本工作流配套）

> 2026-10-18～10-22 真机实测（Windows 10/11 中文系统，KiCad 10.0.6，easyeda2kicad 1.0.1，Python 3.13.5）。
> 每条按**现象 → 原因 → 处理**记录；同一问题第二次出现就固化到这里。
> 快速索引：一 限流｜二 GBK｜三 同名封装｜四 3D 缺失｜五 权限｜六 KiCad 运行中｜七 kicad-cli｜
> 八 fp upgrade｜九 模块类｜十 PATH｜十一 环境变量结构｜十二 引脚名重叠｜十三 UTF-8 BOM｜
> 十四 逐引脚真值｜十五 项目本地注册｜十六 LCKiConverter｜十七 封装命名/元数据｜
> 十八 3D 绝对路径｜附录 A 料号表

---

## 一、API 限流（最容易让人以为"工具坏了"）

**现象**
- 批量转 17 个料号：前约 **13 个成功**，之后**全部** `[ERROR] API request failed: HTTP Error 403: Forbidden`
- 换料号、立刻重试**都还是 403**（不是单件问题）
- 单独测 1 个也 403，等一会才好

**原因**：easyeda.com / LCSC API 有**频率限制**，按 IP 计数。

**处理**
1. 每个料号间隔 **5~10 秒**（`--delay 8`）
2. 失败退避 **30~60 秒**重试 2~4 次（`--backoff 45 --retries 4`）
3. 仍 403 ⇒ **等 10~30 分钟**，或换网络（手机热点）
4. 17 个元件按此策略约 **8~10 分钟**完成，实测 **17/17 成功**

**顺带**：`easyeda.com/api/...` 用裸 `urllib` 请求会直接 403（缺 User-Agent/Referer 等）；
不要自己写请求，用 `easyeda2kicad` 自带客户端。

---

## 二、中文 Windows 控制台 GBK 崩溃（阴险：崩在"已成功"之后）

**现象**
```
UnicodeEncodeError: 'gbk' codec can't encode character '\u2713' in position 6
```
- 脚本打印 `✓`/`✗`、或者输出中文时崩
- **第一次批量任务就崩在"第 1 个已转换成功之后"**，让人误以为转换失败

**原因**：中文 Windows 控制台默认 `GBK`（cp936），Python 的 stdout 用它编码。

**处理**（脚本已内置）
```python
import sys
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")
```
+ 日志标记用 ASCII：`[OK]` / `[FAIL]`。
另外：PowerShell 里跑 python 前设 `[Console]::OutputEncoding = [Text.Encoding]::UTF8` 更稳。

---

## 三、同名封装冲突（看着像错误，其实无害）

**现象**
```
[ERROR] Footprint for C17414 already exists. Use --overwrite to replace
```
所有 0805 电阻共用封装 `R0805`，第二个料号开始就报这个。

**处理**：用本技能脚本时**默认就带 `--overwrite`**（`build_kicad_lib.py` 自动传给 easyeda2kicad），
所以正常流程不会看到这条报错；只有直接手工调 `easyeda2kicad` 才需要自己加。
> 覆盖是有意行为：同一料号的同名封装本来就该是同一个文件。
> 若确实要"保留已经存在的旧封装、不覆盖"，用脚本的 `--no-overwrite`（不是 `--overwrite`）。
> 注意：两个料号封装同名但内容不同（很少见）时，后一个会覆盖前一个；要区分请改封装名。

---

## 四、3D 模型缺失是常态

**现象**：`[WARNING] No 3D model available for this component`
实测 17 个元件里 **14 个有 3D**，470µF 电解（C48970781）没有。

**处理**：忽略。没有 3D 不影响出板，只是 3D 预览里少个模型。

---

## 五、写 KiCad 配置目录被拒（沙箱/权限）

**现象**：`UnauthorizedAccessException` / `Permission denied`
- `%APPDATA%\kicad`（Windows）、`~/.config/kicad`（Linux）都在**项目工作区之外**
- 更阴险的是：脚本只判断"没抛异常"就会**打印"已写入"但其实没写**

**处理**
1. 脚本**写后读回校验**（本工作流的脚本都这么干）
2. 给足文件权限（沙箱里需要放宽到可写该目录）
3. 手动兜底：直接编辑 `sym-lib-table` / `fp-lib-table` / `kicad_common.json`

---

## 六、KiCad 运行中改配置会被覆盖

**现象**：改了 `sym-lib-table`，重启 KiCad 后新库不见了。
**原因**：KiCad 退出时会把内存里的库表重写回文件。
**处理**：**先关 KiCad 再改**（`import_to_kicad.py` 会检测进程并警告/拒绝）。

---

## 七、`kicad-cli` 在受限环境里刷注册表错误（通常不致命）

**现象**：stderr 刷几十行 `Error: Can't create registry key 'HKCU\Software\kicad-cli' (error 5)`。
**原因**：Windows 上 `kicad-cli` 启动会写注册表；受限沙箱禁止写。
**处理**：**以 exit code 与输出文件为准**——实测 10.0.6 在沙箱里刷这些错误仍 `exit=0`、产物正常
（`sym upgrade`、`fp upgrade --force` 都通过）。真跑不起来时给正常权限，
或 `verify_library.py --skip-cli` 只做静态检查。

---

## 八、`kicad-cli fp upgrade` 的正确用法（错误提示会误导）

| 写法 | 结果 |
|---|---|
| `kicad-cli fp upgrade <单个.kicad_mod>` | `exit=4`，提示"转换旧版库和非 KiCad 库时必须指定输出路径"（**其实是参数用错，不是库坏了**） |
| `kicad-cli fp upgrade <库.pretty>` | `exit=4`，同上（默认不是原地升级） |
| **`kicad-cli fp upgrade -o <输出目录> <库.pretty>`** | ✅ **exit=0**，输出与输入数量一致 |
| `kicad-cli fp upgrade` 对一个**已是当前格式**的库 | 提示 `封装库未更新`、**不产生输出**（不是失败） | 加 `--force` 强制重写，或视为已解析成功；`verify_library.py` 已按此处理 |

`kicad-cli sym upgrade <库.kicad_sym>` 则**可以原地**升级（在副本上用）。
`kicad-cli fp export svg -o <目录> <库.pretty>` 也能用，但实测在某个封面上会崩、只出 4 个 SVG
（`exit=-1`，提示"创建文件失败"）—— **不要用它当验证手段**，用 `upgrade`。

---

## 九、模块类元件：转换 ≠ 可用于出板

**现象**：`B1205S-3WR3`、`EE19-600:600-3+3`、`RJ11_6P4C`、排针等"模块/连接器"转换出来的符号能用，
但**封装尺寸随厂家差异很大**，直接出板可能焊不上。

**建议**
- **模块类优先用 KiCad 自带通用符号**（`Connector_Generic:Conn_01x11` 等）+ 文字标注引脚名
- 必须用转换结果时：**逐个对照数据手册核对**封装外形、引脚间距、**引脚编号顺序**（镜像/反向是高频错误）

---

## 十、安装位置与 PATH

**现象**：`easyeda2kicad` 命令找不到（`'easyeda2kicad' 不是内部或外部命令`）。
**原因**：pip 装到用户目录（`%APPDATA%\Python\PythonXY\Scripts`），不在 PATH。
**处理**：一律用 `python -m easyeda2kicad ...`；或把该 Scripts 目录加入 PATH。

---

## 十一、`kicad_common.json` 环境变量：结构写错会**静默失效**

**现象**
- 把 `"EASYEDA2KICAD"` 直接写进 `environment` 里，看起来写成功了
- 打开一次 KiCad 后**变量消失**，且**没有任何报错**

**原因**
- KiCad 10 的 `kicad_common.json` 里 `environment` 的实际结构是
  `{"environment": {"vars": {"NAME": "path"}}}`，原文件本来就有 `"vars": null`
- 平铺写会制造**重复的 `environment` 键**：JSON 解析取**最后一个**（即 `{"vars": null}`），
  自定义变量被丢弃；KiCad 退出时再重写文件，就把它彻底抹掉了

**正确写法**
```json
"environment": {
  "vars": { "EASYEDA2KICAD": "C:/Users/you/myproject/kicad" }
}
```

**处理**
- `import_to_kicad.py` 已按 `environment.vars` 写入并**读回校验**
- 更稳的做法：库表里直接用**绝对路径**，不依赖环境变量
  （实测：绝对路径的库条目在 KiCad 打开/关闭后**完好保留**）
- 路径比较要归一化：`C:\a\b` 与 `C:/a/b` 视为同一路径（脚本已做）

**验证命令**
```bash
python -c "import json,os; d=json.load(open(os.path.expandvars(r'%APPDATA%\kicad\10.0\kicad_common.json'),encoding='utf-8')); print((d.get('environment') or {}).get('vars'))"
```

---

## 十二、符号里**引脚名与引脚编号重叠**（easyeda2kicad 输出必踩）

**现象**
KiCad 符号选择器里，引脚旁的**红色编号**（1/2）和**绿色引脚名**（A/K）**画在同一位置**、完全重叠，
符号看着糊成一团。立创EDA 里同一个元件显示正常。

**根因**
`easyeda2kicad` 生成的 `.kicad_sym` **没有 `(pin_names ...)` 指令**。
KiCad 缺省时把 **pin name offset 当作 0**，而 **offset = 0 的语义是"引脚名画在引脚线外侧上方"**
—— 与引脚编号同位置 ⇒ 必然重叠。
（KiCad 自己的标准符号都显式写了 `(pin_names (offset 1.016) hide)` 之类，所以不会。）

**修法**（本技能自带 `scripts/fix_symbol_pin_names.py`）
```bash
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --dry-run   # 先看
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym             # 改（自动备份）
```
规则：
- 引脚名是**纯数字**（与编号重复，如 电阻/电容/排针）→ `(pin_names (offset 1.016) hide)`：只显示编号，干净
- 引脚名**有意义**（A/K、+VIN/-VOUT、AN/CA/COL/EM）→ `(pin_names (offset 0.762))`：名字移到体内，编号仍在引脚上方

**改完必须重启 KiCad**（符号库在启动时加载并缓存；否则看到的还是旧的）。

**实测**：某项目库 17 个符号、另一个库 1 个符号
都补了指令，括号平衡、`kicad-cli sym upgrade` 解析 exit=0。

### 附：嘉立创到底怎么做的（实测 EasyEDA 原始数据，2026-10-18）

直接用 easyeda2kicad 的 `EasyedaSymbolImporter` 读**原始符号数据**，看每个引脚的
`name.is_displayed`：

| 元件 | 引脚名 | `name.is_displayed` |
|---|---|---|
| 1N4148WS（C2128） | A / K | **False** |
| P6KE82A（C434967） | A / C | **False** |
| PC817X2NIP0F（C130469） | AN/CA/COL/EM | **False** |
| CL21B104（C28233） | 1 / 2 | **False** |
| 1x11 排针（C5754991） | 1…11 | True |
| B1205S-3WR3（C55019967） | +VIN/-VIN/+VOUT/-VOUT | True（画在管脚外侧） |

**结论：嘉立创默认不显示引脚名**，只有少数（电源模块的 +VIN/-VOUT、排针编号）才显示。
easyeda2kicad 把这个开关**丢掉了**，而 KiCad 又缺省 offset=0（名字画在引脚外侧上方）→ 必然重叠。

⇒ 正确修法不是"把名字往体内挪"，而是**对齐嘉立创：默认隐藏名字**。
最终策略（脚本默认 `--mode hide`）：16 个隐藏、只给 `B1205S-3WR3` 保留显示（`--show B1205S-3WR3`）。

> 2026-10-21 起，脚本优先用**逐引脚真值**（见 §十四），上面的 `--mode` 只在拿不到原始数据时兜底。

---

## 十三、UTF-8 BOM：KiCad 直接拒绝加载符号库（实测，必看）

**现象**：`kicad-cli sym upgrade` 报 `无法加载库`，exit=2；KiCad 里库能注册但符号列表为空/报错。
**原因**：文件开头有 UTF-8 BOM（`EF BB BF`）。KiCad 的 S 表达式解析器不剥 BOM。

**实测对照**（同一份符号库）：

| 文件 | `kicad-cli sym upgrade` |
|---|---|
| 原文件（无 BOM） | exit=0，提示"用更新的格式保存符号库" |
| 加 BOM | **exit=2，无法加载库** |
| 去掉 BOM | exit=0 |

**处理**
* `fix_symbol_pin_names.py` 已内置：读入用 `utf-8-sig`，**写回一律无 BOM**，并在检测到 BOM 时告警。
* 手工兜底：用支持"UTF-8 无 BOM"的编辑器另存；或用 PowerShell：
  `[IO.File]::WriteAllText($p, [IO.File]::ReadAllText($p,[Text.Encoding]::UTF8), (New-Object Text.UTF8Encoding($false)))`
* 同理：`.json`（`--easyeda-json`）也按 `utf-8-sig` 读，避免 BOM 让 `json.loads` 直接失败。

---

## 十四、逐引脚引脚名显示：用嘉立创真值，别只靠启发式

**背景**：每个引脚的 `name.is_displayed` 是 EasyEDA 原始数据自带的开关（std 段 3 第 1 字段，
Pro 格式同义）。旧脚本只能按"名字是不是纯数字"猜；现在两条路拿真值：

```bash
# a) 联网按料号取（走 easyeda2kicad 客户端，带缓存与限流退避）
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 C55019967

# b) 离线用已保存的原始 JSON（API 响应、或其中的 result）
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --easyeda-json raw/
```

**匹配方式**（不需要手工指定符号名）：Datasheet 属性里的 `C\d+` → EasyEDA 料号；
否则按元件名（忽略大小写与符号）；再否则按引脚号指纹唯一匹配。

**三种决定**
| 真值情况 | 处理 |
|---|---|
| 全部隐藏 | 符号级 `(pin_names (offset 1.016) hide)`，**保留**名字文本 |
| 全部显示 | 符号级 `(pin_names (offset 0.762))` |
| 混合 | `--per-pin blank`（默认）清空隐藏引脚的 name 文本；`--per-pin symbol` 只保留符号级开关 |

**KiCad 格式约束**（实测，详见 `easyeda-kicad-mapping.md` §七）：
引脚级 `(hide yes)` 会被 kicad-cli **静默丢弃**；name 写 `"~"` 会显示字面 `~`；写 `""` 才真正不显示。
⇒ 逐引脚隐藏=清空名字文本，属有意行为；`--report` 会记录清空了哪些引脚号，原始名字随时可从 EasyEDA 数据再取。

**缓存与限流**：`--fetch` 的缓存目录是**当前工作目录**下的 `.easyeda_cache/`；换目录会重新联网。
失败按 `--delay/--retries/--backoff` 退避；仍 403 见 §一。`--fetch` 一个料号一次请求，比批量转换温和得多。

**实测**（2026-10-21，17 符号库，2 个料号）：
`1N4148WST4`（C2128）2/2 引脚 → "EasyEDA 全隐藏"；`B1205S-3WR3`（C55019967）4/4 引脚 → "EasyEDA 全显示"；
与 §十二 手测表一致；改后 `kicad-cli sym upgrade` exit=0；重复运行输出"无需修改"。

---

## 十五、项目本地注册（`--project-local`）：推荐做法

**为什么**：全局注册要改 `%APPDATA%\kicad\<版本>\sym-lib-table` / `fp-lib-table` /
`kicad_common.json`（见 §五、§六、§十一）；项目本地注册只动项目自己的两张表，天然绕开这些坑。

```bash
# 先干跑
python scripts/import_to_kicad.py --lib-basename ./myproject/kicad/myproj \
    --project-local ./myproject --dry-run

# 注册（条目写成 ${KIPRJMOD}/kicad/myproj.kicad_sym）
python scripts/import_to_kicad.py --lib-basename ./myproject/kicad/myproj \
    --project-local ./myproject

# 卸载
python scripts/import_to_kicad.py --nickname myproj --project-local ./myproject --remove
```

**要点**
* `${KIPRJMOD}` 是 KiCad 项目变量；库放在项目内时，整个项目可搬走、可提交。
* 库在项目外也能注册，但会告警：`${KIPRJMOD}/../...` 换机器/换目录会失效。
* 该模式**不写环境变量、不碰全局库表**；无需关闭 KiCad（项目表只在该项目保存时被 KiCad 改写）。
* 实测（2026-10-22）：dry-run→写入→读回 OK→重复运行 skip→`--remove` 后表内只剩表头。

---

## 十六、LCKiConverter（浏览器扩展）可借鉴清单

仓库：<https://github.com/xtoolbox/lckiconverter>（MIT，169 star，最后 push 2023-02-27）。
完整格式对照与"别照抄"清单见 `easyeda-kicad-mapping.md` §八，这里只记结论：

1. **数据获取路线**：页面内按 uuid 取数（`lceda.cn/api/components/<uuid>`、
   `analyzer/api/3dmodel/<uuid>`、`modules.lceda.cn/3dmodel/<uuid>`），
   交互式一次一件，避开 easyeda2kicad 批量 403 的触发点。
2. **项目本地库表**：`${KIPRJMOD}` 相对路径 —— 本技能已按此法实现 `--project-local`（§十五）。
3. **真实抓包做夹具**：`test_data/*.js` 存页面原始 shape 字符串（槽孔、负坐标、中文斜体都有）。
4. **降级策略**：无 courtyard 用 BBox 生成、bezier 拆 polyline、>180° 圆弧拆两段、未知 shape 输出注释。
5. **别照抄**：KiCad 5 legacy 输出；`svg_solve_arc` / `svg_calc_arc` 在旋转椭圆弧下公式写错；
   `rotatePoint` 丢中间结果；`svg.ts` 模块级全局坐标状态。细节见映射表 §八。

---

## 十七、封装编辑器搜不到元件型号 / 描述空白（正常，但有改法）

**现象**
* 符号编辑器搜 `CH334F` 能搜到；封装编辑器搜 `CH334F` 搜不到，搜库昵称 `C5187527` 才行。
* 封装属性里"描述/关键词/数据手册"全空。

**原因**
1. **命名惯例不同**：easyeda2kicad 把**符号**命名成"型号_料号"（`CH334F_C5187527`），
   把**封装**命名成"封装型号"（`QFN-24_L4.0-W4.0-P0.50-BL-EP2.8`）；库昵称是注册时定的
   （本工作流默认取库基名，例如 `C5187527`）。封装选择器按"库昵称 + 封装名"过滤，
   所以按元件型号搜不到，按料号（昵称）反而能搜到。
2. **元数据缺口**：转换器只在 `.kicad_mod` 里写 `(property "LCSC Part")`，
   不写 `(descr ...)` / `(tags ...)` / `"Datasheet"` / `"Description"`。
   KiCad 10 标准库实测 776/776 个封装都有 `descr`+`tags`，149 个还有 `Datasheet` 属性。

**正常性判断**：这是转换器行为，不是故障。日常"分配封装"走符号的 Footprint 字段，
不需要搜索；只有手动换封装时才需要按封装名/库昵称搜。

**改法（可选，按需）**
* **A 补元数据**：给 `.kicad_mod` 加
  `(descr "…")`、`(tags "CH334F USB HUB WCH QFN-24 LCSC C5187527")`、
  `(property "Datasheet" "<URL>")`、`(property "Description" "…")`；
  再把 `(property "Value" ...)` 改成新封装名。
* **B 封装改名带型号**：文件名 + `(footprint "…")` + `Value` 一起改成
  `CH334F_C5187527_QFN-24_…`；同时把符号的 `Footprint` 字段改成
  `"<昵称>:CH334F_C5187527_QFN-24_…"`。搜索 `CH334F` 即命中。
* **C 改库昵称**：把昵称改成 `CH334F_C5187527`（全局库表两行 + 符号 Footprint 前缀）。
* 注意：**重新转换会覆盖 A/B 的改动**，把改名/补元数据当成转换后的固定后处理步骤。

**实测（2026-10-22，CH334F / C5187527）**：
A+B 均已落地 —— `descr`/`tags`/`Datasheet`/`Description` 填好，封装名改
`CH334F_C5187527_QFN-24_L4.0-W4.0-P0.50-BL-EP2.8`，符号 Footprint 字段同步；
`kicad-cli fp upgrade --force` exit=0，元数据全部被 KiCad 解析保留。

---

## 十八、3D 模型写成绝对路径：换机器/换目录 3D 就丢

**现象**：`(model "C:/Users/…/xxx.3dshapes/part.wrl")`，换一台机器、或把项目挪个目录后，
封装本身正常，但 3D 预览空白。
**原因**：easyeda2kicad 把转换时的**绝对路径**写进封装；KiCad 不会自动去找 `.3dshapes` 邻居目录。
**处理**
1. `fix_model_paths.py --lib-basename <库基名> --base <项目根>` 把路径改成 `${KIPRJMOD}/…`
   （全局 3D 目录用 `--variable KICAD10_3DMODEL_DIR`）；可重复运行，已是变量的跳过。
2. 手工兜底：封装属性 → 3D Models → 把模型路径改成变量形式。
**实测（2026-10-22）**：CH334F 库改写后 `kicad-cli fp upgrade --force` exit=0；
复跑输出"无需修改（已是路径变量…）"。

---

## 附录 A：本机实测的 17 个料号（某 SLIC 接口板项目）

| 料号 | 元件 | 备注 |
|---|---|---|
| C5754991 | 1x11 排针 2.54 | 代表 KS0835F SLIC 模块 |
| C9900010030 | 1x2 排针 | J1 12V / JP1 PD |
| C9900010031 | 1x3 排针 | J3 音频 |
| C9900010184 | 1x4 排针 | J2 控制 / 光耦模块 |
| C9900020346 | RJ11 6P4C | J4 话机 |
| C55019967 | B1205S-3WR3 | 隔离 DC-DC 12V→5V 3W |
| C9900292356 | EE19-600:600-3+3 | 音频隔离变压器 |
| C17513 | 0805 1kΩ | 光耦 LED 限流 |
| C17414 | 0805 10kΩ | 光耦下拉 |
| C17514 | 0805 1MΩ | 偏置（可选） |
| C48970781 | 470µF/10V 电解 | 储能；**无 3D** |
| C28233 | 0805 0.1µF | 去耦/耦合 |
| C2680605 | JK250-050U | 500mA PTC |
| C2831634 | JK250-120U | 1.2A PTC |
| C434967 | P6KE82A | 82V TVS |
| C2128 | 1N4148WS | PD 二极管 |
| C130469 | PC817X2NIP0F | 光耦（裸件方案） |

> 可直接当 `--ids-file` 的版本见 `sample-lcsc-ids.md`。

