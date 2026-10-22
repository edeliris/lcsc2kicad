#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 步：按立创料号（C 开头）下载并转换成 KiCad 库（符号 + 封装 + 3D）。

用法
----
  # 单个 / 几个（料号可省略 C 前缀：5187527 会自动补成 C5187527）
  python build_kicad_lib.py --lcsc-id C2040 --output ./kicad/myproj
  python build_kicad_lib.py --lcsc-id C17513 C17414 C17514 --output ./kicad/myproj

  # 从文件读料号（每行一个料号，# 开头为注释；行尾可用空格/# 写备注）
  python build_kicad_lib.py --ids-file parts.txt --output ./kicad/myproj

  # 直接从 BOM 读（CSV / TXT / XLSX；扫描全部单元格，自动去重、过滤 C0402 之类假料号）
  python build_kicad_lib.py --bom bom.xlsx --output ./kicad/myproj

  # 工程清单：记录每个料号的备注与结果，支持断点续跑
  python build_kicad_lib.py --manifest parts.json --output ./kicad/myproj
  python build_kicad_lib.py --manifest parts.json --output ./kicad/myproj --retry-all

  # 弱网模式：间隔/退避翻倍、重试 +2、等待时间 ±20% 抖动
  python build_kicad_lib.py --ids-file parts.txt --output ./kicad/myproj --weak-network

  # 更新模式：按清单里的文件哈希校验，只补缺失/变化的产物
  python build_kicad_lib.py --manifest parts.json --output ./kicad/myproj --update-mode

为什么需要"间隔 + 退避重试"
--------------------------
立创/EasyEDA 的 API 有频率限制（实测：连续 17 个料号，前约 13 个成功后
全部返回 `HTTP Error 403: Forbidden`，且换料号/立即重试无效）。
默认 --delay 6 --retries 3 --backoff 30 可稳定跑完；仍 403 就等 10~30 分钟。

产物（--output 是"库基名"，不是目录）
------------------------------------
  <基名>.kicad_sym      符号库（KiCad v6+）
  <基名>.pretty/        封装库（*.kicad_mod）
  <基名>.3dshapes/      3D 模型（*.step + *.wrl）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

# ★ 中文 Windows 控制台默认 GBK，打印 ✓/✗ 等字符会抛
#   UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
#   强制 UTF-8 输出，避免脚本在"第一件已成功"之后崩掉（曾导致误判）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

# 常见封装代号不是 LCSC 料号（C0402/C0603/... 会被 BOM 全文扫描误抓）
PACKAGE_FALSE_POSITIVES = {
    "C0201", "C0402", "C0603", "C0805", "C1206", "C1210",
    "C1812", "C2010", "C2512", "C3225", "C4532",
}
RE_LCSC_TEXT = re.compile(r"\bC\d{4,}\b", re.IGNORECASE)


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
def normalize_lcsc_id(raw: str) -> str:
    """`5187527` / `c5187527` / `C5187527` → `C5187527`；认不出来就原样返回。"""
    s = raw.strip().upper()
    if s.startswith("C"):
        s = s[1:]
    return f"C{s}" if re.fullmatch(r"\d{3,}", s) else raw.strip()


def _sleep(seconds: float, jitter: float = 0.0) -> None:
    """等待 seconds 秒；jitter>0 时加 ±jitter 比例抖动，避免同步重试。"""
    if seconds <= 0:
        return
    if jitter:
        seconds *= random.uniform(1.0 - jitter, 1.0 + jitter)
    time.sleep(seconds)


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


def setup_log(path: str | None):
    if not path:
        return None
    logf = open(Path(path).expanduser(), "a", encoding="utf-8", newline="")
    sys.stdout = _Tee(sys.__stdout__, logf)  # type: ignore[arg-type]
    return logf


def check_tool() -> bool:
    """检查 easyeda2kicad 是否可调用（注意：它常装在不在 PATH 的用户目录）。"""
    r = subprocess.run(
        [sys.executable, "-m", "easyeda2kicad", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


# --------------------------------------------------------------------------
# BOM 解析（CSV / TXT / XLSX）
# --------------------------------------------------------------------------
def _read_text_any(path: Path) -> str:
    """优先 UTF-8（含 BOM），失败退回 GBK（Excel 导出的中文 CSV 常见）。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "gbk"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _extract_lcsc_ids(text: str) -> list[str]:
    ids: list[str] = []
    for m in RE_LCSC_TEXT.findall(text):
        cid = normalize_lcsc_id(m)
        if cid in PACKAGE_FALSE_POSITIVES:
            continue
        ids.append(cid)
    return ids


def parse_bom_file(path: Path) -> list[str]:
    """从 BOM 文件提取 LCSC 料号（去重保序）。支持 .csv/.txt/.tsv 与 .xlsx。"""
    suffix = path.suffix.lower()
    ids: list[str] = []
    if suffix in {".csv", ".txt", ".tsv"}:
        ids = _extract_lcsc_ids(_read_text_any(path))
    elif suffix in {".xlsx", ".xlsm"}:
        try:
            from openpyxl import load_workbook  # 可选依赖
        except ImportError as e:  # pragma: no cover
            raise SystemExit("[FAIL] 解析 .xlsx 需要 openpyxl：pip install openpyxl") from e
        wb = load_workbook(filename=str(path), read_only=True, data_only=True)
        try:
            for ws in wb.worksheets:
                for row in ws.iter_rows(values_only=True):
                    for value in row:
                        if isinstance(value, str):
                            ids.extend(_extract_lcsc_ids(value))
        finally:
            wb.close()
    elif suffix == ".xls":
        raise SystemExit("[FAIL] 不支持旧版 .xls：请在 Excel 里另存为 .xlsx 或 .csv")
    else:
        # 未知扩展名：先按文本试，失败再按 xlsx
        try:
            ids = _extract_lcsc_ids(_read_text_any(path))
        except Exception:  # noqa: BLE001
            ids = []

    seen: set[str] = set()
    return [x for x in ids if not (x in seen or seen.add(x))]


# --------------------------------------------------------------------------
# 工程清单（manifest）
# --------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, dict]:
    """返回 {lcsc_id: {lcsc, comment, status, updated, files}}；坏文件给空表。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        print(f"[WARN] manifest 读取失败（忽略旧清单）：{e}", flush=True)
        return {}
    out: dict[str, dict] = {}
    for part in data.get("parts", []) if isinstance(data, dict) else []:
        cid = normalize_lcsc_id(str(part.get("lcsc", "")))
        if cid:
            files = part.get("files")
            out[cid] = dict(lcsc=cid,
                            comment=str(part.get("comment", "") or ""),
                            status=str(part.get("status", "") or ""),
                            updated=str(part.get("updated", "") or ""),
                            files=dict(files) if isinstance(files, dict) else {})
    return out


def save_manifest(path: Path, entries: dict[str, dict], library: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    parts = [entries[k] for k in sorted(entries)]
    data = dict(schema=1,
                library=str(library),
                updated=datetime.now().isoformat(timespec="seconds"),
                parts=parts)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# --------------------------------------------------------------------------
# 产物快照 / 文件级更新
# --------------------------------------------------------------------------
def _lib_files(out: Path) -> dict[str, tuple[int, int]]:
    """记录 <基名>.pretty 与 <基名>.3dshapes 下每个文件的 (大小, mtime_ns)。"""
    snap: dict[str, tuple[int, int]] = {}
    base = out.parent
    for root in (Path(str(out) + ".pretty"), Path(str(out) + ".3dshapes")):
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.is_file():
                st = p.stat()
                rel = str(p.relative_to(base)).replace("\\", "/")
                snap[rel] = (st.st_size, st.st_mtime_ns)
    return snap


def _changed_files(before: dict[str, tuple[int, int]],
                   after: dict[str, tuple[int, int]]) -> list[str]:
    return sorted(rel for rel, meta in after.items() if before.get(rel) != meta)


def _hash_files(base: Path, rels: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for rel in rels:
        p = base / rel
        if p.is_file():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def _files_intact(base: Path, files: dict[str, str]) -> bool:
    """清单里记录的文件都还在、且哈希未变。"""
    if not files:
        return False
    for rel, digest in files.items():
        p = base / rel
        if not p.is_file():
            return False
        if hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            return False
    return True


# --------------------------------------------------------------------------
# 料号输入
# --------------------------------------------------------------------------
def read_ids_file(path: Path) -> list[tuple[str, str]]:
    """返回 [(lcsc_id, comment)]；支持行内空格/逗号/制表符后跟备注。"""
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        comment = ""
        for sep in ("#", "\t", ",", ";"):
            if sep in s:
                s, comment = s.split(sep, 1)
                comment = comment.strip()
                break
        parts = s.split()
        if not parts:
            continue
        cid = normalize_lcsc_id(parts[0])
        if not comment and len(parts) > 1:
            comment = " ".join(parts[1:])
        rows.append((cid, comment))
    return rows


# --------------------------------------------------------------------------
# 转换
# --------------------------------------------------------------------------
def convert_one(
    lcsc_id: str, out: Path, retries: int, backoff_s: float, jitter: float,
    full: bool, use_symbol: bool, use_footprint: bool, use_3d: bool,
    overwrite: bool,
) -> bool:
    cmd = [sys.executable, "-m", "easyeda2kicad"]
    if full:
        cmd.append("--full")
    else:
        if use_symbol:
            cmd.append("--symbol")
        if use_footprint:
            cmd.append("--footprint")
        if use_3d:
            cmd.append("--3d")
    if overwrite:
        cmd.append("--overwrite")
    cmd += ["--lcsc_id", lcsc_id, "--output", str(out)]

    for attempt in range(1, retries + 1):
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        blob = (r.stdout or "") + (r.stderr or "")
        ok = r.returncode == 0 and "Created Kicad symbol" in blob and "403" not in blob
        if ok:
            return True
        reason = "403 限流" if "403" in blob else f"exit={r.returncode}"
        print(f"      第 {attempt}/{retries} 次失败（{reason}）", flush=True)
        if attempt < retries:
            _sleep(backoff_s, jitter)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="立创料号 → KiCad 库（符号/封装/3D），带限流退避、BOM 导入与工程清单")
    ap.add_argument("--lcsc-id", nargs="+", default=None,
                    help="立创料号，如 C2040 或 2040 C17513（C 前缀可省略）")
    ap.add_argument("--ids-file", default=None,
                    help="料号文件（每行一个；# 开头为注释；行尾可写备注）")
    ap.add_argument("--bom", nargs="+", default=None, metavar="FILE",
                    help="BOM 文件（CSV/TXT/XLSX，可给多个）：扫描全部单元格提取料号")
    ap.add_argument("--manifest", default=None,
                    help="工程清单 JSON：记录备注/结果；存在时自动跳过已成功的料号（断点续跑）")
    ap.add_argument("--retry-all", action="store_true",
                    help="忽略 manifest 里的成功记录，全部重转（换了转换器版本时用）")
    ap.add_argument("--update-mode", action="store_true",
                    help="更新模式：按清单记录的文件哈希校验，只重转缺失/变化的产物（需要 --manifest）")
    ap.add_argument("--output", required=True, help="库基名（不带扩展名），如 ./kicad/myproj")
    ap.add_argument("--delay", type=float, default=6.0, help="料号间隔秒（默认 6，限流时调到 10）")
    ap.add_argument("--retries", type=int, default=3, help="单个失败重试次数（默认 3）")
    ap.add_argument("--backoff", type=float, default=30.0, help="重试等待秒（默认 30）")
    ap.add_argument("--weak-network", action="store_true",
                    help="弱网模式：间隔/退避翻倍、重试 +2、等待加 ±20%% 抖动")
    ap.add_argument("--symbol", action="store_true", help="只要符号（与 --full 互斥）")
    ap.add_argument("--footprint", action="store_true", help="只要封装")
    ap.add_argument("--3d", dest="use_3d", action="store_true", help="只要 3D 模型")
    ap.add_argument("--no-overwrite", action="store_true",
                    help="已存在的封装不覆盖（默认覆盖，避免同封装冲突报错）")
    ap.add_argument("--log", default=None, help="把输出追加写入日志文件")
    args = ap.parse_args()

    logf = setup_log(args.log)
    try:
        return _run(args)
    finally:
        if logf:
            logf.close()
            sys.stdout = sys.__stdout__  # type: ignore[assignment]


def _run(args) -> int:
    if not check_tool():
        print("[FAIL] easyeda2kicad 不可用。请先： pip install easyeda2kicad", flush=True)
        return 2
    if args.update_mode and not args.manifest:
        print("[FAIL] --update-mode 需要配合 --manifest 使用（要记录文件哈希）。", flush=True)
        return 2

    # ---------- 收集料号（四个来源合并；manifest 提供备注与历史状态） ----------
    entries: dict[str, dict] = {}
    if args.manifest:
        for cid, row in load_manifest(Path(args.manifest)).items():
            entries[cid] = dict(row)
    for raw in args.lcsc_id or []:
        if not raw.strip():
            continue
        cid = normalize_lcsc_id(raw)
        entries.setdefault(cid, dict(lcsc=cid, comment="", status="", updated="", files={}))
    if args.ids_file:
        for cid, comment in read_ids_file(Path(args.ids_file)):
            row = entries.setdefault(cid, dict(lcsc=cid, comment="", status="", updated="", files={}))
            if comment and not row.get("comment"):
                row["comment"] = comment
    if args.bom:
        for bom in args.bom:
            p = Path(bom).expanduser()
            if not p.is_file():
                print(f"[FAIL] BOM 不存在：{p}", flush=True)
                return 2
            found = parse_bom_file(p)
            print(f"BOM {p.name}: 提取 {len(found)} 个料号", flush=True)
            for cid in found:
                entries.setdefault(cid, dict(lcsc=cid, comment="", status="", updated="", files={}))

    if not entries:
        print("[FAIL] 没有料号。用 --lcsc-id / --ids-file / --bom / --manifest 指定。", flush=True)
        return 2

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    sym_file = out.with_suffix(".kicad_sym")
    lib_exists = sym_file.exists()
    base = out.parent

    delay = args.delay
    retries = args.retries
    backoff = args.backoff
    jitter = 0.0
    if args.weak_network:
        delay *= 2
        retries += 2
        backoff *= 2
        jitter = 0.2

    # 跳过已成功且产物齐全的料号（断点续跑 / 更新模式）
    todo: list[str] = []
    skipped: list[str] = []
    for cid in sorted(entries):
        row = entries[cid]
        up_to_date = row.get("status") == "ok" and lib_exists
        if up_to_date and args.update_mode:
            up_to_date = _files_intact(base, row.get("files") or {})
        if (not args.retry_all) and up_to_date:
            skipped.append(cid)
        else:
            todo.append(cid)

    any_specific = args.symbol or args.footprint or args.use_3d
    full = not any_specific
    use_symbol = args.symbol or full
    use_footprint = args.footprint or full
    use_3d = args.use_3d or full

    print(f"输出库基名 : {out}")
    print(f"料号数量   : {len(entries)}（待转 {len(todo)}，跳过已成功 {len(skipped)}）")
    print(f"转换内容   : full={full} symbol={use_symbol} footprint={use_footprint} 3d={use_3d}")
    mode = "更新模式（文件哈希）" if args.update_mode else "断点续跑（按料号状态）"
    print(f"跳过策略   : {mode}")
    print(f"限流策略   : 间隔 {delay:.1f}s，失败重试 {retries} 次（退避 {backoff:.1f}s"
          + ("，弱网抖动 ±20%）" if args.weak_network else "）"))
    if skipped:
        print("跳过料号   : " + " ".join(skipped))
    print("-" * 66, flush=True)

    ok_new, fail_new = [], []
    t0 = time.time()
    for i, cid in enumerate(todo, 1):
        comment = entries[cid].get("comment", "")
        suffix = f"  # {comment}" if comment else ""
        print(f"[{i}/{len(todo)}] {cid}{suffix}", flush=True)
        snap_before = _lib_files(out) if args.manifest else {}
        good = convert_one(
            cid, out, retries, backoff, jitter,
            full, use_symbol, use_footprint, use_3d,
            overwrite=not args.no_overwrite,
        )
        entries[cid]["status"] = "ok" if good else "fail"
        entries[cid]["updated"] = datetime.now().isoformat(timespec="seconds")
        if good:
            ok_new.append(cid)
            if args.manifest:
                changed = _changed_files(snap_before, _lib_files(out))
                if changed:
                    entries[cid]["files"] = _hash_files(base, changed)
                print(f"      [OK]（{len(entries[cid].get('files') or {})} 个产物文件）", flush=True)
            else:
                print("      [OK]", flush=True)
        else:
            fail_new.append(cid)
            print("      [FAIL]（可用 --lcsc-id 补跑）", flush=True)
        if args.manifest:
            save_manifest(Path(args.manifest), entries, out)
        if i < len(todo):
            _sleep(delay, jitter)

    elapsed = time.time() - t0
    print("=" * 66)
    print(f"成功 {len(ok_new)} / 失败 {len(fail_new)}，跳过 {len(skipped)}，耗时 {elapsed:.0f}s")
    failed_all = [cid for cid in entries if entries[cid].get("status") == "fail"]
    if fail_new:
        print("本次失败料号：" + " ".join(fail_new))
        print("补跑：python build_kicad_lib.py --lcsc-id " + " ".join(fail_new)
              + f" --output {out}")
    if args.manifest:
        save_manifest(Path(args.manifest), entries, out)
        print(f"工程清单 : {Path(args.manifest).resolve()}")

    pretty = Path(str(out) + ".pretty")
    d3 = Path(str(out) + ".3dshapes")
    if sym_file.exists():
        print(f"符号库 : {sym_file}  ({sym_file.stat().st_size/1024:.1f} KB)")
    if pretty.exists():
        print(f"封装库 : {pretty}  ({len(list(pretty.glob('*.kicad_mod')))} 个 .kicad_mod)")
    if d3.exists():
        print(f"3D 模型: {d3}  ({len(list(d3.iterdir()))} 个文件)")
    return 0 if not failed_all and not fail_new else 1


def cli() -> None:
    """pip 安装后的控制台入口（lcsc2kicad-build）。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
