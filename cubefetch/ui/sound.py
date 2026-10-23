"""轻量提醒音效（仅标准库 winsound，非 Windows 或失败时完全静默）。

用法：
    from . import sound
    sound.play("info")     # info / warn / error / success

设计要点：
* 每种提示音 0.3 秒内最多响一次，避免连续界面更新时“滴滴滴”刷屏；
* 使用 ``winsound.PlaySound(..., SND_ALIAS | SND_ASYNC)``，不阻塞 UI 线程；
* 非 Windows、控件销毁、音频设备异常等情况一律静默，不影响主流程。
"""
from __future__ import annotations

import logging
import threading
import time

log = logging.getLogger("cubefetch.ui.sound")

__all__ = ["play", "reset", "sound_kind", "MIN_INTERVAL"]

MIN_INTERVAL = 0.3  # 同类提示音的最小间隔（秒）

# 界面里的 kind 与音效 kind 的小映射（NoticeBar 等允许 “warning” 写法）。
_KIND_ALIASES = {"warning": "warn", "err": "error", "ok": "success"}


def sound_kind(kind: str) -> str:
    """把界面提示类型归一化成 info / warn / error / success。"""
    key = str(kind or "").strip().lower()
    key = _KIND_ALIASES.get(key, key)
    return key if key in _ALIASES else "info"

# 系统事件别名：异步播放，不需要音频文件。
_ALIASES = {
    "info": "SystemAsterisk",
    "success": "SystemAsterisk",
    "warn": "SystemExclamation",
    "error": "SystemHand",
}

# PlaySound 不可用（例如精简系统）时退化为 MessageBeep 的蜂鸣类型。
_BEEPS = {
    "info": 0x00000040,  # MB_ICONASTERISK
    "success": 0x00000040,
    "warn": 0x00000030,  # MB_ICONEXCLAMATION
    "error": 0x00000010,  # MB_ICONHAND
}

_lock = threading.Lock()
_last_played: dict[str, float] = {}


def play(kind: str = "info") -> bool:
    """播放提醒音。返回 True 表示真的播放了（未被节流/静默跳过）。"""
    key = sound_kind(kind)
    now = time.monotonic()
    with _lock:
        last = _last_played.get(key, 0.0)
        if now - last < MIN_INTERVAL:
            return False
        _last_played[key] = now

    try:
        import winsound
    except Exception:  # noqa: BLE001 - 非 Windows / 无 winsound
        return False

    try:
        winsound.PlaySound(_ALIASES[key], winsound.SND_ALIAS | winsound.SND_ASYNC)
        return True
    except Exception:  # noqa: BLE001 - 沙箱 / 无音频设备
        log.debug("PlaySound 失败，尝试 MessageBeep", exc_info=True)

    try:
        winsound.MessageBeep(_BEEPS[key])
        return True
    except Exception:  # noqa: BLE001
        log.debug("提醒音播放失败（已忽略）", exc_info=True)
        return False


def reset() -> None:
    """清空节流记录（自测/调试用）。"""
    with _lock:
        _last_played.clear()
