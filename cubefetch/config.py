"""全局配置与路径。"""
from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .core.util import ensure_dir, read_json, write_json

APP_NAME = "CubeFetch"
APP_NAME_CN = "方块速递"
APP_VERSION = "1.2.1-beta"
APP_USER_AGENT = f"CubeFetch/{APP_VERSION} (+https://github.com/)"

MIRRORS = ("auto", "official", "bmclapi")
LAUNCHERS = ("auto", "pcl", "hmcl", "custom")


def _base_dir() -> Path:
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    if xdg:
        return Path(xdg) / APP_NAME
    return Path.home() / f".{APP_NAME.lower()}"


def appdata_dir() -> Path:
    """程序数据目录：%LOCALAPPDATA%/CubeFetch。"""
    return ensure_dir(_base_dir())


def cache_dir() -> Path:
    return ensure_dir(appdata_dir() / "cache")


def log_dir() -> Path:
    return ensure_dir(appdata_dir() / "logs")


def config_path() -> Path:
    return appdata_dir() / "config.json"


def default_minecraft_root() -> Path:
    """未检测到启动器时的默认 .minecraft 位置。"""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            return Path(appdata) / ".minecraft"
    return Path.home() / ".minecraft"


@dataclass
class AppConfig:
    """用户配置。所有字段都必须能安全序列化为 JSON。"""

    mc_root: str | None = None
    launcher: str = "auto"          # auto|pcl|hmcl|custom
    mirror: str = "auto"            # auto|official|bmclapi
    threads: int = 32
    remember_days: int = 30
    java_path: str | None = None
    last_version: str | None = None
    last_loader: str | None = None
    close_to_tray: bool = False
    extra: dict = field(default_factory=dict)

    # ---- 派生属性 ----
    @property
    def mc_root_path(self) -> Path:
        return Path(self.mc_root) if self.mc_root else default_minecraft_root()

    # ---- 持久化 ----
    @classmethod
    def load(cls) -> "AppConfig":
        data = read_json(config_path(), {})
        if not isinstance(data, dict):
            data = {}
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        clean = {k: v for k, v in data.items() if k in known}
        cfg = cls(**clean)
        cfg.sanitize()
        return cfg

    def save(self) -> None:
        write_json(config_path(), asdict(self))

    # ---- 约束 ----
    def sanitize(self) -> None:
        """把越界或非法的配置值纠回安全范围。"""
        if self.mirror not in MIRRORS:
            self.mirror = "auto"
        if self.launcher not in LAUNCHERS:
            self.launcher = "auto"
        try:
            self.threads = max(1, min(64, int(self.threads)))
        except (TypeError, ValueError):
            self.threads = 32
        try:
            self.remember_days = max(1, min(365, int(self.remember_days)))
        except (TypeError, ValueError):
            self.remember_days = 30
        if self.mc_root:
            try:
                Path(self.mc_root)
            except (OSError, ValueError):
                self.mc_root = None
        if not isinstance(self.extra, dict):
            self.extra = {}


def dump_config(cfg: AppConfig) -> str:
    return json.dumps(asdict(cfg), ensure_ascii=False, indent=2)
