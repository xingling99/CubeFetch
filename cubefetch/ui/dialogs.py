"""自绘现代弹窗（替换系统 messagebox / simpledialog 的默认灰色外观）。

保留与旧实现一致的公开 API：

    ask_yes_no(title, message, *, icon="warning", sound_kind="warn", parent=None) -> bool
    ask_string(title, prompt, *, initialvalue="", sound_kind="info", parent=None) -> str | None
    show_info(title, message, *, parent=None) -> None
    show_warning(title, message, *, parent=None) -> None
    show_error(title, message, *, parent=None) -> None

弹窗为模态 Toplevel：深色圆角卡片 + 标题 + 正文 + 右侧按钮行，
Enter 确认、Esc 取消；调用前播放对应提示音（播放失败静默）。
本模块只在 UI 线程调用，不做阻塞式 I/O。
"""
from __future__ import annotations

import tkinter as tk
from typing import Any

from . import sound, theme
from .widgets import Card, primary_button, secondary_button

__all__ = [
    "ask_yes_no",
    "ask_string",
    "show_info",
    "show_warning",
    "show_error",
]

_DEFAULT_WIDTH = 460
_DEFAULT_PADDING = theme.PAD_XL


def _resolve_parent(parent: Any = None) -> tk.Misc | None:
    """找到弹窗的父窗口（默认当前聚焦窗口或根窗口）。"""
    if parent is not None:
        try:
            return parent.winfo_toplevel() if hasattr(parent, "winfo_toplevel") else parent
        except tk.TclError:  # pragma: no cover - 控件已销毁
            return None
    try:
        root = tk._get_default_root()  # noqa: SLF001 - tkinter 没有公开 API
    except Exception:  # noqa: BLE001
        root = None
    if root is None:  # pragma: no cover - 没有根窗口时的兜底
        return None
    try:
        focused = root.focus_get()
    except (tk.TclError, KeyError):
        focused = None
    if focused is not None and hasattr(focused, "winfo_toplevel"):
        try:
            return focused.winfo_toplevel()
        except tk.TclError:  # pragma: no cover
            return None
    return root


class _ModernDialog(tk.Toplevel):
    """自绘模态弹窗基类。"""

    def __init__(self, parent: tk.Misc, title: str, *,
                 width: int = _DEFAULT_WIDTH) -> None:
        super().__init__(parent)
        self.withdraw()
        self._result: Any = None
        self.title(title)
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        try:
            self.transient(parent)
        except tk.TclError:  # pragma: no cover
            pass
        self._card = Card(self, bg=theme.SURFACE, border=theme.BORDER_LIGHT,
                          padding=_DEFAULT_PADDING)
        self._card.pack(fill="both", expand=True, padx=theme.PAD_SM, pady=theme.PAD_SM)
        self._content = tk.Frame(self._card.body, bg=theme.SURFACE)
        self._content.pack(fill="both", expand=True)
        self._width = theme.sp(width)
        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self.bind("<Escape>", lambda _e: self._cancel())

    # -- 结构 ------------------------------------------------------------
    def _add_header(self, text: str) -> None:
        header = tk.Frame(self._content, bg=theme.SURFACE)
        header.pack(fill="x")
        label = tk.Label(header, text=text, bg=theme.SURFACE, fg=theme.TEXT,
                         font=theme.bold(theme.FS_TITLE), anchor="w", justify="left")
        label.pack(side="left", anchor="w")
        close = tk.Label(header, text="×", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                         font=theme.font(theme.FS_HEADING), cursor="hand2")
        close.pack(side="right", anchor="n")
        close.bind("<Button-1>", lambda _e: self._cancel())

    def _add_message(self, message: str) -> None:
        label = tk.Label(self._content, text=message, bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                         font=theme.font(theme.FS_BODY), anchor="w", justify="left",
                         wraplength=self._width - theme.PAD_XXL)
        label.pack(fill="x", pady=(theme.PAD_SM, theme.PAD_LG))

    def _button_row(self) -> tk.Frame:
        row = tk.Frame(self._content, bg=theme.SURFACE)
        row.pack(fill="x", side="bottom")
        return row

    # -- 显示 / 关闭 ------------------------------------------------------
    def _show(self, focus_widget: tk.Misc | None = None) -> None:
        self.update_idletasks()
        parent = self.master
        width = max(self.winfo_reqwidth(), self._width)
        height = self.winfo_reqheight()
        try:
            px = parent.winfo_rootx() + max(0, (parent.winfo_width() - width) // 2)
            py = parent.winfo_rooty() + max(0, (parent.winfo_height() - height) // 2)
        except tk.TclError:  # pragma: no cover
            px = (self.winfo_screenwidth() - width) // 2
            py = (self.winfo_screenheight() - height) // 2
        self.geometry(f"{width}x{height}+{px}+{py}")
        self.deiconify()
        self.lift()
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass
        if focus_widget is not None:
            try:
                focus_widget.focus_set()
            except tk.TclError:  # pragma: no cover
                pass
        try:
            self.wait_window(self)
        except tk.TclError:  # pragma: no cover
            pass

    def _confirm(self) -> None:
        self._result = True
        self._close()

    def _cancel(self) -> None:
        if self._result is None:
            self._result = False
        self._close()

    def _close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------
def ask_yes_no(
    title: str,
    message: str,
    *,
    icon: str = "warning",
    sound_kind: str = "warn",
    parent: Any = None,
) -> bool:
    """是/否确认框，返回用户是否点了“是”。"""
    sound.play(sound_kind)
    resolved = _resolve_parent(parent)
    if resolved is None:  # pragma: no cover - 无根窗口
        return False
    dialog = _ModernDialog(resolved, title)
    dialog._add_header(title)
    dialog._add_message(message)
    row = dialog._button_row()

    def _no() -> None:
        dialog._result = False
        dialog._close()

    def _yes() -> None:
        dialog._result = True
        dialog._close()

    secondary_button(row, "取消", _no, width=theme.sp(96)).pack(side="right", padx=(theme.PAD_SM, 0))
    confirm = primary_button(row, "确定", _yes, width=theme.sp(112),
                             kind="primary")
    confirm.pack(side="right")
    dialog.bind("<Return>", lambda _e: _yes())
    dialog._show(confirm)
    return bool(dialog._result)


def ask_string(
    title: str,
    prompt: str,
    *,
    initialvalue: str = "",
    sound_kind: str = "info",
    parent: Any = None,
) -> str | None:
    """文本输入框；用户取消时返回 None（调用方据此中止流程）。"""
    sound.play(sound_kind)
    resolved = _resolve_parent(parent)
    if resolved is None:  # pragma: no cover - 无根窗口
        return None
    dialog = _ModernDialog(resolved, title)
    dialog._add_header(title)
    message = tk.Label(dialog._content, text=prompt, bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                       font=theme.font(theme.FS_BODY), anchor="w", justify="left",
                       wraplength=dialog._width - theme.PAD_XXL)
    message.pack(fill="x", pady=(theme.PAD_SM, theme.PAD_SM))
    value_var = tk.StringVar(master=dialog, value=str(initialvalue or ""))
    entry = tk.Entry(dialog._content, textvariable=value_var, style="Dark.TEntry",
                     font=theme.font(theme.FS_BODY), bg=theme.BG_INPUT,
                     fg=theme.TEXT, insertbackground=theme.TEXT)
    entry.pack(fill="x", ipady=theme.sp(4), pady=(0, theme.PAD_LG))
    row = dialog._button_row()

    def _cancel() -> None:
        dialog._result = None
        dialog._close()

    def _confirm() -> None:
        dialog._result = value_var.get()
        dialog._close()

    secondary_button(row, "取消", _cancel, width=theme.sp(96)).pack(side="right", padx=(theme.PAD_SM, 0))
    primary_button(row, "确定", _confirm, width=theme.sp(112)).pack(side="right")
    dialog.protocol("WM_DELETE_WINDOW", _cancel)
    dialog.bind("<Escape>", lambda _e: _cancel())
    dialog.bind("<Return>", lambda _e: _confirm())
    entry.bind("<Return>", lambda _e: _confirm())
    dialog._show(entry)
    result = dialog._result
    return None if result is None else str(result)


def _show_message(title: str, message: str, *, kind: str, parent: Any = None) -> None:
    sound.play(kind)
    resolved = _resolve_parent(parent)
    if resolved is None:  # pragma: no cover - 无根窗口
        return
    dialog = _ModernDialog(resolved, title)
    dialog._add_header(title)
    dialog._add_message(message)
    row = dialog._button_row()

    def _ok() -> None:
        dialog._result = True
        dialog._close()

    primary_button(row, "确定", _ok, width=theme.sp(112)).pack(side="right")
    dialog.bind("<Return>", lambda _e: _ok())
    dialog._show(None)


def show_info(title: str, message: str, *, parent: Any = None) -> None:
    _show_message(title, message, kind="info", parent=parent)


def show_warning(title: str, message: str, *, parent: Any = None) -> None:
    _show_message(title, message, kind="warn", parent=parent)


def show_error(title: str, message: str, *, parent: Any = None) -> None:
    _show_message(title, message, kind="error", parent=parent)
