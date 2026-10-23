"""服务层端到端冒烟：走 ``AppServices.install()`` 完整链路（含正版门禁与版本隔离）。

用法（开发期）：

    python -X utf8 tools\\smoke_services.py --root <目录> --game 1.20.1 [--kind vanilla]

注意：脚本会伪造一个「已登录且 token 未过期」的账号，只用于验证安装链路本身；
真实登录必须由用户在界面上完成（本脚本不联网验证账号）。
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

from cubefetch.config import AppConfig  # noqa: E402
from cubefetch.core.auth import Account  # noqa: E402


def _fake_account() -> Account:
    now = time.time()
    return Account(
        access_token="smoke-token",
        refresh_token="smoke-refresh",
        expires_at=now + 24 * 3600,
        uuid="00000000000000000000000000000000",
        name="SmokePlayer",
        verified_at=now,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CubeFetch 服务层 E2E 冒烟")
    parser.add_argument("--root", required=True, help="目标 .minecraft 根目录（会被写入）")
    parser.add_argument("--game", default="1.20.1")
    parser.add_argument("--kind", default="vanilla", choices=["vanilla", "fabric", "forge", "neoforge", "quilt"])
    parser.add_argument("--loader", default=None)
    parser.add_argument("--mirror", default="auto")
    parser.add_argument("--threads", type=int, default=32)
    args = parser.parse_args(argv)

    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    from cubefetch.services import AppServices

    config = AppConfig(mc_root=str(root), mirror=args.mirror, threads=args.threads)
    services = AppServices(config)
    services.bootstrap()
    services.account = _fake_account()
    services.ticket = services.gate.issue(services.account)

    print(f"== 目标 {root} | {args.game} | {args.kind} {args.loader or ''} | mirror={args.mirror}")
    print(
        "== 检测启动器："
        f"{[(p.kind, p.installed, str(p.mc_root)) for p in (services.detection.probes if services.detection else [])]}"
    )
    if services.detection is not None:
        print(
            f"   needs_choice={services.detection.needs_choice} "
            f"candidates={[str(p) for p in services.detection.candidate_roots]} "
            f"reason={services.detection.reason}"
        )

    last = [0.0]

    def progress(p) -> None:
        now = time.time()
        if now - last[0] < 1.0:
            return
        last[0] = now
        mb_done = p.bytes_done / 1024 / 1024
        mb_total = p.bytes_total / 1024 / 1024
        print(
            f"[{now - started:7.1f}s] {p.done}/{p.total} "
            f"{mb_done:.1f}/{mb_total:.1f} MB {p.speed / 1024 / 1024:.1f} MB/s {p.message}",
            flush=True,
        )

    started = time.time()
    loader_version = args.loader
    if args.kind != "vanilla" and not loader_version:
        loader_version = services.recommend_loader(args.kind, args.game)
        print(f"== 自动选择 {args.kind} 加载器版本：{loader_version}")
    try:
        profile_id = services.install(
            args.kind,
            loader_version,
            args.game,
            progress_cb=progress,
            cancel=threading.Event(),
        )
    except Exception as exc:  # noqa: BLE001
        print(f"!! 安装失败：{type(exc).__name__}: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    elapsed = time.time() - started

    version_json = root / "versions" / profile_id / f"{profile_id}.json"
    version_jar = root / "versions" / profile_id / f"{profile_id}.jar"
    libraries = list((root / "libraries").rglob("*.jar")) if (root / "libraries").is_dir() else []
    assets_index = list((root / "assets" / "indexes").glob("*.json")) if (root / "assets" / "indexes").is_dir() else []
    objects = list((root / "assets" / "objects").rglob("*")) if (root / "assets" / "objects").is_dir() else []
    total = sum(f.stat().st_size for f in root.rglob("*") if f.is_file())
    print()
    print(f"== 完成 profile_id={profile_id} 用时 {elapsed:.1f}s")
    print(f"   版本 JSON：{version_json.is_file()} ({version_json})")
    print(f"   版本 JAR ：{version_jar.is_file()}" + (f" ({version_jar.stat().st_size} 字节)" if version_jar.is_file() else ""))
    print(f"   libraries：{len(libraries)} 个 jar")
    print(f"   assets 索引：{[p.name for p in assets_index]}")
    print(f"   assets objects：{len([p for p in objects if p.is_file()])} 个文件")
    print(f"   目录总大小：{total / 1024 / 1024:.1f} MB")
    isolation_files = list((root / "versions" / profile_id).glob("PCL/Setup.ini")) + list(
        (root / "versions" / profile_id).glob("instance-game-settings.json")
    )
    print(f"   版本隔离文件：{[str(p.relative_to(root)) for p in isolation_files]}")
    print(f"   last_version={services.config.last_version} last_loader={services.config.last_loader}")

    ok = version_json.is_file() and bool(libraries)
    print("SMOKE PASS" if ok else "SMOKE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
