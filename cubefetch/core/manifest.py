"""Minecraft 版本清单：下载、分类、缓存与版本 JSON 获取。

对应 docs/CONTRACTS.md §4。本模块只通过 :class:`~cubefetch.core.http.HttpSession`
访问网络，把官方版本清单缓存到 ``cache_dir()`` 下，并按契约分类为
release / snapshot / old / aprilfool 四类。
"""
from __future__ import annotations

import datetime as _dt
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from ..config import cache_dir
from .errors import CubeFetchError, NetworkError
from .http import HttpSession
from .util import ensure_dir, read_json, write_json

log = logging.getLogger("cubefetch.manifest")

#: 官方版本清单地址
MANIFEST_URL = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

CATEGORY_RELEASE = "release"
CATEGORY_SNAPSHOT = "snapshot"
CATEGORY_OLD = "old"
CATEGORY_APRIL = "aprilfool"

CATEGORY_ORDER: tuple[str, ...] = (
    CATEGORY_RELEASE,
    CATEGORY_SNAPSHOT,
    CATEGORY_OLD,
    CATEGORY_APRIL,
)

#: 官方清单里类型为 snapshot、但实为愚人节版本的版本号。
APRIL_FOOLS_VERSIONS = frozenset(
    {
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
)


def parse_classification(version_type: str, version_id: str = "") -> str:
    """把官方 ``type`` 字段归类为四类之一。

    - ``old_beta`` / ``old_alpha`` → old
    - ``release`` → release
    - 已知愚人节版本（且类型为 snapshot）→ aprilfool
    - 其余（含未知类型）→ snapshot
    """
    kind = (version_type or "").strip().lower()
    if kind in ("old_beta", "old_alpha"):
        return CATEGORY_OLD
    if version_id in APRIL_FOOLS_VERSIONS and kind in ("snapshot", ""):
        return CATEGORY_APRIL
    if kind == CATEGORY_RELEASE:
        return CATEGORY_RELEASE
    return CATEGORY_SNAPSHOT


#: 兼容别名（契约里的分类函数名字未固定）
classify_version = parse_classification


@dataclass(frozen=True)
class VersionEntry:
    """版本清单中的一条记录。"""

    id: str
    type: str
    url: str
    release_time: str
    category: str

    def release_date(self) -> _dt.datetime:
        """解析 ``release_time`` 为 datetime；解析失败返回 1970-01-01 (UTC)。"""
        raw = (self.release_time or "").strip()
        for parser in (_dt.datetime.fromisoformat, _parse_iso_compact):
            try:
                parsed = parser(raw)  # type: ignore[operator]
            except (TypeError, ValueError):
                continue
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=_dt.timezone.utc)
            return parsed
        return _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def _parse_iso_compact(value: str) -> _dt.datetime:
    """兼容 ``2026-10-20T09:54:00+0000``（fabric 风格）与 ``...Z``。"""
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    return _dt.datetime.fromisoformat(value)


def _release_sort_key(entry: VersionEntry) -> str:
    # release_time 是 ISO8601 字符串，字典序与时间序一致；缺失时排最后。
    return entry.release_time or ""


class ManifestService:
    """版本清单服务（带磁盘缓存与 TTL）。"""

    def __init__(
        self,
        http: HttpSession,
        cache_ttl: float = 3600,
        *,
        cache_root: Path | None = None,
    ) -> None:
        self.http = http
        self.cache_ttl = max(0.0, float(cache_ttl))
        self._cache_root = Path(cache_root) if cache_root is not None else Path(cache_dir())
        self._entries: list[VersionEntry] | None = None
        self._lock = threading.RLock()

    # -- 缓存路径 ---------------------------------------------------------

    @property
    def cache_root(self) -> Path:
        return self._cache_root

    def manifest_cache_path(self) -> Path:
        return self._cache_root / "version_manifest.json"

    def version_cache_path(self, version_id: str) -> Path:
        return self._cache_root / "versions" / f"{version_id}.json"

    # -- 清单加载 ---------------------------------------------------------

    def load(self, *, force: bool = False) -> list[VersionEntry]:
        """返回版本列表；优先使用未过期的磁盘缓存。"""
        with self._lock:
            cached = self._read_manifest_cache()
            if not force and cached is not None and self._is_fresh(cached):
                entries = self._entries_from_payload(cached["manifest"])
                self._entries = entries
                return list(entries)

            try:
                payload = self.http.get_json(MANIFEST_URL)
            except CubeFetchError as exc:
                if cached is not None:
                    log.warning("版本清单刷新失败，使用过期缓存：%s", exc)
                    entries = self._entries_from_payload(cached["manifest"])
                    self._entries = entries
                    return list(entries)
                raise
            entries = self._entries_from_payload(payload)
            self._write_manifest_cache(payload)
            self._entries = entries
            return list(entries)

    def refresh(self) -> list[VersionEntry]:
        """强制重新下载版本清单。"""
        return self.load(force=True)

    def categories(self) -> dict[str, list[VersionEntry]]:
        """四类版本列表，每类按 release_time 降序（新→旧）。"""
        entries = self.load()
        grouped: dict[str, list[VersionEntry]] = {name: [] for name in CATEGORY_ORDER}
        for entry in entries:
            grouped.setdefault(entry.category, []).append(entry)
        for name in grouped:
            grouped[name].sort(key=_release_sort_key, reverse=True)
        return grouped

    def find_version(self, version_id: str) -> VersionEntry | None:
        """按版本号查找清单条目。"""
        if not version_id:
            return None
        for entry in self.load():
            if entry.id == version_id:
                return entry
        return None

    # -- 版本 JSON -------------------------------------------------------

    def fetch_version_json(self, version_id: str) -> dict:
        """获取版本 JSON：优先读缓存，其次按清单 URL 下载并写缓存。"""
        version_id = str(version_id or "").strip()
        if not version_id:
            raise CubeFetchError("版本号为空，无法获取版本信息")
        path = self.version_cache_path(version_id)
        cached = self._read_version_cache(path)
        if cached is not None:
            return cached
        entry = self.find_version(version_id)
        if entry is None:
            raise CubeFetchError(
                f"版本列表中找不到 {version_id}", detail="manifest 中没有该版本"
            )
        data = self.http.get_json(entry.url)
        ensure_dir(path.parent)
        write_json(path, data)
        return data

    def load_cached_version_json(self, version_id: str) -> dict | None:
        """只从磁盘缓存读取版本 JSON（不联网）；供 install 解析父版本使用。"""
        return self._read_version_cache(self.version_cache_path(str(version_id or "")))

    # -- 内部 -------------------------------------------------------------

    def _read_version_cache(self, path: Path) -> dict | None:
        data = read_json(path, None)
        if isinstance(data, dict) and data:
            return dict(data)
        return None

    def _read_manifest_cache(self) -> dict | None:
        data = read_json(self.manifest_cache_path(), None)
        if not isinstance(data, dict):
            return None
        manifest = data.get("manifest")
        fetched_at = data.get("fetched_at")
        if not isinstance(manifest, dict) or not isinstance(fetched_at, (int, float)):
            return None
        return data

    def _is_fresh(self, cached: dict) -> bool:
        if self.cache_ttl <= 0:
            return False
        fetched_at = float(cached.get("fetched_at", 0))
        return (time.time() - fetched_at) < self.cache_ttl

    def _write_manifest_cache(self, payload: dict) -> None:
        path = self.manifest_cache_path()
        try:
            ensure_dir(path.parent)
            write_json(path, {"fetched_at": time.time(), "manifest": payload})
        except OSError as exc:  # pragma: no cover - 磁盘异常时不应中断安装
            log.warning("写入版本清单缓存失败：%s", exc)

    @staticmethod
    def _entries_from_payload(payload: dict) -> list[VersionEntry]:
        raw_versions = payload.get("versions")
        if not isinstance(raw_versions, list):
            raise NetworkError(
                "版本清单数据格式异常", detail="version_manifest 中缺少 versions 列表"
            )
        entries: list[VersionEntry] = []
        for item in raw_versions:
            if not isinstance(item, dict):
                continue
            version_id = str(item.get("id") or "").strip()
            if not version_id:
                continue
            version_type = str(item.get("type") or "")
            entries.append(
                VersionEntry(
                    id=version_id,
                    type=version_type,
                    url=str(item.get("url") or ""),
                    release_time=str(item.get("releaseTime") or ""),
                    category=parse_classification(version_type, version_id),
                )
            )
        return entries


def group_entries_by_category(
    entries: Sequence[VersionEntry],
) -> dict[str, list[VersionEntry]]:
    """把任意条目序列按分类分组并排序（供测试/调用方复用）。"""
    grouped: dict[str, list[VersionEntry]] = {name: [] for name in CATEGORY_ORDER}
    for entry in entries:
        grouped.setdefault(entry.category, []).append(entry)
    for name in grouped:
        grouped[name].sort(key=_release_sort_key, reverse=True)
    return grouped
