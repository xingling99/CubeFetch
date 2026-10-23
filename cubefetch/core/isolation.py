"""版本隔离：把「每个版本使用独立游戏目录」的配置写进启动器能识别的文件。

调研结论（详见 ``docs/research/loader-notes.md``）：

* PCL2 / PCLCE（**VERIFIED**，Meloong-Git/PCL 源码 ``Settings.vb`` /
  ``ModMinecraft.vb``）：每个版本自己的设置文件是
  ``versions/<id>/PCL/Setup.ini``（无分节 INI，``Key:Value``，UTF-8 无 BOM）。
  - ``VersionArgumentIndieV2:True`` 表示隔离（新键，保存后优先）；
  - 旧键 ``VersionArgumentIndie``：``1`` 强制隔离、``2`` 强制不隔离。
* HMCL（**VERIFIED**，HMCL-dev/HMCL 2026 HEAD）：
  - 新版用 ``versions/<id>/instance-game-settings.json``：
    ``runningDirectory`` 为空 + ``defaultIsolationType == "ALWAYS"`` 即隔离；
  - 旧版（<3.16）用 ``versions/<id>/hmclversion.cfg``（JSON），
    ``gameDirType == 1``（VERSION_FOLDER）即隔离。

原则：**只做读改写**，保留用户配置里的其它键；改动前把原文件备份成
``<文件名>.bak``（仅第一次）；重复调用必须幂等；对未安装/未知启动器跳过并
在返回的中文说明里写清原因。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Mapping

from .util import atomic_write_bytes, atomic_write_text, ensure_dir

log = logging.getLogger("cubefetch.core.isolation")

PCL_KIND = "pcl"
HMCL_KIND = "hmcl"

#: PCL 版本级隔离键（新键优先，旧键兜底）—— VERIFIED。
PCL_KEY_V2 = "VersionArgumentIndieV2"
PCL_KEY_LEGACY = "VersionArgumentIndie"

#: HMCL 新版实例设置文件与 schema —— VERIFIED。
HMCL_INSTANCE_FILE = "instance-game-settings.json"
HMCL_INSTANCE_SCHEMA = "https://schemas.glavo.site/hmcl/instance-game-settings/1.0.1"
HMCL_ISOLATION_ALWAYS = "ALWAYS"
HMCL_ISOLATION_NEVER = "NEVER"

#: HMCL 旧版实例设置文件 —— VERIFIED（文件内容是 JSON，旧代码用 JsonUtils 序列化）。
HMCL_LEGACY_FILE = "hmclversion.cfg"
HMCL_GAME_DIR_TYPE_VERSION = 1  # VERSION_FOLDER：游戏目录 = versions/<id>
HMCL_GAME_DIR_TYPE_ROOT = 0  # ROOT_FOLDER：游戏目录 = .minecraft 根

_TRUTHY = {"true", "1", "yes", "on"}


# ---------------------------------------------------------------------------
# 底层文件工具
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8-sig", errors="replace")
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("读取配置文件失败：%s", path, exc_info=True)
        return None


def _backup_once(path: Path) -> Path | None:
    """第一次修改前备份原文件到 ``<path>.bak``；已存在备份则不覆盖。"""
    if not path.is_file():
        return None
    backup = path.with_name(path.name + ".bak")
    if backup.is_file():
        return backup
    try:
        atomic_write_bytes(backup, path.read_bytes())
        return backup
    except OSError:  # pragma: no cover - 磁盘错误
        log.warning("备份配置文件失败：%s", path, exc_info=True)
        return None


def _write_text(path: Path, text: str) -> None:
    ensure_dir(path.parent)
    atomic_write_text(path, text, encoding="utf-8")


def _load_json_dict(path: Path) -> dict[str, Any] | None:
    """读取 JSON 对象；文件不存在返回 ``None``；内容损坏抛 ``ValueError``。"""
    text = _read_text(path)
    if text is None:
        return None
    if not text.strip():
        return {}
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"配置文件不是合法 JSON：{path}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"配置文件不是 JSON 对象：{path}")
    return data


def _update_ini_file(path: Path, updates: Mapping[str, str]) -> tuple[bool, bool]:
    """无分节 INI 读改写（PCL 格式）；返回 ``(changed, backed_up)``。

    - 已存在的键原地替换（保留其它行与换行风格），缺失的键追加到末尾；
    - 内容没有变化时不写盘（幂等）。
    """
    existing = _read_text(path)
    backed_up = False
    lines: list[str] = []
    newline = "\n"
    if existing is not None:
        newline = "\r\n" if "\r\n" in existing else "\n"
        lines = existing.splitlines()
    updated = dict(updates)
    written: set[str] = set()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()
        key = stripped.partition(":")[0].strip() if ":" in stripped else ""
        if key in updated and key not in written:
            out.append(f"{key}:{updated[key]}")
            written.add(key)
        else:
            out.append(line)
    for key, value in updated.items():
        if key not in written:
            out.append(f"{key}:{value}")
    text = newline.join(out)
    if out:
        text += newline
    if existing is not None and text == existing:
        return False, False
    if existing is not None:
        backed_up = _backup_once(path) is not None
    _write_text(path, text)
    return True, backed_up


def _update_json_file(path: Path, updates: Mapping[str, Any]) -> tuple[bool, bool]:
    """JSON 对象读改写；返回 ``(changed, backed_up)``。

    文件内容损坏时抛 ``ValueError``，由调用方决定跳过（绝不覆盖用户数据）。
    """
    if path.is_file():
        original = path.read_text(encoding="utf-8-sig", errors="replace")
    else:
        original = None
    data = _load_json_dict(path)
    if data is None:
        data = {}
    changed = False
    for key, value in updates.items():
        if data.get(key) != value:
            data[key] = value
            changed = True
    if not changed:
        return False, False
    text = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    backed_up = False
    if original is not None:
        backed_up = _backup_once(path) is not None
    _write_text(path, text)
    return True, backed_up


def _format_value(enabled: bool) -> str:
    return "True" if enabled else "False"


# ---------------------------------------------------------------------------
# 对外接口（契约 §7）
# ---------------------------------------------------------------------------


class VersionIsolation:
    """按启动器写入/读取「版本隔离」配置。"""

    @staticmethod
    def apply(
        mc_root: Path,
        version_id: str,
        launcher_kinds: list[str],
        enabled: bool = True,
    ) -> list[str]:
        """为 ``version_id`` 写入版本隔离配置，返回中文说明列表。

        - 只处理 ``launcher_kinds`` 里列出的启动器（服务层传入的是已安装启动器）；
        - 未提供/非法的版本号、不存在的版本目录直接跳过并说明；
        - 单个启动器写入失败不影响其它启动器（异常被捕获成说明文字）。
        """
        mc_root = Path(mc_root)
        notes: list[str] = []
        version = str(version_id or "").strip()
        if not version or version in (".", "..") or any(
            separator in version for separator in ("/", "\\")
        ):
            notes.append(f"版本号无效（{version_id!r}），跳过版本隔离配置")
            return notes

        version_dir = mc_root / "versions" / version
        if not version_dir.is_dir():
            notes.append(f"版本目录不存在：{version_dir}，跳过版本隔离配置")
            return notes

        kinds: list[str] = []
        for kind in launcher_kinds or []:
            normalized = str(kind or "").strip().lower()
            if normalized and normalized not in kinds:
                kinds.append(normalized)
        if not kinds:
            notes.append("未检测到已安装的 PCL2 / HMCL，跳过版本隔离配置")
            return notes

        for kind in kinds:
            try:
                if kind == PCL_KIND:
                    notes.extend(VersionIsolation._apply_pcl(version_dir, version, enabled))
                elif kind == HMCL_KIND:
                    notes.extend(VersionIsolation._apply_hmcl(version_dir, version, enabled))
                else:
                    notes.append(f"未知启动器 {kind}，跳过版本隔离配置")
            except Exception:  # noqa: BLE001 - 隔离失败不应影响安装
                log.exception("写入 %s 版本隔离配置失败", kind)
                notes.append(f"{VersionIsolation._label(kind)}：写入版本隔离配置失败（详见日志）")
        return notes

    @staticmethod
    def is_isolated(mc_root: Path, version_id: str, launcher_kinds: list[str]) -> bool:
        """检查 ``version_id`` 是否至少对一个启动器处于隔离状态。"""
        mc_root = Path(mc_root)
        version = str(version_id or "").strip()
        if not version or version in (".", "..") or any(
            separator in version for separator in ("/", "\\")
        ):
            return False
        version_dir = mc_root / "versions" / version
        if not version_dir.is_dir():
            return False
        for kind in launcher_kinds or []:
            normalized = str(kind or "").strip().lower()
            try:
                if normalized == PCL_KIND and VersionIsolation._pcl_isolated(version_dir):
                    return True
                if normalized == HMCL_KIND and VersionIsolation._hmcl_isolated(version_dir):
                    return True
            except OSError:  # pragma: no cover - 无权限时视为未隔离
                log.debug("读取版本隔离配置失败：%s", version_dir, exc_info=True)
        return False

    # -- PCL --------------------------------------------------------------

    @staticmethod
    def _apply_pcl(version_dir: Path, version: str, enabled: bool) -> list[str]:
        setup = version_dir / "PCL" / "Setup.ini"
        updates = {
            PCL_KEY_V2: _format_value(enabled),
            PCL_KEY_LEGACY: "1" if enabled else "2",
        }
        changed, backed_up = _update_ini_file(setup, updates)
        label = VersionIsolation._label(PCL_KIND)
        action = "启用" if enabled else "关闭"
        relative = setup.relative_to(version_dir.parent.parent)
        if changed:
            backup_note = f"，原文件已备份为 {setup.name}.bak" if backed_up else ""
            return [f"{label}：已为版本 {version} {action}版本隔离（{relative}{backup_note}）"]
        return [f"{label}：版本 {version} 的隔离配置无需修改（已是目标状态）"]

    @staticmethod
    def _pcl_isolated(version_dir: Path) -> bool:
        setup = version_dir / "PCL" / "Setup.ini"
        text = _read_text(setup)
        if text is None:
            return False
        values: dict[str, str] = {}
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or ":" not in stripped:
                continue
            key, _, value = stripped.partition(":")
            values[key.strip()] = value.strip()
        if PCL_KEY_V2 in values:
            return values[PCL_KEY_V2].strip().lower() in _TRUTHY
        legacy = values.get(PCL_KEY_LEGACY, "")
        return legacy.strip() == "1"

    # -- HMCL -------------------------------------------------------------

    @staticmethod
    def _apply_hmcl(version_dir: Path, version: str, enabled: bool) -> list[str]:
        notes: list[str] = []
        label = VersionIsolation._label(HMCL_KIND)

        # 1) 新版 HMCL：instance-game-settings.json
        instance_file = version_dir / HMCL_INSTANCE_FILE
        try:
            existing = _load_json_dict(instance_file)
        except ValueError as exc:
            notes.append(f"{label}：{exc}，已跳过新版隔离配置写入")
            existing = None
        if existing is not None or not instance_file.is_file():
            running = existing.get("runningDirectory") if existing is not None else None
            custom_dir = isinstance(running, str) and running.strip() != ""
            if custom_dir and enabled:
                # 用户已设置自定义运行目录：不覆盖用户选择，只提示。
                # 此时也不写旧版 hmclversion.cfg，避免两套配置互相矛盾。
                notes.append(
                    f"{label}：版本 {version} 已设置自定义运行目录 {running!r}，"
                    "为避免破坏用户配置，跳过自动隔离"
                )
                return notes
            else:
                updates: dict[str, Any] = {
                    "$schema": HMCL_INSTANCE_SCHEMA,
                    "defaultIsolationType": (
                        HMCL_ISOLATION_ALWAYS if enabled else HMCL_ISOLATION_NEVER
                    ),
                }
                if not custom_dir:
                    updates["runningDirectory"] = ""
                try:
                    changed, backed_up = _update_json_file(instance_file, updates)
                except (OSError, ValueError) as exc:
                    notes.append(f"{label}：写入新版隔离配置失败（{exc}）")
                    changed, backed_up = False, False
                if changed:
                    action = "启用" if enabled else "关闭"
                    relative = instance_file.relative_to(version_dir.parent.parent)
                    backup_note = f"，原文件已备份为 {instance_file.name}.bak" if backed_up else ""
                    notes.append(f"{label}：已为版本 {version} {action}版本隔离（{relative}{backup_note}）")

        # 2) 旧版 HMCL：hmclversion.cfg（JSON，gameDirType 1 = 版本目录）
        legacy_file = version_dir / HMCL_LEGACY_FILE
        try:
            legacy = _load_json_dict(legacy_file)
        except ValueError:
            legacy = None
            if legacy_file.is_file():
                notes.append(
                    f"{label}：{legacy_file.name} 格式无法识别，已跳过旧版隔离配置写入"
                )
        if legacy is not None or not legacy_file.is_file():
            updates = {
                "gameDirType": (
                    HMCL_GAME_DIR_TYPE_VERSION if enabled else HMCL_GAME_DIR_TYPE_ROOT
                )
            }
            try:
                changed, backed_up = _update_json_file(legacy_file, updates)
            except (OSError, ValueError) as exc:
                notes.append(f"{label}：写入旧版隔离配置失败（{exc}）")
                changed, backed_up = False, False
            if changed:
                action = "启用" if enabled else "关闭"
                relative = legacy_file.relative_to(version_dir.parent.parent)
                backup_note = f"，原文件已备份为 {legacy_file.name}.bak" if backed_up else ""
                notes.append(
                    f"{label}：已为版本 {version} {action}版本隔离（旧版 {relative}{backup_note}）"
                )
        if not notes:
            notes.append(f"{label}：版本 {version} 的隔离配置无需修改（已是目标状态）")
        return notes

    @staticmethod
    def _hmcl_isolated(version_dir: Path) -> bool:
        # 新版
        instance_file = version_dir / HMCL_INSTANCE_FILE
        try:
            data = _load_json_dict(instance_file)
        except ValueError:
            data = None
        if isinstance(data, dict):
            running = data.get("runningDirectory")
            blank = running is None or (isinstance(running, str) and running.strip() == "")
            isolation = str(data.get("defaultIsolationType") or "").strip().upper()
            if blank and isolation == HMCL_ISOLATION_ALWAYS:
                return True
        # 旧版
        try:
            legacy = _load_json_dict(version_dir / HMCL_LEGACY_FILE)
        except ValueError:
            legacy = None
        if isinstance(legacy, dict):
            value = legacy.get("gameDirType")
            if value == HMCL_GAME_DIR_TYPE_VERSION or str(value).strip().upper() == "VERSION_FOLDER":
                return True
        return False

    # -- 杂项 -------------------------------------------------------------

    @staticmethod
    def _label(kind: str) -> str:
        return {"pcl": "PCL2", "hmcl": "HMCL"}.get(kind, kind)


__all__ = ["VersionIsolation"]
