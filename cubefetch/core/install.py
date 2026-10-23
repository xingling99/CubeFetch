"""Minecraft 版本安装器：依赖解析、下载、natives 解压与目录布局。

严格遵循 docs/CONTRACTS.md §5 / §7。安装流程分阶段：

1. 静态文件：客户端 jar、libraries（含旧式 natives 分类器 jar）、日志配置、资源索引；
2. 资源对象：``assets/objects/<前两位>/<hash>``，多线程下载；
3. natives 解压：旧式 ``natives`` 映射的库解压到 ``versions/<id>/<id>-natives/``；
4. 旧版 virtual 资源：把 objects 复制/硬链接到 ``assets/virtual/<assetId>/``；
5. 写回 ``versions/<id>/<id>.json``。

设计决策：1.19+ 的 natives 以普通 classpath 依赖形式提供（库名带 ``natives-*``
分类器，jar 内为 ``windows/x64/...`` 结构，由 LWJGL 从 classpath 自行加载），
因此只有带 ``natives`` 映射的旧式库才解压到 natives 目录。
"""
from __future__ import annotations

import copy
import logging
import os
import platform
import re
import shutil
import sys
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Mapping, Sequence

from .errors import CancelledError, CubeFetchError, InstallError
from .http import DownloadTask, HttpSession, Progress, ThreadedDownloader
from .manifest import ManifestService
from .util import ensure_dir, read_json, write_json

log = logging.getLogger("cubefetch.install")

#: 官方资源对象地址前缀
ASSET_OBJECT_BASE = "https://resources.download.minecraft.net/"
#: 官方 libraries 地址前缀
DEFAULT_LIBRARY_BASE = "https://libraries.minecraft.net/"

#: 现代 os.arch 与规则里 arch 别名的映射；规则里的 "x86" 仅匹配 32 位系统。
_ARCH_GROUPS: dict[str, tuple[str, ...]] = {
    "x86_64": ("x86_64", "amd64", "x64"),
    "x86": ("x86", "i386", "i486", "i586", "i686"),
    "arm64": ("arm64", "aarch64"),
    "arm": ("arm", "armv7", "armv7l"),
}

_OS_NAMES = {"win32": "windows", "cygwin": "windows", "darwin": "osx"}


# ---------------------------------------------------------------------------
# 请求与平台识别
# ---------------------------------------------------------------------------


@dataclass
class InstallRequest:
    """一次安装请求。``version_json`` 为目标版本（可以是加载器 profile）。"""

    mc_root: Path
    version_id: str
    version_json: dict = field(default_factory=dict)
    workers: int = 32
    progress_cb: Callable[[Progress], None] | None = None
    cancel: threading.Event | None = None


def detect_os() -> tuple[str, str, str]:
    """返回当前平台 ``(os.name, os.arch, os.version)``，与规则字段一一对应。"""
    raw_platform = sys.platform
    os_name = _OS_NAMES.get(raw_platform)
    if os_name is None:
        os_name = "linux" if raw_platform.startswith("linux") else raw_platform
    machine = platform.machine().lower().strip()
    os_arch = machine
    for canonical, aliases in _ARCH_GROUPS.items():
        if machine in aliases:
            os_arch = canonical
            break
    if os_name == "windows":
        os_version = platform.version()
    elif os_name == "osx":
        os_version = platform.mac_ver()[0] or platform.release()
    else:
        os_version = platform.release()
    return os_name, os_arch, os_version


def _arch_matches(required: str, actual: str) -> bool:
    if not required:
        return True
    required = str(required).lower()
    actual = str(actual).lower()
    if required == actual:
        return True
    return required in _ARCH_GROUPS.get(actual, (actual,))


def _rule_matches(
    rule: Mapping,
    os_name: str,
    os_arch: str,
    os_version: str,
    features: Mapping[str, bool] | None,
) -> bool:
    os_rule = rule.get("os")
    if isinstance(os_rule, Mapping):
        name = os_rule.get("name")
        if name is not None and str(name).lower() != os_name:
            return False
        arch = os_rule.get("arch")
        if arch is not None and not _arch_matches(str(arch), os_arch):
            return False
        version_rule = os_rule.get("version")
        if version_rule is not None:
            try:
                pattern = re.compile(str(version_rule))
            except re.error:
                log.warning("忽略无效的 os.version 规则：%r", version_rule)
                return False
            if not pattern.search(os_version or ""):
                return False
    feature_rule = rule.get("features")
    if isinstance(feature_rule, Mapping):
        for key, expected in feature_rule.items():
            if bool((features or {}).get(str(key), False)) != bool(expected):
                return False
    return True


def evaluate_rules(
    rules: Sequence[Mapping] | None,
    os_name: str | None = None,
    os_arch: str | None = None,
    os_version: str | None = None,
    features: Mapping[str, bool] | None = None,
) -> bool:
    """按 Minecraft 规则求值（最后一条命中的规则生效；无命中则默认允许）。

    ``rules`` 为 None/空 时返回 True。
    """
    if not rules:
        return True
    if os_name is None or os_arch is None:
        detected_name, detected_arch, detected_version = detect_os()
        os_name = os_name if os_name is not None else detected_name
        os_arch = os_arch if os_arch is not None else detected_arch
        os_version = os_version if os_version is not None else detected_version
    allowed = False
    for rule in rules:
        if not isinstance(rule, Mapping):
            continue
        if not _rule_matches(rule, os_name, os_arch, os_version or "", features):
            continue
        allowed = str(rule.get("action", "")).lower() == "allow"
    return allowed


# ---------------------------------------------------------------------------
# 版本继承链合并
# ---------------------------------------------------------------------------


def merge_version_chain(chain: Sequence[dict]) -> dict:
    """合并 ``[目标版本, 父版本, 祖父版本, ...]`` 为一份完整版本 JSON。

    - ``libraries``：父在前、子在后；同一 ``group:artifact`` 以子版本为准并去重；
    - ``arguments``：``game``/``jvm`` 列表父在前子在后，其余键深合并（子覆盖父）；
    - 其它字段：子覆盖父（``mainClass``、``downloads`` 等）；``inheritsFrom`` 保留。
    """
    if not chain:
        raise InstallError("版本 JSON 为空，无法合并继承链")
    merged = copy.deepcopy(dict(chain[-1]))
    for layer in reversed(list(chain[:-1])):
        merged = _merge_layer(merged, dict(layer))
    return merged


def _merge_layer(base: dict, overlay: dict) -> dict:
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        if key == "libraries":
            merged["libraries"] = _merge_libraries(base.get("libraries"), value)
        elif key == "arguments":
            merged["arguments"] = _merge_arguments(base.get("arguments"), value)
        elif key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _merge_libraries(base: Sequence | None, overlay: Sequence | None) -> list:
    overlay_list = [lib for lib in (overlay or []) if isinstance(lib, dict)]
    base_list = [lib for lib in (base or []) if isinstance(lib, dict)]
    keys = {_library_key(lib) for lib in overlay_list}
    merged = [copy.deepcopy(lib) for lib in overlay_list]
    for lib in base_list:
        if _library_key(lib) not in keys:
            merged.append(copy.deepcopy(lib))
    return merged


def _library_key(lib: Mapping) -> tuple[str, str, str]:
    """库去重键：``(group, artifact, classifier)``。

    classifier 参与键计算，避免子版本覆盖同名 artifact 时误删父版本的
    natives 变体（如 ``org.lwjgl:lwjgl:3.3.1`` 与 ``...:natives-windows``）。
    """
    name = str(lib.get("name") or "").split("@")[0]
    parts = name.split(":")
    group = parts[0] if parts else ""
    artifact = parts[1] if len(parts) > 1 else ""
    classifier = parts[3] if len(parts) > 3 else ""
    return group, artifact, classifier


def _merge_arguments(base, overlay) -> dict:
    if not isinstance(base, dict):
        return copy.deepcopy(overlay) if isinstance(overlay, dict) else {}
    if not isinstance(overlay, dict):
        return copy.deepcopy(base)
    merged = copy.deepcopy(base)
    for key, value in overlay.items():
        old = merged.get(key)
        if isinstance(old, list) and isinstance(value, list):
            merged[key] = list(old) + list(value)
        elif isinstance(old, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(old, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _deep_merge(base, overlay):
    if isinstance(base, dict) and isinstance(overlay, dict):
        merged = copy.deepcopy(base)
        for key, value in overlay.items():
            if key in merged:
                merged[key] = _deep_merge(merged[key], value)
            else:
                merged[key] = copy.deepcopy(value)
        return merged
    return copy.deepcopy(overlay)


def _client_jar_layer(chain: Sequence[tuple[str, dict]]) -> tuple[str, dict] | None:
    """继承链中第一个提供 ``downloads.client`` 的层级（通常是父版本）。"""
    for version_id, data in chain:
        downloads = data.get("downloads")
        if not isinstance(downloads, dict):
            continue
        client = downloads.get("client")
        if isinstance(client, dict) and client.get("url"):
            return version_id, client
    return None


# ---------------------------------------------------------------------------
# 库文件解析
# ---------------------------------------------------------------------------


def maven_path_from_name(name: str, classifier: str | None = None) -> str:
    """把 ``group:artifact:version[:classifier][@ext]`` 转成 Maven 相对路径。"""
    coord, _, extension = str(name or "").partition("@")
    extension = extension or "jar"
    parts = coord.split(":")
    if len(parts) < 3 or not parts[0] or not parts[1] or not parts[2]:
        raise InstallError("无效的 Maven 依赖坐标", detail=str(name))
    group, artifact, version = parts[0], parts[1], parts[2]
    builtin_classifier = parts[3] if len(parts) >= 4 else None
    final_classifier = classifier or builtin_classifier
    filename = f"{artifact}-{version}"
    if final_classifier:
        filename += f"-{final_classifier}"
    filename += f".{extension}"
    return "/".join([*group.split("."), artifact, version, filename])


def _library_base_url(lib: Mapping) -> str:
    base = str(lib.get("url") or DEFAULT_LIBRARY_BASE)
    return base if base.endswith("/") else base + "/"


def library_artifact(lib: Mapping) -> dict | None:
    """解析库主 artifact（``libraries/`` 下的文件）；无需下载时返回 None。

    - 有 ``downloads.artifact`` 时直接用；
    - 无 ``downloads`` 字段的库（如 Fabric profile）按 Maven 坐标 + ``url`` 构造；
    - 只声明了 ``downloads.classifiers`` 而没有主 artifact 的库（旧式 natives）返回 None。
    """
    downloads = lib.get("downloads")
    if isinstance(downloads, dict):
        artifact = downloads.get("artifact")
        if isinstance(artifact, dict) and artifact.get("path"):
            return dict(artifact)
        if lib.get("natives") or artifact is not None or downloads.get("classifiers"):
            return None
    name = lib.get("name")
    if not name:
        return None
    path = maven_path_from_name(str(name))
    return {"path": path, "url": _library_base_url(lib) + path, "sha1": None, "size": None}


def native_classifier(
    lib: Mapping, os_name: str | None = None, os_arch: str | None = None
) -> str | None:
    """返回旧式 natives 库在当前平台应使用的 classifier（无则 None）。"""
    natives = lib.get("natives")
    if not isinstance(natives, Mapping):
        return None
    if os_name is None or os_arch is None:
        detected_name, detected_arch, _ = detect_os()
        os_name = os_name if os_name is not None else detected_name
        os_arch = os_arch if os_arch is not None else detected_arch
    template = natives.get(os_name)
    if not isinstance(template, str) or not template:
        return None
    arch_bits = "32" if str(os_arch) in ("x86", "i386", "i486", "i586", "i686", "arm") else "64"
    return template.replace("${arch}", arch_bits)


def library_native_artifact(
    lib: Mapping, os_name: str | None = None, os_arch: str | None = None
) -> tuple[str, dict] | None:
    """解析旧式 natives 分类器文件；返回 ``(classifier, artifact)`` 或 None。"""
    classifier = native_classifier(lib, os_name, os_arch)
    if not classifier:
        return None
    downloads = lib.get("downloads")
    if isinstance(downloads, dict):
        classifiers = downloads.get("classifiers")
        if isinstance(classifiers, dict):
            artifact = classifiers.get(classifier)
            if isinstance(artifact, dict) and artifact.get("path"):
                return classifier, dict(artifact)
            if classifiers:
                # 声明了分类器但缺少当前平台的分类器：视为该平台无需该库。
                return None
    name = lib.get("name")
    if not name:
        return None
    path = maven_path_from_name(str(name), classifier)
    return classifier, {
        "path": path,
        "url": _library_base_url(lib) + path,
        "sha1": None,
        "size": None,
    }


def relevant_libraries(
    version_json: Mapping,
    os_name: str | None = None,
    os_arch: str | None = None,
    os_version: str | None = None,
    features: Mapping[str, bool] | None = None,
) -> list[dict]:
    """按规则过滤出当前平台需要安装的库。"""
    result: list[dict] = []
    for lib in version_json.get("libraries") or []:
        if not isinstance(lib, dict):
            continue
        if not evaluate_rules(
            lib.get("rules"),
            os_name=os_name,
            os_arch=os_arch,
            os_version=os_version,
            features=features,
        ):
            continue
        result.append(lib)
    return result


# ---------------------------------------------------------------------------
# 资源路径
# ---------------------------------------------------------------------------


def asset_object_relpath(asset_hash: str) -> str:
    normalized = str(asset_hash or "").strip().lower()
    if len(normalized) < 2:
        raise InstallError("资源哈希无效", detail=str(asset_hash))
    return f"{normalized[:2]}/{normalized}"


def asset_object_path(mc_root: Path | str, asset_hash: str) -> Path:
    return Path(mc_root) / "assets" / "objects" / asset_object_relpath(asset_hash)


def asset_object_url(asset_hash: str, base: str | None = None) -> str:
    root = (base or ASSET_OBJECT_BASE)
    if not root.endswith("/"):
        root += "/"
    return root + asset_object_relpath(asset_hash)


def asset_virtual_dir(mc_root: Path | str, asset_id: str) -> Path:
    return Path(mc_root) / "assets" / "virtual" / str(asset_id)


def _safe_relative_name(name: str) -> str | None:
    if not name:
        return None
    normalized = str(name).replace("\\", "/").strip()
    if not normalized or normalized.startswith("/"):
        return None
    parts = normalized.split("/")
    if any(part in ("", ".", "..") for part in parts):
        return None
    return normalized


# ---------------------------------------------------------------------------
# natives 解压
# ---------------------------------------------------------------------------


def _is_excluded(name: str, excludes: Iterable[str]) -> bool:
    for prefix in excludes or ():
        text = str(prefix).replace("\\", "/").strip()
        if not text:
            continue
        if name == text.rstrip("/") or name.startswith(text):
            return True
    return False


def _native_entries(jar: Path, excludes: Sequence[str]) -> list[zipfile.ZipInfo]:
    entries: list[zipfile.ZipInfo] = []
    with zipfile.ZipFile(jar) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            if _safe_relative_name(name) is None:
                continue
            if _is_excluded(name, excludes):
                continue
            entries.append(info)
    return entries


def natives_extracted(
    natives_dir: Path | str, jars: Sequence[tuple[Path | str, Sequence[str]]]
) -> bool:
    """检查所有 natives 文件是否已解压且大小一致（用于跳过重复解压）。"""
    root = Path(natives_dir)
    if not root.is_dir():
        return False
    for jar, excludes in jars:
        jar_path = Path(jar)
        if not jar_path.is_file():
            return False
        try:
            entries = _native_entries(jar_path, excludes)
        except (OSError, zipfile.BadZipFile):
            return False
        for info in entries:
            target = root / info.filename.replace("\\", "/")
            try:
                if not target.is_file() or target.stat().st_size != info.file_size:
                    return False
            except OSError:
                return False
    return True


def extract_natives(
    natives_dir: Path | str,
    jars: Sequence[tuple[Path | str, Sequence[str]]],
    *,
    cancel: threading.Event | None = None,
) -> int:
    """把旧式 natives jar 解压到 ``natives_dir``，返回解压文件数。"""
    root = Path(natives_dir)
    ensure_dir(root)
    extracted = 0
    for jar, excludes in jars:
        _ensure_not_cancelled(cancel)
        jar_path = Path(jar)
        if not jar_path.is_file():
            raise InstallError("natives 文件缺失，无法解压", detail=str(jar_path))
        try:
            with zipfile.ZipFile(jar_path) as archive:
                for info in archive.infolist():
                    if info.is_dir():
                        continue
                    name = info.filename.replace("\\", "/")
                    if _safe_relative_name(name) is None:
                        continue
                    if _is_excluded(name, excludes):
                        continue
                    _ensure_not_cancelled(cancel)
                    target = root / name
                    ensure_dir(target.parent)
                    with archive.open(info) as source, open(target, "wb") as output:
                        shutil.copyfileobj(source, output)
                    extracted += 1
        except zipfile.BadZipFile as exc:
            raise InstallError("natives 压缩包损坏，请重新下载", detail=str(jar_path)) from exc
    return extracted


# ---------------------------------------------------------------------------
# 安装器
# ---------------------------------------------------------------------------


class Installer:
    """完整安装一个 Minecraft 版本（含加载器继承链）。"""

    def __init__(
        self,
        http: HttpSession,
        manifest: ManifestService,
        gate=None,
    ) -> None:
        self.http = http
        self.manifest = manifest
        self.gate = gate

    # -- 对外接口 ---------------------------------------------------------

    def plan(self, req: InstallRequest) -> list[DownloadTask]:
        """纯静态规划（不联网、不写盘）：返回需要下载的静态文件任务。

        包含客户端 jar、libraries、旧式 natives 分类器、日志配置与资源索引；
        资源对象（assets/objects）依赖下载后的资源索引，由 :meth:`install` 负责。
        """
        chain = self._resolve_chain(req, allow_network=False)
        return self._static_tasks(req, chain)

    def install(self, req: InstallRequest) -> Path:
        """执行安装，返回 ``versions/<id>`` 目录。"""
        if self.gate is not None:
            self.gate.require_active()
        _ensure_not_cancelled(req.cancel)
        mc_root = Path(req.mc_root)
        version_id = str(req.version_id or "").strip()
        if not version_id:
            raise InstallError("安装请求缺少版本号")
        if not req.version_json:
            raise InstallError("安装请求缺少版本 JSON", detail=version_id)

        chain = self._resolve_chain(req, allow_network=True)
        version_dir = mc_root / "versions" / version_id
        try:
            ensure_dir(version_dir)
            downloader = ThreadedDownloader(self.http, workers=self._workers(req))

            # ---- 阶段 1：静态文件 ----
            static_tasks = self._static_tasks(req, chain)
            static_total = _tasks_bytes(static_tasks)
            static_files = len(static_tasks)
            self._emit(
                req.progress_cb,
                Progress(
                    done=0,
                    total=static_files,
                    bytes_done=0,
                    bytes_total=static_total,
                    message="正在准备下载文件",
                    stage="正在下载游戏文件",
                    stage_done=0,
                    stage_total=static_files,
                ),
            )

            def on_static(progress: Progress) -> None:
                # 静态阶段的“完成”事件暂缓发送：接下来资源阶段会把总文件数扩大，
                # 提前发 100% 会让界面先满格再回跳。
                if progress.done >= static_files:
                    return
                self._emit(
                    req.progress_cb,
                    Progress(
                        done=progress.done,
                        total=static_files,
                        bytes_done=progress.bytes_done,
                        bytes_total=max(static_total, progress.bytes_done),
                        speed=progress.speed,
                        message="正在下载游戏文件",
                        stage="正在下载游戏文件",
                        stage_done=progress.stage_done,
                        stage_total=static_files,
                    ),
                )

            downloader.run(
                static_tasks,
                progress_cb=on_static,
                cancel=req.cancel,
                stage="正在下载游戏文件",
            )
            static_done_bytes = _actual_bytes(static_tasks)
            self._materialize_parent_json(mc_root, chain)
            _ensure_not_cancelled(req.cancel)

            # ---- 阶段 2：资源对象 ----
            asset_tasks, asset_index, virtual_asset_id = self._asset_tasks(req, chain)
            asset_count = len(asset_tasks)
            total_files = static_files + asset_count
            total_bytes = static_done_bytes + _tasks_bytes(asset_tasks)
            # 进入资源阶段前先把最终的“整体口径”定下来并补发一次事件：
            # 之后 total/bytes_total 不再变化，避免界面分母切换、百分比回跳。
            self._emit(
                req.progress_cb,
                Progress(
                    done=static_files,
                    total=total_files,
                    bytes_done=static_done_bytes,
                    bytes_total=total_bytes,
                    message="正在下载资源文件" if asset_count else "正在准备安装文件",
                    stage="正在下载资源文件" if asset_count else "",
                    stage_done=0,
                    stage_total=asset_count,
                ),
            )
            if asset_tasks:

                def on_assets(progress: Progress) -> None:
                    self._emit(
                        req.progress_cb,
                        Progress(
                            done=static_files + min(progress.done, asset_count),
                            total=total_files,
                            bytes_done=static_done_bytes + progress.bytes_done,
                            bytes_total=max(
                                total_bytes, static_done_bytes + progress.bytes_done
                            ),
                            speed=progress.speed,
                            message="正在下载资源文件",
                            stage="正在下载资源文件",
                            stage_done=min(progress.done, asset_count),
                            stage_total=asset_count,
                        ),
                    )

                downloader.run(
                    asset_tasks,
                    progress_cb=on_assets,
                    cancel=req.cancel,
                    stage="正在下载资源文件",
                )
                total_bytes = static_done_bytes + _actual_bytes(asset_tasks)
            _ensure_not_cancelled(req.cancel)

            # ---- 阶段 3：natives 解压 ----
            native_jars = self._native_extract_plan(req, chain)
            if native_jars:
                natives_dir = version_dir / f"{version_id}-natives"
                if not natives_extracted(natives_dir, native_jars):
                    self._emit(
                        req.progress_cb,
                        Progress(
                            done=total_files,
                            total=total_files,
                            bytes_done=total_bytes,
                            bytes_total=total_bytes,
                            message="正在解压 natives",
                        ),
                    )
                    ensure_dir(natives_dir)
                    extract_natives(natives_dir, native_jars, cancel=req.cancel)
            _ensure_not_cancelled(req.cancel)

            # ---- 阶段 4：旧版 virtual 资源 ----
            if virtual_asset_id is not None and asset_index is not None:
                self._emit(
                    req.progress_cb,
                    Progress(
                        done=total_files,
                        total=total_files,
                        bytes_done=total_bytes,
                        bytes_total=total_bytes,
                        message="正在整理旧版资源文件",
                    ),
                )
                self._build_virtual_assets(mc_root, virtual_asset_id, asset_index, req.cancel)

            # ---- 阶段 5：写回版本 JSON ----
            self._emit(
                req.progress_cb,
                Progress(
                    done=total_files,
                    total=total_files,
                    bytes_done=total_bytes,
                    bytes_total=total_bytes,
                    message="正在写入版本信息",
                ),
            )
            self._write_version_json(req, version_dir, version_id)
            self._emit(
                req.progress_cb,
                Progress(
                    done=total_files,
                    total=total_files,
                    bytes_done=total_bytes,
                    bytes_total=total_bytes,
                    message="安装完成",
                ),
            )
            return version_dir
        except CancelledError:
            raise
        except CubeFetchError:
            raise
        except OSError as exc:
            raise InstallError(
                "安装失败：无法写入目标目录，请检查磁盘空间与权限",
                detail=f"{version_dir}: {exc}",
            ) from exc

    # -- 版本链 -----------------------------------------------------------

    def _resolve_chain(
        self, req: InstallRequest, *, allow_network: bool
    ) -> list[tuple[str, dict]]:
        version_id = str(req.version_id or "").strip()
        if not version_id:
            raise InstallError("安装请求缺少版本号")
        if not isinstance(req.version_json, dict) or not req.version_json:
            raise InstallError("安装请求缺少版本 JSON", detail=version_id)
        chain: list[tuple[str, dict]] = [(version_id, req.version_json)]
        seen = {version_id}
        current = req.version_json
        while True:
            parent_id = current.get("inheritsFrom")
            if not isinstance(parent_id, str) or not parent_id.strip():
                break
            parent_id = parent_id.strip()
            if parent_id in seen:
                raise InstallError("版本继承链存在循环引用", detail=parent_id)
            seen.add(parent_id)
            parent = self._load_parent_json(req, parent_id, allow_network=allow_network)
            chain.append((parent_id, parent))
            current = parent
            if len(chain) > 16:
                raise InstallError("版本继承层级过深", detail=version_id)
        return chain

    def _load_parent_json(
        self, req: InstallRequest, parent_id: str, *, allow_network: bool
    ) -> dict:
        mc_root = Path(req.mc_root)
        disk_path = mc_root / "versions" / parent_id / f"{parent_id}.json"
        cached = read_json(disk_path, None)
        if isinstance(cached, dict) and cached.get("id"):
            return cached
        cached = None
        loader = getattr(self.manifest, "load_cached_version_json", None)
        if callable(loader):
            try:
                cached = loader(parent_id)
            except CubeFetchError:
                cached = None
        if isinstance(cached, dict) and cached:
            return cached
        if not allow_network:
            raise InstallError(
                f"缺少父版本 {parent_id} 的版本文件，请先安装该版本",
                detail=str(disk_path),
            )
        try:
            data = self.manifest.fetch_version_json(parent_id)
        except CubeFetchError as exc:
            raise InstallError(
                f"无法获取父版本 {parent_id} 的版本信息，请先安装该版本",
                detail=str(exc),
            ) from exc
        if not isinstance(data, dict) or not data:
            raise InstallError(
                f"父版本 {parent_id} 的版本信息无效", detail=str(disk_path)
            )
        return data

    def _materialize_parent_json(
        self, mc_root: Path, chain: Sequence[tuple[str, dict]]
    ) -> None:
        for parent_id, data in chain[1:]:
            path = mc_root / "versions" / parent_id / f"{parent_id}.json"
            existing = read_json(path, None)
            if isinstance(existing, dict) and existing.get("id"):
                continue
            ensure_dir(path.parent)
            write_json(path, data)

    # -- 静态任务 ---------------------------------------------------------

    def _static_tasks(
        self, req: InstallRequest, chain: Sequence[tuple[str, dict]]
    ) -> list[DownloadTask]:
        mc_root = Path(req.mc_root)
        os_name, os_arch, os_version = detect_os()
        merged = merge_version_chain([data for _, data in chain])
        tasks: list[DownloadTask] = []
        used_destinations: set[Path] = set()

        def add(url: str | None, dest: Path, sha1: str | None = None, size=None) -> None:
            if not url:
                return
            destination = Path(dest)
            if destination in used_destinations:
                return
            used_destinations.add(destination)
            tasks.append(
                DownloadTask(url=str(url), dest=destination, sha1=sha1, size=size)
            )

        # 客户端 jar：加载器 profile 继承父版本时，jar 位于父版本目录。
        client_layer = _client_jar_layer(chain)
        if client_layer is not None:
            jar_id, client = client_layer
            add(
                client.get("url"),
                mc_root / "versions" / jar_id / f"{jar_id}.jar",
                client.get("sha1"),
                client.get("size"),
            )

        # libraries：主 artifact + 旧式 natives 分类器
        for lib in relevant_libraries(merged, os_name, os_arch, os_version):
            artifact = library_artifact(lib)
            if artifact is not None:
                add(
                    artifact.get("url"),
                    mc_root / "libraries" / str(artifact["path"]),
                    artifact.get("sha1"),
                    artifact.get("size"),
                )
            native = library_native_artifact(lib, os_name, os_arch)
            if native is not None:
                _, native_artifact = native
                add(
                    native_artifact.get("url"),
                    mc_root / "libraries" / str(native_artifact["path"]),
                    native_artifact.get("sha1"),
                    native_artifact.get("size"),
                )

        # 日志配置
        logging_config = merged.get("logging")
        if isinstance(logging_config, dict):
            client_logging = logging_config.get("client")
            if isinstance(client_logging, dict):
                log_file = client_logging.get("file")
                if isinstance(log_file, dict) and log_file.get("url") and log_file.get("id"):
                    file_id = str(log_file["id"])
                    add(
                        log_file.get("url"),
                        mc_root / "assets" / "log_configs" / file_id,
                        log_file.get("sha1"),
                        log_file.get("size"),
                    )

        # 资源索引
        asset_index = merged.get("assetIndex")
        if isinstance(asset_index, dict):
            index_id = str(asset_index.get("id") or "").strip()
            if index_id and asset_index.get("url"):
                add(
                    asset_index.get("url"),
                    mc_root / "assets" / "indexes" / f"{index_id}.json",
                    asset_index.get("sha1"),
                    asset_index.get("size"),
                )
        return tasks

    # -- 资源对象 ---------------------------------------------------------

    def _asset_tasks(
        self, req: InstallRequest, chain: Sequence[tuple[str, dict]]
    ) -> tuple[list[DownloadTask], dict | None, str | None]:
        mc_root = Path(req.mc_root)
        merged = merge_version_chain([data for _, data in chain])
        index_info = merged.get("assetIndex")
        if not isinstance(index_info, dict):
            return [], None, None
        index_id = str(index_info.get("id") or "").strip()
        if not index_id:
            return [], None, None
        index_path = mc_root / "assets" / "indexes" / f"{index_id}.json"
        index = read_json(index_path, None)
        if not isinstance(index, dict):
            raise InstallError(
                "资源索引文件缺失或损坏，请重试安装", detail=str(index_path)
            )
        objects = index.get("objects")
        if not isinstance(objects, dict):
            objects = {}
        tasks: list[DownloadTask] = []
        seen_hashes: set[str] = set()
        for entry in objects.values():
            if not isinstance(entry, dict):
                continue
            asset_hash = str(entry.get("hash") or "").strip().lower()
            if len(asset_hash) < 2 or any(ch not in "0123456789abcdef" for ch in asset_hash):
                log.warning("跳过无效的资源对象哈希：%r", entry.get("hash"))
                continue
            if asset_hash in seen_hashes:
                continue
            seen_hashes.add(asset_hash)
            size = entry.get("size")
            try:
                size_int = int(size) if size is not None else None
            except (TypeError, ValueError):
                size_int = None
            tasks.append(
                DownloadTask(
                    url=asset_object_url(asset_hash),
                    dest=asset_object_path(mc_root, asset_hash),
                    sha1=asset_hash,
                    size=size_int,
                )
            )
        virtual_asset_id = index_id if _needs_virtual_assets(merged, index, index_id) else None
        return tasks, index, virtual_asset_id

    def _build_virtual_assets(
        self,
        mc_root: Path,
        virtual_asset_id: str,
        index: dict,
        cancel: threading.Event | None,
    ) -> None:
        target_dir = asset_virtual_dir(mc_root, virtual_asset_id)
        objects = index.get("objects")
        if not isinstance(objects, dict):
            objects = {}
        ensure_dir(target_dir)
        for logical_name, entry in objects.items():
            _ensure_not_cancelled(cancel)
            safe_name = _safe_relative_name(str(logical_name))
            if safe_name is None:
                log.warning("跳过不安全的资源文件名：%r", logical_name)
                continue
            if not isinstance(entry, dict):
                continue
            asset_hash = str(entry.get("hash") or "").strip().lower()
            if len(asset_hash) < 2:
                continue
            source = asset_object_path(mc_root, asset_hash)
            if not source.is_file():
                raise InstallError(
                    "资源文件缺失，无法构建旧版资源目录", detail=str(source)
                )
            target = target_dir / safe_name
            try:
                if target.is_file() and target.stat().st_size == source.stat().st_size:
                    continue
                ensure_dir(target.parent)
                try:
                    os.link(source, target)
                except OSError:
                    shutil.copy2(source, target)
            except OSError as exc:
                raise InstallError(
                    "构建旧版资源目录失败", detail=f"{target}: {exc}"
                ) from exc

    # -- natives ----------------------------------------------------------

    def _native_extract_plan(
        self, req: InstallRequest, chain: Sequence[tuple[str, dict]]
    ) -> list[tuple[Path, tuple[str, ...]]]:
        """返回需要解压的旧式 natives ``[(jar 路径, 排除前缀), ...]``。"""
        mc_root = Path(req.mc_root)
        os_name, os_arch, os_version = detect_os()
        merged = merge_version_chain([data for _, data in chain])
        plan: list[tuple[Path, tuple[str, ...]]] = []
        seen: set[Path] = set()
        for lib in relevant_libraries(merged, os_name, os_arch, os_version):
            native = library_native_artifact(lib, os_name, os_arch)
            if native is None:
                continue
            _, artifact = native
            jar_path = mc_root / "libraries" / str(artifact["path"])
            if jar_path in seen:
                continue
            seen.add(jar_path)
            excludes: tuple[str, ...] = ()
            extract = lib.get("extract")
            if isinstance(extract, dict):
                raw_excludes = extract.get("exclude")
                if isinstance(raw_excludes, (list, tuple)):
                    excludes = tuple(str(item) for item in raw_excludes)
            plan.append((jar_path, excludes))
        return plan

    # -- 杂项 -------------------------------------------------------------

    def _workers(self, req: InstallRequest) -> int:
        workers = int(req.workers or 0)
        if workers <= 0:
            workers = int(getattr(self.http, "workers", 32) or 32)
        return max(1, min(workers, 64))

    @staticmethod
    def _write_version_json(
        req: InstallRequest, version_dir: Path, version_id: str
    ) -> None:
        path = version_dir / f"{version_id}.json"
        write_json(path, req.version_json)

    @staticmethod
    def _emit(
        callback: Callable[[Progress], None] | None, progress: Progress
    ) -> None:
        if callback is None:
            return
        try:
            callback(progress)
        except Exception:  # noqa: BLE001 - 用户回调异常不影响安装
            log.debug("进度回调抛出异常", exc_info=True)


def _needs_virtual_assets(version_json: Mapping, index: Mapping, index_id: str) -> bool:
    if bool(index.get("virtual")):
        return True
    assets_field = str(version_json.get("assets") or "")
    if assets_field in ("legacy", "pre-1.6"):
        return True
    return index_id in ("legacy", "pre-1.6")


def _ensure_not_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("安装已取消")


def _tasks_bytes(tasks: Sequence[DownloadTask]) -> int:
    return sum(int(task.size or 0) for task in tasks)


def _actual_bytes(tasks: Sequence[DownloadTask]) -> int:
    total = 0
    for task in tasks:
        if task.size is not None:
            total += int(task.size)
            continue
        try:
            total += task.dest.stat().st_size
        except OSError:
            continue
    return total
