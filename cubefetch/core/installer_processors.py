"""Forge / NeoForge 安装器的 processors 执行引擎。

官方 installer 的客户端安装流程本质上是：

1. 下载 ``install_profile.json`` 中 ``libraries`` 声明的库（产物 + 工具链）；
2. 解析 ``data`` 表，把 ``[maven 坐标]``、``/data/xxx.lzma`` 等展开成绝对路径；
3. 依次执行 ``processors``（MCP_DATA、DOWNLOAD_MOJMAPS、MERGE_MAPPING、
   jarsplitter、AutoRenamingTool、binarypatcher），产出打过补丁的客户端 jar；
4. 写出 installer jar 内嵌的 ``version.json``（``inheritsFrom`` 指回原版）。

本模块自己实现上述步骤，从而绕过官方 installer 中 ``DOWNLOAD_MOJMAPS``
硬编码 ``launchermeta.mojang.com`` 的问题（该地址在部分网络连接会被重置）。
官方 installer 路径仍保留在 :mod:`cubefetch.core.loader` 中作为回退开关。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import threading
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .errors import CancelledError, CubeFetchError, InstallError, NetworkError
from .http import BMCLAPI_BASE, DownloadTask, HttpSession, Progress, ThreadedDownloader
from .util import atomic_write_bytes, ensure_dir, read_json, run_process, sha1_file, write_json

log = logging.getLogger("cubefetch.installer_processors")

#: install_profile.json 在 installer jar 中的路径
PROFILE_ENTRY = "install_profile.json"
#: 安装器内嵌的版本 JSON 路径
VERSION_ENTRY = "version.json"
#: 工具 jar 的清单路径
MANIFEST_ENTRY = "META-INF/MANIFEST.MF"

#: 只处理客户端安装（本下载器不安装服务端）
CLIENT_SIDE = "client"

#: 单个 processor 的超时时间（大版本合并映射可能耗时数分钟）
PROCESSOR_TIMEOUT = 1800.0
#: processor 失败后的重试次数
PROCESSOR_RETRIES = 1

#: 需要特殊处理的 processor 任务名（官方实现会硬编码 Mojang 官方地址）
DOWNLOAD_MOJMAPS_TASK = "DOWNLOAD_MOJMAPS"

_LIBRARIES_SUBDIR = "libraries"
_INSTALLER_CACHE_SUBDIR = (".cubefetch", "installers")
_COORD_RE = re.compile(r"^\[([^\]]+)\]$")
_QUOTED_RE = re.compile(r"^(['\"])(.*)\1$")
_PLACEHOLDER_RE = re.compile(r"\{([A-Za-z0-9_]+)\}")
_SHA1_RE = re.compile(r"^[0-9a-fA-F]{40}$")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InstallerProfile:
    """installer jar 中解析出来的安装配置。"""

    jar_path: Path
    profile: dict[str, Any]
    version_json: dict[str, Any]
    minecraft: str
    profile_id: str


@dataclass(frozen=True)
class LibraryItem:
    """一个需要预置到 ``<mc_root>/libraries/`` 的文件。"""

    name: str
    path: str
    url: str | None = None
    sha1: str | None = None
    size: int | None = None

    def destination(self, mc_root: Path) -> Path:
        return Path(mc_root) / _LIBRARIES_SUBDIR / Path(self.path)


@dataclass
class PreseedReport:
    """依赖库预置结果。"""

    missing: list[str] = field(default_factory=list)
    skipped: int = 0
    pending: int = 0


# ---------------------------------------------------------------------------
# 坐标 / 路径工具
# ---------------------------------------------------------------------------


def maven_path(name: str) -> str:
    """把 Maven 坐标转成 ``libraries/`` 下的相对路径。

    ``group:artifact:version[:classifier][@ext]`` → ``group/path/artifact/version/name.ext``。
    例如 ``net.neoforged:AutoRenamingTool:2.0.3:all@jar`` →
    ``net/neoforged/AutoRenamingTool/2.0.3/AutoRenamingTool-2.0.3-all.jar``。
    """
    text = str(name or "").strip()
    if not text:
        raise ValueError("空的 Maven 坐标")
    extension = "jar"
    if "@" in text:
        text, extension = text.rsplit("@", 1)
        text = text.strip()
        extension = extension.strip() or "jar"
    parts = text.split(":")
    if len(parts) < 3:
        raise ValueError(f"无法解析 Maven 坐标：{name}")
    group, artifact, version = parts[0].strip(), parts[1].strip(), parts[2].strip()
    classifier = parts[3].strip() if len(parts) > 3 else ""
    if not group or not artifact or not version:
        raise ValueError(f"无法解析 Maven 坐标：{name}")
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{extension}"
    return "/".join([*group.split("."), artifact, version, filename])


def _safe_maven_path(name: str) -> str | None:
    try:
        return maven_path(name)
    except ValueError:
        return None


def required_java_major(minecraft_version: str) -> int:
    """根据 Minecraft 版本推断运行安装补丁所需 Java 大版本。"""
    text = str(minecraft_version or "").strip()
    match = re.match(r"^(\d+)\.(\d+)", text)
    if not match:
        # 快照版本：23w/24w 等；1.21+ 已统一要求 21
        return 21 if text.startswith(("24w", "25w", "26w")) else 17
    major = int(match.group(1))
    minor = int(match.group(2))
    if major >= 25:
        return 25
    if major != 1:
        return 21
    if minor >= 21:
        return 21
    if minor >= 20:
        # 1.20.5 起要求 Java 21
        patch_match = re.match(r"^1\.(\d+)\.(\d+)", text)
        patch = int(patch_match.group(2)) if patch_match else 0
        return 21 if (minor, patch) >= (20, 5) else 17
    if minor == 18 or minor == 19:
        return 17
    if minor == 17:
        return 16
    return 8


def _strip_quotes(text: str) -> str:
    stripped = str(text or "").strip()
    match = _QUOTED_RE.match(stripped)
    if match:
        return match.group(2)
    return stripped


def _raise_if_cancelled(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise CancelledError("操作已取消")


def _emit(progress_cb: Callable[[Progress], None] | None, message: str) -> None:
    if progress_cb is None:
        return
    try:
        progress_cb(Progress(message=message))
    except Exception:  # pragma: no cover - 进度回调不应影响安装
        log.debug("进度回调异常", exc_info=True)


def _tail_output(stdout: str | None, stderr: str | None, *, limit: int = 15) -> str:
    lines: list[str] = []
    for text in (stdout or "", stderr or ""):
        for line in str(text).splitlines():
            stripped = line.strip()
            if stripped:
                lines.append(stripped)
    return "\n".join(lines[-limit:])


# ---------------------------------------------------------------------------
# installer jar 读取
# ---------------------------------------------------------------------------


def read_installer(jar_path: Path) -> InstallerProfile:
    """读取 installer jar 中的 ``install_profile.json`` 与内嵌 ``version.json``。"""
    jar_path = Path(jar_path)
    if not jar_path.is_file():
        raise InstallError("安装器文件不存在，无法执行补丁流程", detail=str(jar_path))
    try:
        with zipfile.ZipFile(jar_path) as archive:
            names = set(archive.namelist())
            if PROFILE_ENTRY not in names:
                raise InstallError(
                    "安装器内缺少 install_profile.json，无法执行补丁流程",
                    detail=str(jar_path),
                )
            if VERSION_ENTRY not in names:
                raise InstallError(
                    "安装器内缺少 version.json，无法执行补丁流程",
                    detail=str(jar_path),
                )
            raw_profile = archive.read(PROFILE_ENTRY)
            raw_version = archive.read(VERSION_ENTRY)
    except InstallError:
        raise
    except zipfile.BadZipFile as exc:
        raise InstallError("无法读取安装器文件（不是有效的 jar）", detail=str(exc)) from exc
    except OSError as exc:
        raise InstallError("读取安装器文件失败", detail=str(exc)) from exc

    try:
        profile = json.loads(raw_profile.decode("utf-8"))
        version_json = json.loads(raw_version.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InstallError("安装器配置解析失败", detail=str(exc)) from exc
    if not isinstance(profile, dict) or not isinstance(version_json, dict):
        raise InstallError("安装器配置格式不正确", detail=str(jar_path))

    minecraft = str(profile.get("minecraft") or version_json.get("inheritsFrom") or "").strip()
    profile_id = str(version_json.get("id") or "").strip()
    if not minecraft:
        raise InstallError("安装器未声明对应的 Minecraft 版本", detail=str(jar_path))
    if not profile_id:
        raise InstallError("安装器未声明生成的版本 id", detail=str(jar_path))
    return InstallerProfile(
        jar_path=jar_path,
        profile=profile,
        version_json=version_json,
        minecraft=minecraft,
        profile_id=profile_id,
    )


# ---------------------------------------------------------------------------
# libraries 枚举 / data 解析
# ---------------------------------------------------------------------------


def library_items(profile: InstallerProfile) -> list[LibraryItem]:
    """枚举 install_profile 中需要预置的库（按 maven 路径去重）。"""
    raw_libraries = profile.profile.get("libraries") or []
    if not isinstance(raw_libraries, list):
        raise InstallError("install_profile.json 的 libraries 字段格式不正确")
    items: dict[str, LibraryItem] = {}
    for raw in raw_libraries:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        downloads = raw.get("downloads") if isinstance(raw.get("downloads"), dict) else {}
        artifact = downloads.get("artifact") if isinstance(downloads, dict) else None
        if not isinstance(artifact, dict):
            artifact = {}
        path = str(artifact.get("path") or "").strip().replace("\\", "/")
        if not path:
            path = _safe_maven_path(name) or ""
        if not path:
            log.debug("跳过无法解析坐标的库：%s", name)
            continue
        size_value = artifact.get("size")
        try:
            size = int(size_value) if size_value is not None else None
        except (TypeError, ValueError):
            size = None
        item = LibraryItem(
            name=name,
            path=path,
            url=str(artifact.get("url") or "").strip() or None,
            sha1=str(artifact.get("sha1") or "").strip() or None,
            size=size,
        )
        existing = items.get(item.path)
        if existing is None or (not existing.url and item.url):
            items[item.path] = item
    return list(items.values())


def _processor_classpath(processor: dict[str, Any]) -> list[str]:
    raw = processor.get("classpath")
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return [str(token) for token in raw if isinstance(token, (str, int))]
    return []


def _processor_jar(processor: dict[str, Any]) -> str:
    raw = processor.get("jar")
    return str(raw).strip() if isinstance(raw, str) else ""


def _coordinate_from_token(token: str) -> str | None:
    text = str(token or "").strip()
    if not text or "{" in text or "$" in text:
        return None
    match = _COORD_RE.match(text)
    if match:
        text = match.group(1).strip()
    if text.count(":") < 2:
        return None
    return text


def extra_tool_items(
    profile: InstallerProfile, known_paths: set[str] | None = None
) -> list[LibraryItem]:
    """补充 processors 引用、但没写在 ``libraries`` 里的工具 jar。

    NeoForge 的 ``jar`` / ``classpath`` 使用不带 ``@jar`` 的明文坐标，
    它们对应的文件其实在 ``libraries`` 里以 ``...@jar`` 形式声明；这里按
    maven 路径去重后补齐，若确实缺失则预置下载。
    """
    seen = set(known_paths or set())
    items: list[LibraryItem] = []
    raw_processors = profile.profile.get("processors") or []
    if not isinstance(raw_processors, list):
        return items
    for processor in raw_processors:
        if not isinstance(processor, dict):
            continue
        tokens = [_processor_jar(processor), *_processor_classpath(processor)]
        for token in tokens:
            coordinate = _coordinate_from_token(token)
            if not coordinate:
                continue
            relative = _safe_maven_path(coordinate)
            if not relative or relative in seen:
                continue
            seen.add(relative)
            items.append(LibraryItem(name=coordinate, path=relative))
    return items


def _side_value(raw: Any, side: str, key: str) -> str:
    if isinstance(raw, dict):
        value = raw.get(side)
        if value is None:
            value = raw.get(CLIENT_SIDE) if side == CLIENT_SIDE else raw.get("server")
        if value is None:
            raise InstallError(f"install_profile.json 的 data 缺少 {key}/{side} 值")
        return str(value)
    if isinstance(raw, (list, tuple)):
        if not raw:
            raise InstallError(f"install_profile.json 的 data 中 {key} 为空")
        return str(raw[0])
    return str(raw)


def _data_dir(installer_jar: Path, mc_root: Path) -> Path:
    return ensure_dir(
        Path(mc_root) / _INSTALLER_CACHE_SUBDIR[0] / _INSTALLER_CACHE_SUBDIR[1]
        / (Path(installer_jar).stem + ".data")
    )


def _extract_jar_entry(installer_jar: Path, entry: str, target: Path) -> Path:
    entry = entry.replace("\\", "/").lstrip("/")
    if not entry or ".." in Path(entry).parts:
        raise InstallError("安装器内的资源路径不合法", detail=str(entry))
    try:
        with zipfile.ZipFile(installer_jar) as archive:
            data = archive.read(entry)
    except KeyError as exc:
        raise InstallError(f"安装器内缺少文件：{entry}", detail=str(installer_jar)) from exc
    except zipfile.BadZipFile as exc:
        raise InstallError("无法读取安装器文件（不是有效的 jar）", detail=str(exc)) from exc
    ensure_dir(target.parent)
    atomic_write_bytes(target, data)
    return target


def _resolve_data_token(
    value: str,
    *,
    installer_jar: Path,
    mc_root: Path,
    extract_dir: Path,
) -> str:
    text = _strip_quotes(str(value or ""))
    if not text:
        return ""
    match = _COORD_RE.match(text)
    if match:
        relative = maven_path(match.group(1))
        return str(Path(mc_root) / _LIBRARIES_SUBDIR / Path(relative))
    if text.startswith(("/", "\\")):
        entry = text.lstrip("/\\")
        target = Path(extract_dir) / Path(entry)
        return str(_extract_jar_entry(installer_jar, entry, target))
    return text


def resolve_variables(
    profile: InstallerProfile,
    *,
    mc_root: Path,
    side: str = CLIENT_SIDE,
) -> dict[str, str]:
    """展开 ``data`` 表与内置占位符，返回 ``{KEY: 绝对路径/字面值}``。"""
    mc_root = Path(mc_root)
    installer_jar = profile.jar_path
    variables: dict[str, str] = {
        "SIDE": side,
        "ROOT": str(mc_root),
        "LIBRARY_DIR": str(mc_root / _LIBRARIES_SUBDIR),
        "MINECRAFT_JAR": str(mc_root / "versions" / profile.minecraft / f"{profile.minecraft}.jar"),
        "MINECRAFT_VERSION": profile.minecraft,
        "INSTALLER": str(installer_jar),
    }
    raw_data = profile.profile.get("data") or {}
    if not isinstance(raw_data, dict):
        raise InstallError("install_profile.json 的 data 字段格式不正确")
    extract_dir = _data_dir(installer_jar, mc_root)
    for key, raw in raw_data.items():
        text = _side_value(raw, side, str(key))
        variables[str(key)] = _resolve_data_token(
            text,
            installer_jar=installer_jar,
            mc_root=mc_root,
            extract_dir=extract_dir,
        )
    return variables


# ---------------------------------------------------------------------------
# processors 过滤 / 命令行
# ---------------------------------------------------------------------------


def processor_included(processor: dict[str, Any], side: str = CLIENT_SIDE) -> bool:
    """``sides`` 为空表示两端都跑；否则必须包含当前 side。"""
    sides = processor.get("sides")
    if not sides:
        return True
    if isinstance(sides, str):
        sides = [sides]
    if not isinstance(sides, (list, tuple)):
        return True
    return side in [str(item).strip().lower() for item in sides]


def processor_args(processor: dict[str, Any]) -> list[Any]:
    raw = processor.get("args")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, (list, tuple)):
        return list(raw)
    return []


def processor_task_name(processor: dict[str, Any]) -> str | None:
    """返回 processor 的任务名（如 ``MCP_DATA`` / ``DOWNLOAD_MOJMAPS``）。"""
    for argument in processor_args(processor):
        if not isinstance(argument, str):
            continue
        text = argument.strip()
        if not text or text.startswith("-") or "{" in text or "[" in text:
            continue
        if text.isupper() and "_" in text:
            return text
    return None


def is_download_mojmaps(processor: dict[str, Any]) -> bool:
    for argument in processor_args(processor):
        if isinstance(argument, str) and argument.strip().upper() == DOWNLOAD_MOJMAPS_TASK:
            return True
    return False


def _substitute(text: str, variables: dict[str, str]) -> str:
    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in variables:
            return variables[key]
        return match.group(0)

    return _PLACEHOLDER_RE.sub(replace, text)


def resolve_token(token: Any, variables: dict[str, str], mc_root: Path) -> str:
    """展开 processor 参数中的占位符 / maven 坐标 / 引号字面值。"""
    text = str(token)
    match = re.fullmatch(r"\{([A-Za-z0-9_]+)\}", text.strip())
    if match and match.group(1) in variables:
        return variables[match.group(1)]
    if "{" in text:
        text = _substitute(text, variables)
    text = _strip_quotes(text)
    coordinate = _COORD_RE.match(text)
    if coordinate:
        return str(Path(mc_root) / _LIBRARIES_SUBDIR / Path(maven_path(coordinate.group(1))))
    # processor 的 ``jar`` / ``classpath`` 也可能是不带方括号的明文坐标
    # （如 ``net.minecraftforge:installertools:1.3.0``），同样映射到 libraries。
    if ":" in text and "/" not in text and " " not in text and "\\" not in text:
        relative = _safe_maven_path(text)
        if relative:
            return str(Path(mc_root) / _LIBRARIES_SUBDIR / Path(relative))
    return text


def read_main_class(jar_path: Path) -> str:
    """从工具的 ``META-INF/MANIFEST.MF`` 读取 ``Main-Class``。"""
    jar_path = Path(jar_path)
    try:
        with zipfile.ZipFile(jar_path) as archive:
            raw = archive.read(MANIFEST_ENTRY)
    except KeyError as exc:
        raise InstallError("工具 jar 缺少 MANIFEST.MF，无法确定主类", detail=str(jar_path)) from exc
    except zipfile.BadZipFile as exc:
        raise InstallError("工具 jar 不是有效的 zip 文件", detail=str(jar_path)) from exc
    except OSError as exc:
        raise InstallError("读取工具 jar 失败", detail=str(exc)) from exc
    text = raw.decode("utf-8", "replace")
    unfolded: list[str] = []
    for line in text.splitlines():
        if line.startswith(" ") and unfolded:
            unfolded[-1] += line[1:]
        else:
            unfolded.append(line)
    for line in unfolded:
        if line.lower().startswith("main-class:"):
            value = line.split(":", 1)[1].strip()
            if value:
                return value
    raise InstallError("工具 jar 未声明 Main-Class", detail=str(jar_path))


def build_processor_command(
    profile: InstallerProfile,
    processor: dict[str, Any],
    variables: dict[str, str],
    *,
    mc_root: Path,
    java_exe: Path,
) -> tuple[list[str], Path]:
    """拼装 ``java -cp "...;tool.jar" <Main-Class> <args...>`` 命令行。"""
    jar_token = _processor_jar(processor)
    if not jar_token:
        raise InstallError("install_profile.json 的 processor 缺少 jar 字段")
    jar_path = Path(resolve_token(jar_token, variables, mc_root))
    if not jar_path.is_file():
        raise InstallError("缺少 processor 工具 jar，无法执行补丁流程", detail=str(jar_path))
    classpath: list[str] = []
    for token in _processor_classpath(processor):
        path = Path(resolve_token(token, variables, mc_root))
        if not path.is_file():
            raise InstallError("缺少 processor classpath 中的工具 jar", detail=str(path))
        classpath.append(str(path))
    classpath.append(str(jar_path))
    main_class = read_main_class(jar_path)
    arguments = [
        resolve_token(argument, variables, mc_root)
        for argument in processor_args(processor)
        if isinstance(argument, (str, int))
    ]
    command = [
        str(java_exe),
        "-cp",
        os.pathsep.join(classpath),
        main_class,
        *arguments,
    ]
    return command, jar_path


def verify_processor_outputs(
    processor: dict[str, Any],
    variables: dict[str, str],
    *,
    mc_root: Path,
) -> list[str]:
    """校验 processor ``outputs`` 声明的产物（存在 + sha1）。"""
    outputs = processor.get("outputs")
    if not isinstance(outputs, dict):
        return []
    checked: list[str] = []
    for key, expected in outputs.items():
        target = Path(resolve_token(key, variables, mc_root))
        if not target.is_file():
            raise InstallError(f"processor 未生成预期文件：{target}")
        expected_text = resolve_token(str(expected), variables, mc_root)
        if _SHA1_RE.match(expected_text):
            actual = sha1_file(target)
            if actual.lower() != expected_text.lower():
                raise InstallError(
                    f"processor 产物校验失败：{target.name}",
                    detail=f"期望 {expected_text}，实际 {actual}",
                )
        checked.append(str(target))
    return checked


# ---------------------------------------------------------------------------
# 依赖库预置
# ---------------------------------------------------------------------------


def _file_matches(path: Path, sha1: str | None) -> bool:
    if not path.is_file():
        return False
    if not sha1:
        return path.stat().st_size > 0
    try:
        return sha1_file(path).lower() == str(sha1).strip().lower()
    except OSError:
        return False


def _mirror_maven_url(relative_path: str) -> str:
    return f"{BMCLAPI_BASE.rstrip('/')}/maven/{relative_path.lstrip('/')}"


def _candidate_urls(item: LibraryItem, mirror_mode: str) -> list[str]:
    mirror = _mirror_maven_url(item.path)
    mode = str(mirror_mode or "auto").strip().lower()
    candidates: list[str] = []
    if mode == "official":
        if item.url:
            candidates.append(item.url)
        candidates.append(mirror)
    else:
        candidates.append(mirror)
        if item.url:
            candidates.append(item.url)
    # 去重且保序
    result: list[str] = []
    for url in candidates:
        if url and url not in result:
            result.append(url)
    return result


def _library_progress(
    progress_cb: Callable[[Progress], None] | None, label: str
) -> Callable[[Progress], None] | None:
    if progress_cb is None:
        return None

    def _on_progress(progress: Progress) -> None:
        _emit(
            progress_cb,
            f"正在下载 {label} 依赖库（{progress.done}/{progress.total}）…",
        )

    return _on_progress


def preseed_libraries(
    http: HttpSession,
    mc_root: Path,
    items: Sequence[LibraryItem],
    *,
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    label: str = "Forge",
) -> PreseedReport:
    """把依赖库并发下载到 ``<mc_root>/libraries/``。

    已经存在且 sha1 匹配的文件会跳过；批量下载失败（如个别可选产物 404）
    时退回逐个重试，仍然失败的记入 ``missing`` 而不是直接中断安装。
    """
    items = list(items)
    libraries_dir = Path(mc_root) / _LIBRARIES_SUBDIR
    pending: list[LibraryItem] = []
    skipped = 0
    for item in items:
        destination = libraries_dir / Path(item.path)
        if _file_matches(destination, item.sha1):
            skipped += 1
            continue
        pending.append(item)
    report = PreseedReport(missing=[], skipped=skipped, pending=len(pending))
    if not pending:
        return report

    mirror_mode = str(getattr(http, "mirror", "auto") or "auto").strip().lower()
    tasks: list[DownloadTask] = []
    for item in pending:
        candidates = _candidate_urls(item, mirror_mode)
        if not candidates:
            continue
        tasks.append(
            DownloadTask(
                url=candidates[0],
                dest=libraries_dir / Path(item.path),
                sha1=item.sha1,
                size=item.size,
                use_mirror=False,
            )
        )
    if tasks:
        _emit(progress_cb, f"正在下载 {label} 依赖库（{len(tasks)} 个）…")
        try:
            ThreadedDownloader(
                http, workers=max(1, int(getattr(http, "workers", 32) or 32))
            ).run(
                tasks,
                progress_cb=_library_progress(progress_cb, label),
                cancel=cancel,
                stage=f"正在下载 {label} 依赖库",
            )
        except CancelledError:
            raise
        except CubeFetchError as exc:
            log.info("批量下载 %s 依赖库时出现失败，改为逐个重试：%s", label, exc)

    # 批量下载中断/失败的余量：逐个文件按候选 URL 顺序重试。
    for item in pending:
        _raise_if_cancelled(cancel)
        destination = libraries_dir / Path(item.path)
        if _file_matches(destination, item.sha1):
            continue
        last_error: CubeFetchError | None = None
        downloaded = False
        for url in _candidate_urls(item, mirror_mode):
            try:
                http.download(
                    url,
                    destination,
                    sha1=item.sha1,
                    size=item.size,
                    use_mirror=False,
                    cancel=cancel,
                )
                downloaded = True
                break
            except CancelledError:
                raise
            except CubeFetchError as exc:
                last_error = exc
                log.info("依赖库下载失败：%s（%s）", item.name, exc)
        if downloaded:
            continue
        if not _file_matches(destination, item.sha1):
            log.warning(
                "%s 依赖库未能下载（将跳过）：%s%s",
                label,
                item.name,
                f"（{last_error}）" if last_error else "",
            )
            report.missing.append(item.name)
    return report


def _required_tool_paths(
    profile: InstallerProfile, variables: dict[str, str], mc_root: Path
) -> list[Path]:
    paths: list[Path] = []
    raw_processors = profile.profile.get("processors") or []
    if not isinstance(raw_processors, list):
        return paths
    for processor in raw_processors:
        if not isinstance(processor, dict):
            continue
        if not processor_included(processor):
            continue
        jar_token = _processor_jar(processor)
        if jar_token:
            paths.append(Path(resolve_token(jar_token, variables, mc_root)))
        for token in _processor_classpath(processor):
            paths.append(Path(resolve_token(token, variables, mc_root)))
    return paths


# ---------------------------------------------------------------------------
# Mojang 混淆映射（替代官方 DOWNLOAD_MOJMAPS）
# ---------------------------------------------------------------------------


def client_mappings_download(vanilla_version_json: dict[str, Any] | None) -> dict[str, Any] | None:
    """从原版版本 JSON 中取 ``downloads.client_mappings``。"""
    downloads = (vanilla_version_json or {}).get("downloads")
    if not isinstance(downloads, dict):
        return None
    mappings = downloads.get("client_mappings")
    if not isinstance(mappings, dict):
        return None
    url = str(mappings.get("url") or "").strip()
    if not url:
        return None
    size_value = mappings.get("size")
    try:
        size = int(size_value) if size_value is not None else None
    except (TypeError, ValueError):
        size = None
    return {
        "url": url,
        "sha1": str(mappings.get("sha1") or "").strip() or None,
        "size": size,
    }


def download_mojmaps(
    http: HttpSession,
    variables: dict[str, str],
    mappings: dict[str, Any],
    *,
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    label: str = "Forge",
) -> Path:
    """下载官方混淆映射到 ``data["MOJMAPS"]`` 解析出的路径。"""
    raw_target = str(variables.get("MOJMAPS") or "").strip()
    if not raw_target:
        raise InstallError("install_profile.json 未声明 MOJMAPS 变量")
    target = Path(raw_target)
    if _file_matches(target, mappings.get("sha1")):
        return target
    _emit(progress_cb, f"正在下载 {label} 混淆映射文件…")

    def _on_bytes(done: int) -> None:
        _emit(progress_cb, f"正在下载 {label} 混淆映射文件（{done} 字节）…")

    http.download(
        str(mappings["url"]),
        target,
        sha1=mappings.get("sha1"),
        size=mappings.get("size"),
        use_mirror=True,
        progress_cb=_on_bytes,
        cancel=cancel,
    )
    if not target.is_file() or target.stat().st_size == 0:
        raise InstallError("混淆映射文件下载失败", detail=str(target))
    return target


# ---------------------------------------------------------------------------
# processors 执行
# ---------------------------------------------------------------------------


def _run_one_processor(
    profile: InstallerProfile,
    processor: dict[str, Any],
    variables: dict[str, str],
    *,
    mc_root: Path,
    java_exe: Path,
    timeout: float,
    label: str,
) -> list[str]:
    command, _jar_path = build_processor_command(
        profile, processor, variables, mc_root=mc_root, java_exe=java_exe
    )
    task = processor_task_name(processor) or "processor"
    last_error: InstallError | None = None
    for attempt in range(PROCESSOR_RETRIES + 1):
        try:
            completed = run_process(command, cwd=None, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            last_error = InstallError(
                f"{label} 补丁任务 {task} 执行超时（{int(timeout)} 秒）",
                detail=_tail_output(
                    exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else exc.stdout,
                    exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else exc.stderr,
                ),
            )
            log.info("%s 补丁任务 %s 超时（第 %d 次）", label, task, attempt + 1)
            continue
        if completed.returncode == 0:
            try:
                verify_processor_outputs(processor, variables, mc_root=mc_root)
            except InstallError as exc:
                last_error = exc
                log.info("%s 补丁任务 %s 产物校验失败（第 %d 次）：%s", label, task, attempt + 1, exc)
                continue
            return [
                line
                for line in _tail_output(completed.stdout, completed.stderr).splitlines()
                if line
            ]
        last_error = InstallError(
            f"{label} 补丁任务 {task} 执行失败（退出码 {completed.returncode}）",
            detail=_tail_output(completed.stdout, completed.stderr),
        )
        log.info("%s 补丁任务 %s 失败（第 %d 次）", label, task, attempt + 1)
    assert last_error is not None
    raise last_error


def run_client_processors(
    http: HttpSession,
    profile: InstallerProfile,
    variables: dict[str, str],
    *,
    mc_root: Path,
    java_exe: Path,
    mappings: dict[str, Any] | None,
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = PROCESSOR_TIMEOUT,
    label: str = "Forge",
) -> list[str]:
    """顺序执行客户端相关 processors。"""
    raw_processors = profile.profile.get("processors") or []
    if not isinstance(raw_processors, list):
        raise InstallError("install_profile.json 的 processors 字段格式不正确")
    client_processors = [
        processor
        for processor in raw_processors
        if isinstance(processor, dict) and processor_included(processor)
    ]
    total = len(client_processors)
    collected: list[str] = []
    for index, processor in enumerate(client_processors, 1):
        _raise_if_cancelled(cancel)
        task = processor_task_name(processor) or "processor"
        if is_download_mojmaps(processor):
            if mappings is None:
                raise InstallError(
                    "原版版本信息缺少混淆映射（client_mappings），无法自动安装该版本",
                    detail=profile.minecraft,
                )
            _emit(
                progress_cb,
                f"正在下载 {profile.minecraft} 混淆映射（{index}/{total}）…",
            )
            download_mojmaps(
                http, variables, mappings, progress_cb=progress_cb, cancel=cancel, label=label
            )
            continue
        _emit(progress_cb, f"正在执行 {label} 补丁任务 {task}（{index}/{total}）…")
        collected.extend(
            _run_one_processor(
                profile,
                processor,
                variables,
                mc_root=mc_root,
                java_exe=java_exe,
                timeout=timeout,
                label=label,
            )
        )
        _raise_if_cancelled(cancel)
    return collected


# ---------------------------------------------------------------------------
# 原版基础 / 版本 JSON
# ---------------------------------------------------------------------------


def ensure_vanilla_base(
    http: HttpSession,
    mc_root: Path,
    minecraft: str,
    vanilla_version_json: dict[str, Any],
    *,
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
) -> Path:
    """确保原版版本 JSON 与客户端 jar 存在（processor 需要 client jar）。"""
    mc_root = Path(mc_root)
    version_dir = ensure_dir(mc_root / "versions" / minecraft)
    json_path = version_dir / f"{minecraft}.json"
    if not isinstance(read_json(json_path, None), dict):
        write_json(json_path, dict(vanilla_version_json))
    downloads = (vanilla_version_json or {}).get("downloads")
    client = (downloads or {}).get("client") if isinstance(downloads, dict) else None
    if not isinstance(client, dict) or not str(client.get("url") or "").strip():
        raise InstallError(
            f"原版 {minecraft} 版本信息缺少客户端下载地址，无法准备补丁基础文件",
        )
    jar_path = version_dir / f"{minecraft}.jar"
    _emit(progress_cb, f"正在准备原版 {minecraft} 客户端文件…")

    def _on_bytes(done: int) -> None:
        _emit(progress_cb, f"正在下载原版 {minecraft} 客户端（{done} 字节）…")

    size_value = client.get("size")
    try:
        size = int(size_value) if size_value is not None else None
    except (TypeError, ValueError):
        size = None
    http.download(
        str(client["url"]),
        jar_path,
        sha1=str(client.get("sha1") or "").strip() or None,
        size=size,
        use_mirror=True,
        progress_cb=_on_bytes,
        cancel=cancel,
    )
    if not jar_path.is_file() or jar_path.stat().st_size == 0:
        raise InstallError(f"原版 {minecraft} 客户端 jar 下载失败", detail=str(jar_path))
    return jar_path


def write_installer_version(profile: InstallerProfile, mc_root: Path) -> Path:
    """把安装器内嵌的 version.json 写到 ``versions/<id>/<id>.json``。"""
    target_dir = ensure_dir(Path(mc_root) / "versions" / profile.profile_id)
    version_json = dict(profile.version_json)
    version_json.setdefault("id", profile.profile_id)
    target = target_dir / f"{profile.profile_id}.json"
    write_json(target, version_json)
    return target


# ---------------------------------------------------------------------------
# 对外总入口
# ---------------------------------------------------------------------------


def install_with_processors(
    http: HttpSession,
    installer_jar: Path,
    mc_root: Path,
    *,
    game_version: str,
    vanilla_version_json: dict[str, Any],
    java_exe: Path,
    label: str = "Forge",
    progress_cb: Callable[[Progress], None] | None = None,
    cancel: threading.Event | None = None,
    timeout: float = PROCESSOR_TIMEOUT,
) -> tuple[str, dict[str, Any]]:
    """自己执行 install_profile 的 processors，完成 Forge/NeoForge 客户端安装。

    返回 ``(profile_id, 写入的版本 JSON)``；调用方随后应把版本 JSON 交给
    :class:`cubefetch.core.install.Installer` 补齐 assets / 继承的原版资源。
    """
    installer_jar = Path(installer_jar)
    mc_root = Path(mc_root)
    _raise_if_cancelled(cancel)
    profile = read_installer(installer_jar)
    game = str(game_version or profile.minecraft).strip()
    if game and game != profile.minecraft:
        log.info(
            "安装器声明的 Minecraft 版本为 %s，与请求的 %s 不一致，以安装器为准",
            profile.minecraft,
            game,
        )
    variables = resolve_variables(profile, mc_root=mc_root)
    ensure_vanilla_base(
        http,
        mc_root,
        profile.minecraft,
        vanilla_version_json,
        progress_cb=progress_cb,
        cancel=cancel,
    )
    _raise_if_cancelled(cancel)

    library_list = library_items(profile)
    tool_list = extra_tool_items(profile, {item.path for item in library_list})
    all_items = [*library_list, *tool_list]
    report = preseed_libraries(
        http,
        mc_root,
        all_items,
        progress_cb=progress_cb,
        cancel=cancel,
        label=label,
    )
    _raise_if_cancelled(cancel)

    required_paths = _required_tool_paths(profile, variables, mc_root)
    missing_required = [path for path in required_paths if not path.is_file()]
    if missing_required:
        details = "\n".join(str(path) for path in missing_required[:10])
        raise InstallError(
            f"{label} 安装缺少必要的工具 jar（{len(missing_required)} 个）",
            detail=details,
        )
    if report.missing:
        log.warning(
            "%s 有 %d 个依赖库下载失败，已跳过：%s",
            label,
            len(report.missing),
            ", ".join(report.missing[:5]),
        )

    mappings = client_mappings_download(vanilla_version_json)
    run_client_processors(
        http,
        profile,
        variables,
        mc_root=mc_root,
        java_exe=Path(java_exe),
        mappings=mappings,
        progress_cb=progress_cb,
        cancel=cancel,
        timeout=timeout,
        label=label,
    )
    _raise_if_cancelled(cancel)

    patched = str(variables.get("PATCHED") or "").strip()
    if patched:
        patched_path = Path(patched)
        if not patched_path.is_file() or patched_path.stat().st_size == 0:
            raise InstallError(
                f"{label} 客户端补丁未生成，安装未完成", detail=str(patched_path)
            )
    else:
        log.info("%s 的 install_profile 未声明 PATCHED 变量，跳过补丁产物校验", label)

    version_json = dict(profile.version_json)
    version_json.setdefault("id", profile.profile_id)
    write_json(
        ensure_dir(mc_root / "versions" / profile.profile_id)
        / f"{profile.profile_id}.json",
        version_json,
    )
    _emit(progress_cb, f"{label} {profile.profile_id} 补丁安装完成")
    return profile.profile_id, version_json
