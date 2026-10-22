---
name: lcsc2kicad
description: >-
  立创商城（LCSC）/ 嘉立创EDA（EasyEDA）元件库 → KiCad 完整工作流：
  下载符号+封装+3D → 对齐引脚名 → 3D 路径变量化 → 注册进库表 → 用 kicad-cli 验证 →（可选）补封装元数据。
  含限流 403、中文控制台 GBK、UTF-8 BOM、逐引脚 name.is_displayed、BOM 导入、工程清单、项目本地注册等实测坑。
  触发词："立创元件转KiCad"、"LCSC 元件库"、"easyeda2kicad"、"立创EDA 导入 KiCad"、
  "把立创料号做成 KiCad 库"、"lcsc2kicad"、"C2040 转 KiCad"。
  也适用于：按立创料号（C 开头）批量建 KiCad 库、给 KiCad 项目补上 LCSC 元件。
license: MIT
compatibility: Windows / macOS / Linux；Python 3.9+；KiCad 6.0+（本工作流在 KiCad 10.0.6 实测通过）；需联网访问 easyeda.com API
metadata:
  author: TP
  version: "1.4.0"
  openclaw:
    requires:
      bins:
        - python
        - pip
      optional_bins:
        - kicad-cli
---

# 立创（LCSC / EasyEDA）元件库 → KiCad 工作流

把**立创料号（C 开头）**变成 KiCad 里可用的**符号 + 封装 + 3D 模型**，注册进库表，并用 KiCad 自己的解析器验证。

**管道**：① 转换 → ② 引脚名对齐 → ③ 3D 路径相对化 → ④ 注册 → ⑤ 验证；另可选封装元数据。
①④ 必需；②③⑤ 推荐。每步一个脚本，也可用统一入口 `lcsc2kicad <build|pins|models|register|verify>`。

---

## 0. 先决条件（缺一个都会失败）

| 需要 | 检查命令 | 说明 |
|---|---|---|
| Python 3.9+ 与 pip | `python --version` | 官方建议用 **KiCad 自带 Python**（开始菜单 → *KiCad Command Prompt*） |
| `easyeda2kicad` | `python -m easyeda2kicad --help` | 没有就 `pip install easyeda2kicad` |
| 联网 | 能访问 `easyeda.com` API | 公司/校园网可能拦截；**限流后仍 403 ⇒ 换网络** |
| 写库表目录的权限 | 见 §3 | 全局库表在用户目录之外，受限沙箱会拒绝 |
| 可选 `kicad-cli` | `kicad-cli version` | 第 4 步验证用；没有也可手动验证 |

> **安装位置坑**：pip 可能装到 `%APPDATA%\Python\PythonXY\Scripts`（不在 PATH）
> ⇒ 一律用 `python -m easyeda2kicad` 调用，别指望 `easyeda2kicad` 命令（detail: pitfalls §十）。
>
> 也可直接安装成命令：`pipx install "lcsc2kicad[easyeda] @ git+https://github.com/edeliris/lcsc2kicad-skill.git"`，
> 之后用 `lcsc2kicad <命令>`（见 §7）；不装也能直接 `python scripts/<脚本>.py`。

---

## 1. 转换 — `scripts/build_kicad_lib.py`

```bash
# 单个 / 几个（料号可省略 C 前缀：5187527 会自动补成 C5187527）
python scripts/build_kicad_lib.py --lcsc-id C2040 --output ./kicad/myproj

# 批量（推荐：自带间隔与退避重试）
python scripts/build_kicad_lib.py --lcsc-id C17513 C17414 C17514 --output ./kicad/myproj

# 从文件读料号（每行一个；# 开头为注释；行尾可写备注）
python scripts/build_kicad_lib.py --ids-file my_parts.txt --output ./kicad/myproj

# 工程清单：记录备注与结果，已成功的自动跳过（断点续跑）
python scripts/build_kicad_lib.py --manifest parts.json --output ./kicad/myproj
python scripts/build_kicad_lib.py --manifest parts.json --output ./kicad/myproj --retry-all

# 工程清单 + 文件级更新模式：只补缺失/变化（按记录的文件哈希）
python scripts/build_kicad_lib.py --manifest parts.json --output ./kicad/myproj --update-mode

# BOM 直接读（CSV/TXT/XLSX；扫描全表、去重、过滤 C0402/C0603 等封装代号）
python scripts/build_kicad_lib.py --bom bom.xlsx --output ./kicad/myproj

# 弱网：间隔/退避翻倍、重试 +2、等待 ±20% 抖动
python scripts/build_kicad_lib.py --ids-file my_parts.txt --output ./kicad/myproj --weak-network

# 只补失败的那几个 / 日志落盘
python scripts/build_kicad_lib.py --lcsc-id C2128 C434967 --output ./kicad/myproj --log build.log
```

> BOM 解析：`.csv/.txt/.tsv` 用 UTF-8/GBK 扫描；`.xlsx` 需要 `pip install openpyxl`；
> 旧版 `.xls` 请另存为 `.xlsx`。`--bom` 会自动忽略封装代号（C0402 等），不会当料号转。

`--output` 是**库基名**，不是目录。产出三件套：

```
<基名>.kicad_sym       符号库
<基名>.pretty/         封装库（*.kicad_mod）
<基名>.3dshapes/       3D 模型（*.step + *.wrl）
```

**转换前必须知道的三个坑**（详见 pitfalls）：

| 坑 | 现象 | 处理 |
|---|---|---|
| API 限流 | 连续转约 13 个后全部 `HTTP 403`，换料号无效 | 间隔 5~10 s、退避 30~60 s；仍 403 等 10~30 分钟或换网络（pitfalls §一） |
| 中文控制台 GBK | `UnicodeEncodeError: 'gbk'`，常崩在"已成功"之后 | 脚本已内置 UTF-8 重配置；日志用 ASCII 标记（pitfalls §二） |
| 同名封装冲突 | `Footprint ... already exists` | 脚本**默认带 `--overwrite`**；要保留旧封装用 `--no-overwrite`（pitfalls §三） |

---

## 2. 引脚名对齐 — `scripts/fix_symbol_pin_names.py`

**为什么**：`easyeda2kicad` 不写 `(pin_names ...)`，KiCad 缺省 offset=0 会把引脚名和编号画在同一位置。
嘉立创原始数据里每个引脚本来就有 `name.is_displayed`，脚本按料号取真值、按引脚号对齐。

```bash
# 真值优先：按料号联网取（走 easyeda2kicad 客户端，带缓存与限流退避）
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 C55019967 --dry-run
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 C55019967

# 离线：用保存好的原始 JSON（API 响应或其中的 result，可给目录）
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --easyeda-json raw/

# 拿不到原始数据时退回启发式
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --mode hide
python scripts/fix_symbol_pin_names.py kicad/myproj.kicad_sym --show B1205S-3WR3
```

决定规则：全隐藏 → 符号级 hide（**保留**名字文本）；全显示 → 显示；
混合 → `--per-pin blank`（默认）清空隐藏引脚的名字文本，`--per-pin symbol` 只保留符号级开关。

**三条 KiCad 格式事实**（实测）：逐引脚 `(hide yes)` 会被静默丢弃；名字 `"~"` 显示字面 `~`；
名字 `""` 才真正不显示 ⇒ 逐引脚隐藏只能清空文本。`--report` 可记录清空了哪些引脚号。

> 详情：pitfalls §十二（重叠根因）、§十三（UTF-8 BOM）、§十四（逐引脚真值）；
> 格式事实汇总：`references/easyeda-kicad-mapping.md` §七。**改完必须重启 KiCad**。

---

## 3. 注册 — `scripts/import_to_kicad.py`

### 3.0 工程根判定（Agent 手做，不写脚本）

1. 从库路径向上找含 `.kicad_pro` 的目录 → 它就是工程根；找不到再问用户。
2. 找到 → 用 `--project-local <工程根>`，条目会写成 `${KIPRJMOD}/…`。
3. 找不到、或用户不让改库表 → **不要动任何库表**；在库旁边生成 `KiCad_IMPORT.md`：
   写清符号库/封装库里有什么，附可直接粘贴的 `sym-lib-table` / `fp-lib-table` 两行，
   以及 KiCad 菜单步骤（Preferences → Manage Libraries → Add Existing）。
4. 只有用户明确要求"全局注册"时，才走下面的全局流程（先备份、读回校验）。

**首选：项目本地注册**——只改项目自己的两张库表，用 `${KIPRJMOD}` 相对路径，
不动全局配置、不写环境变量，项目可搬走/可提交：

```bash
python scripts/import_to_kicad.py --lib-basename ./myproject/kicad/myproj \
    --project-local ./myproject --dry-run          # 先看
python scripts/import_to_kicad.py --lib-basename ./myproject/kicad/myproj \
    --project-local ./myproject                    # 写入（自动备份、读回校验）
python scripts/import_to_kicad.py --nickname myproj --project-local ./myproject --remove
```

**备选：全局注册**（多个项目共用一个库时）——改三个纯文本文件，脚本会自动备份并读回校验：

| 改哪个文件 | 内容 |
|---|---|
| `<KiCad用户目录>/<版本>/sym-lib-table` | 一行符号库条目 |
| `<KiCad用户目录>/<版本>/fp-lib-table` | 一行封装库条目 |
| `<KiCad用户目录>/<版本>/kicad_common.json` | `environment.vars.<变量名>`（**不是**平铺在 `environment` 下） |

- 用户目录：Windows `%APPDATA%\kicad\<版本>`；macOS `~/Library/Preferences/kicad/<版本>`；Linux `~/.config/kicad/<版本>`。
- **改前必须关闭 KiCad**（退出时会重写库表）；脚本会检测进程并拒绝。
- 全局条目用**绝对路径**最稳；环境变量只是可选的便捷写法。

> 三个坑：权限（写不进去却"假装成功"）、KiCad 运行中被覆盖、`kicad_common.json` 结构写错静默失效
> ⇒ pitfalls §五、§六、§十一。

### 3.1 3D 模型路径相对化（推荐，注册前后都行）

`easyeda2kicad` 会把 `(model ...)` 写成**绝对路径**，换机器/换目录 3D 预览就丢了。
注册前顺手把模型路径改成 `${KIPRJMOD}` 相对路径，库跟着项目走：

```bash
python scripts/fix_model_paths.py --lib-basename ./myproject/kicad/myproj --base ./myproject --dry-run
python scripts/fix_model_paths.py --lib-basename ./myproject/kicad/myproj --base ./myproject
# 全局 3D 目录：--base "<3dmodels 根>" --variable KICAD10_3DMODEL_DIR
```

只改 `.pretty/*.kicad_mod` 里的 `(model ...)`；已是变量或相对路径的跳过；可重复运行。
实测 `kicad-cli fp upgrade --force` 接受 `${KIPRJMOD}` 形式的模型路径。

---

## 4. 验证 — `scripts/verify_library.py`

```bash
python scripts/verify_library.py --lib-basename ./kicad/myproj
# kicad-cli 不在 PATH 时：
python scripts/verify_library.py --lib-basename ./kicad/myproj --kicad-cli "<KiCad安装目录>/bin/kicad-cli.exe"
```

做四件事：计数（符号/封装/3D）→ 括号平衡 → `sym upgrade --force` → `fp upgrade --force -o`。
后两项用 KiCad 自己的解析器，exit=0 才说明真能读。`--force` 保证"已是当前格式"的库也会重写，
不会误报失败（pitfalls §八）。

> 沙箱里 kicad-cli 可能向 stderr 刷 `Can't create registry key ... error 5`，但通常仍能跑，
> **以 exit code 和输出文件为准**。验证通过后：KiCad → 原理图按 `A` → 搜昵称。

---

## 5. 封装元数据与命名（可选后处理）

转换出来的封装只有 `(property "LCSC Part")`，所以封装属性里**描述/关键词/数据手册为空**，
且封装名是封装型号（如 `QFN-24_L4.0-…`），按元件型号搜不到。这是转换器行为，不是故障。

需要时按 pitfalls §十七 做后处理：补 `(descr ...)`、`(tags ...)`、`Datasheet`、`Description`，
或把封装改名为 `型号_料号_封装型号` 并同步符号的 `Footprint` 字段。注意**重新转换会覆盖**这些改动。

---

## 6. 一键全流程（复制即用）

```bash
base=./kicad/myproj
python scripts/build_kicad_lib.py --ids-file my_parts.txt --output "$base" --manifest parts.json --log build.log
python scripts/fix_symbol_pin_names.py "$base.kicad_sym" --fetch C2040 C2128
python scripts/fix_model_paths.py --lib-basename "$base" --base ./myproject
python scripts/import_to_kicad.py --lib-basename "$base" --project-local ./myproject
python scripts/verify_library.py --lib-basename "$base"
```

装了包之后也可一条命令分步跑：`lcsc2kicad build|pins|models|register|verify …`。

---

## 7. 脚本一览

| 脚本 | 作用 | 关键参数 | 产物 |
|---|---|---|---|
| `build_kicad_lib.py` | 下载并转 KiCad 库 | `--lcsc-id` / `--ids-file` / `--bom` / `--manifest` / `--retry-all` / `--update-mode` / `--weak-network` / `--output` / `--delay --retries --backoff` / `--log` | `.kicad_sym` + `.pretty/` + `.3dshapes/` + 清单 |
| `fix_symbol_pin_names.py` | 引脚名对齐、去 BOM | `--fetch` / `--easyeda-json` / `--per-pin` / `--mode` / `--report` / `--log` | 原地改写 `.kicad_sym`（自动备份） |
| `fix_model_paths.py` | 3D 路径变量化 | `--lib-basename` / `--base` / `--variable` / `--dry-run` | 原地改写 `.kicad_mod` |
| `import_to_kicad.py` | 注册库表 | `--lib-basename` / `--nickname` / `--project-local` / `--dry-run` / `--remove` / `--log` | 项目或全局库表 |
| `verify_library.py` | 验证可解析 | `--lib-basename` / `--kicad-cli` / `--skip-cli` / `--log` | 报告 + exit code |
| `lcsc2kicad.py` | 统一入口 | `build|pins|models|register|verify` | — |

---

## 8. 检查清单

**转换后（脚本已保证，抽查即可）**
- [ ] `verify_library.py` 全部通过（sym/fp 均 exit=0）
- [ ] 符号库 UTF-8 无 BOM；引脚名与嘉立创一致
- [ ] 库表条目读回正确（`--dry-run` 可复核）
- [ ] 3D 模型路径已是变量/相对路径（`fix_model_paths.py`），换机器不丢模型
- [ ] 工程清单（`parts.json`）记录了成功/失败（可选）

**上板前人工核对**
- [ ] 封装尺寸、引脚间距**对照数据手册**
- [ ] **引脚编号顺序**（IC、模块、连接器；镜像/反向是高频错误）
- [ ] 3D 模型路径能在 KiCad 里找到（缺失约 10~20%，不影响出板）
- [ ] 模块类（排针、RJ11、变压器、隔离电源）优先用 KiCad 通用符号 + 文字标注
- [ ] 需要按型号搜封装时，封装名或 `(tags ...)` 带型号（pitfalls §十七）

---

## 9. 失败排查速查表

| 现象 | 原因 | 处理 |
|---|---|---|
| `HTTP Error 403` | IP 限流 | 加 `--delay`、等 10~30 分钟、换网络 |
| `UnicodeEncodeError: 'gbk'` | 中文控制台 | 用本技能脚本（已内置 UTF-8 重配置） |
| `Footprint ... already exists` | 直接调 easyeda2kicad，没加覆盖 | 脚本默认带 `--overwrite`；手工调时自己加 |
| `无法加载库`（`kicad-cli` exit=2） | 符号库带 UTF-8 BOM | 脚本自动剥；手工另存为无 BOM（pitfalls §十三） |
| 引脚名仍压在编号上 | 缺 `(pin_names ...)` 或启发式误判 | `--fetch <料号>` 拿真值重跑（§2） |
| `--fetch` 失败 / 403 | 限流 | 加 `--delay`/`--backoff`；或 `--easyeda-json` 离线（缓存目录：CWD `.easyeda_cache/`） |
| 符号库为空/只有几个 | 中途崩（限流或编码） | 看 `[FAIL]` 清单，`--lcsc-id` 补跑 |
| KiCad 看不到库 | 库表没写进去 / 运行中被覆盖 | `--dry-run` 复核 → 关 KiCad 重新注册 |
| 符号能选但封装找不到 | 封装库 URI 写错或多一个 `/` | 检查 `<基名>.pretty`（**目录，不是文件**） |
| 3D 不显示 | 3D 路径未登记 | `首选项 → 3D 搜索路径` 加 `<基名>.3dshapes` |
| 封装编辑器搜不到型号 | 封装按封装型号命名、昵称是料号 | 搜昵称/封装名；或改名 + 补 tags（pitfalls §十七） |
| 封装描述/数据手册空白 | 转换器只写 `LCSC Part` | 正常；按需补元数据（pitfalls §十七） |
| `No 3D model available` | 该元件无 3D | 正常，忽略 |

---

## 10. 本机实测记录（参考基线）

| 项 | 结果 |
|---|---|
| 工具版本 | `easyeda2kicad 1.0.1`，`kicad-cli 10.0.6`，KiCad 10.0.6，Python 3.13.5 |
| 批量 17 个料号 | 加间隔后 **17/17 成功**，约 10 分钟 |
| 产物 | 符号库 43.9 KB / 17 符号；封装 15 个；3D 14×2；总计 **22.42 MB** |
| 验证 | `sym upgrade` exit=0；`fp upgrade --force -o` exit=0 且数量一致 |
| 逐引脚真值（2026-10-21） | C2128 → 2/2 引脚判"全隐藏"；C55019967 → 4/4 判"全显示"；改后 `sym upgrade` exit=0，复跑"无需修改" |
| 项目本地注册（2026-10-22） | dry-run → 写入 `${KIPRJMOD}/…` → 读回 OK → 幂等 skip → `--remove` 干净 |
| KiCad 格式事实（2026-10-21） | 逐引脚 `(hide yes)` 无效；`"~"` 显示字面；`""` 不显示；UTF-8 BOM 使库无法加载 |
| 封装元数据/改名（2026-10-22） | CH334F：补 `descr`/`tags`/`Datasheet`/`Description` + 改名，`fp upgrade --force` exit=0，元数据被保留 |
| v1.3 新能力（2026-10-22） | 料号补 `C` 前缀、`--manifest` 断点续跑、`--log`、3D 路径变量化、`pip install`/`pipx` 入口；CH334F 全流程实测通过 |
| v1.4 新能力（2026-10-22） | `--bom`（CSV/XLSX 全表扫描、过滤 C0402 等假料号）、`--weak-network`（翻倍+抖动）、`--update-mode`（清单文件哈希）；C5187527 实测：BOM 提取 1 个、更新模式跳过/删文件后重转均正确 |

---

## 11. 参考与文件索引

| 文件 | 内容 |
|---|---|
| `references/pitfalls.md` | 18 条实测踩坑 + 17 个料号附录（现象 → 原因 → 处理） |
| `references/easyeda-kicad-mapping.md` | 单位/图层/焊盘/3D 映射表、KiCad 格式实测事实、LCKiConverter 抄/不抄 |
| `references/sample-lcsc-ids.md` | 回归测试用 17 个料号（可直接当 `--ids-file`） |

外部参考：EasyEDA 文件格式文档、KiCad 库文件格式说明、easyeda2kicad、LCKiConverter（链接见映射表 §九）。
