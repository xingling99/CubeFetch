"""AppServices —— 连接 UI 与核心逻辑的唯一门面。

UI 只允许通过本类访问网络/认证/安装能力；所有耗时方法都是阻塞的，
由 UI 在工作线程中调用（契约 §10.4）。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from .config import APP_USER_AGENT, AppConfig
from .core.account_store import AccountStore
from .core.auth import Account, AuthService, DeviceFlow
from .core.detect import DetectionResult, LauncherDetector
from .core.errors import (
    AuthError,
    CancelledError,
    CubeFetchError,
    InstallError,
    NetworkError,
    NotOwnedError,
)
from .core.http import HttpSession, Progress
from .core.install import Installer
from .core.isolation import VersionIsolation
from .core.loader import LoaderBuild, LoaderService
from .core.manifest import (
    CATEGORY_APRIL,
    CATEGORY_OLD,
    CATEGORY_RELEASE,
    CATEGORY_SNAPSHOT,
    ManifestService,
    VersionEntry,
)
from .core.security import AuthGate, Ticket, clear_active
from .core.util import pick_java

log = logging.getLogger("cubefetch.services")

CATEGORY_ORDER = (CATEGORY_RELEASE, CATEGORY_SNAPSHOT, CATEGORY_OLD, CATEGORY_APRIL)


def required_java_major(game_version: str | None) -> int | None:
    """按游戏版本推断所需 Java 主版本；None 表示不强制。

    Mojang 官方基线（与 docs/research/ecosystem.md 一致）：
    1.16.5 及更早 → 8；1.17.x → 16；1.18–1.20.4 → 17；1.20.5+ / 1.21.x → 21；
    新版本号方案 26.x（含 26.4-snapshot-*）→ 25。旧式快照按年份推断（23w→17，24w+→21）。
    """
    if not game_version:
        return None
    text = game_version.strip()
    if not text:
        return None
    try:
        if text.startswith("1."):
            parts = text.split(".")
            minor = int(parts[1])
            patch = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            if minor >= 21:
                return 21
            if minor == 20 and patch >= 5:
                return 21
            if minor >= 18:
                return 17
            if minor == 17:
                return 16
            return 8
        # 旧式快照：23w13a_or_b / 24w14potato …
        head = text[:2]
        if len(text) >= 3 and text[2] == "w" and head.isdigit():
            return 17 if int(head) <= 23 else 21
        # 新版本号方案：26.3 / 26.4-snapshot-4
        major_text = text.split(".")[0].split("-")[0]
        if major_text.isdigit():
            major = int(major_text)
            if major >= 25:
                return 25
            if major >= 24:
                return 21
    except (IndexError, ValueError):
        pass
    return None


class AppServices:
    """应用服务门面。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig.load()
        self.http = HttpSession(
            mirror=self.config.mirror,
            user_agent=APP_USER_AGENT,
        )
        self.manifest = ManifestService(self.http)
        self.store = AccountStore()
        self.auth = AuthService(self.http, self.store, remember_days=self.config.remember_days)
        self.gate = AuthGate()
        self.detector = LauncherDetector()
        self.installer = Installer(self.http, self.manifest, gate=self.gate)
        self.loaders = LoaderService(self.http, self.installer, self.manifest)

        self.account: Account | None = None
        self.ticket: Ticket | None = None
        self.detection: DetectionResult | None = None
        self.java_exe: Path | None = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ 启动
    def bootstrap(self) -> None:
        """离线初始化：读配置、检测启动器、恢复本地账号。不联网。"""
        # 1) 启动器检测（失败不致命）
        try:
            self.detection = self.detector.detect()
        except Exception:  # noqa: BLE001  检测失败不应阻止启动
            log.exception("启动器检测失败")
            self.detection = None

        # 2) 写入目标目录：检测到任何可用目录就直接自动选择最佳目录（版本数最多），
        #    不再弹窗要求用户挑选（用户反馈：检测到了就自动选择）。
        if self.detection is not None:
            default_root = getattr(self.detection, "default_root", None)
            if default_root and not self.config.mc_root:
                self.config.mc_root = str(default_root)
                try:
                    self.config.save()
                except OSError:
                    log.warning("保存配置失败", exc_info=True)

        # 3) Java：优先使用配置里的路径
        if self.config.java_path:
            candidate = Path(self.config.java_path)
            if candidate.is_file():
                self.java_exe = candidate

        # 4) 账号恢复（含 30 天强制重验逻辑）
        try:
            account = self.store.load()
        except Exception:  # noqa: BLE001  本地存储损坏也允许继续
            log.exception("读取本地账号失败")
            account = None
        if account is None:
            self.account = None
            return
        try:
            if self.store.needs_reverify(account, self.config.remember_days):
                log.info("账号超过 %s 天未验证，需要重新登录", self.config.remember_days)
                self.account = None
                self.ticket = None
                return
        except Exception:  # noqa: BLE001
            log.exception("账号有效期判定失败")
            self.account = None
            return
        self.account = account
        try:
            self.ticket = self.gate.issue(account)
        except Exception:  # noqa: BLE001
            log.exception("签发本地票据失败")
            self.ticket = None

    # ---------------------------------------------------------------- 账号
    @property
    def logged_in(self) -> bool:
        return self.account is not None

    def login_flow(self) -> DeviceFlow:
        """开始设备码登录，返回给 UI 展示的授权信息。"""
        return self.auth.start_login()

    def finish_login(
        self,
        flow: DeviceFlow,
        cancel: threading.Event,
        status_cb=None,
    ) -> Account:
        """阻塞等待浏览器授权完成（UI 工作线程调用）。成功后持久化账号。"""
        account = self.auth.wait_login(flow, cancel, status_cb=status_cb)
        self.store.save(account)
        with self._lock:
            self.account = account
            self.ticket = self.gate.issue(account)
        return account

    def logout(self) -> None:
        with self._lock:
            self.account = None
            self.ticket = None
        try:
            clear_active()  # 同时清空 security 模块级激活票据，防止残留 ticket 被复用
        except Exception:  # noqa: BLE001
            log.debug("清除激活票据失败", exc_info=True)
        try:
            self.store.clear()
        except OSError:
            log.warning("清除本地账号失败", exc_info=True)
        try:
            self.http.clear_mirror_memo()
        except AttributeError:
            pass

    def _ensure_account(self) -> Account:
        if self.account is None:
            raise AuthError("请先登录 Microsoft 账号", detail="no_account")
        return self.account

    def ensure_session(self) -> Account:
        """保证账号可用的 MC token：过期则静默刷新，票根重签。"""
        account = self._ensure_account()
        changed = False
        try:
            if not account.token_valid():
                account = self.auth.refresh(account)
                changed = True
        except NotOwnedError:
            raise
        except AuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AuthError("登录状态已失效，请重新登录", detail=str(exc)) from exc
        with self._lock:
            self.account = account
            self.ticket = self.gate.issue(account)
            if changed:
                try:
                    self.store.save(account)
                except OSError:
                    log.warning("保存刷新后的账号失败", exc_info=True)
        return account

    # ---------------------------------------------------------------- 版本
    def versions_categories(self, refresh: bool = False) -> dict[str, list[VersionEntry]]:
        """四类版本列表（按发布时间倒序）。"""
        entries = self.manifest.refresh() if refresh else self.manifest.load()
        result: dict[str, list[VersionEntry]] = {key: [] for key in CATEGORY_ORDER}
        for entry in entries:
            result.setdefault(entry.category, []).append(entry)
        for items in result.values():
            items.sort(key=lambda item: item.release_time, reverse=True)
        return result

    # -------------------------------------------------------------- 加载器
    def loaders_for(self, kind: str, game_version: str) -> list[dict]:
        return self.loaders.list_loaders(kind, game_version)

    def recommend_loader(self, kind: str, game_version: str) -> str | None:
        try:
            return self.loaders.recommend(kind, game_version)
        except CubeFetchError:
            return None

    # ---------------------------------------------------------------- 安装
    def install(
        self,
        kind: str,
        loader_version: str | None,
        game_version: str,
        progress_cb=None,
        cancel: threading.Event | None = None,
        custom_version_name: str | None = None,
    ) -> str:
        """完整安装（vanilla/fabric/forge/neoforge）。阻塞，返回 profile_id。

        UI 必须在工作线程调用；cancel.set() 后尽快抛 CancelledError。
        custom_version_name 非空时，安装完成后把该版本重命名为用户输入的名字。
        """
        self.ensure_session()  # 正版门禁：任何安装都必须先通过身份校验
        mc_root = self.config.mc_root_path

        build: LoaderBuild = self.loaders.prepare(kind, loader_version, game_version)
        java_exe = self.ensure_java(required_major=required_java_major(game_version))
        profile_id = self.loaders.install(
            build,
            mc_root,
            java_exe=java_exe,
            progress_cb=progress_cb,
            cancel=cancel,
        )

        # 用户自定义版本名（用户需 4：检测到版本重命名时允许自己输入名字）
        if custom_version_name:
            from .core.version_rename import rename_installed_version

            profile_id = rename_installed_version(mc_root, profile_id, custom_version_name)
            log.info("版本已重命名：%s", profile_id)

        # 版本隔离（契约 §7）：按已安装的启动器写入对应配置
        try:
            kinds = [p.kind for p in (self.detection.probes if self.detection else []) if p.installed]
            if kinds:
                notes = VersionIsolation.apply(mc_root, profile_id, kinds, enabled=True)
                log.info("版本隔离配置：%s", notes)
        except Exception:  # noqa: BLE001  隔离失败不应让整个安装失败
            log.exception("写入版本隔离配置失败")

        self.config.last_version = game_version
        self.config.last_loader = kind
        self.config.mc_root = str(mc_root)
        try:
            self.config.save()
        except OSError:
            log.warning("保存配置失败", exc_info=True)
        return profile_id

    # -------------------------------------------------------------- Java
    def ensure_java(self, required_major: int | None = None) -> Path | None:
        """返回可用 Java；优先配置路径，其次自动探测。"""
        with self._lock:
            if self.java_exe is not None:
                return self.java_exe
        configured = self.config.java_path
        if configured:
            candidate = Path(configured)
            if candidate.is_file():
                self.java_exe = candidate
                return candidate
        chosen = pick_java(required_major)
        if chosen is not None:
            with self._lock:
                self.java_exe = chosen
        return chosen

    # ------------------------------------------------- 官方 Java 运行时下载
    def _java_runtime(self):
        """当前 Minecraft 根目录对应的 Java 运行时下载服务（懒构造）。"""
        from .core.java_runtime import JavaRuntimeService

        return JavaRuntimeService(self.http, self.config.mc_root_path)

    def java_status(self, game_version: str) -> dict:
        """检查该游戏版本需要的 Java 是否已在本机可用（不联网）。"""
        major = required_java_major(game_version)
        component: str | None = None
        runtime_java: Path | None = None
        try:
            runtime = self._java_runtime()
            info = runtime.resolve(required_major=major)
            if info is not None:
                component = info.component
                runtime_java = runtime.find_installed(component)
        except Exception:  # noqa: BLE001 运行时引擎不可用时退化为普通探测
            log.debug("Java 运行时探测失败", exc_info=True)
        java = runtime_java or self.ensure_java(required_major=major)
        return {
            "required_major": major,
            "java_path": java,
            "component": component,
            "need_download": java is None,
        }

    def download_java(
        self,
        game_version: str,
        progress_cb=None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """下载该游戏版本对应的官方 Java 运行时（默认目录 .minecraft/runtime）。"""
        major = required_java_major(game_version)
        version_json = None
        try:
            version_json = self.manifest.fetch_version_json(game_version)
        except Exception:  # noqa: BLE001 拿不到版本 JSON 时用静态映射兜底
            log.debug("获取版本 JSON 失败，改用静态 Java 映射", exc_info=True)
        runtime = self._java_runtime()
        info = runtime.resolve(required_major=major, version_json=version_json)
        if info is None:
            raise InstallError(
                f"无法确定 Minecraft {game_version} 需要的 Java 运行时",
                detail="java_component_unknown",
            )
        log.info("开始下载 Java 运行时 %s（Java %s）", info.component, major)
        java = runtime.install(info.component, progress_cb=progress_cb, cancel=cancel)
        with self._lock:
            self.java_exe = java
        self.config.java_path = str(java)
        try:
            self.config.save()
        except OSError:
            log.warning("保存 Java 路径失败", exc_info=True)
        return java

    def existing_version(self, kind: str, loader_version: str | None, game_version: str) -> str | None:
        """若该游戏版本 + 加载器组合已安装，返回已有版本 id。"""
        from .core.version_rename import existing_version

        try:
            return existing_version(
                self.config.mc_root_path, kind, game_version, loader_version
            )
        except Exception:  # noqa: BLE001 探测失败不应阻止安装
            log.debug("探测已存在版本失败", exc_info=True)
            return None

    # ------------------------------------------------------------- 启动器
    def detect_launchers(self) -> DetectionResult | None:
        try:
            self.detection = self.detector.detect()
        except Exception:  # noqa: BLE001
            log.exception("启动器检测失败")
        return self.detection

    def choose_root(self, path: Path | str) -> None:
        """用户在弹窗中选定的 .minecraft 根目录。"""
        with self._lock:
            self.config.mc_root = str(Path(path))
        try:
            self.config.save()
        except OSError:
            log.warning("保存配置失败", exc_info=True)

    def current_mc_root(self) -> Path:
        return self.config.mc_root_path

    # --------------------------------------------------------------- 杂项
    def save_config(self) -> None:
        self.config.sanitize()
        self.config.save()
        # 线程数与下载源热更新
        if self.config.mirror != self.http.mirror:
            self.http.set_mirror(self.config.mirror)
        self.http.workers = self.config.threads

    def open_folder(self, path: Path | str) -> None:
        from .core.util import open_in_explorer

        open_in_explorer(Path(path))
