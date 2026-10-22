#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
第 5 步（可选）：把封装里的 3D 模型路径改成路径变量（默认 `${KIPRJMOD}`）。

为什么
------
easyeda2kicad 把 `(model "C:/Users/.../myproj.3dshapes/xxx.step")` 写成**绝对路径**：
换机器、换目录、把项目提交到 git 后，3D 预览就全丢了。
改成 `${KIPRJMOD}/...` 相对路径后，库跟着项目走。

用法
----
  # 项目内相对化（--base 是 KiCad 项目根目录）
  python fix_model_paths.py --lib-basename ./myproject/kicad/myproj --base ./myproject

  # 干跑看会改什么
  python fix_model_paths.py --lib-basename ./myproject/kicad/myproj --base ./myproject --dry-run

  # 用 KiCad 的全局 3D 目录变量（模型放在 KiCad 3D 搜索路径里时）
  python fix_model_paths.py --lib-basename ./myproj --base "C:/KiCad/3dmodels" \
      --variable KICAD10_3DMODEL_DIR

  # 只允许模型在 base 之外时也改写（会写 ../，换机器可能失效）
  python fix_model_paths.py --lib-basename ./myproj --base . --allow-outside

只改 `<基名>.pretty/*.kicad_mod` 里的 `(model ...)`；可重复运行（已是变量形式的跳过）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # pragma: no cover
    pass

RE_MODEL = re.compile(r'\(model\s+"([^"]+)"')


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


def rewrite_path(raw: str, base: Path, variable: str, allow_outside: bool) -> tuple[str, str]:
    """返回 (新路径, 状态)；状态：changed / already-var / relative / outside / bad。"""
    p = raw.strip()
    if not p:
        return raw, "bad"
    if p.startswith("${") or p.startswith("$(") or p.startswith("$"):
        return raw, "already-var"
    target = Path(p.replace("\\", "/"))
    if not target.is_absolute():
        return raw, "relative"
    try:
        rel = os.path.relpath(target, base).replace("\\", "/")
    except ValueError:  # Windows 跨盘符
        return raw, "outside"
    if rel == ".":
        return raw, "bad"
    if rel.startswith("../") and not allow_outside:
        return raw, "outside"
    return f"${{{variable}}}/{rel}", "changed"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="把 .pretty 里的 3D 模型绝对路径改写成路径变量（默认 ${KIPRJMOD}）")
    ap.add_argument("--lib-basename", required=True, help="库基名（不带扩展名），如 ./kicad/myproj")
    ap.add_argument("--base", required=True,
                    help="路径变量指向的目录；通常是 KiCad 项目根（--project-local 时同一目录）")
    ap.add_argument("--variable", default="KIPRJMOD",
                    help="路径变量名（默认 KIPRJMOD；全局 3D 目录可用 KICAD10_3DMODEL_DIR）")
    ap.add_argument("--allow-outside", action="store_true",
                    help="模型在 base 之外时也改写（会写 ../，换机器可能失效）")
    ap.add_argument("--dry-run", action="store_true", help="只显示将要做什么，不写文件")
    ap.add_argument("--no-backup", action="store_true", help="不生成 .bak_* 备份")
    ap.add_argument("--log", default=None, help="把输出追加写入日志文件")
    args = ap.parse_args()
    setup_log(args.log)

    base = Path(args.base).resolve()
    if not base.is_dir():
        print(f"[FAIL] base 目录不存在：{base}")
        return 2
    pretty = Path(str(Path(args.lib_basename).resolve()) + ".pretty")
    if not pretty.is_dir():
        print(f"[FAIL] 封装库目录不存在：{pretty}")
        return 2

    files = sorted(pretty.glob("*.kicad_mod"))
    if not files:
        print(f"[FAIL] {pretty} 里没有 .kicad_mod")
        return 2

    print(f"封装库    : {pretty}")
    print(f"变量       : ${{{args.variable}}} → {base}")
    stats = dict(changed=0, already=0, relative=0, outside=0, bad=0)
    changed_files: list[Path] = []
    missing_models: list[str] = []

    for f in files:
        text = f.read_text(encoding="utf-8")
        lines = text.split("\n")
        file_changed = 0
        for i, line in enumerate(lines):
            m = RE_MODEL.search(line)
            if not m:
                continue
            new, status = rewrite_path(m.group(1), base, args.variable, args.allow_outside)
            key = {"changed": "changed", "already-var": "already", "relative": "relative",
                   "outside": "outside", "bad": "bad"}[status]
            if status == "changed":
                raw_target = m.group(1).replace("\\", "/")
                if not Path(raw_target).is_file():
                    missing_models.append(raw_target)
                lines[i] = line.replace(f'"{m.group(1)}"', f'"{new}"', 1)
                file_changed += 1
            stats[key] += 1
        if file_changed:
            changed_files.append(f)
            print(f"[change] {f.name}: {file_changed} 个模型路径")
            if not args.dry_run:
                if not args.no_backup:
                    bak = f.with_name(f"{f.name}.bak_{datetime.now():%Y%m%d_%H%M%S}")
                    shutil.copy2(f, bak)
                f.write_text("\n".join(lines), encoding="utf-8", newline="")

    print("-" * 66)
    print(f"文件 {len(files)} 个：改动 {stats['changed']} 处 / 已是变量 {stats['already']} 处 / "
          f"相对路径跳过 {stats['relative']} / 在 base 外跳过 {stats['outside']} / 异常 {stats['bad']}")
    if missing_models:
        print(f"[WARN] {len(missing_models)} 个模型文件在磁盘上不存在（路径仍会改写）：")
        for p in missing_models[:5]:
            print("       " + p)
    if stats["outside"] and not args.allow_outside:
        print("[注] 有模型在 base 之外；确认要相对化时加 --allow-outside")

    if args.dry_run:
        print(f"\n[DRY RUN] 将修改 {len(changed_files)} 个文件，未写文件。")
        return 0
    if not changed_files:
        print("\n无需修改（已是路径变量或没有可改写的模型路径）。")
        return 0

    # 读回校验
    bad = 0
    for f in changed_files:
        back = f.read_text(encoding="utf-8")
        if f"${{{args.variable}}}/" not in back:
            print(f"[FAIL] 读回校验失败：{f}")
            bad += 1
    print(f"已写入: {len(changed_files)} 个文件；读回校验={'OK' if not bad else f'FAIL({bad})'}")
    return 0 if not bad else 1


def cli() -> None:
    """pip 安装后的控制台入口（lcsc2kicad-models）。"""
    raise SystemExit(main())


if __name__ == "__main__":
    raise SystemExit(main())
