#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
修复 easyeda2kicad 转出的 KiCad 符号：引脚名与引脚编号重叠。

两种数据源
----------
1) **逐引脚真值（推荐）**：`--fetch C2128 ...` 或 `--easyeda-json raw/*.json`
   嘉立创每个引脚自带 `name.is_displayed`；本脚本用 easyeda2kicad 的
   `EasyedaSymbolImporter` 解析原始数据，再按**引脚号**匹配 `.kicad_sym`。
2) **启发式（无原始数据时）**：`--mode hide|auto|show`，只看名字是不是纯数字。

KiCad 格式事实（2026-10-21 用 kicad-cli 10.0.6 实测）
----------------------------------------------------
* 符号级 `(pin_names ... hide)` 是唯一的官方隐藏开关；
* 引脚级的 `(hide yes)` 写在 name 的 effects 里，会被 `kicad-cli sym upgrade`
  **静默丢弃**（名字照常显示）；
* 名字写成 `"~"` 会**显示字面的 ~**；写成 `""` 才真正不显示。
=> 逐引脚隐藏只能"清空该引脚的 name 文本"，即 `--per-pin blank`（默认）。

用法
----
    # 逐引脚：按料号联网取原始数据（带缓存、限流退避）
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 C55019967

    # 逐引脚：离线用已保存的原始 JSON（API 响应、或其中的 result）
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --easyeda-json raw/C2128.json

    # 只看不改
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 --dry-run

    # 保守：保留全部名字文本，不用逐引脚清空
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --fetch C2128 --per-pin symbol

    # 无原始数据时（旧行为）
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --mode hide
    python fix_symbol_pin_names.py kicad/myproj.kicad_sym --show B1205S-3WR3

可重复运行：已清空的名字再清一次是 no-op；符号级开关会被改写为当前决定。
"""
from __future__ import annotations

import argparse
import json
import random
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

RE_TOP_SYMBOL = re.compile(r'^(\s*)\(symbol "([^"]+)"\s*$')
RE_UNIT = re.compile(r"_\d+_\d+$")
RE_PIN_NAMES = re.compile(r'^(\s*)\(pin_names\b')
RE_PIN_START = re.compile(r"^(\s*)\(pin\b")
RE_NAME = re.compile(r'\(name\s+"((?:[^"\\]|\\.)*)"')
RE_NUMBER = re.compile(r'\(number\s+"((?:[^"\\]|\\.)*)"')
RE_DATASHEET = re.compile(r'\(property\s+"Datasheet"\s+"((?:[^"\\]|\\.)*)"')
RE_LCSC = re.compile(r"(C\d{3,})")


def normalize_lcsc_id(raw: str) -> str:
    """`5187527` / `c5187527` / `C5187527` → `C5187527`；认不出来就原样返回。"""
    s = raw.strip().upper()
    if s.startswith("C"):
        s = s[1:]
    return f"C{s}" if re.fullmatch(r"\d{3,}", s) else raw.strip()


class _Tee:
    """把输出同时写到控制台和日志文件。"""

    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            try:
                s.write(data)
            except Exception:  # noqa: BLE001
                pass
        return len(data)

    def flush(self):
        for s in self.streams:
            try:
                s.flush()
            except Exception:  # noqa: BLE001
                pass


def setup_log(path: str | None) -> None:
    if not path:
        return
    import atexit

    logf = open(Path(path).expanduser(), "a", encoding="utf-8", newline="")
    sys.stdout = _Tee(sys.__stdout__, logf)  # type: ignore[assignment]
    atexit.register(logf.close)


# --------------------------------------------------------------------------
# S 表达式扫描
# --------------------------------------------------------------------------
def scan_block(lines: list[str], start: int) -> int:
    """返回从 lines[start] 开始的 s 表达式结束行号（括号配对，忽略字符串内的括号）。"""
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(lines)):
        for ch in lines[i]:
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            else:
                if ch == '"':
                    in_str = True
                elif ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                    if depth == 0:
                        return i
    return len(lines) - 1


def find_symbols(lines: list[str]) -> list[dict]:
    out: list[dict] = []
    i = 0
    while i < len(lines):
        m = RE_TOP_SYMBOL.match(lines[i])
        if not m or RE_UNIT.search(m.group(2)):
            i += 1
            continue
        end = scan_block(lines, i)
        block = "\n".join(lines[i:end + 1])
        pn_idx = None
        for j in range(i + 1, end):
            if RE_PIN_NAMES.match(lines[j]):
                pn_idx = j
                break
        mds = RE_DATASHEET.search(block)
        pins: list[dict] = []
        j = i + 1
        while j < end:
            if RE_PIN_START.match(lines[j]):
                pe = scan_block(lines, j)
                pin_block = "\n".join(lines[j:pe + 1])
                mn, mnum = RE_NAME.search(pin_block), RE_NUMBER.search(pin_block)
                if mn and mnum:
                    name_line = j + pin_block[:mn.start()].count("\n")
                    pins.append(dict(num=mnum.group(1), name=mn.group(1), name_line=name_line))
                j = pe + 1
            else:
                j += 1
        out.append(dict(name=m.group(2), head=i, end=end, indent=m.group(1),
                        pn_idx=pn_idx, datasheet=mds.group(1) if mds else "", pins=pins))
        i = end + 1
    return out


# --------------------------------------------------------------------------
# EasyEDA 原始数据 → 每个引脚的 name.is_displayed
# --------------------------------------------------------------------------
def _iter_cad(data, depth: int = 0):
    """从 API 响应 / result / CAD dict / 列表 / 按 uuid 索引的 dict 中取出 CAD dict。"""
    if depth > 4:
        return
    if isinstance(data, list):
        for d in data:
            yield from _iter_cad(d, depth + 1)
        return
    if not isinstance(data, dict):
        return
    if isinstance(data.get("result"), dict):
        yield from _iter_cad(data["result"], depth + 1)
        return
    if isinstance(data.get("dataStr"), dict):
        yield data
        return
    for v in data.values():
        if isinstance(v, dict) and (isinstance(v.get("dataStr"), dict)
                                   or isinstance(v.get("result"), dict)):
            yield from _iter_cad(v, depth + 1)


def component_from_cad(cad: dict, importer_cls, source: str) -> dict:
    head = (cad.get("dataStr") or {}).get("head") or {}
    c_para = head.get("c_para") or {}
    try:
        sym = importer_cls(cad).get_symbol()
    except Exception as e:  # noqa: BLE001
        return dict(name=str(c_para.get("name") or "?"), lcsc="", pins=[],
                    source=source, error=f"{type(e).__name__}: {e}")
    pins: list[tuple[str, bool, str]] = []

    def collect(s) -> None:
        for pin in getattr(s, "pins", []) or []:
            num = str(getattr(pin.settings, "spice_pin_number", "") or "").strip()
            if not num:
                continue
            pins.append((num, bool(getattr(pin.name, "is_displayed", True)),
                         str(getattr(pin.name, "text", "") or "")))

    collect(sym)
    for sub in getattr(sym, "sub_symbols", []) or []:
        collect(sub)
    info = getattr(sym, "info", None)
    return dict(name=str(getattr(info, "name", "") or c_para.get("name") or "?"),
                lcsc=str(getattr(info, "lcsc_id", "") or ""),
                pins=pins, source=source, error=None)


def load_components_from_json(paths: list[str]) -> tuple[list[dict], list[str]]:
    try:
        from easyeda2kicad.easyeda.easyeda_importer import EasyedaSymbolImporter
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"[FAIL] 需要 easyeda2kicad：{e}") from e
    files: list[Path] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            files += sorted(p.glob("*.json"))
        elif p.is_file():
            files.append(p)
        else:
            print(f"[WARN] 路径不存在，跳过：{p}")
    comps: list[dict] = []
    errs: list[str] = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as e:
            errs.append(f"{f.name}: {e}")
            continue
        n = 0
        for cad in _iter_cad(data):
            comps.append(component_from_cad(cad, EasyedaSymbolImporter, source=f.name))
            n += 1
        if n == 0:
            errs.append(f"{f.name}: 没找到 dataStr 结构（不是 easyeda2kicad 认的原始数据？）")
    return comps, errs


def fetch_components(ids: list[str], delay: float, retries: int, backoff: float) -> list[dict]:
    try:
        from easyeda2kicad.easyeda.easyeda_api import EasyedaApi
        from easyeda2kicad.easyeda.easyeda_importer import EasyedaSymbolImporter
    except ImportError as e:  # pragma: no cover
        raise SystemExit(f"[FAIL] 需要 easyeda2kicad：{e}") from e
    api = EasyedaApi(use_cache=True)  # 缓存目录：CWD/.easyeda_cache
    ids = [normalize_lcsc_id(x) for x in ids]
    comps: list[dict] = []
    for idx, cid in enumerate(ids):
        cad: dict = {}
        for attempt in range(1, retries + 1):
            cad = api.get_cad_data_of_component(cid) or {}
            if cad:
                break
            print(f"[WAIT] {cid} 第 {attempt}/{retries} 次失败（403/网络），{backoff:.0f}s 后重试")
            if attempt < retries:
                time.sleep(backoff * random.uniform(0.8, 1.2))
        if not cad:
            comps.append(dict(name=cid, lcsc=cid, pins=[], source=f"fetch:{cid}",
                              error="拉取失败（限流或元件不存在）"))
        else:
            comps.append(component_from_cad(cad, EasyedaSymbolImporter, source=f"fetch:{cid}"))
        if idx < len(ids) - 1:
            time.sleep(delay * random.uniform(0.8, 1.2))
    return comps


# --------------------------------------------------------------------------
# 匹配 + 决策
# --------------------------------------------------------------------------
def norm_name(s: str) -> str:
    return re.sub(r"[^0-9a-z]", "", s.lower())


def pin_sig(nums) -> tuple:
    return tuple(sorted({str(n).strip() for n in nums if str(n).strip()}))


def match_components(symbols: list[dict], comps: list[dict]) -> tuple[dict, list[dict]]:
    """返回 ({符号名: 元件}, 未用上的元件)。匹配顺序：料号 → 名字 → 引脚号指纹。"""
    used: set[int] = set()
    mapping: dict[str, dict] = {}

    # 1) Datasheet 里的 C 料号
    for s in symbols:
        m = RE_LCSC.search(s["datasheet"])
        if not m:
            continue
        for k, c in enumerate(comps):
            if k not in used and c.get("lcsc") and c["lcsc"].upper() == m.group(1).upper():
                mapping[s["name"]] = c
                used.add(k)
                break

    # 2) 名字（忽略大小写/非字母数字）
    for s in symbols:
        if s["name"] in mapping:
            continue
        for k, c in enumerate(comps):
            if k not in used and norm_name(c.get("name", "")) == norm_name(s["name"]):
                mapping[s["name"]] = c
                used.add(k)
                break

    # 3) 引脚号指纹唯一匹配
    sym_sigs: dict[tuple, list[dict]] = {}
    for s in symbols:
        if s["name"] not in mapping:
            sym_sigs.setdefault(pin_sig(p["num"] for p in s["pins"]), []).append(s)
    comp_sigs: dict[tuple, list[int]] = {}
    for k, c in enumerate(comps):
        if k not in used:
            comp_sigs.setdefault(pin_sig(p[0] for p in c["pins"]), []).append(k)
    for s_sig, syms in sym_sigs.items():
        cands = comp_sigs.get(s_sig) or []
        if len(syms) == 1 and len(cands) == 1 and len(s_sig) > 1:
            mapping[syms[0]["name"]] = comps[cands[0]]
            used.add(cands[0])

    unmatched = [c for k, c in enumerate(comps) if k not in used]
    return mapping, unmatched


def decide(sym: dict, comp: dict, args) -> dict:
    """返回 dict(visible, blank=[引脚号], why, matched)。"""
    truth = {str(num).strip(): (disp, text) for num, disp, text in comp.get("pins", [])}
    matched = [(p, truth[p["num"]]) for p in sym["pins"] if p["num"] in truth]
    hidden = [(p, t) for p, t in matched if not t[0]]
    shown = [(p, t) for p, t in matched if t[0]]

    if sym["name"] in args.hide:
        return dict(visible=False, blank=[], why="用户 --hide", matched=len(matched))
    if sym["name"] in args.show:
        return dict(visible=True, blank=[], why="用户 --show", matched=len(matched))
    if matched:
        if not shown:  # 嘉立创全部隐藏 → 符号级隐藏，保留名字文本
            return dict(visible=False, blank=[], why="EasyEDA 全隐藏", matched=len(matched))
        if not hidden:
            return dict(visible=True, blank=[], why="EasyEDA 全显示", matched=len(matched))
        if args.per_pin == "blank":  # 混合 → 逐引脚清空（视觉与嘉立创一致）
            return dict(visible=True, blank=[p["num"] for p, _ in hidden],
                        why=f"混合：清空 {len(hidden)} 个引脚名", matched=len(matched))
        return dict(visible=True, blank=[],
                    why=f"混合：保留全部名字（{len(hidden)} 个本应隐藏）", matched=len(matched))

    # 无真值 → 旧启发式
    meaningful = any(not p["name"].isdigit() for p in sym["pins"])
    if args.mode == "show":
        return dict(visible=True, blank=[], why="启发式 show", matched=0)
    if args.mode == "auto":
        return dict(visible=meaningful, blank=[], why="启发式 auto", matched=0)
    return dict(visible=False, blank=[], why="启发式 hide", matched=0)


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(
        description="修复引脚名/编号重叠：优先用嘉立创逐引脚 name.is_displayed 真值")
    ap.add_argument("lib", help=".kicad_sym 路径")

    src = ap.add_argument_group("逐引脚真值数据源（可只给一个；都没有则退回 --mode 启发式）")
    src.add_argument("--easyeda-json", nargs="+", metavar="PATH",
                     help="原始 EasyEDA JSON（API 响应、或其中的 result；可给目录）")
    src.add_argument("--fetch", nargs="+", metavar="LCSC_ID",
                     help="按料号联网取原始数据（easyeda2kicad 客户端，带缓存）")
    src.add_argument("--delay", type=float, default=6.0, help="--fetch 时的料号间隔秒（默认 6）")
    src.add_argument("--retries", type=int, default=3, help="--fetch 单个料号重试次数（默认 3）")
    src.add_argument("--backoff", type=float, default=30.0, help="--fetch 重试等待秒（默认 30）")

    ap.add_argument("--per-pin", choices=["blank", "symbol"], default="blank",
                    help="blank=混合时清空隐藏引脚的名字（默认，视觉对齐嘉立创）；"
                         "symbol=只用符号级开关、保留全部名字文本")
    ap.add_argument("--mode", choices=["auto", "hide", "show"], default="hide",
                    help="没有真值时的启发式：auto=纯数字名隐藏/字母显示；hide=全隐藏（默认）；show=全显示")
    ap.add_argument("--show", nargs="*", default=[], metavar="SYMBOL",
                    help="强制显示引脚名的符号（优先级最高）")
    ap.add_argument("--hide", nargs="*", default=[], metavar="SYMBOL",
                    help="强制隐藏引脚名的符号（优先级最高）")
    ap.add_argument("--offset", type=float, default=0.762, help="显示时的偏移 mm（默认 0.762）")
    ap.add_argument("--numeric-offset", type=float, default=1.016, help="隐藏时的偏移 mm（默认 1.016）")
    ap.add_argument("--report", metavar="JSON", help="把每个符号的决定写成 JSON 报告")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-backup", action="store_true")
    ap.add_argument("--log", default=None, help="把输出追加写入日志文件")
    args = ap.parse_args()
    setup_log(args.log)

    p = Path(args.lib)
    if not p.is_file():
        print(f"[FAIL] 文件不存在：{p}")
        return 2
    blob = p.read_bytes()
    had_bom = blob.startswith(b"\xef\xbb\xbf")
    raw = blob.decode("utf-8-sig")
    if had_bom:
        print("[WARN] 输入带 UTF-8 BOM：KiCad 会拒绝加载该库（实测 kicad-cli 直接报「无法加载库」），已去掉")
    crlf = "\r\n" in raw
    lines = raw.replace("\r\n", "\n").split("\n")
    syms = find_symbols(lines)
    if not syms:
        print("[FAIL] 没找到顶层符号，格式对吗？")
        return 2

    # ---------- 取真值 ----------
    comps: list[dict] = []
    notes: list[str] = []
    if args.easyeda_json:
        loaded, errs = load_components_from_json(args.easyeda_json)
        comps += loaded
        notes += errs
    if args.fetch:
        comps += fetch_components(args.fetch, args.delay, args.retries, args.backoff)
    for c in comps:
        if c.get("error"):
            notes.append(f"{c['name']}（{c['source']}）：{c['error']}")
    comps = [c for c in comps if c.get("pins")]
    if not comps:
        if args.easyeda_json or args.fetch:
            print("[WARN] 没有拿到可用引脚真值，退回启发式模式")

    mapping, unmatched = match_components(syms, comps) if comps else ({}, [])
    for c in unmatched:
        notes.append(f"原始数据未匹配到符号：{c['name']}（{c.get('lcsc') or '无料号'}，来自 {c['source']}）")
    for s in syms:
        if comps and s["name"] not in mapping:
            notes.append(f"符号没有原始数据，退回启发式：{s['name']}")

    # ---------- 规划 ----------
    print(f"库文件  : {p}")
    print(f"顶层符号: {len(syms)} 个   原始数据: {len(comps)} 个   逐引脚策略: {args.per_pin}")
    print(f"{'符号':<26}{'引脚':>4}{'命中':>4}  {'决定':<6} 说明")
    print("-" * 96)
    plan: list[dict] = []
    for s in syms:
        comp = mapping.get(s["name"]) or dict(pins=[], name="", lcsc="", source="", error=None)
        d = decide(s, comp, args)
        line_of = {pin["num"]: pin["name_line"] for pin in s["pins"]}
        blanks = [line_of[num] for num in d["blank"] if num in line_of]
        plan.append(dict(sym=s["name"], head=s["head"], indent=s["indent"],
                         pn_idx=s["pn_idx"], d=d, blanks=blanks))
        print(f"{s['name']:<26}{len(s['pins']):>4}{d['matched']:>4}  "
              f"{'显示' if d['visible'] else '隐藏':<6} {d['why']}")
        if d["blank"]:
            more = " ..." if len(d["blank"]) > 8 else ""
            print(f"{'':<26}{'':>4}{'':>4}  {'':<6} 清空: {', '.join(d['blank'][:8])}{more}")

    for n in notes:
        print(f"[注] {n}")

    # ---------- 执行（从后往前，行号不漂移） ----------
    changed = 0
    for item in sorted(plan, key=lambda x: x["head"], reverse=True):
        d, head, indent, pn_idx = item["d"], item["head"], item["indent"], item["pn_idx"]
        # 先改引脚名（都在插入点之后，行号不受插入影响）
        for nl in sorted(item["blanks"], reverse=True):
            m = RE_NAME.search(lines[nl])
            if not m or m.group(1) == "":
                continue
            if not args.dry_run:
                lines[nl] = RE_NAME.sub('(name ""', lines[nl], count=1)
            changed += 1
        # 再改/插入符号级开关
        new_line = (f"{indent}\t(pin_names (offset {args.offset}))" if d["visible"]
                    else f"{indent}\t(pin_names (offset {args.numeric_offset}) hide)")
        if pn_idx is not None:
            if lines[pn_idx].rstrip() != new_line.rstrip():
                if not args.dry_run:
                    lines[pn_idx] = new_line
                changed += 1
        else:
            if not args.dry_run:
                lines.insert(head + 1, new_line)
            changed += 1

    if args.report:
        report = dict(library=str(p), generated=datetime.now().isoformat(timespec="seconds"),
                      per_pin=args.per_pin,
                      symbols=[dict(symbol=it["sym"],
                                    decision="show" if it["d"]["visible"] else "hide",
                                    reason=it["d"]["why"],
                                    matched_pins=it["d"]["matched"],
                                    blanked=it["d"]["blank"]) for it in plan])
        Path(args.report).write_text(json.dumps(report, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        print(f"\n报告已写: {args.report}")

    if args.dry_run:
        print(f"\n[DRY RUN] 将修改 {changed} 处（符号级开关 + 引脚名），未写文件。")
        return 0
    if changed == 0:
        print("\n无需修改（已符合当前规则）。")
        return 0

    if not args.no_backup:
        bk = p.with_name(f"{p.name}.bak_{datetime.now():%Y%m%d_%H%M%S}")
        shutil.copy2(p, bk)
        print(f"\n已备份: {bk.name}")

    out = "\n".join(lines)
    if crlf:
        out = out.replace("\n", "\r\n")
    p.write_text(out, encoding="utf-8", newline="")

    # ---------- 读回校验 ----------
    back = p.read_text(encoding="utf-8")
    n_pn = len(re.findall(r"\(pin_names\s", back))
    n_hide = len(re.findall(r"\(pin_names[^\n]*hide", back))
    n_empty = len(re.findall(r'\(name ""', back))
    ok_brace = back.count("(") == back.count(")")
    print(f"已写入: 改 {changed} 处；pin_names={n_pn}（hide={n_hide}）；空名字={n_empty}；"
          f"括号平衡={'OK' if ok_brace else 'FAIL'}")
    return 0 if ok_brace else 1


def cli() -> None:
    """pip 安装后的控制台入口（lcsc2kicad-pins）。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
