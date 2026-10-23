"""真实网络验证：自己执行 Forge/NeoForge installer 的 processors。

该脚本使用与 ``LoaderService.install`` 相同的底层调用路径（下载安装器 jar、
准备原版基础、预置依赖库、执行 client processors、写版本 JSON），但默认不执行
最后的 ``Installer.install``（会下载完整 assets，数百 MB）；传 ``--full`` 可补跑。

用法（仓库根目录）::

    python tools/validate_processors_install.py --kind neoforge \
        --game 1.21.1 --loader 21.1.80 --root D:/Temp/CubeFetch_neo_self

退出码 0 表示校验通过。
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cubefetch.core import installer_processors  # noqa: E402
from cubefetch.core.http import HttpSession, Progress  # noqa: E402
from cubefetch.core.install import InstallRequest, Installer  # noqa: E402
from cubefetch.core.loader import LoaderService  # noqa: E402
from cubefetch.core.manifest import ManifestService  # noqa: E402


class Printer:
    """节流打印进度，避免下载进度刷屏。"""

    def __init__(self, interval: float = 2.0) -> None:
        self.interval = interval
        self.last_message = ""
        self.last_time = 0.0

    def __call__(self, progress: Progress) -> None:
        message = str(progress.message or "").strip()
        now = time.monotonic()
        if not message:
            return
        if message == self.last_message and now - self.last_time < self.interval * 5:
            return
        if message != self.last_message and now - self.last_time < 0.3:
            return
        self.last_message = message
        self.last_time = now
        print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def log(message: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {message}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="验证 processors 安装路径")
    parser.add_argument("--kind", choices=("forge", "neoforge"), required=True)
    parser.add_argument("--game", required=True, help="Minecraft 版本，如 1.21.1")
    parser.add_argument("--loader", required=True, help="Forge/NeoForge 版本")
    parser.add_argument("--root", required=True, help="目标 .minecraft 根目录")
    parser.add_argument("--java", default=None, help="指定 Java 可执行文件")
    parser.add_argument("--mirror", default="auto", choices=("auto", "bmclapi", "official"))
    parser.add_argument(
        "--full", action="store_true", help="额外执行 Installer.install 补齐 assets"
    )
    args = parser.parse_args(argv)

    kind = args.kind
    label = "Forge" if kind == "forge" else "NeoForge"
    game = args.game
    loader = args.loader
    root = Path(args.root)
    root.mkdir(parents=True, exist_ok=True)

    http = HttpSession(mirror=args.mirror)
    manifest = ManifestService(http)
    installer = Installer(http, manifest)
    service = LoaderService(http, installer, manifest)
    progress = Printer()

    log(f"=== {label} {loader} / MC {game} → {root} ===")
    started = time.monotonic()
    build = service.prepare(kind, loader, game)
    log(f"prepare: profile_id={build.profile_id} needs_installer={build.needs_installer}")

    jar_started = time.monotonic()
    jar_path = service._ensure_installer_jar(
        kind, game, loader, root, progress, None
    )
    log(
        f"installer jar: {jar_path.name} "
        f"({jar_path.stat().st_size} 字节, {time.monotonic() - jar_started:.1f}s)"
    )

    java_exe = Path(args.java) if args.java else service._resolve_java(None, game, label)
    log(f"java: {java_exe}")

    vanilla_json = manifest.fetch_version_json(game)
    log(f"vanilla version json: id={vanilla_json.get('id')}")

    processors_started = time.monotonic()
    profile_id, version_json = installer_processors.install_with_processors(
        http,
        jar_path,
        root,
        game_version=game,
        vanilla_version_json=vanilla_json,
        java_exe=java_exe,
        label=label,
        progress_cb=progress,
    )
    processors_elapsed = time.monotonic() - processors_started
    log(f"processors 完成：{profile_id}（{processors_elapsed:.1f}s）")

    checks: list[tuple[str, bool, str]] = []
    version_path = root / "versions" / profile_id / f"{profile_id}.json"
    checks.append(("版本 JSON", version_path.is_file(), str(version_path)))
    if version_path.is_file():
        import json

        data = json.loads(version_path.read_text(encoding="utf-8"))
        checks.append(
            ("inheritsFrom", data.get("inheritsFrom") == game, str(data.get("inheritsFrom")))
        )
        checks.append(("id 一致", data.get("id") == profile_id, str(data.get("id"))))

    profile = installer_processors.read_installer(jar_path)
    variables = installer_processors.resolve_variables(profile, mc_root=root)
    patched = Path(variables.get("PATCHED", ""))
    checks.append(
        (
            "patched client jar",
            patched.is_file() and patched.stat().st_size > 0,
            f"{patched} ({patched.stat().st_size if patched.is_file() else 0} 字节)",
        )
    )
    if kind == "neoforge":
        client_jar = (
            root
            / "libraries"
            / Path(installer_processors.maven_path(f"net.neoforged:neoforge:{loader}:client"))
        )
        checks.append(
            (
                "neoforge client jar",
                client_jar.is_file() and client_jar.stat().st_size > 0,
                f"{client_jar} ({client_jar.stat().st_size if client_jar.is_file() else 0} 字节)",
            )
        )
    vanilla_jar = root / "versions" / game / f"{game}.jar"
    checks.append(
        (
            "vanilla client jar",
            vanilla_jar.is_file() and vanilla_jar.stat().st_size > 0,
            f"{vanilla_jar} ({vanilla_jar.stat().st_size if vanilla_jar.is_file() else 0} 字节)",
        )
    )

    ok = all(passed for _, passed, _ in checks)
    for name, passed, detail in checks:
        log(f"  [{'OK' if passed else 'FAIL'}] {name}: {detail}")

    if args.full and ok:
        log("=== 继续执行 Installer.install 补齐继承库与 assets（可能较慢） ===")
        assets_started = time.monotonic()
        installer.install(
            InstallRequest(
                mc_root=root,
                version_id=profile_id,
                version_json=version_json,
                progress_cb=progress,
            )
        )
        log(f"Installer.install 完成（{time.monotonic() - assets_started:.1f}s）")

    total = time.monotonic() - started
    log(f"=== 结果：{'PASS' if ok else 'FAIL'}，总耗时 {total:.1f}s ===")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    raise SystemExit(main())
