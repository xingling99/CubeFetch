"""CubeFetch 可复用控件 —— 深色现代极简风格（纯 tkinter Canvas 绘制）。

所有尺寸参数均为 100% 缩放下的设计像素，由 theme.sp() 统一换算。
本模块不依赖 Pillow / glass.py。
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont
from tkinter import ttk
from typing import Any, Callable

from . import sound, theme

__all__ = [
    "master_bg",
    "bind_recursive",
    "set_bg_recursive",
    "truncate",
    "Button",
    "primary_button",
    "secondary_button",
    "danger_button",
    "ghost_button",
    "LinkLabel",
    "Card",
    "Separator",
    "ScrollFrame",
    "ProgressBar",
    "Spinner",
    "Badge",
    "NoticeBar",
    "EmptyState",
    "LogView",
    "SegmentedControl",
    "VersionRow",
    "ChoiceCard",
    "PageHeader",
    "draw_cube_logo",
    "draw_state_icon",
    "category_badge",
]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def master_bg(widget: tk.Misc) -> str:
    """尽力取得父容器的背景色（用于“透明底”控件的视觉融合）。"""
    current: tk.Misc | None = widget
    while current is not None:
        try:
            color = str(current.cget("bg"))
            if color:
                return color
        except (tk.TclError, AttributeError):
            pass
        try:
            current = current.master  # type: ignore[assignment]
        except AttributeError:
            return theme.BG
    return theme.BG


def _walk(widget: tk.Misc):
    yield widget
    try:
        children = widget.winfo_children()
    except (tk.TclError, AttributeError):
        return
    for child in children:
        yield from _walk(child)


def bind_recursive(widget: tk.Misc, sequence: str, callback: Callable) -> None:
    """给控件及其全部子控件绑定事件（列表行 / 卡片点击用）。"""
    for child in _walk(widget):
        try:
            child.bind(sequence, callback)
        except tk.TclError:
            pass


def set_bg_recursive(widget: tk.Misc, color: str) -> None:
    for child in _walk(widget):
        try:
            child.configure(bg=color)
        except tk.TclError:
            pass


def truncate(text: str, limit: int = 90) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _measure(text: str, font_tuple: tuple) -> int:
    try:
        return int(tkfont.Font(font=font_tuple).measure(text))
    except Exception:  # pragma: no cover - 极端环境下回退估算
        return int(len(text) * (font_tuple[1] if len(font_tuple) > 1 else 10) * 1.05)


# ---------------------------------------------------------------------------
# 按钮
# ---------------------------------------------------------------------------
_KIND_STYLES = ("primary", "secondary", "danger", "danger_soft", "success", "ghost")


class Button(tk.Canvas):
    """现代扁平按钮：主按钮渐变，次按钮透明 + 细描边。"""

    def __init__(
        self,
        master: tk.Misc,
        text: str = "",
        command: Callable[[], None] | None = None,
        *,
        kind: str = "primary",
        width: int | None = None,
        height: int = theme.BUTTON_HEIGHT,
        font_size: int = theme.FS_BODY,
        bold_text: bool = False,
        radius: int = theme.RADIUS_SM,
        padx: int = theme.sp(18),
        bg: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._parent_bg = bg or master_bg(master)
        self._height = int(height)
        self._radius = int(radius)
        self._padx = int(padx)
        self._text = str(text)
        self._command = command
        self._kind = kind if kind in _KIND_STYLES else "primary"
        self._bold = bool(bold_text)
        self._font_size = int(font_size)
        self._font: tuple = theme.font(self._font_size, "bold" if bold_text else "normal")
        self._enabled = True
        self._hover = False
        self._pressed = False
        natural = _measure(self._text, self._font) + self._padx * 2
        self._width = theme.sp(int(width)) if width is not None else natural
        kwargs.pop("highlightthickness", None)
        super().__init__(
            master,
            width=self._width,
            height=self._height,
            bg=self._parent_bg,
            highlightthickness=0,
            bd=0,
            takefocus=0,
            **kwargs,
        )
        self.bind("<Configure>", self._on_configure)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<ButtonPress-1>", self._on_press)
        self.bind("<ButtonRelease-1>", self._on_release)
        self._redraw()

    # -- 对外 API ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self._enabled

    def invoke(self) -> None:
        if self._enabled and callable(self._command):
            self._command()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        try:
            self.configure(cursor="hand2" if enabled else "arrow")
        except tk.TclError:
            pass
        self._redraw()

    def set_text(self, text: str) -> None:
        self._text = str(text)
        natural = _measure(self._text, self._font) + self._padx * 2
        if natural > self._width:
            self._width = natural
            self.configure(width=self._width)
        self._redraw()

    def get_text(self) -> str:
        return self._text

    def set_kind(self, kind: str) -> None:
        self._kind = kind if kind in _KIND_STYLES else "primary"
        self._redraw()

    # -- 事件 -------------------------------------------------------------
    def _on_configure(self, _event=None) -> None:
        self._redraw()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        if self._enabled:
            self.configure(cursor="hand2")
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._pressed = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if not self._enabled:
            return
        self._pressed = True
        self._redraw()

    def _on_release(self, event=None) -> None:
        if not self._enabled:
            return
        was_pressed = self._pressed
        self._pressed = False
        self._redraw()
        if was_pressed:
            try:
                inside = 0 <= event.x <= self.winfo_width() and 0 <= event.y <= self.winfo_height()
            except (tk.TclError, AttributeError):
                inside = True
            if inside:
                self.invoke()

    # -- 绘制 -------------------------------------------------------------
    def _redraw(self) -> None:
        try:
            width = self.winfo_width() if self.winfo_width() > 2 else self._width
            height = self.winfo_height() if self.winfo_height() > 2 else self._height
        except tk.TclError:  # pragma: no cover - 已销毁
            return
        self.delete("all")
        radius = min(self._radius, height // 2)
        x1, y1, x2, y2 = 0.5, 0.5, width - 0.5, height - 0.5
        parent_bg = self._parent_bg
        disabled = not self._enabled

        if disabled:
            fill = theme.mix(theme.SURFACE_ALT, parent_bg, 0.35)
            theme.rounded_rect(self, x1, y1, x2, y2, radius, fill=fill,
                               outline=theme.mix(theme.BORDER, parent_bg, 0.4), width=1)
            fg = theme.TEXT_DIM
        elif self._kind == "primary":
            start, end = theme.ACCENT, theme.ACCENT_2
            if self._pressed:
                start, end = theme.darken(start, 0.10), theme.darken(end, 0.10)
            elif self._hover:
                start, end = theme.lighten(start, 0.10), theme.lighten(end, 0.10)
            theme.draw_linear_gradient(self, x1, y1, x2, y2, radius, start, end)
            fg = theme.TEXT_ON_PRIMARY
        elif self._kind in ("secondary", "ghost"):
            if self._hover or self._pressed:
                alpha = 0.10 if self._pressed else 0.06
                fill = theme.mix(parent_bg, theme.WHITE, alpha)
            else:
                fill = parent_bg
            outline = theme.BORDER_LIGHT if self._kind == "secondary" else ""
            theme.rounded_rect(self, x1, y1, x2, y2, radius, fill=fill,
                               outline=outline, width=1)
            fg = theme.TEXT if self._kind == "secondary" else theme.TEXT_MUTED
        elif self._kind == "danger":
            alpha = 0.92 if self._hover else 0.82
            fill = theme.mix(parent_bg, theme.DANGER, alpha)
            theme.rounded_rect(self, x1, y1, x2, y2, radius, fill=fill,
                               outline="", width=0)
            fg = theme.WHITE
        elif self._kind == "danger_soft":
            alpha = 0.22 if self._pressed else 0.17 if self._hover else 0.13
            fill = theme.mix(parent_bg, theme.DANGER, alpha)
            theme.rounded_rect(self, x1, y1, x2, y2, radius, fill=fill,
                               outline=theme.mix(parent_bg, theme.DANGER, 0.45), width=1)
            fg = theme.lighten(theme.DANGER, 0.10)
        else:  # success
            alpha = 0.22 if self._pressed else 0.16 if self._hover else 0.13
            fill = theme.mix(parent_bg, theme.SUCCESS, alpha)
            theme.rounded_rect(self, x1, y1, x2, y2, radius, fill=fill,
                               outline=theme.mix(parent_bg, theme.SUCCESS, 0.45), width=1)
            fg = theme.SUCCESS

        self.create_text(width / 2, height / 2 + 0.5, text=self._text, fill=fg,
                         font=self._font, anchor="center")


def primary_button(master: tk.Misc, text: str, command=None, **kwargs: Any) -> Button:
    kwargs.setdefault("bold_text", True)
    return Button(master, text, command, kind="primary", **kwargs)


def secondary_button(master: tk.Misc, text: str, command=None, **kwargs: Any) -> Button:
    return Button(master, text, command, kind="secondary", **kwargs)


def danger_button(master: tk.Misc, text: str, command=None, **kwargs: Any) -> Button:
    return Button(master, text, command, kind="danger", **kwargs)


def ghost_button(master: tk.Misc, text: str, command=None, **kwargs: Any) -> Button:
    return Button(master, text, command, kind="ghost", **kwargs)


class LinkLabel(tk.Label):
    """强调色文字链接（hover 下划线）。"""

    def __init__(self, master: tk.Misc, text: str = "", command=None, *,
                 bg: str | None = None, fg: str = theme.ACCENT_2,
                 font_size: int = theme.FS_BODY, **kwargs: Any) -> None:
        self._command = command
        self._link_font = theme.font(font_size)
        super().__init__(master, text=text, bg=bg or master_bg(master), fg=fg,
                         font=self._link_font, cursor="hand2", **kwargs)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Enter>", lambda _e: self.configure(
            font=theme.font(font_size, underline=True)))
        self.bind("<Leave>", lambda _e: self.configure(font=self._link_font))

    def _on_click(self, _event=None) -> None:
        if callable(self._command):
            self._command()


# ---------------------------------------------------------------------------
# 卡片 / 分隔线
# ---------------------------------------------------------------------------
class Card(tk.Frame):
    """圆角卡片：Canvas 自绘背景 + 内嵌 body（纯色 + 1px 描边，无渐变厚边）。"""

    def __init__(self, master: tk.Misc, *, padding: int = theme.PAD_XL,
                 bg: str = theme.SURFACE, border: str = theme.BORDER,
                 **kwargs: Any) -> None:
        self._surface = bg
        self._border = border
        self._padding = int(padding)
        self._parent_bg = master_bg(master)
        super().__init__(master, bg=self._parent_bg, bd=0, highlightthickness=0, **kwargs)
        self._panel = tk.Canvas(self, bg=self._parent_bg, highlightthickness=0,
                                bd=0, takefocus=0)
        self._panel.place(x=0, y=0, relwidth=1, relheight=1)
        self.body = tk.Frame(self, bg=self._surface, bd=0, highlightthickness=0)
        self.body.pack(fill="both", expand=True, padx=self._padding, pady=self._padding)
        self._panel.bind("<Configure>", self._on_configure)
        self.bind("<Configure>", self._on_configure)
        self.bind("<Map>", self._on_configure)
        self.after_idle(self._on_configure)

    def _on_configure(self, _event=None) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width <= 2 or height <= 2:
            return
        self._panel.delete("all")
        theme.rounded_rect(self._panel, 0.5, 0.5, width - 0.5, height - 0.5,
                           theme.RADIUS, fill=self._surface, outline=self._border, width=1)

    def set_border(self, color: str) -> None:
        self._border = color
        self._on_configure()


class Separator(tk.Frame):
    def __init__(self, master: tk.Misc, *, bg: str = theme.BORDER, height: int = 1,
                 **kwargs: Any) -> None:
        super().__init__(master, bg=bg, height=theme.sp(height), **kwargs)


# ---------------------------------------------------------------------------
# 滚动容器
# ---------------------------------------------------------------------------
class ScrollFrame(tk.Frame):
    """Canvas + 竖向滚动条 + 鼠标滚轮（在指针位于区域内时生效）。"""

    def __init__(self, master: tk.Misc, *, bg: str = theme.BG,
                 show_scrollbar: bool = True, **kwargs: Any) -> None:
        super().__init__(master, bg=bg, bd=0, highlightthickness=0, **kwargs)
        self._bg = bg
        self._show_scrollbar = show_scrollbar
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0,
                                width=1, height=1, takefocus=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview,
                                  style="Dark.Vertical.TScrollbar")
        if show_scrollbar:
            self.vbar.pack(side="right", fill="y", padx=(theme.PAD_XS, 0))
        self.canvas.configure(yscrollcommand=self._on_scroll)
        self.body = tk.Frame(self.canvas, bg=bg, bd=0, highlightthickness=0)
        self._window = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>", self._on_body_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<Enter>", lambda _e: self._bind_wheel(True))
        self.canvas.bind("<Leave>", lambda _e: self._bind_wheel(False))
        self.bind("<Destroy>", lambda _e: self._bind_wheel(False))

    def _bind_wheel(self, active: bool) -> None:
        try:
            if active:
                self.canvas.bind_all("<MouseWheel>", self._on_wheel)
                self.canvas.bind_all("<Button-4>", self._on_wheel)
                self.canvas.bind_all("<Button-5>", self._on_wheel)
            else:
                self.canvas.unbind_all("<MouseWheel>")
                self.canvas.unbind_all("<Button-4>")
                self.canvas.unbind_all("<Button-5>")
        except tk.TclError:  # pragma: no cover
            pass

    def _on_scroll(self, first: str, last: str) -> None:
        try:
            if self._show_scrollbar:
                if float(first) <= 0.0 and float(last) >= 1.0:
                    self.vbar.pack_forget()
                elif not self.vbar.winfo_ismapped():
                    self.vbar.pack(side="right", fill="y", padx=(theme.PAD_XS, 0))
            self.vbar.set(first, last)
        except (tk.TclError, ValueError):
            pass

    def _on_body_configure(self, _event=None) -> None:
        try:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _on_canvas_configure(self, event) -> None:
        try:
            self.canvas.itemconfigure(self._window, width=event.width)
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        except tk.TclError:
            pass

    def _pointer_inside(self) -> bool:
        try:
            x, y = self.winfo_pointerxy()
            widget = self.winfo_containing(x, y)
        except (tk.TclError, KeyError):
            return False
        if widget is None:
            return False
        own_path = str(self)
        widget_path = str(widget)
        return widget_path == own_path or widget_path.startswith(own_path + ".")

    def _scrollable(self) -> bool:
        try:
            return self.body.winfo_reqheight() > self.canvas.winfo_height() + 2
        except tk.TclError:
            return False

    def _on_wheel(self, event) -> None:
        if not self.winfo_exists():
            return
        if not self._pointer_inside() or not self._scrollable():
            return
        try:
            delta = int(getattr(event, "delta", 0) or 0)
        except (TypeError, ValueError):
            delta = 0
        try:
            num = int(getattr(event, "num", 0) or 0)
        except (TypeError, ValueError):
            num = 0
        if delta:
            step = -int(delta / 2)
        elif num == 4:
            step = -theme.sp(40)
        elif num == 5:
            step = theme.sp(40)
        else:
            return
        try:
            self.canvas.yview_scroll(step, "units")
        except tk.TclError:
            pass

    def clear(self) -> None:
        for child in list(self.body.winfo_children()):
            child.destroy()

    def scroll_to_top(self) -> None:
        try:
            self.canvas.yview_moveto(0.0)
        except tk.TclError:
            pass

    def scroll_to_bottom(self) -> None:
        try:
            self.canvas.yview_moveto(1.0)
        except tk.TclError:
            pass


# ---------------------------------------------------------------------------
# 进度条 / Spinner
# ---------------------------------------------------------------------------
class ProgressBar(tk.Canvas):
    """圆角渐变进度条（带可选柔光扫过）。"""

    def __init__(self, master: tk.Misc, *, height: int = 6, radius: int = 3,
                 track: str = theme.SURFACE_ALT, fill: str = theme.ACCENT,
                 bg: str | None = None, **kwargs: Any) -> None:
        self._bar_height = theme.sp(height)
        self._plan_radius = theme.sp(radius)
        self._track = track
        self._fill = fill
        self._bg = bg or master_bg(master)
        self._fraction = 0.0
        self._indeterminate = False
        self._marquee = 0.0
        self._shimmer = 0.0
        self._job: str | None = None
        self._shimmer_job: str | None = None
        kwargs.pop("highlightthickness", None)
        super().__init__(master, height=self._bar_height, width=1,
                         bg=self._bg, highlightthickness=0, bd=0, takefocus=0, **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Map>", lambda _e: self._sync_shimmer())
        self.bind("<Unmap>", lambda _e: self._stop_animation())
        self.bind("<Destroy>", lambda _e: self._stop_animation())
        self._redraw()

    def set(self, fraction: float, *, start: float = 0.0, stop: float = 1.0) -> None:
        try:
            value = float(fraction)
        except (TypeError, ValueError):
            value = 0.0
        self._fraction = max(start, min(stop, value))
        self._indeterminate = False
        self._stop_marquee()
        self._redraw()
        self._sync_shimmer()

    @property
    def fraction(self) -> float:
        return self._fraction

    def indeterminate(self, active: bool = True) -> None:
        self._indeterminate = bool(active)
        if active:
            self._shimmer = 0.0
            self._start_marquee()
        else:
            self._stop_marquee()
            self._redraw()

    def reset(self) -> None:
        self._fraction = 0.0
        self._indeterminate = False
        self._stop_animation()
        self._redraw()

    # -- 动画 ------------------------------------------------------------
    def _stop_animation(self) -> None:
        self._stop_marquee()
        self._stop_shimmer()

    def _sync_shimmer(self) -> None:
        should = (self._fraction > 0.0 or self._indeterminate) and self.winfo_ismapped()
        if should:
            if self._shimmer_job is None:
                self._shimmer_job = self.after(80, self._tick_shimmer)
        else:
            self._stop_shimmer()

    def _stop_shimmer(self) -> None:
        if self._shimmer_job is not None:
            try:
                self.after_cancel(self._shimmer_job)
            except (tk.TclError, ValueError):
                pass
            self._shimmer_job = None

    def _tick_shimmer(self) -> None:
        self._shimmer_job = None
        if not self.winfo_exists():
            return
        self._shimmer = (self._shimmer + 0.05) % 1.0
        self._redraw()
        self._sync_shimmer()

    def _start_marquee(self) -> None:
        if self._job is None:
            self._job = self.after(50, self._tick_marquee)

    def _stop_marquee(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None

    def _tick_marquee(self) -> None:
        self._job = None
        if not self._indeterminate or not self.winfo_exists():
            return
        self._marquee = (self._marquee + 0.03) % 1.0
        self._redraw()
        self._start_marquee()

    # -- 绘制 ------------------------------------------------------------
    def _redraw(self) -> None:
        try:
            width = self.winfo_width() if self.winfo_width() > 2 else int(self.cget("width"))
            height = self.winfo_height() if self.winfo_height() > 2 else self._bar_height
        except tk.TclError:  # pragma: no cover
            return
        self.delete("all")
        radius = min(self._plan_radius, height // 2)
        theme.rounded_rect(self, 0, 0, width, height, radius,
                           fill=self._track, outline="")
        if self._indeterminate:
            block = max(theme.sp(64), int(width * 0.22))
            x1 = int((width + block) * self._marquee) - block
            theme.rounded_rect(self, max(0, x1), 0, min(width, x1 + block), height,
                               radius, fill=self._fill, outline="")
        elif self._fraction > 0:
            fill_width = max(theme.sp(6), int(round(width * self._fraction)))
            fill_width = min(width, fill_width)
            theme.draw_linear_gradient(self, 0, 0, fill_width, height, radius,
                                       theme.ACCENT, theme.ACCENT_2, bands=12)
            band = max(theme.sp(40), int(width * 0.10))
            center = int(fill_width * self._shimmer)
            x1 = max(0, center - band // 2)
            x2 = min(fill_width, center + band // 2)
            if x2 - x1 > 2:
                overlay = theme.mix(theme.ACCENT_2, theme.WHITE, 0.20)
                theme.rounded_rect(self, x1, 0, x2, height,
                                   min(radius, (x2 - x1) // 2),
                                   fill=overlay, outline="")


class Spinner(tk.Canvas):
    """轻量转圈（12 FPS）。"""

    def __init__(self, master: tk.Misc, *, size: int = 20, bg: str | None = None,
                 color: str = theme.ACCENT, **kwargs: Any) -> None:
        self._size = theme.sp(size)
        self._color = color
        self._bg = bg or master_bg(master)
        self._angle = 0.0
        self._job: str | None = None
        kwargs.pop("highlightthickness", None)
        super().__init__(master, width=self._size, height=self._size, bg=self._bg,
                         highlightthickness=0, bd=0, takefocus=0, **kwargs)
        self.bind("<Destroy>", lambda _e: self.stop())
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        size = self._size
        pad = max(2, int(size * 0.12))
        self.create_oval(pad, pad, size - pad, size - pad,
                         outline=theme.mix(self._bg, theme.BORDER_LIGHT, 0.55),
                         width=max(1, int(size * 0.09)))
        start = (self._angle * 360.0) % 360.0
        self.create_arc(pad, pad, size - pad, size - pad, start=start, extent=100,
                        style="arc", outline=self._color,
                        width=max(2, int(size * 0.13)))

    def start(self) -> None:
        if self._job is None:
            self._tick()

    def stop(self) -> None:
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except (tk.TclError, ValueError):
                pass
            self._job = None

    def _tick(self) -> None:
        self._job = None
        if not self.winfo_exists():
            return
        self._angle = (self._angle + 0.12) % 1.0
        self._draw()
        self._job = self.after(80, self._tick)


# ---------------------------------------------------------------------------
# 徽标 / 提示条
# ---------------------------------------------------------------------------
class Badge(tk.Label):
    """小号扁平标签（分类 / 推荐 / 纯净等）。"""

    def __init__(self, master: tk.Misc, text: str = "", *, fg: str = theme.TEXT_MUTED,
                 bg_color: str = theme.SURFACE_ALT, font_size: int = theme.FS_TINY,
                 padx: int = 7, pady: int = 2, **kwargs: Any) -> None:
        super().__init__(master, text=text, fg=fg, bg=bg_color,
                         font=theme.font(font_size, "bold"),
                         padx=theme.sp(padx), pady=theme.sp(pady),
                         bd=0, highlightthickness=0, **kwargs)

    def set_style(self, *, fg: str, bg_color: str) -> None:
        self.configure(fg=fg, bg=bg_color)


def category_badge(master: tk.Misc, category: str, bg: str | None = None) -> Badge:
    label = theme.CATEGORY_LABELS.get(category, category)
    color = theme.CATEGORY_COLORS.get(category, theme.TEXT_MUTED)
    surface = bg if bg is not None else master_bg(master)
    return Badge(master, label, fg=color, bg_color=theme.mix(surface, color, 0.14))


class NoticeBar(tk.Frame):
    """行内提示条：错误 / 警告 / 信息 / 成功。"""

    _PALETTES = {
        "error": (theme.DANGER, theme.DANGER_SOFT, "!"),
        "warn": (theme.WARNING, theme.WARNING_SOFT, "!"),
        "warning": (theme.WARNING, theme.WARNING_SOFT, "!"),
        "info": (theme.INFO, theme.INFO_SOFT, "i"),
        "success": (theme.SUCCESS, theme.SUCCESS_SOFT, "✓"),
    }

    def __init__(self, master: tk.Misc, *, bg: str | None = None,
                 pady: tuple[int, int] = (0, theme.PAD_MD),
                 reserve_space: bool = True, before: tk.Misc | None = None,
                 **kwargs: Any) -> None:
        self._parent_bg = bg or master_bg(master)
        self._reserve_space = bool(reserve_space)
        self._before = before
        self._pack_options: dict[str, Any] = {"fill": "x", "pady": pady}
        super().__init__(master, bg=self._parent_bg, bd=0, highlightthickness=0, **kwargs)
        self._inner = tk.Frame(self, bg=theme.DANGER_SOFT, bd=0, highlightthickness=0)
        self._accent = tk.Frame(self._inner, bg=theme.DANGER, width=theme.sp(3))
        self._accent.pack(side="left", fill="y")
        self._icon = tk.Label(self._inner, text="!", bg=theme.DANGER_SOFT, fg=theme.DANGER,
                              font=theme.bold(theme.FS_BODY), padx=theme.sp(10),
                              pady=theme.sp(8))
        self._icon.pack(side="left")
        self._label = tk.Label(self._inner, text="", bg=theme.DANGER_SOFT, fg=theme.TEXT,
                               font=theme.font(theme.FS_BODY), anchor="w", justify="left",
                               wraplength=theme.sp(760), padx=theme.sp(2), pady=theme.sp(8))
        self._label.pack(side="left", fill="x", expand=True)
        self._close = tk.Label(self._inner, text="×", bg=theme.DANGER_SOFT, fg=theme.TEXT_MUTED,
                               font=theme.font(theme.FS_SUBTITLE), padx=theme.sp(10),
                               cursor="hand2")
        self._close.pack(side="right")
        self._close.bind("<Button-1>", lambda _e: self.hide())
        self._apply_palette(*self._PALETTES["error"])
        self._visible = False

    def _apply_palette(self, accent: str, surface: str, icon: str) -> None:
        self._inner.configure(bg=surface)
        self._accent.configure(bg=accent)
        self._icon.configure(bg=surface, fg=accent, text=icon)
        self._label.configure(bg=surface)
        self._close.configure(bg=surface)

    def _outer_pack(self) -> None:
        if self.winfo_ismapped():
            return
        options = dict(self._pack_options)
        if self._before is not None and self._before.winfo_exists():
            options["before"] = self._before
        try:
            self.pack(**options)
        except (tk.TclError, AttributeError):
            try:
                self.pack(fill="x")
            except tk.TclError:
                pass

    def show(self, message: str, kind: str = "error") -> None:
        accent, surface, icon = self._PALETTES.get(kind, self._PALETTES["error"])
        self._apply_palette(accent, surface, icon)
        self._label.configure(text=str(message))
        sound.play(sound.sound_kind(kind))
        self._outer_pack()
        self._inner.pack(fill="x")
        self._visible = True

    def hide(self) -> None:
        self._inner.pack_forget()
        self._visible = False
        if not self._reserve_space:
            try:
                self.pack_forget()
            except tk.TclError:
                pass


# ---------------------------------------------------------------------------
# 空状态 / 日志
# ---------------------------------------------------------------------------
class EmptyState(tk.Frame):
    def __init__(self, master: tk.Misc, title: str = "", subtitle: str = "", *,
                 glyph: str = "box", bg: str | None = None, **kwargs: Any) -> None:
        self._bg = bg or master_bg(master)
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0, **kwargs)
        self._glyph = glyph
        self._glyph_size = theme.sp(56)
        self._canvas = tk.Canvas(self, bg=self._bg, width=self._glyph_size,
                                 height=self._glyph_size, highlightthickness=0, bd=0)
        self._canvas.pack(pady=(theme.PAD_LG, theme.PAD_SM))
        self._title = tk.Label(self, text=title, bg=self._bg, fg=theme.TEXT,
                               font=theme.bold(theme.FS_SUBTITLE), anchor="center")
        self._title.pack()
        self._subtitle = tk.Label(self, text=subtitle, bg=self._bg, fg=theme.TEXT_DIM,
                                  font=theme.font(theme.FS_BODY), justify="center",
                                  wraplength=theme.sp(420), anchor="center")
        self._subtitle.pack(pady=(theme.sp(6), theme.PAD_LG))
        self._actions = tk.Frame(self, bg=self._bg)
        self._actions.pack()
        self._draw_glyph()

    def set(self, title: str | None = None, subtitle: str | None = None) -> None:
        if title is not None:
            self._title.configure(text=title)
        if subtitle is not None:
            self._subtitle.configure(text=subtitle)

    def set_glyph(self, glyph: str) -> None:
        self._glyph = glyph
        self._draw_glyph()

    def add_action(self, text: str, command: Callable[[], None]) -> Button:
        button = secondary_button(self._actions, text, command)
        button.pack(side="left", padx=theme.PAD_XS)
        return button

    def _draw_glyph(self) -> None:
        canvas = self._canvas
        canvas.delete("all")
        size = self._glyph_size
        color = theme.mix(self._bg, theme.TEXT_DIM, 0.55)
        accent = theme.mix(self._bg, theme.ACCENT, 0.55)
        if self._glyph == "search":
            r = size * 0.30
            cx, cy = size * 0.44, size * 0.44
            canvas.create_oval(cx - r, cy - r, cx + r, cy + r, outline=color,
                               width=theme.sp(2))
            canvas.create_line(cx + r * 0.75, cy + r * 0.75, size * 0.82, size * 0.82,
                               fill=color, width=theme.sp(2), capstyle="round")
        elif self._glyph == "alert":
            canvas.create_oval(size * 0.12, size * 0.12, size * 0.88, size * 0.88,
                               outline=theme.mix(self._bg, theme.DANGER, 0.7),
                               width=theme.sp(2))
            canvas.create_line(size * 0.5, size * 0.28, size * 0.5, size * 0.58,
                               fill=theme.mix(self._bg, theme.DANGER, 0.8),
                               width=theme.sp(3), capstyle="round")
            canvas.create_line(size * 0.5, size * 0.70, size * 0.5, size * 0.72,
                               fill=theme.mix(self._bg, theme.DANGER, 0.8),
                               width=theme.sp(3), capstyle="round")
        else:
            canvas.create_oval(size * 0.1, size * 0.1, size * 0.9, size * 0.9,
                               outline=color, width=theme.sp(2))
            canvas.create_line(size * 0.32, size * 0.5, size * 0.46, size * 0.64,
                               fill=accent, width=theme.sp(3), capstyle="round")
            canvas.create_line(size * 0.46, size * 0.64, size * 0.70, size * 0.36,
                               fill=accent, width=theme.sp(3), capstyle="round")


class LogView(tk.Frame):
    """等宽字体日志视图（只读、可滚动）。"""

    def __init__(self, master: tk.Misc, *, height: int = 6, bg: str = theme.BG_DEEP,
                 fg: str = theme.TEXT_MUTED, max_lines: int = 400, **kwargs: Any) -> None:
        self._max_lines = int(max_lines)
        self._bg = bg
        super().__init__(master, bg=bg, bd=0, highlightthickness=0, **kwargs)
        self.text = tk.Text(self, height=int(height), bg=bg, fg=fg, bd=0,
                            highlightthickness=0, wrap="none",
                            insertwidth=0, cursor="arrow",
                            font=theme.font(theme.FS_SMALL, mono=True),
                            padx=theme.sp(12), pady=theme.sp(10),
                            selectbackground=theme.mix(bg, theme.ACCENT, 0.30),
                            selectforeground=theme.TEXT)
        self.text.pack(side="left", fill="both", expand=True)
        self._bar = ttk.Scrollbar(self, orient="vertical", command=self.text.yview,
                                  style="Dark.Vertical.TScrollbar")
        self._bar.pack(side="right", fill="y")
        self.text.configure(yscrollcommand=self._bar.set)
        self.text.configure(state="disabled", takefocus=0)

    def append(self, line: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", str(line).rstrip("\n") + "\n")
        try:
            lines = int(self.text.index("end-1c").split(".")[0])
            if lines > self._max_lines:
                self.text.delete("1.0", f"{lines - self._max_lines}.0")
        except (tk.TclError, ValueError):
            pass
        self.text.see("end")
        self.text.configure(state="disabled")

    def clear(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")


# ---------------------------------------------------------------------------
# 分段控件
# ---------------------------------------------------------------------------
class _Segment(tk.Canvas):
    def __init__(self, master: tk.Misc, text: str, *, width: int, height: int,
                 bg: str, selected: bool = False, small: bool = False,
                 on_click: Callable | None = None, **kwargs: Any) -> None:
        self._text = str(text)
        self._parent_bg = bg
        self._selected = bool(selected)
        self._small = bool(small)
        self._on_click = on_click
        self._hover = False
        self._enabled = True
        self._radius = theme.RADIUS_SM if not small else theme.RADIUS_XS
        self._font = theme.font(theme.FS_SMALL if small else theme.FS_BODY,
                                "bold" if selected else "normal")
        kwargs.pop("highlightthickness", None)
        super().__init__(master, width=width, height=height, bg=bg,
                         highlightthickness=0, bd=0, takefocus=0, **kwargs)
        self.bind("<Button-1>", lambda _e: self._click())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._font = theme.font(theme.FS_SMALL if self._small else theme.FS_BODY,
                                "bold" if selected else "normal")
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._redraw()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._redraw()

    def _click(self) -> None:
        if self._enabled and callable(self._on_click):
            self._on_click()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 1 else int(self.cget("width"))
        height = self.winfo_height() if self.winfo_height() > 1 else int(self.cget("height"))
        radius = min(self._radius, height // 2)
        if not self._enabled:
            fill, fg, outline = self._parent_bg, theme.TEXT_DIM, ""
        elif self._selected:
            fill, fg, outline = theme.ACCENT, theme.TEXT_ON_PRIMARY, ""
        elif self._hover:
            fill = theme.mix(self._parent_bg, theme.WHITE, 0.07)
            fg, outline = theme.TEXT, ""
        else:
            fill, fg, outline = self._parent_bg, theme.TEXT_MUTED, ""
        theme.rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, radius,
                           fill=fill, outline=outline, width=1)
        self.create_text(width / 2, height / 2 + 0.5, text=self._text, fill=fg,
                         font=self._font, anchor="center")


class SegmentedControl(tk.Frame):
    """胶囊分段控件（分类 Tab）。"""

    def __init__(self, master: tk.Misc, options: list[tuple[str, str]],
                 value: str | None = None, on_change: Callable[[str], None] | None = None,
                 *, bg: str | None = None, height: int = 32, small: bool = False,
                 padx: int = 10, **kwargs: Any) -> None:
        self._bg = bg or master_bg(master)
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0, **kwargs)
        self._items = list(options)
        self._value = value if value is not None else (self._items[0][1] if self._items else "")
        self._on_change = on_change
        self._segments: dict[str, _Segment] = {}
        self._enabled = True
        self._height = theme.sp(height)
        font_tuple = theme.font(theme.FS_SMALL if small else theme.FS_BODY, "bold")
        for index, (label, option_value) in enumerate(self._items):
            width = _measure(label, font_tuple) + theme.sp(padx) * 2
            segment = _Segment(self, label, width=width, height=self._height,
                               bg=self._bg, selected=(option_value == self._value),
                               small=small, on_click=lambda v=option_value: self._select(v))
            segment.pack(side="left", padx=(0 if index == 0 else theme.sp(4), 0))
            self._segments[option_value] = segment

    def _select(self, value: str) -> None:
        if not self._enabled or value == self._value:
            return
        self._value = value
        for option_value, segment in self._segments.items():
            segment.set_selected(option_value == value)
        if callable(self._on_change):
            self._on_change(value)

    def get(self) -> str:
        return self._value

    def set(self, value: str, *, notify: bool = False) -> None:
        if value not in self._segments:
            return
        self._value = value
        for option_value, segment in self._segments.items():
            segment.set_selected(option_value == value)
        if notify and callable(self._on_change):
            self._on_change(value)

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        for segment in self._segments.values():
            segment.set_enabled(self._enabled)


# ---------------------------------------------------------------------------
# 版本列表行
# ---------------------------------------------------------------------------
class VersionRow(tk.Frame):
    """版本列表行：行高 44、hover 白 5%、选中强调淡底 + 左侧 2px 条。"""

    def __init__(self, master: tk.Misc, version_id: str, category: str,
                 date_text: str, *, selected: bool = False,
                 on_click: Callable[[], None] | None = None,
                 bg: str = theme.SURFACE, **kwargs: Any) -> None:
        self._base_bg = bg
        self._category = category
        self._selected = bool(selected)
        self._hover = False
        self._on_click = on_click
        super().__init__(master, bg=bg, height=theme.ROW_HEIGHT, bd=0,
                         highlightthickness=0, **kwargs)
        self.pack_propagate(False)
        self._accent = tk.Frame(self, bg=bg, width=theme.sp(2))
        self._accent.pack(side="left", fill="y", pady=theme.sp(10))
        self._id_label = tk.Label(self, text=str(version_id), bg=bg, fg=theme.TEXT,
                                  font=theme.bold(theme.FS_SUBTITLE), anchor="w")
        self._id_label.pack(side="left", padx=(theme.PAD_MD, theme.PAD_SM))
        self._badge = category_badge(self, category, bg)
        self._badge.pack(side="left", padx=(0, theme.PAD_SM))
        self._date_label = tk.Label(self, text=str(date_text), bg=bg, fg=theme.TEXT_DIM,
                                    font=theme.font(theme.FS_SMALL), anchor="e")
        self._date_label.pack(side="right", padx=(theme.PAD_SM, theme.PAD_LG))
        for widget in (self, self._accent, self._id_label, self._badge, self._date_label):
            widget.bind("<Enter>", self._on_enter)
            widget.bind("<Leave>", self._on_leave)
            widget.bind("<Button-1>", self._on_press)
        self._apply_state()

    def invoke(self) -> None:
        if callable(self._on_click):
            self._on_click()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._apply_state()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.configure(cursor="hand2")
        self._apply_state()

    def _on_leave(self, event=None) -> None:
        try:
            x, y = self.winfo_pointerxy()
            under = self.winfo_containing(x, y)
            if under is not None:
                path = str(under)
                if path == str(self) or path.startswith(str(self) + "."):
                    return
        except (tk.TclError, KeyError):
            pass
        self._hover = False
        self._apply_state()

    def _on_press(self, _event=None) -> None:
        self.invoke()

    def _apply_state(self) -> None:
        if self._selected:
            background = theme.mix(self._base_bg, theme.ACCENT, 0.12)
            accent = theme.ACCENT
        elif self._hover:
            background = theme.mix(self._base_bg, theme.WHITE, 0.05)
            accent = self._base_bg
        else:
            background = self._base_bg
            accent = self._base_bg
        self.configure(bg=background)
        for widget in (self._accent, self._id_label, self._date_label):
            widget.configure(bg=background)
        self._accent.configure(bg=accent)
        color = theme.CATEGORY_COLORS.get(self._category, theme.TEXT_MUTED)
        self._badge.configure(bg=theme.mix(background, color, 0.14))


# ---------------------------------------------------------------------------
# 加载器选择卡片
# ---------------------------------------------------------------------------
class ChoiceCard(tk.Canvas):
    """加载器选择卡片（圆角 12、高 72，选中强调描边 + 淡强调底）。"""

    def __init__(self, master: tk.Misc, title: str, subtitle: str = "", *,
                 tag: str | None = None, tag_color: str = theme.ACCENT_SOFT,
                 tag_fg: str = theme.ACCENT, selected: bool = False,
                 on_click: Callable[[], None] | None = None,
                 bg: str = theme.SURFACE, **kwargs: Any) -> None:
        self._title = str(title)
        self._subtitle = str(subtitle)
        self._tag = tag
        self._tag_color = tag_color
        self._tag_fg = tag_fg
        self._selected = bool(selected)
        self._hover = False
        self._on_click = on_click
        self._base_bg = bg
        self._enabled = True
        self._font_title = theme.bold(theme.FS_SUBTITLE)
        self._font_sub = theme.font(theme.FS_SMALL)
        self._font_tag = theme.font(theme.FS_TINY, "bold")
        kwargs.pop("highlightthickness", None)
        super().__init__(master, bg=bg, height=theme.LOADER_CARD_HEIGHT,
                         highlightthickness=0, bd=0, takefocus=0,
                         width=theme.sp(150), **kwargs)
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Button-1>", self._on_press)
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self._redraw()

    def invoke(self) -> None:
        if callable(self._on_click):
            self._on_click()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._redraw()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._redraw()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self.configure(cursor="hand2")
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        if self._enabled:
            self.invoke()

    def _redraw(self) -> None:
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 2 else theme.sp(150)
        height = self.winfo_height() if self.winfo_height() > 2 else theme.LOADER_CARD_HEIGHT
        radius = theme.RADIUS_MD
        pad = theme.sp(12)
        if not self._enabled:
            fill, outline = theme.SURFACE_ALT, theme.BORDER
            title_fg = sub_fg = theme.TEXT_DIM
        elif self._selected:
            fill = theme.mix(self._base_bg, theme.ACCENT, 0.10)
            outline = theme.ACCENT
            title_fg, sub_fg = theme.TEXT, theme.TEXT_MUTED
        elif self._hover:
            fill = theme.mix(self._base_bg, theme.WHITE, 0.04)
            outline = theme.BORDER_LIGHT
            title_fg, sub_fg = theme.TEXT, theme.TEXT_MUTED
        else:
            fill, outline = theme.SURFACE_ALT, theme.BORDER
            title_fg, sub_fg = theme.TEXT, theme.TEXT_MUTED
        theme.rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5, radius,
                           fill=fill, outline=outline, width=1)
        dot_r = theme.sp(4)
        dot_x = pad + dot_r
        dot_y = height / 2 - theme.sp(8)
        if self._selected:
            self.create_oval(dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r,
                             fill=theme.ACCENT, outline="")
            self.create_oval(dot_x - dot_r * 0.34, dot_y - dot_r * 0.34,
                             dot_x + dot_r * 0.34, dot_y + dot_r * 0.34,
                             fill=theme.WHITE, outline="")
        else:
            self.create_oval(dot_x - dot_r, dot_y - dot_r, dot_x + dot_r, dot_y + dot_r,
                             outline=theme.TEXT_DIM, width=max(1, theme.sp(1)))
        text_x = dot_x + dot_r + theme.sp(8)
        self.create_text(text_x, dot_y, text=self._title, fill=title_fg,
                         font=self._font_title, anchor="w")
        if self._tag:
            tag_w = _measure(self._tag, self._font_tag) + theme.sp(12)
            tag_x2 = width - pad
            tag_x1 = tag_x2 - tag_w
            tag_y1 = dot_y - theme.sp(8)
            tag_y2 = dot_y + theme.sp(8)
            theme.rounded_rect(self, tag_x1, tag_y1, tag_x2, tag_y2,
                               theme.RADIUS_XS, fill=self._tag_color, outline="")
            self.create_text((tag_x1 + tag_x2) / 2, (tag_y1 + tag_y2) / 2,
                             text=self._tag, fill=self._tag_fg, font=self._font_tag)
        if self._subtitle:
            self.create_text(text_x, height / 2 + theme.sp(12), text=self._subtitle,
                             fill=sub_fg, font=self._font_sub, anchor="w")


# ---------------------------------------------------------------------------
# 页头
# ---------------------------------------------------------------------------
class PageHeader(tk.Frame):
    def __init__(self, master: tk.Misc, title: str, subtitle: str | None = None, *,
                 bg: str | None = None, **kwargs: Any) -> None:
        self._bg = bg or master_bg(master)
        super().__init__(master, bg=self._bg, bd=0, highlightthickness=0, **kwargs)
        self._title = tk.Label(self, text=title, bg=self._bg, fg=theme.TEXT,
                               font=theme.bold(theme.FS_HEADING), anchor="w")
        self._title.pack(anchor="w")
        self._subtitle: tk.Label | None = None
        if subtitle is not None:
            self._subtitle = tk.Label(self, text=subtitle, bg=self._bg, fg=theme.TEXT_MUTED,
                                      font=theme.font(theme.FS_BODY), anchor="w")
            self._subtitle.pack(anchor="w", pady=(theme.sp(4), 0))

    def set_title(self, title: str) -> None:
        self._title.configure(text=title)

    def set_subtitle(self, subtitle: str) -> None:
        if self._subtitle is None:
            self._subtitle = tk.Label(self, text=subtitle, bg=self._bg, fg=theme.TEXT_MUTED,
                                      font=theme.font(theme.FS_BODY), anchor="w")
            self._subtitle.pack(anchor="w", pady=(theme.sp(4), 0))
        else:
            self._subtitle.configure(text=subtitle)


# ---------------------------------------------------------------------------
# 自绘图标
# ---------------------------------------------------------------------------
def draw_cube_logo(canvas: tk.Canvas, size: int, *, surface: str = theme.ACCENT,
                   fg: str = "#ffffff") -> None:
    """圆角渐变方块 + 白色线框立方体（现代 logo）。"""
    try:
        width = canvas.winfo_width()
        height = canvas.winfo_height()
    except tk.TclError:
        width = height = 0
    if width <= 2 or height <= 2:
        width = height = size
    x1 = (width - size) / 2
    y1 = (height - size) / 2
    radius = size * 0.26
    canvas.delete("logo")
    theme.draw_linear_gradient(canvas, x1, y1, x1 + size, y1 + size, radius,
                               surface, theme.ACCENT_2, bands=14, tags=("logo",))
    cx, cy = x1 + size / 2, y1 + size * 0.52
    unit = size * 0.20
    top = (cx, cy - unit * 1.15)
    left = (cx - unit, cy - unit * 0.30)
    right = (cx + unit, cy - unit * 0.30)
    bottom = (cx, cy + unit * 0.55)
    line = max(1, int(round(size * 0.035)))
    for p1, p2 in ((top, left), (top, right), (left, bottom), (right, bottom),
                   (left, right), (bottom, top)):
        canvas.create_line(*p1, *p2, fill=fg, width=line, capstyle="round",
                           joinstyle="round", tags=("logo",))


def draw_state_icon(canvas: tk.Canvas, size: int, state: str = "success") -> None:
    """完成 / 失败 / 取消状态图标（圆环 + 符号）。"""
    colors = {
        "success": theme.SUCCESS,
        "error": theme.DANGER,
        "cancelled": theme.WARNING,
    }
    color = colors.get(state, theme.SUCCESS)
    canvas.delete("all")
    width = max(canvas.winfo_width(), size)
    height = max(canvas.winfo_height(), size)
    cx, cy = width / 2, height / 2
    r = size / 2 - max(1, size * 0.03)
    soft = theme.mix(theme.BG, color, 0.16)
    canvas.create_oval(cx - r, cy - r, cx + r, cy + r, fill=soft, outline=color,
                       width=max(2, int(round(size * 0.035))))
    if state == "success":
        canvas.create_line(cx - r * 0.42, cy + r * 0.02, cx - r * 0.10, cy + r * 0.34,
                           cx + r * 0.46, cy - r * 0.32,
                           fill=color, width=max(2, int(round(size * 0.07))),
                           capstyle="round", joinstyle="round")
    elif state == "error":
        canvas.create_line(cx - r * 0.32, cy - r * 0.32, cx + r * 0.32, cy + r * 0.32,
                           fill=color, width=max(2, int(round(size * 0.07))),
                           capstyle="round")
        canvas.create_line(cx + r * 0.32, cy - r * 0.32, cx - r * 0.32, cy + r * 0.32,
                           fill=color, width=max(2, int(round(size * 0.07))),
                           capstyle="round")
    else:  # cancelled
        canvas.create_line(cx - r * 0.34, cy, cx + r * 0.34, cy,
                           fill=color, width=max(2, int(round(size * 0.07))),
                           capstyle="round")
        canvas.create_line(cx, cy - r * 0.34, cx, cy + r * 0.34,
                           fill=color, width=max(2, int(round(size * 0.07))),
                           capstyle="round")
