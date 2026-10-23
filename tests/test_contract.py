"""契约一致性测试（Lead 维护）。

任何人改动公共接口前都应先看 docs/CONTRACTS.md；本文件只校验「接口形状」，
不校验业务行为（业务行为见各自模块的 tests/test_<module>.py）。
"""
from __future__ import annotations

import dataclasses
import inspect
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _import(name: str):
    return pytest.importorskip(f"cubefetch.{name}", reason=f"模块 {name} 尚未就绪")


def _params(fn) -> inspect.Signature:
    return inspect.signature(fn)


def _has_params(fn, *names: str) -> bool:
    return all(n in _params(fn).parameters for n in names)


def _kwonly(fn, *names: str) -> bool:
    kinds = inspect.Parameter
    sig = _params(fn)
    return all(
        sig.parameters[n].kind
        in (kinds.KEYWORD_ONLY, kinds.POSITIONAL_OR_KEYWORD, kinds.VAR_KEYWORD)
        for n in names
    )


def _fields(cls) -> set[str]:
    assert dataclasses.is_dataclass(cls), f"{cls} 不是 dataclass"
    return {f.name for f in dataclasses.fields(cls)}


# --------------------------------------------------------------------------- #
# 基础模块
# --------------------------------------------------------------------------- #
def test_config_and_errors():
    from cubefetch import config

    for attr in ("APP_NAME", "APP_NAME_CN", "APP_VERSION"):
        assert isinstance(getattr(config, attr), str)
    cfg = config.AppConfig()
    for field in ("mc_root", "launcher", "mirror", "threads", "remember_days", "java_path"):
        assert hasattr(cfg, field), f"AppConfig 缺少 {field}"
    assert cfg.threads >= 1 and cfg.remember_days == 30
    assert callable(config.appdata_dir) and callable(config.cache_dir)

    from cubefetch.core import errors

    for name in ("CubeFetchError", "NetworkError", "AuthError", "InstallError", "CancelledError"):
        cls = getattr(errors, name)
        assert issubclass(cls, Exception)
    err = errors.CubeFetchError("给用户看的错误")
    assert str(err) or err.user_message


# --------------------------------------------------------------------------- #
# core/http.py [install-engine]
# --------------------------------------------------------------------------- #
def test_http_contract():
    http = _import("core.http")
    from cubefetch.core.http import (
        BMCLAPI_BASE,
        MIRROR_RULES,
        DownloadTask,
        HttpSession,
        Progress,
        ThreadedDownloader,
        mirror_url,
    )

    assert BMCLAPI_BASE.startswith("https://")

    assert {"done", "total", "bytes_done", "bytes_total", "speed", "message"} <= _fields(Progress)
    assert {"url", "dest", "sha1", "size", "use_mirror"} <= _fields(DownloadTask)

    init = _params(HttpSession.__init__)
    for kw in ("mirror", "timeout", "retries", "user_agent", "workers"):
        assert kw in init.parameters, f"HttpSession.__init__ 缺少参数 {kw}"

    assert _has_params(HttpSession.get_json, "url")
    assert _has_params(HttpSession.get_bytes, "url")
    assert _has_params(HttpSession.download, "url", "dest")
    assert _kwonly(
        HttpSession.download, "sha1", "size", "use_mirror", "timeout", "progress_cb", "cancel"
    )
    hs = HttpSession()
    try:
        assert hs.mirror in ("auto", "official", "bmclapi")
        assert hs.workers >= 1
        assert hs.session is not None
        for meth in ("set_mirror", "clear_mirror_memo", "close"):
            assert callable(getattr(hs, meth, None)), f"HttpSession 缺少 {meth}()"
    finally:
        hs.close()

    assert _has_params(ThreadedDownloader.__init__, "http", "workers")
    assert _has_params(ThreadedDownloader.run, "tasks")
    assert _kwonly(ThreadedDownloader.run, "progress_cb", "cancel", "stage")

    # 镜像重写：必须保留完整 path，且覆盖 neoforge maven
    assert (
        mirror_url("https://piston-meta.mojang.com/mc/game/version_manifest_v2.json")
        == f"{BMCLAPI_BASE}/mc/game/version_manifest_v2.json"
    )
    assert mirror_url("https://libraries.minecraft.net/a/b.jar") == f"{BMCLAPI_BASE}/maven/a/b.jar"
    assert (
        mirror_url("https://resources.download.minecraft.net/ab/abcdef")
        == f"{BMCLAPI_BASE}/assets/ab/abcdef"
    )
    assert mirror_url("https://meta.fabricmc.net/v2/versions/loader") == (
        f"{BMCLAPI_BASE}/fabric-meta/v2/versions/loader"
    )
    assert mirror_url("https://maven.neoforged.net/releases/net/neoforged/neoforge/x.jar") == (
        f"{BMCLAPI_BASE}/maven/net/neoforged/neoforge/x.jar"
    )
    assert any("neoforged" in p for p, _ in MIRROR_RULES)
    assert mirror_url("https://example.com/keep/me") == "https://example.com/keep/me"


# --------------------------------------------------------------------------- #
# core/manifest.py [install-engine]
# --------------------------------------------------------------------------- #
def test_manifest_contract():
    m = _import("core.manifest")
    from cubefetch.core.manifest import (
        CATEGORY_APRIL,
        CATEGORY_OLD,
        CATEGORY_RELEASE,
        CATEGORY_SNAPSHOT,
        ManifestService,
        VersionEntry,
    )

    assert (CATEGORY_RELEASE, CATEGORY_SNAPSHOT, CATEGORY_OLD, CATEGORY_APRIL) == (
        "release",
        "snapshot",
        "old",
        "aprilfool",
    )
    assert {"id", "type", "url", "release_time", "category"} <= _fields(VersionEntry)

    init = _params(ManifestService.__init__)
    assert "http" in init.parameters
    for meth in ("load", "refresh", "categories", "fetch_version_json"):
        assert callable(getattr(ManifestService, meth, None)), f"ManifestService 缺少 {meth}()"
    assert m is not None  # 保持引用，避免 unused 警告


def test_manifest_april_fools_list():
    from cubefetch.core import manifest as m

    april = getattr(m, "APRIL_FOOLS_VERSIONS", None) or getattr(m, "APRIL_FOOLS", None)
    assert april is not None, "manifest 缺少愚人节版本清单常量"
    expected = {
        "15w14a",
        "1.RV-Pre1",
        "3D Shareware v1.34",
        "20w14infinite",
        "22w13oneblockatatime",
        "23w13a_or_b",
        "24w14potato",
        "25w14craftmine",
        "26w14a",
    }
    assert expected <= set(april)


# --------------------------------------------------------------------------- #
# core/install.py [install-engine]
# --------------------------------------------------------------------------- #
def test_install_contract():
    _import("core.install")
    from cubefetch.core.install import InstallRequest, Installer

    assert {"mc_root", "version_id", "version_json", "workers", "progress_cb", "cancel"} <= _fields(
        InstallRequest
    )
    init = _params(Installer.__init__)
    assert "http" in init.parameters and "manifest" in init.parameters
    assert "gate" in init.parameters, "Installer 必须接受 gate 参数（防绕过）"
    assert callable(Installer.plan) and callable(Installer.install)
    assert _has_params(Installer.install, "req")


# --------------------------------------------------------------------------- #
# core/detect.py + isolation.py + loader.py [loader-integration]
# --------------------------------------------------------------------------- #
def test_detect_contract():
    _import("core.detect")
    from cubefetch.core.detect import DetectionResult, LauncherDetector, LauncherProbe

    assert {"kind", "installed", "exe_hint", "config_path", "mc_root", "managed_versions"} <= _fields(
        LauncherProbe
    )
    assert {"probes", "candidate_roots", "default_root", "needs_choice", "reason"} <= _fields(
        DetectionResult
    )
    assert callable(LauncherDetector.detect)


def test_isolation_contract():
    _import("core.isolation")
    from cubefetch.core.isolation import VersionIsolation

    assert _has_params(VersionIsolation.apply, "mc_root", "version_id", "launcher_kinds")
    assert _has_params(VersionIsolation.is_isolated, "mc_root", "version_id", "launcher_kinds")


def test_loader_contract():
    _import("core.loader")
    from cubefetch.core.loader import LoaderBuild, LoaderService

    assert {"kind", "version", "game_version", "profile_id", "version_json", "needs_installer"} <= (
        _fields(LoaderBuild)
    )
    init = _params(LoaderService.__init__)
    for p in ("http", "installer", "manifest"):
        assert p in init.parameters, f"LoaderService.__init__ 缺少 {p}"
    for meth in ("list_loaders", "recommend", "prepare", "install"):
        assert callable(getattr(LoaderService, meth, None)), f"LoaderService 缺少 {meth}()"
    assert _has_params(LoaderService.install, "build", "mc_root")


# --------------------------------------------------------------------------- #
# core/auth.py + account_store.py + security.py [auth-core]
# --------------------------------------------------------------------------- #
def test_auth_contract():
    _import("core.auth")
    from cubefetch.core.auth import Account, AuthService, DeviceFlow

    assert {
        "device_code",
        "user_code",
        "verification_uri",
        "expires_in",
        "interval",
    } <= _fields(DeviceFlow)
    assert {
        "access_token",
        "refresh_token",
        "expires_at",
        "uuid",
        "name",
        "verified_at",
    } <= _fields(Account)
    init = _params(AuthService.__init__)
    for p in ("http", "store"):
        assert p in init.parameters
    assert "remember_days" in init.parameters, "AuthService 需接受 remember_days"
    for meth in ("start_login", "wait_login", "refresh", "verify_identity"):
        assert callable(getattr(AuthService, meth, None)), f"AuthService 缺少 {meth}()"


def test_account_store_contract():
    _import("core.account_store")
    from cubefetch.core.account_store import AccountStore

    init = _params(AccountStore.__init__)
    assert "path" in init.parameters
    for meth in ("load", "save", "clear", "days_since_verified", "needs_reverify"):
        assert callable(getattr(AccountStore, meth, None)), f"AccountStore 缺少 {meth}()"


def test_security_contract():
    _import("core.security")
    from cubefetch.core.security import AuthGate, IntegrityGuard, Ticket, machine_id, require_ticket

    assert {"uuid", "name", "expires_at", "mac"} <= _fields(Ticket)
    assert callable(AuthGate.issue) and callable(AuthGate.verify)
    assert callable(AuthGate.require_active), "AuthGate 需提供 require_active()（Installer 入口）"
    assert callable(machine_id) and callable(require_ticket)
    assert callable(IntegrityGuard.check)


# --------------------------------------------------------------------------- #
# ui + services [ui] / [Lead]
# --------------------------------------------------------------------------- #
def test_ui_contract():
    window = _import("ui.app_window")
    from cubefetch.ui.app_window import AppWindow, create_app, run

    init = _params(AppWindow.__init__)
    assert "master" in init.parameters and "services" in init.parameters
    assert callable(create_app) and callable(run)
    assert window is not None


def test_services_contract():
    _import("services")
    from cubefetch.services import AppServices

    for meth in (
        "bootstrap",
        "login_flow",
        "finish_login",
        "logout",
        "versions_categories",
        "loaders_for",
        "recommend_loader",
        "install",
        "ensure_java",
        "detect_launchers",
        "choose_root",
        "current_mc_root",
        "save_config",
        "open_folder",
    ):
        assert callable(getattr(AppServices, meth, None)), f"AppServices 缺少 {meth}()"
    init = _params(AppServices.__init__)
    assert "config" in init.parameters


# --------------------------------------------------------------------------- #
# 打包入口
# --------------------------------------------------------------------------- #
def test_entry_point_contract():
    import app as entry

    assert callable(entry.main)
    assert entry.main(["--version"]) == 0
