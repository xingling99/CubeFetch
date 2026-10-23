"""模组加载器（Fabric / Quilt / Forge / NeoForge）的列表、推荐、准备与安装。

接口按 ``docs/CONTRACTS.md`` 第 8 节实现：``LoaderBuild`` 描述待安装的加载器，
``LoaderService`` 负责查询官方元数据并完成安装。研究结论见
``docs/research/loader-notes.md`` 与 ``docs/research/ecosystem.md`` 的 C 节。

要点（VERIFIED，2026-10 实测/源码）：

* Fabric / Quilt：纯 HTTP。版本列表来自官方 meta；profile JSON 自带
  ``inheritsFrom``，写入 ``versions/<profile_id>/<profile_id>.json`` 后交给
  :class:`~cubefetch.core.install.Installer` 补齐继承依赖即可，无需安装器。
* Forge / NeoForge：下载官方 installer jar，用 ``java -jar ... --installClient
  <mc_root>`` 无头安装（Lead 实测 Forge 1.20.1 安装器接受该参数，会自行下载
  vanilla jar 与 libraries 并生成 ``versions/<id>/<id>.json``）。安装器支持
  ``--mirror <URL>``；国内网络直连 forge/neoforged maven 会大量超时（实测退
  出码 1 且报 "These libraries failed to download"），改用
  ``https://bmclapi2.bangbang93.com/maven/`` 后实测一次成功。安装完成后以
  安装器实际写出的 ``id`` 为准，不自己拼 profile_id；随后再用
  :class:`~cubefetch.core.install.Installer` 补齐继承链的资源与依赖（安装器
  只负责打补丁和写 profile，不下载 assets）。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from xml.etree import ElementTree

from . import installer_processors
from .errors import CancelledError, CubeFetchError, InstallError, NetworkError
from .http import BMCLAPI_BASE, HttpSession, Progress
from .install import InstallRequest, Installer
from .manifest import ManifestService
from .util import ensure_dir, pick_java, read_json, write_json

log = logging.getLogger("cubefetch.core.loader")

# ---------------------------------------------------------------------------
# 官方端点
# ---------------------------------------------------------------------------

FABRIC_META = "https://meta.fabricmc.net/v2"
QUILT_META = "https://meta.quiltmc.org/v3"
FORGE_PROMOTIONS_URL = (
    "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
)
FORGE_MAVEN_BASE = "https://maven.minecraftforge.net/net/minecraftforge/forge"
FORGE_METADATA_URL = f"{FORGE_MAVEN_BASE}/maven-metadata.xml"
NEOFORGE_MAVEN_BASE = "https://maven.neoforged.net/releases/net/neoforged"

#: 安装器进程超时（秒）。首次全量安装要下载 vanilla + 全部 libraries，给足时间。
INSTALLER_TIMEOUT = 1800.0

#: 传给 Forge/NeoForge 安装器的 Maven 镜像（BMCLAPI 覆盖 forge/neoforged 与
#: Maven Central 的 artifact；实测直连官方 maven 会大量超时导致安装失败）。
INSTALLER_MAVEN_MIRROR = f"{BMCLAPI_BASE}/maven/"

#: 安装器失败后的额外重试次数。安装器会把已下载文件留在 ``libraries/``，
#: 重跑可续传，因此重试代价远小于一次全新安装。
INSTALLER_RETRIES = 1

#: 加载器列表 / Forge promotions 的内存缓存时长（秒）。
_LIST_CACHE_TTL = 300.0

LOADER_KINDS = ("vanilla", "fabric", "quilt", "forge", "neoforge")

_POPULAR_ALIASES = {
    "neo": "neoforge",
    "neo-forge": "neoforge",
    "neo_forge": "neoforge",
    "原版": "vanilla",
    "forge": "forge",
}

#: 安装器输出里值得转成进度文案的关键行（小写匹配）。
_PROGRESS_HINTS = (
    "considering library",
    "downloading library",
    "download completed",
    "installing client",
    "extracting",
)


# ---------------------------------------------------------------------------
# 数据结构（契约 §8）
# ---------------------------------------------------------------------------


@dataclass
class LoaderBuild:
    """一次待安装的加载器版本。"""

    kind: str
    version: str | None
    game_version: str
    profile_id: str
    version_json: dict | None
    needs_installer: bool = False


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def _normalize_kind(kind: str) -> str:
    value = str(kind or "").strip().lower()
    value = _POPULAR_ALIASES.get(value, value)
    if value not in LOADER_KINDS:
        raise ValueError(f"未知的模组加载器类型：{kind!r}")
    return value


def _require_game_version(game_version: str) -> str:
    value = str(game_version or "").strip()
    if not value:
        raise ValueError("游戏版本号不能为空")
    return value


def _require_loader_version(loader_version: str | None) -> str:
    value = str(loader_version or "").strip()
    if not value:
        raise ValueError("加载器版本号不能为空")
    return value


def _version_tuple(version: str) -> tuple[int, ...]:
    """把 ``0.15.10`` / ``47.2.0`` 之类转成可比较的数字元组。"""
    parts = re.findall(r"\d+", str(version or ""))
    if not parts:
        return ()
    return tuple(int(part) for part in parts)


#: 预发布版本标记（Quilt 列表接口不返回 stable 字段，用版本号推断）。
_PRERELEASE_MARKS = ("alpha", "beta", "pre", "rc", "snapshot", "dev", "nightly")


def _looks_prerelease(version: str) -> bool:
    value = str(version or "").strip().lower()
    return any(mark in value for mark in _PRERELEASE_MARKS)


def _sort_loader_items(items: list[dict]) -> list[dict]:
    """稳定版优先，版本号倒序。"""
    return sorted(
        items,
        key=lambda item: (1 if item.get("stable") else 0, _version_tuple(item.get("version", ""))),
        reverse=True,
    )


def _safe_progress(progress_cb: Callable[[Progress], None] | None, progress: Progress) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(progress)
    except Exception:  # noqa: BLE001 - 进度回调异常不应中断安装
        log.debug("进度回调异常", exc_info=True)


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("操作已取消")


def _terminate_process(proc: "subprocess.Popen[str]") -> None:
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.wait(timeout=10)
    except Exception:  # noqa: BLE001
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


def _run_java_installer(
    java_exe: Path,
    jar_path: Path,
    mc_root: Path,
    *,
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    label: str = "Forge",
    timeout: float = INSTALLER_TIMEOUT,
    mirror_url: str | None = None,
) -> list[str]:
    """运行 Forge/NeoForge 安装器的无头客户端安装，返回输出行。

    ``java -Djava.awt.headless=true -jar <installer> --installClient <mc_root>``
    是 Lead 在本机实测可用的无头安装方式；``mirror_url`` 非空时追加
    ``--mirror <URL>``，把安装器的 Maven 下载指到镜像（见
    :data:`INSTALLER_MAVEN_MIRROR`）。工作目录放在临时目录，避免安装器往当前
    目录写垃圾。取消/超时都会杀掉子进程，非 0 退出码抛 :class:`InstallError`
    并附最后 10 行输出。
    """
    command = [
        str(java_exe),
        "-Djava.awt.headless=true",
        "-jar",
        str(jar_path),
        "--installClient",
        str(mc_root),
    ]
    if mirror_url:
        command.extend(["--mirror", str(mirror_url)])
    creationflags = 0
    if sys.platform == "win32" and hasattr(subprocess, "CREATE_NO_WINDOW"):
        creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW"))
    output: list[str] = []
    last_emit = 0.0
    proc: subprocess.Popen[str] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="cubefetch-installer-") as workdir:
            proc = subprocess.Popen(
                command,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                creationflags=creationflags,
            )
            started = time.monotonic()
            assert proc.stdout is not None
            for raw_line in proc.stdout:
                line = raw_line.rstrip("\r\n")
                if not line:
                    continue
                output.append(line)
                if len(output) > 4000:
                    del output[:2000]
                lowered = line.lower()
                if any(hint in lowered for hint in _PROGRESS_HINTS):
                    now = time.monotonic()
                    if now - last_emit >= 0.2:
                        last_emit = now
                        _safe_progress(
                            progress_cb,
                            Progress(message=f"正在安装 {label}：{line.strip()[:160]}"),
                        )
                if cancel is not None and cancel.is_set():
                    _terminate_process(proc)
                    raise CancelledError("安装已取消")
                if time.monotonic() - started > timeout:
                    _terminate_process(proc)
                    raise InstallError(
                        f"{label} 安装器执行超时（超过 {int(timeout)} 秒）",
                        detail="\n".join(output[-10:]),
                    )
            try:
                code = proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                _terminate_process(proc)
                raise InstallError(
                    f"{label} 安装器进程未正常退出",
                    detail="\n".join(output[-10:]),
                ) from None
    finally:
        if proc is not None and proc.poll() is None:
            _terminate_process(proc)
        if proc is not None and proc.stdout is not None:
            try:
                proc.stdout.close()
            except Exception:  # noqa: BLE001
                pass
    if cancel is not None and cancel.is_set():
        raise CancelledError("安装已取消")
    if code != 0:
        raise InstallError(
            f"{label} 安装器执行失败（退出码 {code}）",
            detail="\n".join(output[-10:]),
        )
    return output


def _neoforge_is_legacy(game_version: str) -> bool:
    """MC 1.20.1 的 NeoForge 仍发布在 ``net/neoforged/forge`` 下（例外）。"""
    return str(game_version or "").strip() == "1.20.1"


def neoforge_version_prefix(game_version: str) -> str:
    """推导 NeoForge 版本前缀（与 MC 版本的映射规则）。

    - MC ``1.20.2`` / ``1.21.1`` 这类：NeoForge 版本形如
      ``<mc-minor>.<mc-patch>.<build>`` → 前缀 ``20.2.`` / ``21.1.``；
      省略 patch 的版本（如 ``1.21``）按 patch=0 处理 → ``21.0.``。
    - MC ``26.3`` 这类新版本号：NeoForge 版本形如
      ``<major>.<minor>.0.<build>`` → 前缀 ``26.3.0.``。
    """
    numbers = [int(part) for part in re.findall(r"\d+", str(game_version or ""))]
    if len(numbers) >= 2 and numbers[0] == 1:
        minor = numbers[1]
        patch = numbers[2] if len(numbers) >= 3 else 0
        return f"{minor}.{patch}."
    if len(numbers) >= 2:
        return f"{numbers[0]}.{numbers[1]}.0."
    return ""


def _neoforge_artifact_version(game_version: str, loader_version: str) -> tuple[str, bool]:
    """返回 ``(maven 版本号, 是否 legacy net/neoforged/forge 路径)``。"""
    game = str(game_version or "").strip()
    loader = str(loader_version or "").strip()
    if _neoforge_is_legacy(game):
        if loader.startswith(f"{game}-"):
            return loader, True
        return f"{game}-{loader}", True
    return loader, False


# ---------------------------------------------------------------------------
# 对外服务（契约 §8）
# ---------------------------------------------------------------------------


class LoaderService:
    """查询模组加载器版本并安装。"""

    def __init__(
        self,
        http: HttpSession,
        installer: Installer,
        manifest: ManifestService,
        *,
        use_official_installer: bool | None = None,
    ) -> None:
        self.http = http
        self.installer = installer
        self.manifest = manifest
        # Forge/NeoForge 默认由本模块执行 install_profile 的 processors（官方
        # installer 的 DOWNLOAD_MOJMAPS 会硬编码 launchermeta 地址并在部分网络
        # 连接被重置）。传 True 强制走官方 installer（如官方镜像模式），传 False
        # 强制 processors；None 表示自动：http.mirror == "official" 时走官方。
        self.use_official_installer = use_official_installer
        # 官方 meta 的列表响应较大（Fabric 单次约 600 KB），短期内缓存避免
        # UI「先取列表再取推荐」重复请求。缓存不参与安装正确性判定。
        self._list_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
        self._promotions_cache: tuple[float, dict[str, str]] | None = None

    # -- 版本列表 / 推荐 ---------------------------------------------------

    def list_loaders(self, kind: str, game_version: str) -> list[dict]:
        """返回 ``[{version, stable, ...}]``；vanilla 返回空列表，稳定版优先、版本倒序。"""
        loader_kind = _normalize_kind(kind)
        game = _require_game_version(game_version)
        if loader_kind == "vanilla":
            return []
        cache_key = (loader_kind, game)
        cached = self._list_cache.get(cache_key)
        now = time.monotonic()
        if cached is not None and now - cached[0] <= _LIST_CACHE_TTL:
            return [dict(item) for item in cached[1]]
        if loader_kind == "fabric":
            items = self._list_fabric(game)
        elif loader_kind == "quilt":
            items = self._list_quilt(game)
        elif loader_kind == "forge":
            items = self._list_forge(game)
        else:
            items = self._list_neoforge(game)
        result = [dict(item) for item in items]
        self._list_cache[cache_key] = (time.monotonic(), result)
        return [dict(item) for item in result]

    def recommend(self, kind: str, game_version: str) -> str | None:
        """返回推荐版本号；没有可用版本时返回 ``None``。"""
        loader_kind = _normalize_kind(kind)
        game = _require_game_version(game_version)
        if loader_kind == "vanilla":
            return None
        if loader_kind == "forge":
            try:
                promotions = self._forge_promotions()
            except CubeFetchError:
                promotions = {}
            for suffix in ("recommended", "latest"):
                value = promotions.get(f"{game}-{suffix}")
                if isinstance(value, str) and value.strip():
                    return value.strip()
            items = self._list_forge(game)
            return str(items[0]["version"]) if items else None
        items = self.list_loaders(loader_kind, game)
        return str(items[0]["version"]) if items else None

    # -- prepare ----------------------------------------------------------

    def prepare(
        self,
        kind: str,
        loader_version: str | None,
        game_version: str,
    ) -> LoaderBuild:
        """准备安装所需的版本信息（Fabric/Quilt 会提前拉取 profile JSON）。"""
        loader_kind = _normalize_kind(kind)
        game = _require_game_version(game_version)

        if loader_kind == "vanilla":
            version_json = self.manifest.fetch_version_json(game)
            if not isinstance(version_json, dict) or not version_json:
                raise InstallError(
                    f"无法获取原版 {game} 的版本信息", detail="manifest.fetch_version_json 返回空"
                )
            return LoaderBuild(
                kind="vanilla",
                version=None,
                game_version=game,
                profile_id=game,
                version_json=version_json,
                needs_installer=False,
            )

        loader = _require_loader_version(loader_version)
        if loader_kind in ("fabric", "quilt"):
            profile_id = f"{game}-{loader_kind}-{loader}"
            profile = self._fetch_profile_json(loader_kind, game, loader)
            if not isinstance(profile, dict) or not profile:
                raise InstallError(
                    f"获取 {loader_kind} 安装配置失败（返回内容为空）",
                    detail=f"{loader_kind} {loader} / {game}",
                )
            if not profile.get("inheritsFrom"):
                raise InstallError(
                    f"{loader_kind} 安装配置缺少 inheritsFrom 字段，无法安装",
                    detail=str(profile.get("id")),
                )
            version_json = dict(profile)
            version_json["id"] = profile_id
            return LoaderBuild(
                kind=loader_kind,
                version=loader,
                game_version=game,
                profile_id=profile_id,
                version_json=version_json,
                needs_installer=False,
            )

        # Forge / NeoForge：需要下载并运行官方 installer。
        profile_id = f"{game}-{loader_kind}-{loader}"
        return LoaderBuild(
            kind=loader_kind,
            version=loader,
            game_version=game,
            profile_id=profile_id,
            version_json=None,
            needs_installer=True,
        )

    # -- install ----------------------------------------------------------

    def install(
        self,
        build: LoaderBuild,
        mc_root: Path,
        *,
        java_exe: Path | None = None,
        progress_cb: Callable[[Progress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> str:
        """安装 ``build``，返回实际写入的 profile id（``versions/<profile_id>``）。"""
        if not isinstance(build, LoaderBuild):
            raise ValueError("install() 需要 LoaderBuild 对象")
        loader_kind = _normalize_kind(build.kind)
        mc_root = Path(mc_root)
        _raise_if_cancelled(cancel)
        if loader_kind == "vanilla":
            return self._install_vanilla(build, mc_root, progress_cb, cancel)
        if loader_kind in ("fabric", "quilt"):
            return self._install_profile(build, mc_root, progress_cb, cancel)
        if self.should_use_official_installer():
            return self._install_with_installer(
                build, mc_root, java_exe=java_exe, progress_cb=progress_cb, cancel=cancel
            )
        return self._install_with_processors(
            build, mc_root, java_exe=java_exe, progress_cb=progress_cb, cancel=cancel
        )

    def should_use_official_installer(self) -> bool:
        """是否使用官方 ``--installClient`` 安装器路径。

        显式设置优先；未设置时仅当下载镜像模式为 ``official`` 才走官方安装器，
        默认（auto/bmclapi）使用自己执行 processors 的路径。
        """
        if self.use_official_installer is not None:
            return bool(self.use_official_installer)
        mode = str(getattr(self.http, "mirror", "auto") or "auto").strip().lower()
        return mode == "official"

    # -- vanilla ----------------------------------------------------------

    def _install_vanilla(
        self,
        build: LoaderBuild,
        mc_root: Path,
        progress_cb: Callable[[Progress], None] | None,
        cancel: threading.Event | None,
    ) -> str:
        if not build.version_json:
            raise InstallError(
                "缺少原版版本 JSON，无法安装", detail=str(build.game_version)
            )
        profile_id = build.profile_id or build.game_version
        json_path = mc_root / "versions" / profile_id / f"{profile_id}.json"
        _safe_progress(progress_cb, Progress(message=f"正在安装原版 {build.game_version}…"))
        req = InstallRequest(
            mc_root=mc_root,
            version_id=profile_id,
            version_json=dict(build.version_json),
            progress_cb=progress_cb,
            cancel=cancel,
        )
        _raise_if_cancelled(cancel)
        self.installer.install(req)
        _raise_if_cancelled(cancel)
        if not json_path.is_file():
            raise InstallError(
                f"原版安装完成但未找到版本文件：{json_path}", detail=profile_id
            )
        return profile_id

    # -- Fabric / Quilt ---------------------------------------------------

    def _install_profile(
        self,
        build: LoaderBuild,
        mc_root: Path,
        progress_cb: Callable[[Progress], None] | None,
        cancel: threading.Event | None,
    ) -> str:
        if not build.version_json:
            raise InstallError(
                f"{build.kind} 安装配置为空，无法安装", detail=str(build.profile_id)
            )
        profile_id = str(build.profile_id or "").strip()
        if not profile_id:
            raise InstallError(f"{build.kind} 缺少 profile_id")
        label = "Fabric" if build.kind == "fabric" else "Quilt"
        _safe_progress(
            progress_cb,
            Progress(message=f"正在安装 {label} {build.version}（{build.game_version}）…"),
        )
        # 先把 profile JSON 落盘，其他启动器/本进程都能看到该版本；随后
        # Installer 会按 inheritsFrom 自动拉取原版依赖（若原版未安装）。
        version_dir = mc_root / "versions" / profile_id
        ensure_dir(version_dir)
        write_json(version_dir / f"{profile_id}.json", dict(build.version_json))
        _raise_if_cancelled(cancel)
        req = InstallRequest(
            mc_root=mc_root,
            version_id=profile_id,
            version_json=dict(build.version_json),
            progress_cb=progress_cb,
            cancel=cancel,
        )
        self.installer.install(req)
        _raise_if_cancelled(cancel)
        json_path = version_dir / f"{profile_id}.json"
        if not json_path.is_file():
            raise InstallError(f"{label} 安装后未找到版本文件：{json_path}", detail=profile_id)
        return profile_id

    # -- Forge / NeoForge -------------------------------------------------

    def _install_with_processors(
        self,
        build: LoaderBuild,
        mc_root: Path,
        *,
        java_exe: Path | None,
        progress_cb: Callable[[Progress], None] | None,
        cancel: threading.Event | None,
    ) -> str:
        """自己执行 installer 的 install_profile processors 完成安装。

        这样可绕过官方 installer 中 DOWNLOAD_MOJMAPS 硬编码
        ``launchermeta.mojang.com`` 的问题；成功后仍需调用 Installer 补齐
        assets 与继承的原版依赖。
        """
        kind = _normalize_kind(build.kind)
        label = "Forge" if kind == "forge" else "NeoForge"
        loader = _require_loader_version(build.version)
        game = _require_game_version(build.game_version)

        existing = self._find_existing_profile(mc_root, build)
        if existing:
            _safe_progress(
                progress_cb,
                Progress(message=f"{label} {loader} 已安装（{existing}），跳过重复安装"),
            )
            return existing

        # 与其他启动器保持一致：安装好的版本根目录至少要有 versions/。
        ensure_dir(mc_root / "versions")
        launcher_profiles = mc_root / "launcher_profiles.json"
        if not launcher_profiles.is_file():
            write_json(launcher_profiles, {"profiles": {}, "settings": {}, "version": 3})

        jar_path = self._ensure_installer_jar(
            kind, game, loader, mc_root, progress_cb, cancel
        )
        java_path = self._resolve_java(java_exe, game, label)

        _safe_progress(
            progress_cb,
            Progress(message=f"正在获取原版 {game} 版本信息…"),
        )
        try:
            vanilla_json = self.manifest.fetch_version_json(game)
        except CancelledError:
            raise
        except CubeFetchError as exc:
            raise InstallError(
                f"无法获取原版 {game} 的版本信息，无法安装 {label}",
                detail=str(exc),
            ) from exc

        try:
            profile_id, version_json = installer_processors.install_with_processors(
                self.http,
                jar_path,
                mc_root,
                game_version=game,
                vanilla_version_json=vanilla_json,
                java_exe=java_path,
                label=label,
                progress_cb=progress_cb,
                cancel=cancel,
                timeout=INSTALLER_TIMEOUT,
            )
        except CancelledError:
            raise
        except InstallError:
            raise
        except CubeFetchError as exc:
            raise InstallError(
                f"{label} 安装过程中网络请求失败，请检查网络或切换下载镜像",
                detail=str(exc),
            ) from exc
        except OSError as exc:
            raise InstallError(f"{label} 安装过程中文件操作失败", detail=str(exc)) from exc
        _raise_if_cancelled(cancel)

        profile_id = str(profile_id or "").strip()
        json_path = mc_root / "versions" / profile_id / f"{profile_id}.json"
        if not profile_id or not json_path.is_file():
            raise InstallError(
                f"{label} 安装后未找到版本文件：{json_path}", detail=profile_id
            )
        final_json = read_json(json_path, None)
        if not isinstance(final_json, dict) or not final_json:
            final_json = dict(version_json or {})
        _safe_progress(
            progress_cb,
            Progress(message=f"正在补齐 {profile_id} 的原版依赖与资源文件…"),
        )
        self.installer.install(
            InstallRequest(
                mc_root=mc_root,
                version_id=profile_id,
                version_json=dict(final_json),
                progress_cb=progress_cb,
                cancel=cancel,
            )
        )
        _raise_if_cancelled(cancel)
        _safe_progress(progress_cb, Progress(message=f"{label} 安装完成：{profile_id}"))
        return profile_id

    def _ensure_installer_jar(
        self,
        kind: str,
        game: str,
        loader: str,
        mc_root: Path,
        progress_cb: Callable[[Progress], None] | None,
        cancel: threading.Event | None,
    ) -> Path:
        """返回安装器 jar 路径；必要时先下载到 ``.cubefetch/installers``。"""
        label = "Forge" if kind == "forge" else "NeoForge"
        url, jar_name = self._installer_jar(kind, game, loader)
        jar_path = self._installer_cache_dir(mc_root) / jar_name
        if jar_path.is_file():
            _safe_progress(
                progress_cb, Progress(message=f"复用已下载的 {label} 安装器：{jar_name}")
            )
            return jar_path
        _safe_progress(progress_cb, Progress(message=f"正在下载 {label} 安装器…"))
        self.http.download(
            url,
            jar_path,
            use_mirror=True,
            progress_cb=self._byte_progress(progress_cb, label),
            cancel=cancel,
        )
        _raise_if_cancelled(cancel)
        if not jar_path.is_file():
            raise InstallError(f"下载 {label} 安装器失败", detail=str(jar_path))
        return jar_path

    def _resolve_java(self, java_exe: Path | None, game: str, label: str) -> Path:
        """选择运行安装补丁的 Java：显式指定 > 推荐大版本 > 任意可用版本。"""
        if java_exe is not None:
            path = Path(java_exe)
            if not path.is_file():
                raise InstallError(
                    f"指定的 Java 不存在，无法运行 {label} 安装程序", detail=str(path)
                )
            return path
        required = installer_processors.required_java_major(game)
        candidate = pick_java(required) or pick_java(17) or pick_java()
        if candidate is None or not Path(candidate).is_file():
            raise InstallError(
                f"未找到可用的 Java，无法安装 {label}（请先在设置中指定 Java）",
                detail=f"需要 Java {required} 或更高版本",
            )
        return Path(candidate)

    # -- Forge / NeoForge：官方 installer 回退路径 ------------------------

    def _install_with_installer(
        self,
        build: LoaderBuild,
        mc_root: Path,
        *,
        java_exe: Path | None,
        progress_cb: Callable[[Progress], None] | None,
        cancel: threading.Event | None,
    ) -> str:
        kind = build.kind
        label = "Forge" if kind == "forge" else "NeoForge"
        loader = _require_loader_version(build.version)
        game = _require_game_version(build.game_version)

        existing = self._find_existing_profile(mc_root, build)
        if existing:
            _safe_progress(
                progress_cb, Progress(message=f"{label} {loader} 已安装（{existing}），跳过重复安装")
            )
            return existing

        # 安装器要求目标目录里有 versions/ 与 launcher_profiles.json。
        ensure_dir(mc_root / "versions")
        launcher_profiles = mc_root / "launcher_profiles.json"
        if not launcher_profiles.is_file():
            write_json(launcher_profiles, {"profiles": {}, "settings": {}, "version": 3})

        jar_path = self._ensure_installer_jar(
            kind, game, loader, mc_root, progress_cb, cancel
        )

        java_path = self._resolve_java(java_exe, game, label)

        _safe_progress(
            progress_cb,
            Progress(
                message=(
                    f"正在运行 {label} 安装器（首次安装需要下载原版与依赖，"
                    "耗时较长，请耐心等待）…"
                )
            ),
        )
        mirror_url = self._installer_mirror()
        attempts = 1 + max(0, int(INSTALLER_RETRIES))
        lines: list[str] = []
        last_error: InstallError | None = None
        profile_id: str | None = None
        for attempt in range(1, attempts + 1):
            _raise_if_cancelled(cancel)
            if attempt > 1:
                _safe_progress(
                    progress_cb,
                    Progress(
                        message=(
                            f"{label} 安装器上次执行失败，正在重试"
                            f"（{attempt}/{attempts}，已下载文件会复用）…"
                        )
                    ),
                )
            try:
                lines = _run_java_installer(
                    Path(java_path),
                    jar_path,
                    mc_root,
                    progress_cb=progress_cb,
                    cancel=cancel,
                    label=label,
                    mirror_url=mirror_url,
                )
            except CancelledError:
                raise
            except InstallError as exc:
                last_error = exc
                log.debug("%s 安装器第 %s 次执行失败：%s", label, attempt, exc)
                continue
            _raise_if_cancelled(cancel)
            profile_id = self._find_existing_profile(mc_root, build)
            if profile_id is not None:
                break

        if profile_id is None:
            detail = ""
            if last_error is not None and str(last_error.detail or "").strip():
                detail = str(last_error.detail)
            else:
                detail = "\n".join(lines[-10:])
            raise InstallError(
                f"{label} 安装器执行完成，但未找到生成的版本文件",
                detail=detail,
            )

        # 安装器只负责打补丁并写 profile：原版 jar/libraries/assets 由 Installer
        # 补齐（幂等，已存在的文件会被跳过），否则其他启动器启动时仍要联网。
        profile_json = read_json(
            mc_root / "versions" / profile_id / f"{profile_id}.json", None
        )
        if isinstance(profile_json, dict) and profile_json:
            _safe_progress(
                progress_cb,
                Progress(message=f"正在补齐 {profile_id} 的原版依赖与资源文件…"),
            )
            self.installer.install(
                InstallRequest(
                    mc_root=mc_root,
                    version_id=profile_id,
                    version_json=dict(profile_json),
                    progress_cb=progress_cb,
                    cancel=cancel,
                )
            )
            _raise_if_cancelled(cancel)
        _safe_progress(progress_cb, Progress(message=f"{label} 安装完成：{profile_id}"))
        return profile_id

    def _find_existing_profile(self, mc_root: Path, build: LoaderBuild) -> str | None:
        """查找安装器可能写出的版本 id（以 json 的 id / 目录名为准）。"""
        versions_dir = mc_root / "versions"
        if not versions_dir.is_dir():
            return None
        predicted = str(build.profile_id or "")
        if predicted and self._profile_matches(mc_root, predicted, build):
            return predicted
        try:
            children = sorted(versions_dir.iterdir(), key=lambda p: p.name.lower())
        except OSError:  # pragma: no cover - 目录不可读
            return None
        for child in children:
            if child.is_dir() and self._profile_matches(mc_root, child.name, build):
                return child.name
        return None

    def _profile_matches(self, mc_root: Path, profile_id: str, build: LoaderBuild) -> bool:
        version_dir = mc_root / "versions" / profile_id
        data = read_json(version_dir / f"{profile_id}.json", None)
        if not isinstance(data, dict):
            return False
        if str(data.get("inheritsFrom") or "").strip() != str(build.game_version).strip():
            return False
        ident = str(data.get("id") or profile_id).strip().lower()
        loader = str(build.version or "").strip().lower()
        if build.kind == "forge":
            return "forge" in ident and (not loader or loader in ident)
        return "neoforge" in ident or "neo" in ident

    def _installer_jar(self, kind: str, game: str, loader: str) -> tuple[str, str]:
        if kind == "forge":
            artifact = f"{game}-{loader}"
            filename = f"forge-{artifact}-installer.jar"
            url = f"{FORGE_MAVEN_BASE}/{artifact}/{filename}"
            return url, filename
        version, legacy = _neoforge_artifact_version(game, loader)
        if legacy:
            filename = f"forge-{version}-installer.jar"
            url = f"{NEOFORGE_MAVEN_BASE}/forge/{version}/{filename}"
        else:
            filename = f"neoforge-{version}-installer.jar"
            url = f"{NEOFORGE_MAVEN_BASE}/neoforge/{version}/{filename}"
        return url, filename

    def _installer_cache_dir(self, mc_root: Path) -> Path:
        target = mc_root / ".cubefetch" / "installers"
        ensure_dir(target)
        return target

    def _installer_mirror(self) -> str | None:
        """安装器使用的 Maven 镜像；用户选「官方」模式时返回 ``None``。"""
        mode = str(getattr(self.http, "mirror", "auto") or "auto").strip().lower()
        if mode == "official":
            return None
        return INSTALLER_MAVEN_MIRROR

    def _byte_progress(
        self,
        progress_cb: Callable[[Progress], None] | None,
        label: str,
    ) -> Callable[[int], None] | None:
        if progress_cb is None:
            return None

        def _on_bytes(done: int) -> None:
            _safe_progress(
                progress_cb,
                Progress(bytes_done=int(done or 0), message=f"正在下载 {label} 安装器…"),
            )

        return _on_bytes

    # -- 版本列表实现 ------------------------------------------------------

    def _list_fabric(self, game: str) -> list[dict]:
        data = self._fetch_json(f"{FABRIC_META}/versions/loader/{game}")
        items: list[dict] = []
        seen: set[str] = set()
        for raw in data if isinstance(data, list) else []:
            if not isinstance(raw, dict):
                continue
            # 官方响应：每项是 {"loader": {"version", "stable", ...}, "intermediary": {...}}；
            # 兼容早期/镜像返回的扁平 {"version", "stable"} 形式。
            loader_info = raw.get("loader")
            if isinstance(loader_info, dict):
                version = str(loader_info.get("version") or "").strip()
                stable = bool(loader_info.get("stable"))
            else:
                version = str(raw.get("version") or "").strip()
                stable = bool(raw.get("stable"))
            if not version or version in seen:
                continue
            seen.add(version)
            items.append({"version": version, "stable": stable})
        return _sort_loader_items(items)

    def _list_quilt(self, game: str) -> list[dict]:
        data = self._fetch_json(f"{QUILT_META}/versions/loader/{game}")
        items: list[dict] = []
        seen: set[str] = set()
        for raw in data if isinstance(data, list) else []:
            if not isinstance(raw, dict):
                continue
            # Quilt 官方响应同样是 {"loader": {...}}；列表接口没有 stable 字段，
            # 用版本号里的 alpha/beta/rc 等标记推断（官方稳定版不带这些标记）。
            loader_info = raw.get("loader")
            if isinstance(loader_info, dict):
                version = str(loader_info.get("version") or "").strip()
                if "stable" in loader_info:
                    stable = bool(loader_info.get("stable"))
                else:
                    stable = not _looks_prerelease(version)
            else:
                version = str(raw.get("version") or "").strip()
                stable = bool(raw.get("stable", not _looks_prerelease(version)))
            if not version or version in seen:
                continue
            seen.add(version)
            items.append({"version": version, "stable": stable})
        return _sort_loader_items(items)

    def _list_forge(self, game: str) -> list[dict]:
        items: dict[str, dict] = {}
        promotions: dict[str, str] = {}
        error: CubeFetchError | None = None
        try:
            promotions = self._forge_promotions()
        except CubeFetchError as exc:
            error = exc
        for suffix in ("recommended", "latest"):
            value = promotions.get(f"{game}-{suffix}")
            if isinstance(value, str) and value.strip():
                version = value.strip()
                items[version] = {"version": version, "stable": True}
        try:
            versions = self._maven_versions(FORGE_METADATA_URL)
        except CubeFetchError as exc:
            error = error or exc
            versions = []
        prefix = f"{game}-"
        for version in versions:
            if not version.startswith(prefix):
                continue
            loader = version[len(prefix):]
            if loader and loader not in items:
                items[loader] = {"version": loader, "stable": False}
        if not items:
            raise NetworkError(
                "获取 Forge 版本列表失败，请检查网络或镜像设置",
                detail=str(error or ""),
            )
        return _sort_loader_items(list(items.values()))

    def _list_neoforge(self, game: str) -> list[dict]:
        try:
            if _neoforge_is_legacy(game):
                versions = self._maven_versions(f"{NEOFORGE_MAVEN_BASE}/forge/maven-metadata.xml")
                prefix = f"{game}-"
                matched = [version for version in versions if version.startswith(prefix)]
                items = [
                    {"version": version[len(prefix):], "stable": True}
                    for version in matched
                    if version[len(prefix):]
                ]
            else:
                prefix = neoforge_version_prefix(game)
                if not prefix:
                    return []
                versions = self._maven_versions(
                    f"{NEOFORGE_MAVEN_BASE}/neoforge/maven-metadata.xml"
                )
                items = [{"version": version, "stable": True} for version in versions if version.startswith(prefix)]
        except CubeFetchError as exc:
            raise NetworkError(
                "获取 NeoForge 版本列表失败，请检查网络或镜像设置",
                detail=str(exc),
            ) from exc
        return _sort_loader_items(items)

    # -- 元数据抓取 --------------------------------------------------------

    def _fetch_json(self, url: str) -> Any:
        """抓 JSON；官方 meta 的列表/对象都支持（优先 get_json，失败回退原始字节）。"""
        getter = getattr(self.http, "get_json", None)
        if callable(getter):
            try:
                return getter(url)
            except CubeFetchError as exc:
                if getattr(self.http, "get_bytes", None) is None:
                    raise
                log.debug("get_json 失败，回退 get_bytes：%s（%s）", url, exc)
        raw = self.http.get_bytes(url)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(f"解析服务器返回的 JSON 失败：{url}", detail=str(exc)) from exc

    def _fetch_profile_json(self, kind: str, game: str, loader: str) -> dict:
        if kind == "fabric":
            url = f"{FABRIC_META}/versions/loader/{game}/{loader}/profile/json"
        else:
            url = f"{QUILT_META}/versions/loader/{game}/{loader}/profile/json"
        data = self._fetch_json(url)
        if not isinstance(data, dict):
            raise InstallError(
                f"获取 {kind} 安装配置失败（返回内容不是 JSON 对象）", detail=url
            )
        return data

    def _maven_versions(self, metadata_url: str) -> list[str]:
        raw = self.http.get_bytes(metadata_url)
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise NetworkError(
                "解析 Maven 元数据失败（编码错误）", detail=f"{metadata_url}: {exc}"
            ) from exc
        try:
            root = ElementTree.fromstring(text)
        except ElementTree.ParseError as exc:
            raise NetworkError(
                "解析 Maven 元数据失败（XML 格式错误）", detail=f"{metadata_url}: {exc}"
            ) from exc
        versions: list[str] = []
        seen: set[str] = set()
        for element in root.iter():
            tag = str(element.tag).rsplit("}", 1)[-1]
            if tag != "version" or not element.text:
                continue
            value = element.text.strip()
            if value and value not in seen:
                seen.add(value)
                versions.append(value)
        return versions

    def _forge_promotions(self) -> dict[str, str]:
        if self._promotions_cache is not None:
            stamp, cached = self._promotions_cache
            if time.monotonic() - stamp <= _LIST_CACHE_TTL:
                return dict(cached)
        data = self._fetch_json(FORGE_PROMOTIONS_URL)
        promotions = data.get("promotions") if isinstance(data, dict) else None
        if not isinstance(promotions, dict):
            promotions = data if isinstance(data, dict) else {}
        result: dict[str, str] = {}
        for key, value in promotions.items():
            if isinstance(key, str) and isinstance(value, str) and value.strip():
                result[key] = value.strip()
        self._promotions_cache = (time.monotonic(), result)
        return dict(result)


__all__ = ["LoaderBuild", "LoaderService", "neoforge_version_prefix"]
