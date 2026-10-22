#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
统一入口：一条命令走完整流水线。

    lcsc2kicad build     # ① 转换（= build_kicad_lib.py）
    lcsc2kicad pins      # ② 引脚名对齐（= fix_symbol_pin_names.py）
    lcsc2kicad register  # ③ 注册库表（= import_to_kicad.py）
    lcsc2kicad verify    # ④ 验证（= verify_library.py）
    lcsc2kicad models    # ⑤ 3D 路径变量化（= fix_model_paths.py）

也可以继续直接 `python scripts/<脚本>.py`，两种方式等价。
"""
from __future__ import annotations

import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

USAGE = """\
lcsc2kicad — 立创/LCSC 元件 → KiCad 库（符号 + 封装 + 3D）

用法:
  lcsc2kicad <命令> [参数...]

命令:
  build     转换：下载并转成 KiCad 库（限流退避、工程清单）
  pins      引脚名对齐：按嘉立创逐引脚真值修（--fetch / --easyeda-json）
  register  注册库表：项目本地（${KIPRJMOD}）或全局，读回校验
  verify    用 kicad-cli 验证符号库/封装库可解析
  models    3D 模型路径变量化（默认 ${KIPRJMOD}）
  version   显示版本
  help      显示本帮助

示例:
  lcsc2kicad build --lcsc-id C5187527 --output ./myproj --manifest parts.json
  lcsc2kicad pins  ./myproj.kicad_sym --fetch C5187527
  lcsc2kicad register --lib-basename ./myproj --project-local .
  lcsc2kicad verify --lib-basename ./myproj
  lcsc2kicad models --lib-basename ./myproj --base . --dry-run

每个命令的完整参数: lcsc2kicad <命令> -h
"""

_COMMANDS = {
    "build": "build_kicad_lib",
    "pins": "fix_symbol_pin_names",
    "register": "import_to_kicad",
    "verify": "verify_library",
    "models": "fix_model_paths",
}

_ALIASES = {
    "convert": "build",
    "symbol": "pins",
    "lib": "register",
    "check": "verify",
    "3d": "models",
}


def get_version() -> str:
    try:
        from importlib.metadata import version

        return version("lcsc2kicad")
    except Exception:  # noqa: BLE001
        return "1.3.0"


def _load_module(name: str):
    """脚本在仓库里时把 scripts/ 放进 sys.path；pip 安装后直接 import。"""
    try:
        return __import__(name)
    except ImportError:
        here = Path(__file__).resolve().parent
        if str(here) not in sys.path:
            sys.path.insert(0, str(here))
        return __import__(name)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(USAGE)
        return 0 if args else 2
    cmd = args.pop(0)
    if cmd in ("-V", "--version", "version"):
        print(f"lcsc2kicad {get_version()}")
        return 0
    cmd = _ALIASES.get(cmd, cmd)
    if cmd not in _COMMANDS:
        print(f"[FAIL] 未知命令：{cmd}\n")
        print(USAGE)
        return 2

    module = _load_module(_COMMANDS[cmd])
    sys.argv = [f"lcsc2kicad {cmd}"] + args
    return int(module.main() or 0)


def cli() -> None:
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
