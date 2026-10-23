"""版本重命名：移动版本目录，并把版本 id 同步到 JSON 与继承引用。

背景（用户需求 4）：下载时如果发现版本重命名（例如同名版本已被占用、或用户想
自定义显示名），UI 会弹窗让用户输入新的版本名。本模块提供后端能力：

- :func:`validate_version_name`：校验用户输入的版本名（返回去掉首尾空白后的名字）。
- :func:`rename_installed_version`：把已安装版本 ``versions/<old_id>`` 重命名为
  ``versions/<new_id>``，同时改写版本 JSON 的 ``id`` / 文件名，并把同根其它版本
  JSON 里指向 ``old_id`` 的 ``inheritsFrom`` 引用改成 ``new_id``。
- :func:`existing_version`：按 loader 的 profile_id 生成规则，判断某个
  (加载器, 游戏版本, 加载器版本) 组合是否已经安装，返回实际存在的版本 id。

设计原则：只改本项目自己管理的版本目录内容，**不动** PCL 的
``versions/<id>/PCL/Setup.ini`` 与 HMCL 的 ``versions/<id>/instance-game-settings.json``
（版本隔离按目录生效，目录改名后启动器会自行按新目录名识别）。所有操作都在本地
文件系统完成，不联网。
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from .errors import InstallError
from .util import read_json, write_json

logger = logging.getLogger("cubefetch.version_rename")

#: 版本名最大长度（字符数）。Windows 目录名上限 255 个 UTF-16 单元，64 足够宽松又安全。
MAX_VERSION_NAME_LENGTH = 64

#: Windows 文件名/目录名不允许出现的字符（控制字符另行检查）。
_INVALID_NAME_CHARS = frozenset('\\/:*?"<>|')

#: 特殊目录名（在任意平台都表示当前/上级目录，绝不能作为版本目录名）。
_RESERVED_DIR_NAMES = frozenset({".", ".."})

#: 与 loader.py 的 ``_POPULAR_ALIASES`` 保持一致（这里只用于解析 kind，不导入
#: loader.py，避免把 http/install 依赖带进本模块的单元测试）。
_KIND_ALIASES = {
    "neo": "neoforge",
    "neo-forge": "neoforge",
    "neo_forge": "neoforge",
    "原版": "vanilla",
}


# ---------------------------------------------------------------------------
# 版本名校验
# ---------------------------------------------------------------------------


def validate_version_name(name: str) -> str:
    """校验并规范化用户输入的版本名，返回去掉首尾空白后的结果。

    合法名字：非空、长度不超过 64、不含 ``\\ / : * ? " < > |`` 或控制字符、
    不是 ``.`` / ``..``、不以点结尾（去空白后不会以空格结尾）。

    不合法时抛 :class:`ValueError`（中文提示，供 UI 直接显示）。
    """
    if not isinstance(name, str):
        raise ValueError("版本名必须是文本")
    value = name.strip()
    if not value:
        raise ValueError("版本名不能为空")
    if len(value) > MAX_VERSION_NAME_LENGTH:
        raise ValueError(
            f"版本名过长：最多 {MAX_VERSION_NAME_LENGTH} 个字符，当前 {len(value)} 个"
        )
    if value in _RESERVED_DIR_NAMES:
        raise ValueError("版本名不能是 . 或 ..")
    invalid = sorted({char for char in value if char in _INVALID_NAME_CHARS})
    if invalid:
        shown = " ".join(invalid)
        raise ValueError(f'版本名不能包含以下字符：\\ / : * ? " < > |（发现：{shown}）')
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise ValueError("版本名不能包含换行、制表符等控制字符")
    if value.endswith("."):
        raise ValueError("版本名不能以点或空格结尾")
    return value


def _validate_old_id(old_id: str) -> str:
    """校验旧版本目录名是安全的单段目录名。"""
    value = str(old_id or "").strip()
    if not value:
        raise InstallError("缺少要重命名的版本 id")
    if value in _RESERVED_DIR_NAMES or "/" in value or "\\" in value:
        raise InstallError(f"版本 id 无效，不能用于定位版本目录：{old_id!r}")
    return value


def _versions_dir(mc_root: Path) -> Path:
    return Path(mc_root) / "versions"


# ---------------------------------------------------------------------------
# 重命名
# ---------------------------------------------------------------------------


def rename_installed_version(mc_root: Path, old_id: str, new_name: str) -> str:
    """把 ``versions/<old_id>`` 重命名为用户输入的新版本名，返回新 id。

    步骤：

    1. 用 :func:`validate_version_name` 校验新名（不合法抛 ``ValueError``）；
    2. 旧版本目录 ``versions/<old_id>`` 必须存在，且其中有 ``<old_id>.json``；
    3. 目标目录 ``versions/<new_id>`` 不能已存在（否则 ``InstallError("目标版本名已存在")``）；
    4. 移动目录，把版本 JSON 的 ``id`` 改成 ``new_id``，另存为 ``<new_id>.json`` 并删除旧 JSON；
    5. 同根其它版本 JSON 中 ``inheritsFrom == old_id`` 的引用改成 ``new_id``。

    不修改 PCL/HMCL 的版本隔离文件（隔离按版本目录生效，目录改名后自然跟随）。
    """
    mc_root = Path(mc_root)
    old_id = _validate_old_id(old_id)
    new_id = validate_version_name(new_name)
    versions_dir = _versions_dir(mc_root)
    source_dir = versions_dir / old_id
    target_dir = versions_dir / new_id

    if not source_dir.is_dir():
        raise InstallError(
            f"找不到要重命名的版本：{old_id}（目录不存在）", detail=str(source_dir)
        )
    old_json = source_dir / f"{old_id}.json"
    if not old_json.is_file():
        raise InstallError(
            f"版本 {old_id} 缺少版本信息文件 {old_id}.json，无法重命名",
            detail=str(old_json),
        )
    payload = read_json(old_json, None)
    if not isinstance(payload, dict):
        raise InstallError(
            f"版本 {old_id} 的版本信息文件无法解析，无法重命名", detail=str(old_json)
        )

    if new_id == old_id:
        # 名字完全没变：视为成功（幂等），但确保 JSON 里的 id 正确。
        if payload.get("id") != new_id:
            payload["id"] = new_id
            write_json(old_json, payload)
        return new_id

    if target_dir.exists():
        raise InstallError(f"目标版本名已存在：{new_id}", detail=str(target_dir))

    try:
        source_dir.rename(target_dir)
    except OSError as exc:
        raise InstallError(
            f"重命名版本目录失败：{new_id}", detail=f"{exc}（{source_dir} → {target_dir}）"
        ) from exc

    # 目录已移动：更新版本 JSON 的 id 与文件名。
    payload["id"] = new_id
    new_json = target_dir / f"{new_id}.json"
    try:
        write_json(new_json, payload)
        moved_old_json = target_dir / f"{old_id}.json"
        if moved_old_json.is_file() and moved_old_json != new_json:
            moved_old_json.unlink()
    except OSError as exc:
        raise InstallError(
            f"版本目录已移动，但写入新的版本信息失败：{new_id}",
            detail=f"{exc}（{new_json}）",
        ) from exc

    # 同根其它版本的 inheritsFrom 引用同步改名（损坏的 JSON 跳过，不影响本次重命名）。
    updated_refs = _rewrite_inherits_from(versions_dir, old_id, new_id, skip=target_dir)
    logger.info(
        "版本已重命名：%s -> %s（同步更新 %d 个继承引用）", old_id, new_id, updated_refs
    )
    return new_id


def _rewrite_inherits_from(
    versions_dir: Path, old_id: str, new_id: str, *, skip: Path | None = None
) -> int:
    """把同根其它版本 JSON 里 ``inheritsFrom == old_id`` 改成 ``new_id``，返回改动数。"""
    if not versions_dir.is_dir():
        return 0
    skip_key = os.path.normcase(str(skip)) if skip is not None else None
    updated = 0
    try:
        children = list(versions_dir.iterdir())
    except OSError:  # pragma: no cover - 目录不可读时静默
        return 0
    for child in children:
        if not child.is_dir():
            continue
        if skip_key is not None and os.path.normcase(str(child)) == skip_key:
            continue
        try:
            json_files = sorted(child.glob("*.json"))
        except OSError:  # pragma: no cover - 目录不可读时静默
            continue
        for json_path in json_files:
            if not json_path.is_file():
                continue
            data = read_json(json_path, None)
            if not isinstance(data, dict):
                # 可能不是版本 JSON（或已损坏）：只记日志，不影响重命名结果。
                logger.debug("跳过无法解析的版本 JSON：%s", json_path)
                continue
            if str(data.get("inheritsFrom") or "").strip() != old_id:
                continue
            data["inheritsFrom"] = new_id
            try:
                write_json(json_path, data)
            except OSError:
                logger.warning("更新继承引用失败：%s", json_path, exc_info=True)
                continue
            updated += 1
    return updated


# ---------------------------------------------------------------------------
# 已安装版本探测
# ---------------------------------------------------------------------------


def _version_installed(mc_root: Path, version_id: str) -> bool:
    version_id = str(version_id or "").strip()
    if not version_id:
        return False
    target = _versions_dir(mc_root) / version_id / f"{version_id}.json"
    return target.is_file()


def _candidate_ids(
    kind: str, game_version: str, loader_version: str | None
) -> list[str]:
    """按 loader.py 的 profile_id 生成规则给出候选版本 id（按优先级）。"""
    game = str(game_version or "").strip()
    if not game:
        return []
    value = str(kind or "").strip().lower()
    value = _KIND_ALIASES.get(value, value)
    if value == "vanilla":
        return [game]
    loader = str(loader_version or "").strip()
    if not loader:
        return []
    if value in ("fabric", "quilt", "forge"):
        # loader.py: profile_id = f"{game}-{kind}-{loader}"
        return [f"{game}-{value}-{loader}"]
    if value == "neoforge":
        # 预测名（prepare 生成）优先；官方/自执行安装器实际常写 "neoforge-<loader>"。
        return [f"{game}-neoforge-{loader}", f"neoforge-{loader}"]
    return []


def existing_version(
    mc_root: Path,
    kind: str,
    game_version: str,
    loader_version: str | None,
) -> str | None:
    """返回该组合已安装的版本 id；未安装返回 ``None``。

    与 ``LoaderService`` 产出的 profile_id 规则一致：

    - vanilla → ``<game>``；
    - fabric / quilt → ``<game>-<kind>-<loader>``；
    - forge → ``<game>-forge-<loader>``；
    - neoforge → 先 ``<game>-neoforge-<loader>``，再 ``neoforge-<loader>``。

    存在判据：``versions/<id>/<id>.json`` 是文件（PCL/HMCL 列版本也依赖它）。
    """
    mc_root = Path(mc_root)
    for candidate in _candidate_ids(kind, game_version, loader_version):
        if _version_installed(mc_root, candidate):
            return candidate
    return None
