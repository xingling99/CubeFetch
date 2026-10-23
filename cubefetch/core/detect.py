"""启动器检测：发现本机的 PCL2 / HMCL，并解析它们使用的 .minecraft 根目录。

调研结论与可信度标记见 ``docs/research/loader-notes.md``：

* PCL2 / PCLCE —— 依据 Meloong-Git/PCL 源码（``Settings.vb``、``ModBase.vb``、
  ``ModMinecraft.vb``）与本机真实配置文件，**VERIFIED**：
  - 启动器目录下 ``PCL/Setup.ini``（无分节 INI，``Key:Value``）里的
    ``LaunchFolderSelect`` 是全局游戏目录；值以 ``$`` 开头时相对启动器目录。
  - ``%APPDATA%/PCLCE/config.v1.json`` 的 ``LaunchFolders`` 是
    ``名字>路径|名字>路径`` 形式的多个目录（社区版）。
* HMCL —— 依据 HMCL-dev/HMCL 2026 HEAD 的 ``Metadata.java`` /
  ``SettingsManager`` / ``LegacyConfigMigrator`` 与 hmcl35 源码，**VERIFIED**：
  - 新版：``<HMCL 目录>/.hmcl/config/game-directories.json`` 存游戏目录列表，
    ``launcher-settings.json`` 的 ``selectedGameDirectory`` 存选中的目录 UUID；
    用户级目录为 ``%APPDATA%/hmcl/config/...``。
  - 旧版（<3.16）：``<HMCL 目录>/hmcl.json`` 的 ``configurations`` 映射里
    ``gameDir`` 是各配置的游戏目录。

检测过程**只读**用户配置，不修改任何文件；任何配置损坏都只跳过对应启动器，
不会让整个检测失败。所有路径解析都基于注入的环境变量/目录，便于离线单测。
"""
from __future__ import annotations

import json
import logging
import os
import string
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

log = logging.getLogger("cubefetch.core.detect")

PCL_KIND = "pcl"
HMCL_KIND = "hmcl"

#: PCL2 / PCLCE 的启动器可执行文件名（大小写不敏感匹配）。
_PCL_EXE_NAMES: tuple[str, ...] = (
    "plain craft launcher 2.exe",
    "plain craft launcher.exe",
    "plain craft launcher ce.exe",
    "pcl2.exe",
    "pclce.exe",
    "pcl.exe",
)

#: HMCL 的可执行文件名 / jar 文件名前缀（大小写不敏感匹配）。
_HMCL_EXE_NAMES: tuple[str, ...] = ("hmcl.exe", "hmcl.jar")

#: 单次扫描每个根目录最多读取的子目录数，避免超大目录拖慢启动。
_MAX_SCAN_CHILDREN = 400

#: 扫描安装根时跳过的系统目录（无权限、与游戏无关，扫描它们只会拖慢启动）。
_SYSTEM_DIR_NAMES: frozenset[str] = frozenset(
    {
        "$recycle.bin",
        "config.msi",
        "documents and settings",
        "msocache",
        "perflogs",
        "program files",
        "program files (x86)",
        "programdata",
        "recovery",
        "system volume information",
        "windows",
    }
)


# ---------------------------------------------------------------------------
# 对外数据结构（契约 §6）
# ---------------------------------------------------------------------------


@dataclass
class LauncherProbe:
    """单个启动器的探测结果。"""

    kind: str  # "pcl" | "hmcl"
    installed: bool
    exe_hint: Path | None
    config_path: Path | None
    mc_root: Path | None  # 从配置解析出的 .minecraft 根目录
    managed_versions: int = 0  # 该根目录下 versions 数量


@dataclass
class DetectionResult:
    """一次启动器检测的完整结果。"""

    probes: list[LauncherProbe] = field(default_factory=list)
    candidate_roots: list[Path] = field(default_factory=list)  # 去重后的候选根目录（存在的）
    default_root: Path = field(default_factory=Path)
    needs_choice: bool = False  # True 时 UI 必须弹窗让用户选
    reason: str = ""  # 中文说明
    #: 扫描到的「有效安装根」及其已安装版本数；按版本数降序、去重。
    #: 有效安装根 = ``<root>/versions/<id>/<id>.json`` 至少存在 1 个（PCL/HMCL 的识别规则）。
    root_versions: list[tuple[Path, int]] = field(default_factory=list)

    @property
    def root_version_counts(self) -> dict[str, int]:
        """``{目录路径字符串: 版本数}`` 只读视图，方便 UI 直接显示。"""
        return {str(path): count for path, count in self.root_versions}


class _Found:
    """内部中间结果：探测数据 + 该启动器解析出的全部候选目录 + 用于文案的标签。"""

    __slots__ = ("probe", "roots", "label")

    def __init__(self, probe: LauncherProbe, roots: Sequence[Path], label: str) -> None:
        self.probe = probe
        self.roots = list(roots)
        self.label = label


# ---------------------------------------------------------------------------
# 小工具（纯函数，便于单测）
# ---------------------------------------------------------------------------


def _absolute(path: Path | str, base: Path | None = None) -> Path:
    """返回绝对路径（不解析符号链接）；相对路径挂到 ``base`` 下。"""
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = (base or Path.cwd()) / candidate
    try:
        return Path(os.path.abspath(str(candidate)))
    except OSError:  # pragma: no cover - 极端路径
        return candidate


def _dedupe_paths(paths: Iterable[Path]) -> list[Path]:
    """按 Windows 的大小写不敏感规则去重，同时保留第一次出现的顺序。"""
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        key = os.path.normcase(str(path))
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _load_json(path: Path) -> Any | None:
    """读取 JSON；文件不存在/损坏/不是 UTF-8 时返回 ``None``，永不抛异常。"""
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return None
    if not text.strip():
        return None
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        log.info("启动器配置无法解析，已跳过：%s", path)
        return None


def _read_pcl_ini(path: Path) -> dict[str, str]:
    """读取 PCL 的无分节 INI（``Key:Value``，按第一个冒号切分，后者覆盖前者）。

    PCL 以 UTF-8（无 BOM）+ CRLF 写出；这里对 BOM/其它换行都容错。
    """
    try:
        text = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return {}
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith(";") or line.startswith("#"):
            continue
        key, sep, value = line.partition(":")
        if not sep:
            continue
        result[key.strip()] = value.strip()
    return result


def _expand_pcl_path(value: str, launcher_dir: Path) -> Path:
    """展开 ``LaunchFolderSelect``：``$`` 前缀表示相对启动器目录（VERIFIED）。"""
    raw = str(value or "").strip().strip('"').strip()
    if not raw:
        return launcher_dir
    if raw.startswith("$"):
        relative = raw[1:].lstrip("\\/")
        return _absolute(relative, launcher_dir) if relative else launcher_dir
    return _absolute(raw, launcher_dir)


def _parse_pcl_folders(value: Any, launcher_dir: Path) -> list[Path]:
    """解析 ``LaunchFolders``：``名字>路径|名字>路径``（PCLCE，VERIFIED）。"""
    roots: list[Path] = []
    if isinstance(value, str):
        items: list[str] = value.split("|")
    elif isinstance(value, (list, tuple)):
        items = [str(item) for item in value if isinstance(item, (str, Path))]
    else:
        return roots
    for item in items:
        item = item.strip()
        if not item:
            continue
        _, sep, path_part = item.partition(">")
        raw_path = path_part if sep else item
        raw_path = raw_path.strip().strip('"')
        if raw_path:
            roots.append(_expand_pcl_path(raw_path, launcher_dir))
    return roots


def _count_managed_versions(root: Path | None) -> int:
    """统计 ``root/versions`` 下含 ``<目录名>.json`` 的目录数量（PCL/HMCL 都按此规则识别版本）。"""
    if root is None:
        return 0
    versions_dir = root / "versions"
    try:
        entries = list(versions_dir.iterdir())
    except OSError:
        return 0
    count = 0
    for entry in entries:
        try:
            if entry.is_dir() and (entry / f"{entry.name}.json").is_file():
                count += 1
        except OSError:  # pragma: no cover - 无权限目录
            continue
    return count


def _safe_is_dir(path: Path) -> bool:
    """``Path.is_dir()`` 的无异常版本（无权限/失效盘符时返回 False）。"""
    try:
        return path.is_dir()
    except OSError:  # pragma: no cover - 无权限/坏盘符
        return False


def _sort_install_roots(items: Iterable[tuple[Path, int]]) -> list[tuple[Path, int]]:
    """去重并排序有效安装根：版本数降序，其次按路径字典序（保证结果稳定）。"""
    best: dict[str, tuple[Path, int]] = {}
    for path, count in items:
        if count < 1:
            continue
        key = os.path.normcase(str(path))
        previous = best.get(key)
        if previous is None or count > previous[1]:
            best[key] = (path, count)
    return sorted(best.values(), key=lambda item: (-item[1], os.path.normcase(str(item[0]))))


def _is_minecraft_folder(path: Path) -> bool:
    """PCL 认为「像 .minecraft」的目录：含 versions/ 或目录名就是 .minecraft（VERIFIED）。

    注意：这只是「弱候选」判据；真正有效的安装根见 :func:`_count_managed_versions`
    （``versions/<id>/<id>.json`` 至少 1 个），空目录不会成为默认安装根。
    """
    try:
        if not path.is_dir():
            return False
        if (path / "versions").is_dir():
            return True
        return path.name.lower() == ".minecraft"
    except OSError:  # pragma: no cover
        return False


def _preferred_root(roots: Sequence[Path]) -> Path | None:
    """从候选根目录里选出「最像真实游戏目录」的一个。"""
    existing = [path for path in roots if _is_minecraft_folder(path)]
    if not existing:
        existing = [path for path in roots if path.is_dir()]
    if not existing:
        return None
    with_versions = [path for path in existing if (path / "versions").is_dir()]
    pool = with_versions or existing
    # 版本多的优先，其次按配置里的原始顺序
    return max(pool, key=lambda path: (_count_managed_versions(path), -pool.index(path)))


def _fixed_drive_roots() -> list[Path]:
    """Windows 固定盘符根目录（只含本地硬盘，避免扫描光驱/网络盘时卡住）。

    用 ``GetDriveTypeW`` 过滤 ``DRIVE_FIXED``；cyptes 不可用或调用失败时退回枚举盘符。
    """
    if sys.platform != "win32":
        return []
    roots: list[Path] = []
    try:  # pragma: no cover - Windows 分支在单测中只走真实环境
        import ctypes

        get_drive_type = ctypes.windll.kernel32.GetDriveTypeW  # type: ignore[attr-defined]
    except (ImportError, AttributeError):  # pragma: no cover
        get_drive_type = None
    for letter in string.ascii_uppercase:
        drive = Path(f"{letter}:\\")
        if get_drive_type is not None:
            try:
                if get_drive_type(str(drive)) != 3:  # DRIVE_FIXED = 3
                    continue
            except OSError:  # pragma: no cover - 空盘符
                continue
        try:
            if drive.is_dir():
                roots.append(drive)
        except OSError:  # pragma: no cover - 光驱/网络盘
            continue
    return roots


def _iter_scan_dirs(roots: Iterable[Path]) -> Iterator[Path]:
    """遍历扫描根目录本身及其一级子目录（不递归，避免拖慢启动）。

    - 每个根最多产出 :data:`_MAX_SCAN_CHILDREN` 个真实子目录；
    - 跳过符号链接、无权限目录与 :data:`_SYSTEM_DIR_NAMES` 里的系统目录。
    """
    for root in roots:
        yield root
        try:
            children = sorted(root.iterdir(), key=lambda item: item.name.lower())
        except OSError:
            continue
        emitted = 0
        for child in children:
            if emitted >= _MAX_SCAN_CHILDREN:
                break
            try:
                if not child.is_dir() or child.is_symlink():
                    continue
            except OSError:  # pragma: no cover - 无权限目录
                continue
            if child.name.lower() in _SYSTEM_DIR_NAMES:
                continue
            emitted += 1
            yield child


def _listdir_names(path: Path) -> set[str]:
    try:
        return {entry.name.lower() for entry in path.iterdir()}
    except OSError:
        return set()


def _find_named_file(directory: Path, names: Iterable[str]) -> Path | None:
    lowered = set(_listdir_names(directory))
    for name in names:
        if name.lower() in lowered:
            return directory / name
    return None


def _looks_like_pcl(directory: Path) -> bool:
    if _find_named_file(directory, _PCL_EXE_NAMES) is not None:
        return True
    try:
        return (directory / "PCL" / "Setup.ini").is_file()
    except OSError:  # pragma: no cover
        return False


def _looks_like_hmcl(directory: Path) -> bool:
    names = _listdir_names(directory)
    if ".hmcl" in names or "hmcl.json" in names:
        return True
    return any(
        name in _HMCL_EXE_NAMES or (name.startswith("hmcl") and name.endswith((".exe", ".jar")))
        for name in names
    )


def _hmcl_modern_roots(
    game_directories: Any, launcher_settings: Any, home: Path, appdata: Path
) -> list[Path]:
    """从 HMCL 新版 ``game-directories.json`` + ``launcher-settings.json`` 解析目录。"""
    roots: list[Path] = []
    by_id: dict[str, Path] = {}
    items = game_directories.get("gameDirectories") if isinstance(game_directories, Mapping) else None
    if isinstance(items, list):
        for item in items:
            if not isinstance(item, Mapping):
                continue
            raw_path = item.get("path")
            if not isinstance(raw_path, str) or not raw_path.strip():
                continue
            directory = _absolute(raw_path.strip(), home)
            identifier = str(item.get("id") or "")
            if identifier:
                by_id[identifier] = directory
            else:  # pragma: no cover - 正常文件一定带 id
                roots.append(directory)

    selected = None
    if isinstance(launcher_settings, Mapping):
        raw_selected = launcher_settings.get("selectedGameDirectory")
        if isinstance(raw_selected, str):
            selected = raw_selected

    order: list[str] = []
    if selected:
        order.append(selected)
    order.extend(key for key in by_id if key not in order)
    for identifier in order:
        if identifier in by_id:
            roots.append(by_id[identifier])
        elif identifier.upper() == "HOME":
            # 内置 HOME 目录就是 %APPDATA%/.minecraft（VERIFIED）
            roots.append(appdata / ".minecraft")
        elif identifier.upper() == "DEFAULT":  # pragma: no cover - 相对启动器目录，无法确定
            log.debug("HMCL 内置 DEFAULT 目录依赖启动器工作目录，已跳过")
    return roots


def _hmcl_legacy_roots(data: Any) -> list[Path]:
    """从 HMCL 旧版 ``hmcl.json`` 的 ``configurations`` 解析游戏目录（VERIFIED）。"""
    roots: list[Path] = []
    if not isinstance(data, Mapping):
        return roots
    configurations = data.get("configurations")
    if isinstance(configurations, Mapping):
        selected = data.get("selectedProfile")
        order: list[str] = []
        if isinstance(selected, str):
            order.append(selected)
        order.extend(str(key) for key in configurations if str(key) not in order)
        for name in order:
            profile = configurations.get(name)
            if not isinstance(profile, Mapping):
                continue
            game_dir = profile.get("gameDir")
            if isinstance(game_dir, str) and game_dir.strip():
                roots.append(_absolute(game_dir.strip()))
    # 兼容「把 gameDir 直接写在根级/global 里」的旧格式（INFERRED）
    for container in (data, data.get("global")):
        if isinstance(container, Mapping):
            game_dir = container.get("gameDir")
            if isinstance(game_dir, str) and game_dir.strip():
                roots.append(_absolute(game_dir.strip()))
    return roots


# ---------------------------------------------------------------------------
# 检测器
# ---------------------------------------------------------------------------


class LauncherDetector:
    """检测本机安装的启动器；所有依赖（APPDATA、扫描目录、注册表）都可注入。"""

    def __init__(
        self,
        *,
        appdata: Path | str | None = None,
        user_home: Path | str | None = None,
        scan_roots: Sequence[Path | str] | None = None,
        env: Mapping[str, str] | None = None,
        use_registry: bool = True,
        scan_launchers: bool = True,
    ) -> None:
        env = env if env is not None else os.environ
        roaming = env.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        home = env.get("USERPROFILE") or str(Path.home())
        self.appdata_dir = Path(appdata) if appdata is not None else Path(roaming)
        self.user_home = Path(user_home) if user_home is not None else Path(home)
        self._scan_roots_override = (
            [Path(item) for item in scan_roots] if scan_roots is not None else None
        )
        self.use_registry = bool(use_registry) and sys.platform == "win32"
        self.scan_launchers = bool(scan_launchers)
        # 扫描根及其一级子目录只计算一次，供 PCL/HMCL 探测与安装根扫描共用。
        self._scan_dirs_cache: list[Path] | None = None

    # -- 对外接口 ---------------------------------------------------------

    def detect(self) -> DetectionResult:
        """探测启动器、扫描本机 Minecraft 安装根，并给出默认游戏目录。"""
        # 重新检测时丢弃上一次的扫描缓存，避免用户移动目录后仍用旧结果。
        self._scan_dirs_cache = None
        found_pcl = self._safe_probe(PCL_KIND, self._detect_pcl)
        found_hmcl = self._safe_probe(HMCL_KIND, self._detect_hmcl)
        probes = [found_pcl.probe, found_hmcl.probe]
        installed = [item for item in (found_pcl, found_hmcl) if item.probe.installed]

        # 启动器配置解析出的目录（优先候选）交给安装根扫描一起验证。
        config_roots: list[Path] = []
        for found in (found_pcl, found_hmcl):
            config_roots.extend(found.roots)
            if found.probe.mc_root is not None:
                config_roots.append(found.probe.mc_root)
        root_versions = self._install_roots(config_roots)
        top_root, top_count = root_versions[0] if root_versions else (None, 0)
        default_minecraft = self._default_minecraft_root()

        if not installed:
            if top_root is not None:
                reason = (
                    f"未检测到 PCL2 / HMCL 启动器，但扫描到 {len(root_versions)} 个已安装游戏的目录，"
                    f"使用版本最多的 {top_root}（{top_count} 个版本）"
                )
                return DetectionResult(
                    probes=probes,
                    candidate_roots=self._candidate_roots(
                        root_versions, [default_minecraft], top_root
                    ),
                    default_root=top_root,
                    needs_choice=False,
                    reason=reason,
                    root_versions=root_versions,
                )
            reason = f"未检测到 PCL2 / HMCL 启动器，默认使用 {default_minecraft}"
            candidates = [default_minecraft] if _safe_is_dir(default_minecraft) else []
            return DetectionResult(
                probes=probes,
                candidate_roots=candidates,
                default_root=default_minecraft,
                needs_choice=False,
                reason=reason,
                root_versions=root_versions,
            )

        if len(installed) == 1:
            found = installed[0]
            parsed_root = found.probe.mc_root
            parsed_count = _count_managed_versions(parsed_root)
            if parsed_root is not None and parsed_count >= 1:
                default_root = parsed_root
                reason = (
                    f"检测到 {found.label}，使用其游戏目录 {parsed_root}"
                    f"（{parsed_count} 个版本）"
                )
            elif top_root is not None:
                default_root = top_root
                reason = (
                    f"检测到 {found.label}，但其配置的游戏目录没有可用版本，"
                    f"使用扫描到的 {top_root}（{top_count} 个版本）"
                )
            else:
                default_root = default_minecraft
                reason = (
                    f"检测到 {found.label}，但未找到有效的游戏目录，"
                    f"默认使用 {default_minecraft}"
                )
            return DetectionResult(
                probes=probes,
                candidate_roots=self._candidate_roots(
                    root_versions, config_roots, default_root
                ),
                default_root=default_root,
                needs_choice=False,
                reason=reason,
                root_versions=root_versions,
            )

        # 双启动器：让用户选择，候选里必须同时含两边的目录；默认根优先有效安装根。
        candidates = self._candidate_roots(
            root_versions, config_roots, top_root or default_minecraft
        )
        default_root = top_root or (candidates[0] if candidates else default_minecraft)
        labels = " 与 ".join(item.label for item in installed)
        reason = f"检测到 {labels} 两个启动器，请选择要使用的 .minecraft 目录"
        return DetectionResult(
            probes=probes,
            candidate_roots=candidates,
            default_root=default_root,
            needs_choice=True,
            reason=reason,
            root_versions=root_versions,
        )

    # -- 环境感知 ---------------------------------------------------------

    def _default_minecraft_root(self) -> Path:
        return self.appdata_dir / ".minecraft"

    def _scan_dirs(self) -> list[Path]:
        """所有扫描根及其一级子目录（只计算一次，启动器探测与安装根扫描共用）。"""
        if self._scan_dirs_cache is None:
            self._scan_dirs_cache = list(_iter_scan_dirs(self._scan_roots()))
        return self._scan_dirs_cache

    def _install_roots(self, extra_roots: Sequence[Path] = ()) -> list[tuple[Path, int]]:
        """扫描本机 Minecraft 安装根，返回「至少装了 1 个版本」的目录及版本数（降序）。

        候选来源：

        - 启动器配置解析出的目录（``extra_roots``，最高优先级）；
        - ``%APPDATA%\\.minecraft``、``~/.minecraft``、``Desktop/Downloads/Documents`` 下的 ``.minecraft``；
        - 各扫描根本身、其一级子目录，以及这些目录下的 ``.minecraft``/``minecraft``
          （例如 ``D:\\pcl2\\.minecraft`` 就是二级目录，正好覆盖用户反馈的场景）。

        只做一级/二级查找、不深度递归；每个目录最多读 :data:`_MAX_SCAN_CHILDREN` 个子目录。
        """
        explicit: list[Path] = list(extra_roots)
        explicit.append(self._default_minecraft_root())
        explicit.append(self.user_home / ".minecraft")
        for folder in ("Desktop", "Downloads", "Documents"):
            explicit.append(self.user_home / folder / ".minecraft")
        for directory in self._scan_dirs():
            explicit.append(directory)
            if directory.name.lower() in (".minecraft", "minecraft"):
                continue
            for child_name in (".minecraft", "minecraft"):
                child = directory / child_name
                if _safe_is_dir(child):
                    explicit.append(child)

        counted: list[tuple[Path, int]] = []
        seen: set[str] = set()
        for candidate in explicit:
            key = os.path.normcase(str(candidate))
            if key in seen:
                continue
            seen.add(key)
            version_count = _count_managed_versions(candidate)
            if version_count >= 1:
                counted.append((candidate, version_count))
        return _sort_install_roots(counted)

    def _scan_roots(self) -> list[Path]:
        if self._scan_roots_override is not None:
            return _dedupe_paths(
                [path for path in self._scan_roots_override if path.is_dir()]
            )
        roots: list[Path] = []
        roots.extend(_fixed_drive_roots())
        roots.extend(
            path
            for path in (
                self.user_home,
                self.user_home / "Desktop",
                self.user_home / "Downloads",
                self.user_home / "Documents",
                self.user_home / "AppData" / "Local" / "Programs",
                self.appdata_dir,
            )
            if path.is_dir()
        )
        return _dedupe_paths(roots)

    # -- PCL --------------------------------------------------------------

    def _detect_pcl(self) -> _Found:
        if not self.scan_launchers and self._scan_roots_override is None:
            launcher_dirs: list[Path] = []
        else:
            launcher_dirs = self._scan_pcl_dirs()
        if self.use_registry:
            launcher_dirs.extend(self._registry_pcl_dirs())
        launcher_dirs = _dedupe_paths([path for path in launcher_dirs if path.is_dir()])

        roots: list[Path] = []
        config_paths: list[Path] = []
        exe_hint: Path | None = None
        label = "PCL2"

        for launcher_dir in launcher_dirs:
            setup_ini = launcher_dir / "PCL" / "Setup.ini"
            ini = _read_pcl_ini(setup_ini)
            if ini:
                config_paths.append(setup_ini)
                raw_root = ini.get("LaunchFolderSelect")
                if raw_root:
                    roots.append(_expand_pcl_path(raw_root, launcher_dir))
                folders = ini.get("LaunchFolders")
                if folders:
                    roots.extend(_parse_pcl_folders(folders, launcher_dir))
                elif raw_root and "pclce" in launcher_dir.name.lower():
                    label = "PCL2（社区版）"
            executable = _find_named_file(launcher_dir, _PCL_EXE_NAMES)
            if executable is not None:
                exe_hint = exe_hint or executable
                if "pclce" in executable.name.lower():
                    label = "PCL2（社区版）"
            launcher_minecraft = launcher_dir / ".minecraft"
            if launcher_minecraft.is_dir():
                roots.append(launcher_minecraft)

        # PCL2（官版）的新配置：%APPDATA%/PCL/config.json —— 只有 Java 列表，
        # 但它的存在可以证明 PCL 被使用过（VERIFIED）。
        pcl_settings = self.appdata_dir / "PCL" / "config.json"
        if _load_json(pcl_settings) is not None:
            config_paths.append(pcl_settings)

        # PCLCE（社区版）：%APPDATA%/PCLCE/config.v1.json 里直接存了 LaunchFolders。
        for candidate in (
            self.appdata_dir / "PCLCE" / "config.v1.json",
            self.appdata_dir / "PCLCE" / "config.json",
        ):
            data = _load_json(candidate)
            if not isinstance(data, Mapping):
                continue
            config_paths.append(candidate)
            label = "PCL2（社区版）"
            for key in ("LaunchFolders", "LaunchFolderSelect", "LaunchFolder"):
                if key in data:
                    roots.extend(_parse_pcl_folders(data.get(key), candidate.parent))

        roots = _dedupe_paths([path for path in roots if path.is_dir()])
        preferred = _preferred_root(roots)
        installed = bool(launcher_dirs or config_paths or roots)
        probe = LauncherProbe(
            kind=PCL_KIND,
            installed=installed,
            exe_hint=exe_hint,
            config_path=config_paths[0] if config_paths else None,
            mc_root=preferred,
            managed_versions=_count_managed_versions(preferred),
        )
        ordered_roots = self._ordered_roots(preferred, roots)
        return _Found(probe, ordered_roots, label)

    def _scan_pcl_dirs(self) -> list[Path]:
        found = [directory for directory in self._scan_dirs() if _looks_like_pcl(directory)]
        return _dedupe_paths(found)

    def _registry_pcl_dirs(self) -> list[Path]:
        """从 ``HKCU\\Software\\PCL[CE]`` 的值反推启动器目录（VERIFIED：本机 CacheDownloadFolder）。"""
        result: list[Path] = []
        try:
            import winreg  # type: ignore[import-not-found]
        except ImportError:  # pragma: no cover - 非 Windows
            return result
        for key_name in (r"Software\PCL", r"Software\PCLCE"):
            try:
                key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_name)
            except OSError:
                continue
            try:
                index = 0
                while True:
                    try:
                        _, value, _ = winreg.EnumValue(key, index)
                    except OSError:
                        break
                    index += 1
                    if not isinstance(value, str) or not value.strip():
                        continue
                    raw = Path(value.strip())
                    for parent in raw.parents:
                        if parent.name.lower() in ("pcl", "pclce"):
                            result.append(parent.parent)
                            break
            finally:
                try:
                    key.Close()
                except OSError:  # pragma: no cover
                    pass
        return _dedupe_paths(result)

    # -- HMCL -------------------------------------------------------------

    def _detect_hmcl(self) -> _Found:
        if not self.scan_launchers and self._scan_roots_override is None:
            launcher_dirs: list[Path] = []
        else:
            launcher_dirs = self._scan_hmcl_dirs()
        launcher_dirs = _dedupe_paths([path for path in launcher_dirs if path.is_dir()])

        homes: list[Path] = []
        user_home_config = self.appdata_dir / "hmcl"
        homes.append(user_home_config)
        for launcher_dir in launcher_dirs:
            homes.append(launcher_dir / ".hmcl")
            homes.append(launcher_dir)
        homes = _dedupe_paths(homes)

        roots: list[Path] = []
        config_paths: list[Path] = []
        exe_hint: Path | None = None
        for home in homes:
            if not home.is_dir():
                continue
            config_dir = home / "config"
            game_directories = _load_json(config_dir / "game-directories.json")
            launcher_settings = _load_json(config_dir / "launcher-settings.json")
            if isinstance(game_directories, Mapping) or isinstance(launcher_settings, Mapping):
                config_paths.extend(
                    path
                    for path in (config_dir / "game-directories.json", config_dir / "launcher-settings.json")
                    if path.is_file()
                )
                roots.extend(_hmcl_modern_roots(game_directories, launcher_settings, home, self.appdata_dir))
            legacy = _load_json(home / "hmcl.json")
            if isinstance(legacy, Mapping):
                config_paths.append(home / "hmcl.json")
                roots.extend(_hmcl_legacy_roots(legacy))
            if exe_hint is None:
                exe_hint = _find_named_file(home, _HMCL_EXE_NAMES)

        # 用户级旧配置 %APPDATA%/hmcl/config.json；以及 Linux 风格的 ~/.hmcl.json（兼容，INFERRED）。
        for legacy_path in (self.appdata_dir / "hmcl" / "config.json", self.user_home / ".hmcl.json"):
            data = _load_json(legacy_path)
            if isinstance(data, Mapping):
                config_paths.append(legacy_path)
                roots.extend(_hmcl_legacy_roots(data))

        for launcher_dir in launcher_dirs:
            if exe_hint is None:
                exe_hint = _find_named_file(launcher_dir, _HMCL_EXE_NAMES)

        roots = _dedupe_paths([path for path in roots if path.is_dir()])
        preferred = _preferred_root(roots)
        installed = bool(launcher_dirs or config_paths or roots or user_home_config.is_dir())
        probe = LauncherProbe(
            kind=HMCL_KIND,
            installed=installed,
            exe_hint=exe_hint,
            config_path=config_paths[0] if config_paths else None,
            mc_root=preferred,
            managed_versions=_count_managed_versions(preferred),
        )
        ordered_roots = self._ordered_roots(preferred, roots)
        return _Found(probe, ordered_roots, "HMCL")

    def _scan_hmcl_dirs(self) -> list[Path]:
        found = [directory for directory in self._scan_dirs() if _looks_like_hmcl(directory)]
        return _dedupe_paths(found)

    # -- 内部 -------------------------------------------------------------

    @staticmethod
    def _ordered_roots(preferred: Path | None, roots: Sequence[Path]) -> list[Path]:
        ordered = list(roots)
        if preferred is not None:
            key = os.path.normcase(str(preferred))
            ordered = [path for path in ordered if os.path.normcase(str(path)) != key]
            ordered.insert(0, preferred)
        return [path for path in _dedupe_paths(ordered) if _is_minecraft_folder(path) or path.is_dir()]

    @staticmethod
    def _candidate_roots(
        root_versions: Sequence[tuple[Path, int]],
        extra_roots: Sequence[Path],
        fallback: Path | None,
    ) -> list[Path]:
        """候选根目录：有效安装根（版本多→少）优先，其次配置根，最后兜底目录。"""
        ordered: list[Path] = [path for path, _ in root_versions]
        ordered.extend(extra_roots)
        if fallback is not None:
            ordered.append(fallback)
        return [path for path in _dedupe_paths(ordered) if _safe_is_dir(path)]

    def _safe_probe(self, kind: str, builder: Any) -> _Found:
        try:
            return builder()
        except Exception:  # noqa: BLE001 - 检测失败不能影响启动
            log.exception("检测启动器 %s 失败", kind)
            return _Found(
                LauncherProbe(
                    kind=kind,
                    installed=False,
                    exe_hint=None,
                    config_path=None,
                    mc_root=None,
                    managed_versions=0,
                ),
                [],
                "PCL2" if kind == PCL_KIND else "HMCL",
            )


__all__ = ["DetectionResult", "LauncherDetector", "LauncherProbe"]
