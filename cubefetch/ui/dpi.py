"""DPI 感知与缩放系数。

Windows 在 125% / 150% 缩放下默认把整个窗口当作位图拉伸（进程 DPI 不感知），
导致字体发虚、布局比预期小。这里做两件事：

* :func:`enable_dpi_awareness` —— 在创建 ``tk.Tk()`` 之前声明 Per-Monitor V2
  感知（失败逐级回退），让 Windows 不再做位图拉伸；幂等，可以随便调用。
* :func:`detect_dpi_scale` —— 返回布局缩放系数（1.0 表示 96 DPI）。优先读
  环境变量 ``CUBEFETCH_DPI_SCALE``（1.0~3.0，便于离线和 CI 模拟 150%），
  否则用 ``GetDpiForSystem()``，探测失败回退 1.0。

``theme.SCALE`` 取这个系数，所有设计稿像素经 ``theme.sp()`` 换算成物理像素；
字体字号保持 pt，由 Tk 的 scaling 机制统一放大，避免重复缩放。
"""
from __future__ import annotations

import ctypes
import logging
import os

log = logging.getLogger("cubefetch.ui.dpi")

MIN_SCALE = 1.0
MAX_SCALE = 3.0
ENV_VAR = "CUBEFETCH_DPI_SCALE"

_awareness_done = False


def _call(func, *args) -> bool:
    """调用 Win32 API，返回是否成功（任何异常都当作失败以便回退）。"""
    try:
        result = func(*args)
    except Exception:  # noqa: BLE001 - 缺少 API / 权限不足时继续回退
        return False
    return result is not None


def enable_dpi_awareness() -> None:
    """声明进程 DPI 感知（幂等；必须在创建 ``tk.Tk()`` 之前调用）。"""
    global _awareness_done
    if _awareness_done:
        return
    if os.name != "nt":
        _awareness_done = True
        return

    user32 = ctypes.windll.user32
    # 1) Per-Monitor V2：DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
    try:
        if _call(user32.SetProcessDpiAwarenessContext, ctypes.c_void_p(-4)):
            log.debug("DPI 感知：Per-Monitor V2")
            _awareness_done = True
            return
    except AttributeError:  # Windows 7 没有该 API
        pass

    # 2) Per-Monitor：PROCESS_PER_MONITOR_DPI_AWARE = 2
    try:
        if _call(ctypes.windll.shcore.SetProcessDpiAwareness, 2):
            log.debug("DPI 感知：Per-Monitor")
            _awareness_done = True
            return
    except (AttributeError, OSError):
        pass

    # 3) 系统级 DPI 感知（Vista+）
    if _call(user32.SetProcessDPIAware):
        log.debug("DPI 感知：System")
    _awareness_done = True


def _env_scale() -> float | None:
    raw = os.environ.get(ENV_VAR, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError:
        log.warning("%s 不是合法数字：%r，已忽略", ENV_VAR, raw)
        return None
    if not MIN_SCALE <= value <= MAX_SCALE:
        log.warning("%s 超出 %.1f~%.1f：%r，已忽略", ENV_VAR, MIN_SCALE, MAX_SCALE, raw)
        return None
    return value


def _system_scale() -> float | None:
    if os.name != "nt":
        return None
    try:
        dpi = int(ctypes.windll.user32.GetDpiForSystem())
    except Exception:  # noqa: BLE001 - Win7 无此 API
        dpi = 0
    if dpi <= 0:
        try:
            hdc = int(ctypes.windll.user32.GetDC(0) or 0)
            dpi = int(ctypes.windll.gdi32.GetDeviceCaps(hdc, 88))  # LOGPIXELSX
            ctypes.windll.user32.ReleaseDC(0, hdc)
        except Exception:  # noqa: BLE001
            dpi = 0
    if dpi > 0:
        return max(MIN_SCALE, min(MAX_SCALE, dpi / 96.0))
    return None


def detect_dpi_scale() -> float:
    """返回当前布局缩放系数（1.0 = 96 DPI，1.5 = 150%）。"""
    env_scale = _env_scale()
    if env_scale is not None:
        return env_scale
    return _system_scale() or 1.0


__all__ = [
    "ENV_VAR",
    "MAX_SCALE",
    "MIN_SCALE",
    "detect_dpi_scale",
    "enable_dpi_awareness",
]
