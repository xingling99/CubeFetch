"""CubeFetch 主题 —— 深色现代极简（Linear / Raycast 风格）。

设计要点：
- 近黑底 + 单一档 1px 描边 + 一级/二级浮层，不叠加厚边框；
- 强调色为紫 → 蓝渐变（CTA / 选中 / 进度）；
- 所有尺寸走 sp()（受 DPI SCALE 影响），字号用 pt（由 Tk scaling 处理）；
- 只保留一层浅色表面层级：卡片 #17171d，内嵌块 #1d1d25。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any

from . import dpi as _dpi

# DPI 感知必须在创建任何 Tk 之前完成（幂等）。
_dpi.enable_dpi_awareness()
SCALE: float = _dpi.detect_dpi_scale()


def sp(px: int) -> int:
    """把 100% 缩放下的设计像素换算为当前缩放的大小。"""
    return max(1, int(round(px * SCALE)))


# ---------------------------------------------------------------------------
# 色板
# ---------------------------------------------------------------------------
BG = "#0e0e12"            # 应用背景
BG_DEEP = "#0a0a0e"       # 日志 / 最深底
SIDEBAR = "#0e0e12"
HEADER = "#0e0e12"

SURFACE = "#17171d"       # 一级浮层（卡片）
SURFACE_ALT = "#1d1d25"   # 二级浮层（内嵌块）
SURFACE_HOVER = "#22222b"
SURFACE_PRESSED = "#24242e"
INPUT_BG = "#12121a"      # 输入框底
BG_INPUT = INPUT_BG       # 兼容旧命名

BORDER = "#2a2a35"        # 唯一 1px 描边色
BORDER_LIGHT = "#35353f"  # hover 时轻微提亮
BORDER_FOCUS = "#7c6cf0"

TEXT = "#f4f5f9"
TEXT_MUTED = "#a3a6b8"
TEXT_DIM = "#6f7282"

ACCENT = "#7c6cf0"
ACCENT_2 = "#58a6ff"
ACCENT_GRADIENT = (ACCENT, ACCENT_2)
TEXT_ON_PRIMARY = "#ffffff"

SUCCESS = "#3ddc97"
WARNING = "#f5b84d"
DANGER = "#ff6b6b"
INFO = "#58a6ff"

WHITE = "#ffffff"
BLACK = "#000000"
SCROLL_THUMB = "#34343f"
SCROLL_THUMB_HOVER = "#43434f"


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    color = color.lstrip("#")
    if len(color) == 3:
        color = "".join(ch * 2 for ch in color)
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def rgb_to_hex(rgb: tuple[int, int, int] | tuple[float, float, float]) -> str:
    return "#{:02x}{:02x}{:02x}".format(
        max(0, min(255, int(round(rgb[0])))),
        max(0, min(255, int(round(rgb[1])))),
        max(0, min(255, int(round(rgb[2])))),
    )


def mix(color_a: str, color_b: str, t: float) -> str:
    """按比例 t（0=全 a，1=全 b）混合两色。"""
    t = max(0.0, min(1.0, float(t)))
    ar, ag, ab = hex_to_rgb(color_a)
    br, bg_, bb = hex_to_rgb(color_b)
    return rgb_to_hex((ar + (br - ar) * t, ag + (bg_ - ag) * t, ab + (bb - ab) * t))


def lighten(color: str, amount: float) -> str:
    return mix(color, WHITE, amount)


def darken(color: str, amount: float) -> str:
    return mix(color, BLACK, amount)


def on(color: str, alpha: float, background: str | None = None) -> str:
    """模拟半透明：把 color 以 alpha 覆盖到 background（默认 BG）上。"""
    return mix(background or BG, color, alpha)


# 强调色 / 状态色的“淡底”版本（用于 hover、选中、徽标）
ACCENT_SOFT = on(ACCENT, 0.14)
ACCENT_DIM = on(ACCENT, 0.08)
ACCENT_HOVER = lighten(ACCENT, 0.10)
SUCCESS_SOFT = on(SUCCESS, 0.14)
WARNING_SOFT = on(WARNING, 0.14)
DANGER_SOFT = on(DANGER, 0.14)
INFO_SOFT = on(INFO, 0.14)

# 兼容旧命名（页面 / selftest 中仍在使用）
PRIMARY = ACCENT
PRIMARY_HOVER = ACCENT_HOVER
PRIMARY_PRESSED = "#6a5ad8"
PRIMARY_SOFT = ACCENT_SOFT
PRIMARY_DIM = ACCENT_DIM
BG_HOVER = SURFACE_HOVER

# 版本分类配色：正式=紫、预览=蓝、远古=灰蓝、愚人节=橙
CATEGORY_COLORS = {
    "release": ACCENT,
    "snapshot": ACCENT_2,
    "old": "#8892b0",
    "aprilfool": WARNING,
}
CATEGORY_SOFT = {key: on(value, 0.14) for key, value in CATEGORY_COLORS.items()}
CATEGORY_LABELS = {
    "release": "正式版",
    "snapshot": "预览版",
    "old": "远古版",
    "aprilfool": "愚人节版",
}
CATEGORY_ORDER = ("release", "snapshot", "old", "aprilfool")
LOADER_LABELS = {
    "vanilla": "原版",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}
MIRROR_LABELS = {
    "auto": "自动（优先镜像）",
    "official": "官方源",
    "bmclapi": "BMCLAPI",
}

# ---------------------------------------------------------------------------
# 尺寸（100% 设计稿像素，全部经 sp()）
# ---------------------------------------------------------------------------
WINDOW_WIDTH = sp(1080)
WINDOW_HEIGHT = sp(720)
MIN_WIDTH = sp(960)
MIN_HEIGHT = sp(640)

HEADER_HEIGHT = sp(64)
SIDEBAR_WIDTH = sp(208)
STATUSBAR_HEIGHT = sp(30)
CONTENT_PADDING = sp(24)

PAD_XS = sp(4)
PAD_SM = sp(8)
PAD_MD = sp(12)
PAD_LG = sp(16)
PAD_XL = sp(24)
PAD_XXL = sp(32)

RADIUS_XS = sp(6)
RADIUS_SM = sp(10)
RADIUS = sp(16)
RADIUS_MD = sp(12)
RADIUS_LG = sp(20)

ROW_HEIGHT = sp(44)
NAV_ITEM_HEIGHT = sp(40)
BUTTON_HEIGHT = sp(40)
BUTTON_HEIGHT_SM = sp(32)
BUTTON_HEIGHT_LG = sp(46)
INPUT_HEIGHT = sp(36)
CARD_WIDTH = sp(460)
LOADER_CARD_HEIGHT = sp(72)

# ---------------------------------------------------------------------------
# 字体
# ---------------------------------------------------------------------------
_FONT_CANDIDATES = (
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans CJK SC",
    "Segoe UI",
    "Arial",
)
_MONO_CANDIDATES = (
    "Cascadia Mono",
    "Cascadia Code",
    "Consolas",
    "JetBrains Mono",
    "Courier New",
)

_unavailable_fonts: set[str] = set()
_ttk_done = False

FONT_FAMILY = _FONT_CANDIDATES[0]
MONO_FAMILY = _MONO_CANDIDATES[0]

# 字号（pt）：48px 大数字 ≈ 32pt；13px 正文 ≈ 10pt
FS_HUGE = 32
FS_DISPLAY = 22
FS_HEADING = 18
FS_TITLE = 15
FS_SUBTITLE = 11
FS_BODY = 10
FS_SMALL = 9
FS_TINY = 8
FS_MONO_CODE = 20


def _resolve(root: tk.Misc) -> None:
    global FONT_FAMILY, MONO_FAMILY
    try:
        available = set(tkfont.families(root))
    except tk.TclError:
        available = set()
    if available:
        for name in _FONT_CANDIDATES:
            if name in available:
                FONT_FAMILY = name
                break
        for name in _MONO_CANDIDATES:
            if name in available:
                MONO_FAMILY = name
                break


def family() -> str:
    return FONT_FAMILY


def mono_family() -> str:
    return MONO_FAMILY


def font(size: int = FS_BODY, weight: str = "normal", *, mono: bool = False,
         slant: str = "roman") -> tuple:
    """返回 tkinter 字体元组（size 为 pt，DPI 倍增由 Tk scaling 处理）。"""
    return (MONO_FAMILY if mono else FONT_FAMILY, max(1, int(size)), weight, slant)


def bold(size: int = FS_BODY, *, mono: bool = False) -> tuple:
    return font(size, "bold", mono=mono)


# ---------------------------------------------------------------------------
# 绘制工具
# ---------------------------------------------------------------------------
def rounded_rect(canvas: tk.Canvas, x1: float, y1: float, x2: float, y2: float,
                 radius: float = RADIUS, **kwargs: Any) -> int:
    """在 Canvas 上画一个圆角矩形（用平滑多边形模拟），返回 item id。"""
    radius = max(0.0, min(radius, (x2 - x1) / 2.0, (y2 - y1) / 2.0))
    points = [
        x1 + radius, y1,
        x2 - radius, y1,
        x2, y1,
        x2, y1 + radius,
        x2, y2 - radius,
        x2, y2,
        x2 - radius, y2,
        x1 + radius, y2,
        x1, y2,
        x1, y2 - radius,
        x1, y1 + radius,
        x1, y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=32, **kwargs)


def draw_linear_gradient(canvas: tk.Canvas, x1: float, y1: float, x2: float,
                         y2: float, radius: float, start: str, end: str,
                         *, bands: int = 18, tags: tuple = ()) -> list[int]:
    """圆角线性渐变（自 start 到 end 自上而下），用逐层圆角矩形叠出。"""
    height = max(1.0, y2 - y1)
    items: list[int] = []
    for index in range(bands):
        offset = height * index / bands
        color = mix(start, end, index / max(1, bands - 1))
        item = rounded_rect(canvas, x1, y1 + offset, x2, y2, radius,
                            fill=color, outline="", tags=tags)
        items.append(item)
    return items


# ---------------------------------------------------------------------------
# ttk 样式（输入控件统一深色扁平）
# ---------------------------------------------------------------------------
def init(root: tk.Misc) -> None:
    """初始化字体与 ttk 样式（创建控件前调用一次）。"""
    global _ttk_done
    _resolve(root)
    if _ttk_done:
        return
    try:
        style = ttk.Style(root)
        style.theme_use("clam")
    except tk.TclError:
        return
    _ttk_done = True

    style.configure("Dark.TEntry",
                    fieldbackground=INPUT_BG, background=INPUT_BG,
                    foreground=TEXT, insertcolor=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(sp(10), sp(7)))
    style.map("Dark.TEntry",
              bordercolor=[("focus", BORDER_FOCUS)],
              lightcolor=[("focus", BORDER_FOCUS)],
              fieldbackground=[("disabled", SURFACE_ALT)],
              foreground=[("disabled", TEXT_DIM)])

    style.configure("Dark.TCombobox",
                    fieldbackground=INPUT_BG, background=INPUT_BG,
                    foreground=TEXT, arrowcolor=TEXT_MUTED,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(sp(8), sp(5)))
    style.map("Dark.TCombobox",
              bordercolor=[("focus", BORDER_FOCUS)],
              lightcolor=[("focus", BORDER_FOCUS)],
              fieldbackground=[("readonly", INPUT_BG), ("disabled", SURFACE_ALT)],
              foreground=[("disabled", TEXT_DIM)])
    try:
        root.option_add("*TCombobox*Listbox.background", SURFACE_ALT)
        root.option_add("*TCombobox*Listbox.foreground", TEXT)
        root.option_add("*TCombobox*Listbox.selectBackground", ACCENT_SOFT)
        root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
        root.option_add("*TCombobox*Listbox.borderWidth", 0)
    except tk.TclError:
        pass

    style.configure("Dark.TSpinbox",
                    fieldbackground=INPUT_BG, background=SURFACE_ALT,
                    foreground=TEXT, insertcolor=TEXT, arrowcolor=TEXT_MUTED,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    padding=(sp(8), sp(4)))
    style.map("Dark.TSpinbox",
              bordercolor=[("focus", BORDER_FOCUS)],
              lightcolor=[("focus", BORDER_FOCUS)],
              fieldbackground=[("disabled", SURFACE_ALT)],
              foreground=[("disabled", TEXT_DIM)])

    style.configure("Dark.TButton",
                    background=SURFACE_ALT, foreground=TEXT,
                    bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER,
                    focuscolor="", padding=(sp(14), sp(7)))
    style.map("Dark.TButton",
              background=[("active", SURFACE_HOVER)],
              bordercolor=[("active", BORDER_LIGHT)])

    style.configure("Dark.Vertical.TScrollbar",
                    background=SURFACE_HOVER, troughcolor=BG,
                    bordercolor=BG, arrowcolor=TEXT_DIM,
                    lightcolor=BG, darkcolor=BG,
                    gripcount=0, arrowsize=sp(10), width=sp(8))
    style.map("Dark.Vertical.TScrollbar",
              background=[("active", SCROLL_THUMB_HOVER), ("!active", SCROLL_THUMB)])


def reset() -> None:
    """重置主题缓存（主要供测试在窗口销毁后重建）。"""
    global _ttk_done
    _ttk_done = False
    _unavailable_fonts.clear()
