"""官方 Java 运行时下载引擎。

用户下载游戏版本时，如果本机没有与 ``version json`` 中
``javaVersion.majorVersion`` 匹配的 Java，启动器需要询问用户并自动下载
Mojang 官方提供的 Java 运行时（java-runtime / jre-legacy）。

本模块只负责“核心引擎”部分：

- 读取 Mojang java-runtime 索引（``all.json``），按平台筛选组件；
- 解析目标版本需要的运行时组件（优先 ``version_json.javaVersion.component``，
  否则按 Java 大版本静态映射，并用索引里的实际版本号校验）；
- 生成下载计划（``plan``，纯网络读取但不写业务文件，便于 UI 预检）；
- 下载缺失文件到 ``<mc_root>/runtime/<component>/<platform>/<component>/``，
  复用 :class:`~cubefetch.core.http.HttpSession`（auto 模式下镜像优先、失败回落官方）
  与 :class:`~cubefetch.core.http.ThreadedDownloader`（并发、重试、取消、
  ``Progress`` 进度回调）。

目录布局与官方启动器一致::

    <mc_root>/runtime/
        java-runtime-gamma/
            windows-x64/
                java-runtime-gamma/
                    bin/java.exe
                    lib/modules
                    release
                    ...

缓存（``all.json`` 与平台文件清单）默认写入 :func:`cubefetch.config.cache_dir`，
测试可传 ``cache_root`` 覆盖；缓存过期后重新拉取，网络失败时回退到旧缓存，
保证离线情况下仍能复用已安装的运行时。
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..config import cache_dir
from .errors import CancelledError, CubeFetchError, InstallError, NetworkError
from .http import DownloadTask, HttpSession, Progress, ThreadedDownloader
from .util import ensure_dir, read_json, write_json

log = logging.getLogger("cubefetch.java_runtime")

#: Mojang 官方 Java 运行时索引（全平台 all.json）。
JAVA_RUNTIME_INDEX_URL = (
    "https://launchermeta.mojang.com/v1/products/java-runtime/"
    "2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
)

#: 当前只支持 Windows x64，与项目其它模块一致。
PLATFORM = "windows-x64"

#: 索引与文件清单的缓存有效期（秒）。
CACHE_TTL = 24 * 60 * 60

#: Java 大版本 → 官方运行时组件。
#:
#: 注意：这里按 Mojang 索引里的**实际版本号**校准过（见模块文档与测试）：
#: ``java-runtime-gamma`` 是 17、``java-runtime-delta`` 是 21、
#: ``java-runtime-epsilon`` 是 25。若索引结构变化，解析时会再用
#: ``version.name`` 校验大版本，校验不匹配则回退到“按大版本扫描索引”。
JAVA_COMPONENT_BY_MAJOR: dict[int, str] = {
    8: "jre-legacy",
    16: "java-runtime-alpha",
    17: "java-runtime-gamma",
    21: "java-runtime-delta",
    25: "java-runtime-epsilon",
}

_VERSION_MAJOR_RE = re.compile(r"\s*(\d+)(?:[.u_](\d+))?")
_JAVA_VERSION_RE = re.compile(r'JAVA_VERSION\s*=\s*"([^"]+)"')


def _version_major(version_name: object) -> int | None:
    """从 ``version.name``（如 ``17.0.15`` / ``8u51-cacert...``）解析 Java 大版本。"""
    match = _VERSION_MAJOR_RE.match(str(version_name or ""))
    if match is None:
        return None
    major = int(match.group(1))
    # 旧式 1.8.0_51 写法 → Java 8
    if major == 1 and match.group(2) is not None:
        return int(match.group(2))
    return major


def _as_int(value: object) -> int | None:
    """尽量把 JSON 里的数字/数字字符串转成 int。"""
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _relative_parts(relpath: object) -> tuple[str, ...] | None:
    """把清单中的相对路径拆成安全路径片段；``..`` / 绝对路径返回 None。"""
    text = str(relpath or "").replace("\\", "/")
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts:
        return None
    for part in parts:
        if part == ".." or part.endswith(":"):
            return None
    return tuple(parts)


def _safe_cache_name(component: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", component) or "component"


@dataclass
class JavaRuntimeInfo:
    """一个可下载的官方 Java 运行时组件。"""

    component: str
    platform: str
    version_name: str = ""


class JavaRuntimeService:
    """官方 Java 运行时的解析、计划与下载。

    实例只持有传入的 :class:`HttpSession` 与 ``mc_root``，不启动线程；
    :meth:`plan` / :meth:`install` 才发起网络请求，:meth:`install` 会阻塞到
    下载完成（调用方应放在工作线程里）。
    """

    def __init__(
        self,
        http: HttpSession,
        mc_root: Path,
        *,
        cache_root: Path | None = None,
        cache_ttl: float = CACHE_TTL,
    ) -> None:
        self.http = http
        self.mc_root = Path(mc_root)
        self.cache_ttl = float(cache_ttl)
        self._cache_root = Path(cache_root) if cache_root is not None else None
        self._index: dict[str, Any] | None = None
        self._manifests: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    # -- 路径 -------------------------------------------------------------

    def runtime_root(self) -> Path:
        """``<mc_root>/runtime``。"""
        return self.mc_root / "runtime"

    def runtime_dir(self, component: str) -> Path:
        """单个运行时的根目录：``<runtime>/<component>/<platform>/<component>``。"""
        name = str(component or "").strip()
        return self.runtime_root() / name / PLATFORM / name

    def java_executable(self, component: str) -> Path:
        """``.../bin/java.exe``。"""
        return self.runtime_dir(component) / "bin" / "java.exe"

    def find_installed(self, component: str) -> Path | None:
        """组件已安装（``bin/java.exe`` 存在）时返回该路径，否则 None。"""
        name = str(component or "").strip()
        if not name:
            return None
        exe = self.java_executable(name)
        return exe if exe.is_file() else None

    # -- 解析 -------------------------------------------------------------

    def resolve(
        self,
        required_major: int | None = None,
        version_json: dict | None = None,
    ) -> JavaRuntimeInfo | None:
        """解析目标版本应使用的 Java 运行时组件。

        - 优先使用 ``version_json["javaVersion"]["component"]``；
        - 否则按 ``required_major`` 静态映射（``JAVA_COMPONENT_BY_MAJOR``）；
        - 本机已安装且大版本匹配时优先返回已安装组件（不联网）；
        - 未知大版本 / 索引中不存在 → ``None``；
        - 需要联网且网络失败 → :class:`NetworkError`。
        """
        required = _as_int(required_major)
        component = ""
        if isinstance(version_json, dict):
            java_version = version_json.get("javaVersion")
            if isinstance(java_version, dict):
                component = str(java_version.get("component") or "").strip()
                if required is None:
                    required = _as_int(java_version.get("majorVersion"))

        if not component and required is None:
            return None

        # 1) 已安装的运行时优先：常见路径（版本已装好）完全不需要联网。
        for candidate in self._installed_candidates(component, required):
            exe = self.find_installed(candidate)
            if exe is None:
                continue
            installed_major = _version_major(self._installed_version(candidate))
            if required is None or installed_major is None or installed_major == required:
                return JavaRuntimeInfo(
                    component=candidate,
                    platform=PLATFORM,
                    version_name=self._installed_version(candidate),
                )

        # 2) 联网读取索引解析。
        index = self._load_index()
        platform_components = self._platform_components(index)

        if component:
            entries = platform_components.get(component)
            if entries:
                info = self._info_for(component, entries)
                if info is not None:
                    if required is None:
                        return info
                    actual = _version_major(info.version_name)
                    if actual is None or actual == required:
                        return info

        if required is None:
            return None
        for candidate in self._candidates_for_major(platform_components, required):
            entries = platform_components.get(candidate)
            if not entries:
                continue
            info = self._info_for(candidate, entries)
            if info is not None:
                return info
        return None

    def _installed_candidates(self, component: str, required: int | None) -> list[str]:
        """可能已安装的组件候选（version_json 指定 → 静态映射 → runtime 目录扫描）。"""
        candidates: list[str] = []
        if component:
            candidates.append(component)
        if required is not None:
            preferred = JAVA_COMPONENT_BY_MAJOR.get(required)
            if preferred and preferred not in candidates:
                candidates.append(preferred)
        for name in self._scan_installed_components():
            if name not in candidates:
                candidates.append(name)
        return candidates

    def _scan_installed_components(self) -> list[str]:
        root = self.runtime_root()
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            return []
        return [child.name for child in children if child.is_dir()]

    def _installed_version(self, component: str) -> str:
        release = self.runtime_dir(component) / "release"
        try:
            text = release.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
        match = _JAVA_VERSION_RE.search(text)
        return match.group(1).strip() if match else ""

    def _candidates_for_major(
        self, platform_components: Mapping[str, Sequence[dict]], major: int
    ) -> list[str]:
        """按大版本给出候选组件：静态映射优先，其次扫描索引里的实际版本号。"""
        candidates: list[str] = []
        preferred = JAVA_COMPONENT_BY_MAJOR.get(major)
        if preferred and preferred in platform_components:
            info = self._info_for(preferred, platform_components[preferred])
            if info is not None and _version_major(info.version_name) == major:
                candidates.append(preferred)
        for name in platform_components:
            if name in candidates:
                continue
            info = self._info_for(name, platform_components[name])
            if info is not None and _version_major(info.version_name) == major:
                candidates.append(name)
        return candidates

    # -- 索引 / 清单 ------------------------------------------------------

    def _cache_dir(self) -> Path:
        return self._cache_root if self._cache_root is not None else cache_dir()

    def _index_cache_path(self) -> Path:
        return self._cache_dir() / "java_runtime_all.json"

    def _manifest_cache_path(self, component: str) -> Path:
        return (
            self._cache_dir() / "java_runtimes" / f"{_safe_cache_name(component)}-{PLATFORM}.json"
        )

    def _cache_fresh(self, payload: object) -> bool:
        if not isinstance(payload, dict) or self.cache_ttl <= 0:
            return False
        fetched_at = payload.get("fetched_at")
        try:
            age = time.time() - float(fetched_at)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return False
        return 0 <= age <= self.cache_ttl

    def _load_index(self) -> dict[str, Any]:
        """读取 all.json：内存 → 磁盘缓存（TTL 内）→ 网络；失败回退旧缓存。"""
        with self._lock:
            if self._index is not None:
                return self._index
            cache_path = self._index_cache_path()
            cached_payload = read_json(cache_path, default=None)
            cached_index = (
                cached_payload.get("index") if isinstance(cached_payload, dict) else None
            )
            if isinstance(cached_index, dict) and cached_index and self._cache_fresh(cached_payload):
                self._index = cached_index
                return cached_index
            try:
                data = self.http.get_json(JAVA_RUNTIME_INDEX_URL)
            except CubeFetchError as exc:
                if isinstance(cached_index, dict) and cached_index:
                    log.warning("获取 Java 运行时索引失败，使用本地缓存：%s", exc)
                    self._index = cached_index
                    return cached_index
                raise NetworkError(
                    "获取 Java 运行时列表失败，请检查网络或切换下载镜像",
                    detail=f"{JAVA_RUNTIME_INDEX_URL} ({exc})",
                ) from exc
            if not isinstance(data, dict) or not data:
                raise NetworkError(
                    "Java 运行时索引格式异常（all.json 不是有效对象）",
                    detail=JAVA_RUNTIME_INDEX_URL,
                )
            self._index = data
            try:
                write_json(cache_path, {"fetched_at": time.time(), "index": data})
            except OSError as exc:  # 缓存写失败不影响功能
                log.warning("写入 Java 运行时索引缓存失败 %s: %s", cache_path, exc)
            return data

    @staticmethod
    def _platform_components(
        index: Mapping[str, Any] | dict[str, Any],
    ) -> dict[str, list[dict[str, Any]]]:
        """从 all.json 中取出当前平台的组件表。

        兼容两种结构：

        - Mojang 现行结构：``{"windows-x64": {"<component>": [entry, ...]}}``；
        - 旧式/备用结构：``{"<component>": {"windows-x64": [entry, ...]}}``。
        """
        if not isinstance(index, dict):
            return {}
        platform = index.get(PLATFORM)
        if isinstance(platform, dict):
            result: dict[str, list[dict[str, Any]]] = {}
            for component, entries in platform.items():
                if isinstance(entries, list):
                    result[str(component)] = [
                        entry for entry in entries if isinstance(entry, dict)
                    ]
            if result:
                return result
        result = {}
        for component, platforms in index.items():
            if not isinstance(platforms, dict):
                continue
            entries = platforms.get(PLATFORM)
            if isinstance(entries, list):
                result[str(component)] = [entry for entry in entries if isinstance(entry, dict)]
        return result

    @staticmethod
    def _entry_available(entry: Mapping[str, Any]) -> bool:
        availability = entry.get("availability", entry.get("available"))
        if isinstance(availability, bool):
            return availability
        if isinstance(availability, dict):
            progress = availability.get("progress")
            if progress is None:
                return True
            try:
                return int(progress) >= 100
            except (TypeError, ValueError):
                return True
        return True

    @classmethod
    def _choose_entry(cls, entries: Sequence[dict[str, Any]]) -> dict[str, Any] | None:
        for entry in entries:
            if cls._entry_available(entry):
                return entry
        return None

    def _info_for(
        self, component: str, entries: Sequence[dict[str, Any]]
    ) -> JavaRuntimeInfo | None:
        entry = self._choose_entry(entries) or (entries[0] if entries else None)
        if entry is None:
            return None
        version = entry.get("version")
        if isinstance(version, dict):
            name = str(version.get("name") or "")
        else:
            name = str(version or "")
        return JavaRuntimeInfo(component=component, platform=PLATFORM, version_name=name)

    def _manifest_for(
        self, component: str, entries: Sequence[dict[str, Any]]
    ) -> dict[str, Any]:
        """读取组件平台清单 ``{"files": {...}}``（内存 → 磁盘 → 网络）。"""
        with self._lock:
            cached_memory = self._manifests.get(component)
            if cached_memory is not None:
                return cached_memory
            entry = self._choose_entry(entries)
            if entry is None:
                raise InstallError(
                    f"Java 运行时组件当前不可用：{component}",
                    detail=f"{PLATFORM} 下没有可下载的版本",
                )
            manifest_meta = entry.get("manifest")
            url = str((manifest_meta or {}).get("url") or "") if isinstance(manifest_meta, dict) else ""
            if not url:
                raise NetworkError(
                    "Java 运行时文件清单地址缺失", detail=f"{component}/{PLATFORM}"
                )
            cache_path = self._manifest_cache_path(component)
            cached_payload = read_json(cache_path, default=None)
            cached_files = (
                cached_payload.get("files") if isinstance(cached_payload, dict) else None
            )
            cached_url = (
                str(cached_payload.get("url") or "") if isinstance(cached_payload, dict) else ""
            )
            if (
                isinstance(cached_files, dict)
                and cached_files
                and cached_url == url
                and self._cache_fresh(cached_payload)
            ):
                self._manifests[component] = cached_files
                return cached_files
            try:
                payload = self.http.get_json(url)
            except CubeFetchError as exc:
                if isinstance(cached_files, dict) and cached_files and cached_url == url:
                    log.warning("获取 Java 运行时文件清单失败，使用本地缓存：%s", exc)
                    self._manifests[component] = cached_files
                    return cached_files
                raise NetworkError(
                    "获取 Java 运行时文件清单失败，请检查网络或切换下载镜像",
                    detail=f"{component} ({exc})",
                ) from exc
            files = payload.get("files") if isinstance(payload, dict) else None
            if not isinstance(files, dict) or not files:
                raise NetworkError("Java 运行时文件清单格式异常", detail=url)
            self._manifests[component] = files
            try:
                write_json(cache_path, {"fetched_at": time.time(), "url": url, "files": files})
            except OSError as exc:  # 缓存写失败不影响功能
                log.warning("写入 Java 运行时清单缓存失败 %s: %s", cache_path, exc)
            return files

    # -- 计划 / 安装 ------------------------------------------------------

    def _component_entries(
        self, component: str
    ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
        index = self._load_index()
        platform_components = self._platform_components(index)
        entries = platform_components.get(component)
        if not entries:
            raise InstallError(
                f"没有找到 Java 运行时组件：{component}",
                detail=f"索引中没有 {component} 的 {PLATFORM} 版本",
            )
        return index, platform_components

    def plan(self, component: str) -> list[DownloadTask]:
        """生成该组件的文件下载计划（不写业务文件）。"""
        name = str(component or "").strip()
        if not name:
            raise InstallError("Java 运行时组件名不能为空")
        index, platform_components = self._component_entries(name)
        entries = platform_components[name]
        files = self._manifest_for(name, entries)
        return self._tasks_for(name, files)

    def _tasks_for(self, component: str, files: Mapping[str, Any]) -> list[DownloadTask]:
        base = self.runtime_dir(component)
        tasks: list[DownloadTask] = []
        for relpath, meta in files.items():
            if not isinstance(meta, dict):
                continue
            if meta.get("type") != "file":
                continue  # directory 在 install 时创建；link 在 Windows 平台跳过
            downloads = meta.get("downloads")
            raw = downloads.get("raw") if isinstance(downloads, dict) else None
            if not isinstance(raw, dict):
                continue
            url = str(raw.get("url") or "")
            parts = _relative_parts(relpath)
            if not url or parts is None:
                log.warning("跳过无效的 Java 运行时清单项：%s", relpath)
                continue
            sha1 = str(raw.get("sha1") or "").lower() or None
            tasks.append(
                DownloadTask(
                    url=url,
                    dest=base.joinpath(*parts),
                    sha1=sha1,
                    size=_as_int(raw.get("size")),
                )
            )
        return tasks

    def install(
        self,
        component: str,
        *,
        progress_cb: Callable[[Progress], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> Path:
        """下载并安装组件缺失的文件，返回 ``bin/java.exe`` 路径。

        - 已存在且 sha1/size 校验通过的文件会跳过；
        - ``progress_cb`` 收到 :class:`~cubefetch.core.http.Progress`
          （阶段文字为「正在下载 Java 运行时…」）；
        - 取消时抛 :class:`~cubefetch.core.errors.CancelledError`；
        - 网络失败抛 ``NetworkError``，组件不存在/安装不完整抛 ``InstallError``。
        """
        name = str(component or "").strip()
        if not name:
            raise InstallError("Java 运行时组件名不能为空")
        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消")

        index, platform_components = self._component_entries(name)
        del index
        entries = platform_components[name]
        files = self._manifest_for(name, entries)
        tasks = self._tasks_for(name, files)

        base = self.runtime_dir(name)
        for relpath, meta in files.items():
            if isinstance(meta, dict) and meta.get("type") == "directory":
                parts = _relative_parts(relpath)
                if parts is not None:
                    ensure_dir(base.joinpath(*parts))
        ensure_dir(base / "bin")

        downloader = ThreadedDownloader(
            self.http, workers=max(1, int(getattr(self.http, "workers", 32) or 32))
        )
        downloader.run(
            tasks,
            progress_cb=progress_cb,
            cancel=cancel,
            stage="正在下载 Java 运行时…",
        )

        exe = self.java_executable(name)
        if not exe.is_file():
            raise InstallError(
                "Java 运行时安装不完整：缺少 bin/java.exe",
                detail=str(exe),
            )
        return exe
