#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 1 步：按立创料号（C 开头）下载并转换成 KiCad 库（符号 + 封装 + 3D）。

用法
----
  # 单个 / 几个
  python build_kicad_lib.py --lcsc-id C2040 --output ./kicad/myproj
  python build_kicad_lib.py --lcsc-id C17513 C17414 C17514 --output ./kicad/myproj

  # 从文件读（每行一个料号，# 开头为注释）
  python build_kicad_lib.py --ids-file parts.txt --output ./kicad/myproj

  # 只转换符号+封装（省流量，不要 3D）
  python build_kicad_lib.py --lcsc-id C2040 --symbol --footprint --output ./kicad/myproj

  # 只补失败的那几个
  python build_kicad_lib.py --lcsc-id C2128 C434967 --output ./kicad/myproj

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
import shutil
import subprocess
import sys
import time
from pathlib import Path

# ★ 中文 Windows 控制台默认 GBK，打印 ✓/✗ 等字符会抛
#   UnicodeEncodeError: 'gbk' codec can't encode character '\u2713'
#   强制 UTF-8 输出，避免脚本在"第一件已成功"之后崩掉（曾导致误判）。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


def check_tool() -> bool:
    """检查 easyeda2kicad 是否可调用（注意：它常装在不在 PATH 的用户目录）。"""
    r = subprocess.run(
        [sys.executable, "-m", "easyeda2kicad", "--help"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return r.returncode == 0


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


def read_ids_file(path: Path) -> list[str]:
    ids: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        # 支持 "C2040" / "C2040  注释" / "C2040,注释"
        for sep in ("\t", " ", ",", ";"):
            if sep in s:
                s = s.split(sep)[0].strip()
                break
        if s:
            ids.append(s)
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(
        description="立创料号 → KiCad 库（符号/封装/3D），带限流退避重试")
    ap.add_argument("--lcsc-id", nargs="+", default=None, help="立创料号，如 C2040 C17513")
    ap.add_argument("--ids-file", default=None, help="料号文件（每行一个，# 注释）")
    ap.add_argument("--output", required=True, help="库基名（不带扩展名），如 ./kicad/myproj")
    ap.add_argument("--delay", type=float, default=6.0, help="料号间隔秒（默认 6，限流时调到 10）")
    ap.add_argument("--retries", type=int, default=3, help="单个失败重试次数（默认 3）")
    ap.add_argument("--backoff", type=float, default=30.0, help="重试等待秒（默认 30）")
    ap.add_argument("--symbol", action="store_true", help="只要符号（与 --full 互斥）")
    ap.add_argument("--footprint", action="store_true", help="只要封装")
    ap.add_argument("--3d", dest="use_3d", action="store_true", help="只要 3D 模型")
    ap.add_argument("--no-overwrite", action="store_true",
                    help="已存在的封装不覆盖（默认覆盖，避免同封装冲突报错）")
    args = ap.parse_args()

    if not check_tool():
        print("[FAIL] easyeda2kicad 不可用。请先： pip install easyeda2kicad", flush=True)
        return 2

    ids: list[str] = []
    if args.lcsc_id:
        ids += [s.strip() for s in args.lcsc_id if s.strip()]
    if args.ids_file:
        ids += read_ids_file(Path(args.ids_file))
    if not ids:
        print("[FAIL] 没有料号。用 --lcsc-id 或 --ids-file 指定。", flush=True)
        return 2

    # 去重保序
    seen: set[str] = set()
    ids = [x for x in ids if not (x in seen or seen.add(x))]

    out = Path(args.output).resolve()
    out.parent.mkdir(parents=True, exist_ok=True)

    any_specific = args.symbol or args.footprint or args.use_3d
    full = not any_specific
    use_symbol = args.symbol or full
    use_footprint = args.footprint or full
    use_3d = args.use_3d or full

    print(f"输出库基名 : {out}")
    print(f"料号数量   : {len(ids)}")
    print(f"转换内容   : full={full} symbol={use_symbol} footprint={use_footprint} 3d={use_3d}")
    print(f"限流策略   : 间隔 {args.delay}s，失败重试 {args.retries} 次（退避 {args.backoff}s）")
    print("-" * 66, flush=True)

    ok_list, fail_list = [], []
    t0 = time.time()
    for i, cid in enumerate(ids, 1):
        print(f"[{i}/{len(ids)}] {cid}", flush=True)
        good = convert_one(
            cid, out, args.retries, args.backoff,
            full, use_symbol, use_footprint, use_3d,
            overwrite=not args.no_overwrite,
        )
        if good:
            ok_list.append(cid)
            print("      [OK]", flush=True)
        else:
            fail_list.append(cid)
            print("      [FAIL]（可用 --lcsc-id 补跑）", flush=True)
        if i < len(ids):
            time.sleep(args.delay)

    elapsed = time.time() - t0
    print("=" * 66)
    print(f"成功 {len(ok_list)} / 失败 {len(fail_list)}，耗时 {elapsed:.0f}s")
    if fail_list:
        print("失败料号：" + " ".join(fail_list))
        print("补跑命令：python build_kicad_lib.py --lcsc-id " + " ".join(fail_list)
              + f" --output {out}")

    sym = out.with_suffix(".kicad_sym")
    pretty = Path(str(out) + ".pretty")
    d3 = Path(str(out) + ".3dshapes")
    if sym.exists():
        print(f"符号库 : {sym}  ({sym.stat().st_size/1024:.1f} KB)")
    if pretty.exists():
        print(f"封装库 : {pretty}  ({len(list(pretty.glob('*.kicad_mod')))} 个 .kicad_mod)")
    if d3.exists():
        n = len(list(d3.iterdir()))
        print(f"3D 模型: {d3}  ({n} 个文件)")
    return 0 if not fail_list else 1


if __name__ == "__main__":
    raise SystemExit(main())
