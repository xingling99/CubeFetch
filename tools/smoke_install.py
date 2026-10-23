"""真实网络冒烟：用正式后端（不走 UI）安装一个版本到指定目录。

用法::

    python tools/smoke_install.py --root D:\\tmp\\mc --game 1.20.1
    python tools/smoke_install.py --root D:\\tmp\\mc --game 1.20.1 --loader fabric
    python tools/smoke_install.py --root D:\\tmp\\mc --game 1.20.1 --loader forge --loader-version 47.2.0

本脚本只用于开发期验证（会真实下载数百 MB 文件），不属于打包产物。
"""
from __future__ import annotations

import argparse
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cubefetch.core.http import HttpSession, Progress  # noqa: E402
from cubefetch.core.install import Installer  # noqa: E402
from cubefetch.core.loader import LoaderService  # noqa: E402
from cubefetch.core.manifest import ManifestService  # noqa: E402


class Printer:
    def __init__(self) -> None:
        self.last = 0.0
        self.start = time.monotonic()
        self.last_bytes = 0

    def __call__(self, progress: Progress) -> None:
        now = time.monotonic()
        if now - self.last < 1.0:
            return
        self.last = now
        speed = progress.speed or 0
        elapsed = now - self.start
        print(
            f"[{elapsed:7.1f}s] {progress.done:>4}/{progress.total:<4} "
            f"{progress.bytes_done/1048576:8.1f}/{progress.bytes_total/1048576:8.1f} MB "
            f"{(speed/1048576 if speed else 0):6.1f} MB/s  {progress.message[:90]}",
            flush=True,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, help="目标 .minecraft 目录")
    parser.add_argument("--game", default="1.20.1")
    parser.add_argument("--loader", default="vanilla", choices=["vanilla", "fabric", "quilt", "forge", "neoforge"])
    parser.add_argument("--loader-version", default="")
    parser.add_argument("--mirror", default="auto", choices=["auto", "bmclapi", "official"])
    parser.add_argument("--threads", type=int, default=32)
    args = parser.parse_args(argv)

    mc_root = Path(args.root)
    mc_root.mkdir(parents=True, exist_ok=True)
    http = HttpSession(mirror=args.mirror, workers=args.threads)
    manifest = ManifestService(http, cache_root=ROOT / ".cache" / "manifest")
    installer = Installer(http, manifest)
    loaders = LoaderService(http, installer, manifest)

    print(f"== 目标 {mc_root} | {args.game} | {args.loader} | mirror={http.mirror}")
    if args.loader != "vanilla":
        loader_version = args.loader_version or loaders.recommend(args.loader, args.game)
        if not loader_version:
            print("!! 找不到可用的加载器版本")
            return 2
        print(f"== 加载器版本 {loader_version}")
    else:
        loader_version = None

    loaded = time.monotonic()
    build = loaders.prepare(args.loader, loader_version, args.game)
    print(f"== prepare 完成 {time.monotonic()-loaded:.1f}s profile_id={build.profile_id}")

    cancel = threading.Event()
    printer = Printer()
    started = time.monotonic()
    profile_id = loaders.install(build, mc_root, progress_cb=printer, cancel=cancel)
    elapsed = time.monotonic() - started
    print(f"== 安装完成 profile_id={profile_id} 用时 {elapsed:.1f}s")

    version_dir = mc_root / "versions" / profile_id
    files = list(version_dir.rglob("*")) if version_dir.is_dir() else []
    total = sum(f.stat().st_size for f in files if f.is_file())
    print(f"== 版本目录 {version_dir}：{sum(1 for f in files if f.is_file())} 个文件 {total/1048576:.1f} MB")
    for name in ("libraries", "assets"):
        folder = mc_root / name
        count = sum(1 for f in folder.rglob("*") if f.is_file()) if folder.is_dir() else 0
        print(f"== {name}/：{count} 个文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
