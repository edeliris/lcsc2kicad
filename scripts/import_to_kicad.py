#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 2 步：把生成的 KiCad 库注册进 KiCad（改 3 个纯文本配置文件，自动备份、幂等）。

用法
----
  # 先干跑看看会改什么
  python import_to_kicad.py --lib-basename ./kicad/myproj --dry-run

  # 真注册（昵称默认取库基名）
  python import_to_kicad.py --lib-basename ./kicad/myproj
  python import_to_kicad.py --lib-basename ./kicad/myproj --nickname myproj

  # 指定 KiCad 版本目录（自动选最高版本）
  python import_to_kicad.py --lib-basename ./kicad/myproj --kicad-version 10.0

  # 卸载
  python import_to_kicad.py --nickname myproj --remove

  # 项目本地注册（推荐；只影响这一个项目，跨机器可迁移）
  python import_to_kicad.py --lib-basename ./kicad/myproj --project-local ./myproject
  python import_to_kicad.py --lib-basename ./kicad/myproj --project-local ./myproject --dry-run
  python import_to_kicad.py --nickname myproj --project-local ./myproject --remove

它会做三件事
------------
  <KiCad用户目录>/<版本>/sym-lib-table   ← 加一行符号库
  <KiCad用户目录>/<版本>/fp-lib-table    ← 加一行封装库
  <KiCad用户目录>/<版本>/kicad_common.json ← 加环境变量（默认 EASYEDA2KICAD）

KiCad 用户目录
  Windows : %APPDATA%\\kicad
  macOS   : ~/Library/Preferences/kicad
  Linux   : ~/.config/kicad

注意
----
* **改之前必须关闭 KiCad**：KiCad 退出时会重写这些文件，运行中改会被覆盖。
* 这些文件在"项目工作区"之外；受限沙箱里写入会被拒绝，而"只判断没报错"会假装成功
  ⇒ 本脚本**逐项读回校验**，失败会明确报错。
* 目录里的 `.kicad_sym` 必须存在；`.pretty` 必须是**目录**。

项目本地模式（`--project-local <项目目录>`）
--------------------------------------------
只改项目下的 `sym-lib-table` / `fp-lib-table`，条目写成 `${KIPRJMOD}/<相对路径>`：

* 不动全局配置、也不写环境变量 ⇒ 天然规避 `kicad_common.json` 的坑；
* 库在项目内时，整个项目可搬走/可提交（相对路径）；
* 库在项目外仍会注册，但会告警：`${KIPRJMOD}/../...` 换机器/换目录会失效。
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime
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


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def kicad_base_dir() -> Path:
    if sys.platform.startswith("win"):
        return Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "kicad"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Preferences" / "kicad"
    return Path.home() / ".config" / "kicad"


def kicad_running() -> list[str]:
    """返回正在运行的 KiCad 相关进程名（跨平台尽力而为，失败返回空）。"""
    try:
        if sys.platform.startswith("win"):
            r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"],
                               capture_output=True, text=True, timeout=15)
            names = []
            for line in (r.stdout or "").splitlines():
                name = line.split(",")[0].strip('"').lower()
                if name.startswith("kicad") or name in (
                        "eeschema.exe", "pcbnew.exe", "bitmap2component.exe"):
                    names.append(name)
            return sorted(set(names))
        for p in ("kicad", "eeschema", "pcbnew"):
            r = subprocess.run(["pgrep", "-f", p], capture_output=True, text=True)
            if r.returncode == 0 and (r.stdout or "").strip():
                return [p]
    except Exception:
        return []
    return []


def pick_version_dir(base: Path, want: str | None) -> Path:
    if want:
        d = base / want
        if not d.is_dir():
            raise SystemExit(f"[FAIL] 不存在版本目录: {d}")
        return d
    cands = [d for d in base.iterdir() if d.is_dir() and d.name[:1].isdigit()] if base.is_dir() else []
    if not cands:
        raise SystemExit(f"[FAIL] 在 {base} 下找不到版本目录（KiCad 安装过吗？）")
    # 按版本号元组排序，取最高
    def key(p: Path):
        parts = []
        for x in p.name.split("."):
            parts.append(int(x) if x.isdigit() else 0)
        return tuple(parts)
    return sorted(cands, key=key)[-1]


# --------------------------------------------------------------------------
# S 表达式（lib-table）增删
# --------------------------------------------------------------------------
def table_add(path: Path, kind: str, nickname: str, uri: str, descr: str,
              dry_run: bool, force: bool) -> tuple[bool, str]:
    """在 sym_lib_table / fp_lib_table 里加一条 lib。返回 (changed, msg)。"""
    root = "sym_lib_table" if kind == "sym" else "fp_lib_table"
    text = path.read_text(encoding="utf-8") if path.exists() else f"({root}\n\t(version 7)\n)\n"
    if f'(name "{nickname}")' in text:
        return False, f"{path.name}: 已存在 nickname={nickname}，跳过"

    entry = (f'  (lib (name "{nickname}")(type "KiCad")(uri "{uri}")'
             f'(options "")(descr "{descr}"))')
    if dry_run:
        return True, f"{path.name}: 将追加 {entry}"

    idx = text.rstrip().rfind(")")
    if idx < 0:
        raise SystemExit(f"[FAIL] {path} 不是合法的 lib-table")
    new_text = text[:idx] + entry + "\n" + text[idx:]
    path.write_text(new_text, encoding="utf-8", newline="")
    if f'(name "{nickname}")' not in path.read_text(encoding="utf-8"):
        raise SystemExit(f"[FAIL] 写入后读回校验失败: {path}")
    return True, f"{path.name}: 已追加 nickname={nickname}"


def table_remove(path: Path, kind: str, nickname: str, dry_run: bool) -> tuple[bool, str]:
    if not path.exists():
        return False, f"{path.name}: 不存在，跳过"
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    keep = [ln for ln in lines if f'(name "{nickname}")' not in ln]
    if len(keep) == len(lines):
        return False, f"{path.name}: 未找到 nickname={nickname}"
    if dry_run:
        return True, f"{path.name}: 将删除 nickname={nickname}"
    path.write_text("".join(keep), encoding="utf-8", newline="")
    if f'(name "{nickname}")' in path.read_text(encoding="utf-8"):
        raise SystemExit(f"[FAIL] 删除后仍存在: {path}")
    return True, f"{path.name}: 已删除 nickname={nickname}"


# --------------------------------------------------------------------------
# 环境变量（kicad_common.json）
# --------------------------------------------------------------------------
def _norm(p: str) -> str:
    """KiCad 里同一个路径可能写成 C:\a\b 或 C:/a/b，比较前统一。"""
    return p.replace("\\", "/").rstrip("/").lower()


def env_set(path: Path, var: str, value: str, dry_run: bool) -> tuple[bool, str]:
    """KiCad 10 的结构是 {"environment": {"vars": {"NAME": "path"}}}。
    注意：**绝不能**把自定义变量平铺在 environment 下（会产生重复键，KiCad 打开时丢弃）。"""
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    env = data.get("environment") or {}
    vars_map = env.get("vars") or {}
    if var in vars_map and _norm(str(vars_map[var])) == _norm(value):
        return False, f"kicad_common.json: {var} 已存在且等价（{vars_map[var]}），跳过"
    if dry_run:
        return True, f"kicad_common.json: 将设置 environment.vars.{var} = {value}"
    vars_map[var] = value
    env["vars"] = vars_map
    data["environment"] = env
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    back = json.loads(path.read_text(encoding="utf-8"))
    if ((back.get("environment") or {}).get("vars") or {}).get(var) != value:
        raise SystemExit(f"[FAIL] 写入后读回校验失败: {path}")
    return True, f"kicad_common.json: 已设置 environment.vars.{var} = {value}"


def env_remove(path: Path, var: str, dry_run: bool) -> tuple[bool, str]:
    if not path.exists():
        return False, "kicad_common.json: 不存在，跳过"
    data = json.loads(path.read_text(encoding="utf-8"))
    env = data.get("environment") or {}
    vars_map = env.get("vars") or {}
    if var not in vars_map:
        return False, f"kicad_common.json: 未设置 {var}"
    if dry_run:
        return True, f"kicad_common.json: 将删除 environment.vars.{var}"
    vars_map.pop(var, None)
    env["vars"] = vars_map or None
    data["environment"] = env
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return True, f"kicad_common.json: 已删除 environment.vars.{var}"


# --------------------------------------------------------------------------
# 项目本地注册（${KIPRJMOD}）
# --------------------------------------------------------------------------
def rel_uri(project_dir: Path, target: Path) -> tuple[str, bool]:
    """把绝对路径转成 ${KIPRJMOD}/相对路径；返回 (uri, 是否在项目外)。"""
    rel = os.path.relpath(target, project_dir).replace("\\", "/")
    return "${KIPRJMOD}/" + rel, rel.startswith("../")


def run_project_local(args) -> int:
    proj = Path(args.project_local).resolve()
    if not proj.is_dir():
        print(f"[FAIL] 项目目录不存在: {proj}")
        return 2
    sym_table = proj / "sym-lib-table"
    fp_table = proj / "fp-lib-table"

    # ---------- 卸载 ----------
    if args.remove:
        nick = args.nickname
        if not nick:
            print("[FAIL] --remove 需要 --nickname")
            return 2
        print(f"\n=== 卸载（项目本地）: {proj} ===")
        rc = 0
        for path, kind in ((sym_table, "sym"), (fp_table, "fp")):
            try:
                ch, msg = table_remove(path, kind, nick, args.dry_run)
            except SystemExit as e:
                print(str(e))
                rc = 1
                continue
            print(("[change] " if ch else "[skip]   ") + msg)
        return rc

    # ---------- 注册 ----------
    if not args.lib_basename:
        print("[FAIL] 需要 --lib-basename（或 --remove）")
        return 2
    base = Path(args.lib_basename).resolve()
    sym_file = base.with_suffix(".kicad_sym")
    pretty_dir = Path(str(base) + ".pretty")
    if not sym_file.exists():
        print(f"[FAIL] 符号库不存在: {sym_file}")
        return 2
    if not pretty_dir.is_dir():
        print(f"[FAIL] 封装库目录不存在: {pretty_dir}")
        return 2

    nick = args.nickname or base.name
    uri_sym, sym_outside = rel_uri(proj, sym_file)
    uri_fp, fp_outside = rel_uri(proj, pretty_dir)

    print(f"项目目录      : {proj}")
    print(f"库昵称        : {nick}")
    print(f"符号库        : {uri_sym}")
    print(f"封装库        : {uri_fp}")
    if sym_outside or fp_outside:
        print("[WARN] 库在项目目录之外：${KIPRJMOD}/../... 换机器/换目录会失效，建议把库放进项目内")
    if args.dry_run:
        print("\n*** DRY RUN（不写文件）***")

    if not args.dry_run:
        bk = proj / f"_backup_{datetime.now():%Y%m%d_%H%M%S}"
        bk.mkdir(exist_ok=True)
        for f in (sym_table, fp_table):
            if f.exists():
                shutil.copy2(f, bk / f.name)
        print(f"已备份到      : {bk}")

    print("\n=== 写入（项目本地） ===")
    rc = 0
    try:
        for path, kind, uri in ((sym_table, "sym", uri_sym), (fp_table, "fp", uri_fp)):
            ch, msg = table_add(path, kind, nick, uri, args.descr, args.dry_run, args.force)
            print(("[change] " if ch else "[skip]   ") + msg)
    except SystemExit as e:
        print(str(e))
        rc = 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}")
        rc = 1

    if rc == 0 and not args.dry_run:
        print("\n=== 验证（读回）===")
        ok_sym = sym_table.read_text(encoding="utf-8")
        ok_fp = fp_table.read_text(encoding="utf-8")
        sym_ok = f'(name "{nick}")' in ok_sym and uri_sym in ok_sym
        fp_ok = f'(name "{nick}")' in ok_fp and uri_fp in ok_fp
        print(f"  sym-lib-table   : {'OK' if sym_ok else 'FAIL'}")
        print(f"  fp-lib-table    : {'OK' if fp_ok else 'FAIL'}")
        rc = 0 if (sym_ok and fp_ok) else 1

    if rc == 0 and not args.dry_run:
        print("\n下一步：")
        print("  KiCad 打开该项目 → 符号库/封装库选择器里搜昵称；项目本地库表只影响这个项目")
    return rc


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser(description="把 easyeda2kicad 生成的库注册进 KiCad")
    ap.add_argument("--lib-basename", help="库基名（不带扩展名），如 ./kicad/myproj")
    ap.add_argument("--nickname", help="KiCad 里的库昵称（默认取库基名）")
    ap.add_argument("--env-var", default="EASYEDA2KICAD", help="环境变量名（默认 EASYEDA2KICAD）")
    ap.add_argument("--kicad-version", default=None, help="KiCad 版本目录，如 10.0（默认最高版本）")
    ap.add_argument("--descr", default="LCSC/EasyEDA parts (easyeda2kicad)", help="库描述")
    ap.add_argument("--project-local", default=None, metavar="项目目录",
                    help="项目本地注册：只改该项目的 sym-lib-table/fp-lib-table（${KIPRJMOD} 相对路径）")
    ap.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不写文件")
    ap.add_argument("--remove", action="store_true", help="卸载该昵称的库与环境变量")
    ap.add_argument("--force", action="store_true", help="KiCad 正在运行时也强制写入")
    ap.add_argument("--log", default=None, help="把输出追加写入日志文件")
    args = ap.parse_args()
    setup_log(args.log)

    if args.project_local:
        return run_project_local(args)

    ver_dir = pick_version_dir(kicad_base_dir(), args.kicad_version)
    print(f"KiCad 配置目录 : {ver_dir}")

    running = kicad_running()
    if running:
        print(f"[WARN] KiCad 正在运行: {', '.join(running)}")
        print("       KiCad 退出时会重写配置，运行中修改可能被覆盖！")
        if not args.force and not args.dry_run:
            print("       → 请先关闭 KiCad；或加 --force 强行写入")
            return 3

    sym_table = ver_dir / "sym-lib-table"
    fp_table = ver_dir / "fp-lib-table"
    common = ver_dir / "kicad_common.json"

    # ---------- 卸载 ----------
    if args.remove:
        nick = args.nickname
        if not nick:
            print("[FAIL] --remove 需要 --nickname")
            return 2
        print("\n=== 卸载 ===")
        for path, kind in ((sym_table, "sym"), (fp_table, "fp")):
            ch, msg = table_remove(path, kind, nick, args.dry_run)
            print(("[change] " if ch else "[skip]   ") + msg)
        ch, msg = env_remove(common, args.env_var, args.dry_run)
        print(("[change] " if ch else "[skip]   ") + msg)
        return 0

    # ---------- 注册 ----------
    if not args.lib_basename:
        print("[FAIL] 需要 --lib-basename（或 --remove）")
        return 2
    base = Path(args.lib_basename).resolve()
    sym_file = base.with_suffix(".kicad_sym")
    pretty_dir = Path(str(base) + ".pretty")
    if not sym_file.exists():
        print(f"[FAIL] 符号库不存在: {sym_file}")
        return 2
    if not pretty_dir.is_dir():
        print(f"[FAIL] 封装库目录不存在: {pretty_dir}")
        return 2

    nick = args.nickname or base.name
    # KiCad 里用正斜杠更稳
    uri_sym = str(sym_file).replace("\\", "/")
    uri_fp = str(pretty_dir).replace("\\", "/")
    env_value = str(base.parent).replace("\\", "/")

    print(f"库昵称        : {nick}")
    print(f"符号库        : {uri_sym}")
    print(f"封装库        : {uri_fp}")
    print(f"环境变量      : {args.env_var} = {env_value}")
    if args.dry_run:
        print("\n*** DRY RUN（不写文件）***")

    # 备份
    if not args.dry_run:
        bk = ver_dir / f"_backup_{datetime.now():%Y%m%d_%H%M%S}"
        bk.mkdir(parents=True, exist_ok=True)
        for f in (sym_table, fp_table, common):
            if f.exists():
                shutil.copy2(f, bk / f.name)
        print(f"已备份到      : {bk}")

    print("\n=== 写入 ===")
    rc = 0
    try:
        for path, kind, uri in ((sym_table, "sym", uri_sym), (fp_table, "fp", uri_fp)):
            ch, msg = table_add(path, kind, nick, uri, args.descr, args.dry_run, args.force)
            print(("[change] " if ch else "[skip]   ") + msg)
        ch, msg = env_set(common, args.env_var, env_value, args.dry_run)
        print(("[change] " if ch else "[skip]   ") + msg)
    except SystemExit as e:
        print(str(e))
        rc = 1
    except Exception as e:  # noqa: BLE001
        print(f"[FAIL] {type(e).__name__}: {e}")
        print("       常见原因：没有写权限（文件在项目工作区之外）。")
        rc = 1

    if rc == 0 and not args.dry_run:
        print("\n=== 验证（读回）===")
        st = sym_table.read_text(encoding="utf-8")
        ft = fp_table.read_text(encoding="utf-8")
        cm = json.loads(common.read_text(encoding="utf-8"))
        ok_sym = f'(name "{nick}")' in st
        ok_fp = f'(name "{nick}")' in ft
        ok_env = ((cm.get("environment") or {}).get("vars") or {}).get(args.env_var) is not None
        print(f"  sym-lib-table   : {'OK' if ok_sym else 'FAIL'}")
        print(f"  fp-lib-table    : {'OK' if ok_fp else 'FAIL'}")
        print(f"  kicad_common.env: {'OK' if ok_env else 'FAIL'}")
        rc = 0 if (ok_sym and ok_fp and ok_env) else 1

    if rc == 0 and not args.dry_run:
        print("\n下一步：")
        print("  1) 启动 KiCad → 原理图按 A → 搜昵称 → 应能看到元件")
        print(f"  2) 验证库可被 KiCad 解析：python verify_library.py --lib-basename \"{base}\"")
    return rc


def cli() -> None:
    """pip 安装后的控制台入口（lcsc2kicad-register）。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
