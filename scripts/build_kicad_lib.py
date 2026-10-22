#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 步：按立创料号（C 开头）下载并转换成 KiCad 库（符号 + 封装 + 3D）。

用法
----
  # 单个 / 几个（料号可省略 C 前缀：5187527 会自动补成 C5187527）
  python build_kicad_lib.py --lcsc-id C2040 --output ./kicad/myproj
  python build_kicad_lib.py --lcsc-id C17513 C17414 C17514 --output ./kicad/myproj

  # 从文件读（每行一个料号，# 开头为注释；行尾可用空格/# 写备注）
  python build_kicad_lib.py --ids-file parts.txt --output ./kicad/myproj

  # 工程清单：记录每个料号的备注与结果，支持断点续跑
  python build_kicad_lib.py --manifest parts.json --output ./kicad/myproj
  python build_kicad_lib.py --manifest parts.json --output ./kicad/myproj --retry-all

  # 只转换符号+封装（省流量，不要 3D）
  python build_kicad_lib.py --lcsc-id C2040 --symbol --footprint --output ./kicad/myproj

  # 只补失败的那几个
  python build_kicad_lib.py --lcsc-id C2128 C434967 --output ./kicad/myproj

  # 日志落盘（追加写）
  python build_kicad_lib.py --ids-file parts.txt --output ./kicad/myproj --log build.log

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
import json
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

RE_LCSC = re.compile(r"^C?(\d{3,})$", re.IGNORECASE)


# --------------------------------------------------------------------------
# 通用小工具
# --------------------------------------------------------------------------
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
# 工程清单（manifest）
# --------------------------------------------------------------------------
def load_manifest(path: Path) -> dict[str, dict]:
    """返回 {lcsc_id: {lcsc, comment, status, updated}}；坏文件给空表。"""
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
            out[cid] = dict(lcsc=cid,
                            comment=str(part.get("comment", "") or ""),
                            status=str(part.get("status", "") or ""),
                            updated=str(part.get("updated", "") or ""))
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
# 料号输入
# --------------------------------------------------------------------------
def read_ids_file(path: Path) -> list[tuple[str, str]]:
    """返回 [(lcsc_id, comment)]；支持行内空格/逗号/制表符后跟备注。"""
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        comment = ""
        for sep in ("#", "\t", ",", ";"):
            if sep in s:
                s, comment = s.split(sep, 1)
                comment = comment.strip()
                break
        # 空格分隔：第一部分是料号，其余是备注
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
    lcsc_id: str, out: Path, retries: int, backoff_s: float,
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
            time.sleep(backoff_s)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(
        description="立创料号 → KiCad 库（符号/封装/3D），带限流退避重试与工程清单")
    ap.add_argument("--lcsc-id", nargs="+", default=None,
                    help="立创料号，如 C2040 或 2040 C17513（C 前缀可省略）")
    ap.add_argument("--ids-file", default=None,
                    help="料号文件（每行一个；# 开头为注释；行尾可写备注）")
    ap.add_argument("--manifest", default=None,
                    help="工程清单 JSON：记录备注/结果；存在时自动跳过已成功的料号（断点续跑）")
    ap.add_argument("--retry-all", action="store_true",
                    help="忽略 manifest 里的成功记录，全部重转（换了转换器版本时用）")
    ap.add_argument("--output", required=True, help="库基名（不带扩展名），如 ./kicad/myproj")
    ap.add_argument("--delay", type=float, default=6.0, help="料号间隔秒（默认 6，限流时调到 10）")
    ap.add_argument("--retries", type=int, default=3, help="单个失败重试次数（默认 3）")
    ap.add_argument("--backoff", type=float, default=30.0, help="重试等待秒（默认 30）")
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

    # ---------- 收集料号（三个来源合并；manifest 提供备注与历史状态） ----------
    entries: dict[str, dict] = {}
    if args.manifest:
        for cid, row in load_manifest(Path(args.manifest)).items():
            entries[cid] = dict(row)
    for raw in args.lcsc_id or []:
        if not raw.strip():
            continue
        cid = normalize_lcsc_id(raw)
        entries.setdefault(cid, dict(lcsc=cid, comment="", status="", updated=""))
    if args.ids_file:
        for cid, comment in read_ids_file(Path(args.ids_file)):
            row = entries.setdefault(cid, dict(lcsc=cid, comment="", status="", updated=""))
            if comment and not row.get("comment"):
                row["comment"] = comment

    if not entries:
        print("[FAIL] 没有料号。用 --lcsc-id / --ids-file / --manifest 指定。", flush=True)
        return 2

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    sym_file = out.with_suffix(".kicad_sym")
    lib_exists = sym_file.exists()

    # 跳过已成功且库还在的料号（断点续跑）
    todo: list[str] = []
    skipped: list[str] = []
    for cid in sorted(entries):
        row = entries[cid]
        if (not args.retry_all) and row.get("status") == "ok" and lib_exists:
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
    print(f"限流策略   : 间隔 {args.delay}s，失败重试 {args.retries} 次（退避 {args.backoff}s）")
    if skipped:
        print("跳过料号   : " + " ".join(skipped))
    print("-" * 66, flush=True)

    ok_new, fail_new = [], []
    t0 = time.time()
    for i, cid in enumerate(todo, 1):
        comment = entries[cid].get("comment", "")
        suffix = f"  # {comment}" if comment else ""
        print(f"[{i}/{len(todo)}] {cid}{suffix}", flush=True)
        good = convert_one(
            cid, out, args.retries, args.backoff,
            full, use_symbol, use_footprint, use_3d,
            overwrite=not args.no_overwrite,
        )
        entries[cid]["status"] = "ok" if good else "fail"
        entries[cid]["updated"] = datetime.now().isoformat(timespec="seconds")
        if good:
            ok_new.append(cid)
            print("      [OK]", flush=True)
        else:
            fail_new.append(cid)
            print("      [FAIL]（可用 --lcsc-id 补跑）", flush=True)
        if args.manifest:
            save_manifest(Path(args.manifest), entries, out)
        if i < len(todo):
            time.sleep(args.delay)

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
