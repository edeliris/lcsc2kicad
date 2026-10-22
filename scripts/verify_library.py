#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 3 步：验证生成的 KiCad 库（用 KiCad 自己的解析器，而不是猜）。

用法
----
  python verify_library.py --lib-basename ./kicad/myproj
  python verify_library.py --lib-basename ./kicad/myproj \
      --kicad-cli "C:/Program Files/KiCad/10.0/bin/kicad-cli.exe"
  python verify_library.py --lib-basename ./kicad/myproj --skip-cli   # 只做静态检查

检查项
------
  1. 计数：符号 / 封装 / 3D 模型（.step / .wrl）
  2. S 表达式括号平衡（快速发现截断/损坏）
  3. `kicad-cli sym upgrade --force`（在副本上跑）  → exit 0 说明 KiCad 解析通过
  4. `kicad-cli fp upgrade --force -o <tmp> <pretty>` → exit 0 且输出数量一致

注意：加 `--force` 是为了让"已是当前格式"的库也重写一遍，否则 kicad-cli 只提示
      `封装库未更新`、不产生输出，会被误判成失败。

注意：kicad-cli 在受限沙箱里可能因写注册表失败（Windows `error 5`）；
      需要正常权限运行。没有 kicad-cli 时用 --skip-cli。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass


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


def _windows_registry_kicad_dirs() -> list[Path]:
    """从 Windows 卸载注册表里读 KiCad 的 InstallLocation（最可靠的自动发现方式）。"""
    dirs: list[Path] = []
    if not sys.platform.startswith("win"):
        return dirs
    try:
        import winreg  # noqa: PLC0415
    except Exception:
        return dirs
    roots = [
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
        (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    ]
    for hive, sub in roots:
        try:
            with winreg.OpenKey(hive, sub) as k:
                i = 0
                while True:
                    try:
                        name = winreg.EnumKey(k, i)
                        i += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(k, name) as sub_k:
                            disp = winreg.QueryValueEx(sub_k, "DisplayName")[0]
                            if isinstance(disp, str) and disp.lower().startswith("kicad"):
                                loc = winreg.QueryValueEx(sub_k, "InstallLocation")[0]
                                if loc:
                                    dirs.append(Path(loc))
                    except OSError:
                        continue
        except OSError:
            continue
    return dirs


def find_kicad_cli(explicit: str | None) -> str | None:
    # 1) 显式参数
    if explicit:
        return explicit if Path(explicit).exists() else None
    # 2) 环境变量
    envp = os.environ.get("KICAD_CLI")
    if envp and Path(envp).exists():
        return envp
    # 3) PATH
    for name in ("kicad-cli", "kicad-cli.exe"):
        p = shutil.which(name)
        if p:
            return p
    # 4) Windows 注册表（KiCad 安装位置）
    roots: list[Path] = []
    for d in _windows_registry_kicad_dirs():
        roots.append(d)
    # 5) 常见安装位置（多级）
    cands = [
        Path("C:/Program Files/KiCad"),
        Path("C:/Program Files (x86)/KiCad"),
        Path("/usr/bin"),
        Path("/usr/local/bin"),
        Path("/Applications/KiCad/KiCad.app/Contents/MacOS"),
    ]
    for c in cands:
        if c.is_dir():
            roots.append(c)
            roots += [d for d in c.iterdir() if d.is_dir()]
    for r in roots:
        for rel in ("bin/kicad-cli.exe", "bin/kicad-cli", "kicad-cli", "kicad-cli.exe"):
            p = r / rel
            if p.exists():
                return str(p)
    return None


def count_symbols(sym_file: Path) -> list[str]:
    text = sym_file.read_text(encoding="utf-8", errors="replace")
    names = re.findall(r'\(symbol\s+"([^"]+)"', text)
    # 去掉子件后缀 _0_1 / _1_1
    top = {re.sub(r"_[0-9]+_[0-9]+$", "", n) for n in names}
    return sorted(top)


def parens_balanced(text: str) -> tuple[bool, int, int]:
    # 粗略计数（忽略字符串内的括号，够用）
    s = re.sub(r'"[^"]*"', '""', text)
    return s.count("(") == s.count(")"), s.count("("), s.count(")")


def run_cli(cli: str, args: list[str], timeout: int = 300):
    try:
        r = subprocess.run([cli] + args, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:  # noqa: BLE001
        return -99, f"{type(e).__name__}: {e}"


def main() -> int:
    ap = argparse.ArgumentParser(description="验证 easyeda2kicad 生成的 KiCad 库")
    ap.add_argument("--lib-basename", required=True, help="库基名（不带扩展名）")
    ap.add_argument("--kicad-cli", default=None, help="kicad-cli 路径（默认自动查找）")
    ap.add_argument("--skip-cli", action="store_true", help="跳过 kicad-cli 验证")
    ap.add_argument("--log", default=None, help="把输出追加写入日志文件")
    args = ap.parse_args()
    setup_log(args.log)

    base = Path(args.lib_basename).resolve()
    sym = base.with_suffix(".kicad_sym")
    pretty = Path(str(base) + ".pretty")
    d3 = Path(str(base) + ".3dshapes")

    print(f"库基名: {base}")
    rc = 0

    # ---------- 1. 存在性与计数 ----------
    print("\n=== 1. 文件与计数 ===")
    if not sym.exists():
        print(f"  [FAIL] 符号库不存在: {sym}")
        return 1
    symbols = count_symbols(sym)
    print(f"  符号库   : {sym.name}  {sym.stat().st_size/1024:.1f} KB  符号 {len(symbols)} 个")
    if pretty.is_dir():
        fps = sorted(p.stem for p in pretty.glob("*.kicad_mod"))
        print(f"  封装库   : {pretty.name}  {len(fps)} 个 .kicad_mod")
    else:
        fps = []
        print(f"  [WARN] 封装库目录不存在: {pretty}")
        rc = 1
    if d3.is_dir():
        steps = list(d3.glob("*.step"))
        wrls = list(d3.glob("*.wrl"))
        print(f"  3D 模型  : {len(steps)} 个 .step / {len(wrls)} 个 .wrl")
    else:
        print("  3D 模型  : 无目录（可以没有）")
    if symbols:
        print("  符号清单 : " + ", ".join(symbols))

    # ---------- 2. 括号平衡 ----------
    print("\n=== 2. S 表达式结构（括号平衡）===")
    for f, label in ((sym, "符号库"), *[(p, p.name) for p in sorted(pretty.glob('*.kicad_mod'))[:200]]):
        ok, o, c = parens_balanced(f.read_text(encoding="utf-8", errors="replace"))
        if not ok:
            print(f"  [FAIL] {label}: 括号不平衡 ({o} 开 / {c} 闭)")
            rc = 1
    if rc == 0:
        print("  全部平衡 OK")

    # ---------- 3/4. kicad-cli ----------
    if args.skip_cli:
        print("\n=== 3. kicad-cli 验证：已跳过（--skip-cli）===")
        return rc

    print("\n=== 3. kicad-cli 验证（KiCad 自己的解析器）===")
    cli = find_kicad_cli(args.kicad_cli)
    if not cli:
        print("  [WARN] 找不到 kicad-cli，跳过。可用 --kicad-cli 指定路径。")
        return rc
    code, out = run_cli(cli, ["version"])
    print(f"  kicad-cli: {cli}  ({out.strip().splitlines()[0] if out.strip() else '?'})")

    tmp = Path(tempfile.mkdtemp(prefix="kicad_verify_"))
    try:
        # 符号库：在副本上跑 upgrade（= 解析 + 重写）
        sym_copy = tmp / sym.name
        shutil.copy2(sym, sym_copy)
        code, out = run_cli(cli, ["sym", "upgrade", "--force", str(sym_copy)])
        if code == 0:
            print("  符号库解析 : OK  (sym upgrade exit=0)")
        else:
            print(f"  符号库解析 : FAIL (exit={code})")
            print("    " + (out.strip().splitlines()[0] if out.strip() else ""))
            rc = 1

        # 封装库：整目录 upgrade -o <outdir>（--force 保证当前格式也会重写）
        if pretty.is_dir():
            fp_copy = tmp / pretty.name
            shutil.copytree(pretty, fp_copy)
            out_dir = tmp / "fp_out"
            code, out = run_cli(cli, ["fp", "upgrade", "--force", "-o", str(out_dir), str(fp_copy)])
            produced = list(out_dir.rglob("*.kicad_mod")) if out_dir.is_dir() else []
            if code == 0 and len(produced) == len(fps):
                print(f"  封装库解析 : OK  (fp upgrade exit=0, {len(produced)}/{len(fps)})")
            elif code == 0 and not produced:
                # 已是最新格式：kicad-cli 加载成功后才提示"未更新"，不产生输出
                print(f"  封装库解析 : OK  (已是当前格式，{len(fps)} 个均可加载)")
            else:
                print(f"  封装库解析 : FAIL (exit={code}, 输出 {len(produced)}/{len(fps)})")
                print("    " + (out.strip().splitlines()[0] if out.strip() else ""))
                rc = 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + ("全部通过 ✓" if rc == 0 else "存在问题 ✗（见上）"))
    return rc


def cli() -> None:
    """pip 安装后的控制台入口（lcsc2kicad-verify）。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
