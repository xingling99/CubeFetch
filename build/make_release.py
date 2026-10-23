"""生成发布包（win64 运行包 + 源码包）。

发布包全部用 Python zipfile 写入，中文文件名会带 UTF-8 标志位，跨平台解压不乱码。

用法：

    python -X utf8 build\\make_release.py            # 两个包都生成
    python -X utf8 build\\make_release.py --exe-only # 只生成运行包
    python -X utf8 build\\make_release.py --source-only
"""
from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist"
APP_VERSION = "1.2.1-beta"
RUN_ZIP_NAME = f"CubeFetch-v{APP_VERSION}-win64.zip"
SRC_ZIP_NAME = f"CubeFetch-v{APP_VERSION}-source.zip"

#: 源码包里逐文件/目录收集时跳过的名字（任何层级）。
SKIP_DIRS = {"__pycache__", ".cache", "build", "dist", "release", ".git", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".log", ".part"}

SOURCE_TOP_FILES = ["app.py", "README.md", "requirements.txt", ".gitignore"]
SOURCE_TREES = ["cubefetch", "tests", "tools", "docs", "build"]
SOURCE_EXTRA = ["release/使用说明.txt"]


def _write_zip(zip_path: Path, entries: list[tuple[Path, str]]) -> int:
    """entries: [(本地文件绝对路径, zip 内相对路径)]，逐个写入并返回字节数。"""
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    if zip_path.exists():
        zip_path.unlink()
    count = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for source, arcname in entries:
            if not source.is_file():
                raise FileNotFoundError(f"缺少发布文件：{source}")
            zf.write(source, arcname)
            count += 1
    return count


def build_run_package() -> Path:
    exe = DIST / "CubeFetch.exe"
    if not exe.is_file():
        raise FileNotFoundError(f"缺少打包产物 {exe}，请先运行 build\\build.ps1")
    entries: list[tuple[Path, str]] = [(exe, "CubeFetch.exe")]
    for name in ("README.md", "使用说明.txt"):
        path = ROOT / name if name == "README.md" else ROOT / "release" / name
        entries.append((path, name))
    entries.append((ROOT / "docs" / "PROGRESS.md", "docs/PROGRESS.md"))
    out = DIST / RUN_ZIP_NAME
    count = _write_zip(out, entries)
    print(f"运行包已生成：{out}（{count} 个文件，{out.stat().st_size / 1024 / 1024:.2f} MB）")
    return out


def build_source_package() -> Path:
    entries: list[tuple[Path, str]] = []
    for name in SOURCE_TOP_FILES:
        path = ROOT / name
        if path.is_file():
            entries.append((path, name))
    for name in SOURCE_EXTRA:
        path = ROOT / name
        if path.is_file():
            entries.append((path, name.replace("\\", "/")))
    for tree in SOURCE_TREES:
        base = ROOT / tree
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            # 收集根本身允许与 SKIP_DIRS 同名（如 build/ 需要随源码分发），
            # 只跳过根以下的缓存/产物目录。
            dirs = relative.parts[:-1]
            if any(part in SKIP_DIRS for part in dirs[1:]):
                continue
            if len(dirs) >= 2 and dirs[0] == "build" and dirs[1] == "out":
                continue
            if path.suffix.lower() in SKIP_SUFFIXES:
                continue
            entries.append((path, str(relative).replace("\\", "/")))
    out = DIST / SRC_ZIP_NAME
    count = _write_zip(out, entries)
    print(f"源码包已生成：{out}（{count} 个文件，{out.stat().st_size / 1024 / 1024:.2f} MB）")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成 CubeFetch 发布包")
    parser.add_argument("--exe-only", action="store_true", help="只生成运行包")
    parser.add_argument("--source-only", action="store_true", help="只生成源码包")
    args = parser.parse_args(argv)

    try:
        if not args.source_only:
            build_run_package()
        if not args.exe_only:
            build_source_package()
    except FileNotFoundError as exc:
        print(f"!! {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
