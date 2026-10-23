"""完成页：安装成功 / 失败 / 取消 / 空闲四态（深色现代极简版）。"""
from __future__ import annotations

import logging
import threading
import tkinter as tk

from ...core import util
from .. import sound, theme
from ..widgets import (
    Card,
    NoticeBar,
    draw_state_icon,
    ghost_button,
    primary_button,
    secondary_button,
)
from .base import BasePage

log = logging.getLogger("cubefetch.ui.done")

KIND_LABELS = {
    "vanilla": "原版（无加载器）",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}

_CARD_WIDTH = 540


def _format_elapsed(seconds) -> str:
    try:
        total = float(seconds or 0)
    except (TypeError, ValueError):
        total = 0.0
    if total < 60:
        return f"{total:.0f} 秒"
    minutes, secs = divmod(int(total), 60)
    if minutes < 60:
        return f"{minutes} 分 {secs:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


class DonePage(BasePage):
    title = "完成"
    nav_key = "done"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> None:
        self._params: dict = {}

        holder = tk.Frame(self, bg=theme.BG)
        holder.pack(fill="both", expand=True)
        for column in (0, 2):
            holder.grid_columnconfigure(column, weight=1)
        for row in (0, 2):
            holder.grid_rowconfigure(row, weight=1)

        center = tk.Frame(holder, bg=theme.BG)
        center.grid(row=1, column=1)

        self._card = Card(center, padding=theme.PAD_XXL, bg=theme.SURFACE, border=theme.BORDER)
        self._card.pack()

        self.notice = NoticeBar(center, reserve_space=False, before=self._card, bg=theme.BG)
        self.notice.hide()

        body = self._card.body
        body.configure(bg=theme.SURFACE)

        self._icon = tk.Canvas(body, width=theme.sp(72), height=theme.sp(72),
                               bg=theme.SURFACE, highlightthickness=0, bd=0)
        self._icon.pack(pady=(0, theme.PAD_MD))

        self._title_label = tk.Label(body, text="", bg=theme.SURFACE, fg=theme.TEXT,
                                     font=theme.bold(24))
        self._title_label.pack()

        self._subtitle_label = tk.Label(
            body, text="", bg=theme.SURFACE, fg=theme.TEXT_MUTED,
            font=theme.font(theme.FS_BODY), justify="center",
            wraplength=theme.sp(440),
        )
        self._subtitle_label.pack(pady=(theme.PAD_XS, theme.PAD_LG))

        # 二级浮层：元信息内嵌块（不用再加一圈描边）
        self._details_card = tk.Frame(body, bg=theme.SURFACE_ALT, bd=0, highlightthickness=0)
        self._details_card.pack(fill="x")
        self._details = tk.Frame(self._details_card, bg=theme.SURFACE_ALT)
        self._details.pack(fill="x", padx=theme.PAD_LG, pady=theme.PAD_MD)
        self._details.grid_columnconfigure(0, minsize=theme.sp(88))
        self._details.grid_columnconfigure(1, weight=1)

        self._buttons = tk.Frame(body, bg=theme.SURFACE)
        self._buttons.pack(fill="x", pady=(theme.PAD_LG, 0))

        self._show_idle()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_show(self, **kwargs) -> None:
        self._params = dict(kwargs)
        if kwargs.get("success"):
            self._show_success(kwargs)
        elif kwargs.get("cancelled"):
            self._show_cancelled(kwargs)
        elif kwargs.get("error"):
            self._show_failure(kwargs)
        else:
            self._show_idle()

    # ------------------------------------------------------------------
    # 状态渲染
    # ------------------------------------------------------------------
    def _clear_details(self) -> None:
        for child in list(self._details.winfo_children()):
            child.destroy()

    def _clear_buttons(self) -> None:
        for child in list(self._buttons.winfo_children()):
            child.destroy()

    def _detail_row(self, row: int, caption: str, value: str, *, mono: bool = False) -> None:
        tk.Label(self._details, text=caption, bg=theme.SURFACE_ALT, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_SMALL)).grid(row=row, column=0, sticky="w",
                                                      pady=theme.sp(3))
        label = tk.Label(
            self._details, text=value or "—", bg=theme.SURFACE_ALT, fg=theme.TEXT,
            font=theme.font(theme.FS_BODY, mono=mono), anchor="w", justify="left",
            wraplength=theme.sp(340),
        )
        label.grid(row=row, column=1, sticky="w", padx=(theme.PAD_SM, 0), pady=theme.sp(3))

    def _mc_root(self) -> str:
        config = self.config
        path = getattr(config, "mc_root_path", None)
        if path is not None:
            try:
                return str(path)
            except (OSError, TypeError):
                pass
        return str(getattr(config, "mc_root", "") or "未设置")

    @staticmethod
    def _version_id(params: dict) -> str:
        """从 VersionEntry / 字符串里取出游戏版本 id。"""
        version = params.get("version")
        if version is None:
            return "未知"
        identifier = getattr(version, "id", None)
        return str(identifier) if identifier else str(version)

    def _version_line(self, params: dict) -> str:
        version_id = self._version_id(params)
        kind = str(params.get("kind") or "vanilla")
        loader_version = params.get("loader_version")
        label = KIND_LABELS.get(kind, kind)
        if kind == "vanilla" or not loader_version:
            return f"Minecraft {version_id}"
        return f"Minecraft {version_id} · {label} {loader_version}"

    def _set_title(self, text: str, color: str) -> None:
        self._title_label.configure(text=text, fg=color)

    def _show_success(self, params: dict) -> None:
        self._status = "success"
        sound.play("success")
        self.notice.hide()
        self._icon.delete("all")
        draw_state_icon(self._icon, theme.sp(72), "success")
        self._set_title("安装完成！", theme.SUCCESS)
        self._subtitle_label.configure(
            text=f"{self._version_line(params)} 已安装完成，可以直接在启动器中看到它。"
        )
        self._clear_details()
        kind = str(params.get("kind") or "vanilla")
        loader_version = params.get("loader_version")
        loader_text = KIND_LABELS.get(kind, kind)
        if kind != "vanilla" and loader_version:
            loader_text = f"{loader_text} {loader_version}"
        self._detail_row(0, "游戏版本", self._version_id(params))
        self._detail_row(1, "加载器", loader_text)
        self._detail_row(2, "配置名", str(params.get("profile_id") or "—"), mono=True)
        self._detail_row(3, "安装位置", self._mc_root(), mono=True)
        self._detail_row(4, "耗时", _format_elapsed(params.get("elapsed")))
        self._clear_buttons()
        primary_button(self._buttons, "完成", self._finish, width=theme.sp(110),
                       bg=theme.SURFACE).pack(side="right")
        secondary_button(self._buttons, "再来一个", self._again, width=theme.sp(130),
                         bg=theme.SURFACE).pack(side="right", padx=(0, theme.PAD_SM))
        secondary_button(self._buttons, "打开文件夹", self._open_folder, width=theme.sp(130),
                         bg=theme.SURFACE).pack(side="right", padx=(0, theme.PAD_SM))
        self.set_status(f"安装完成：{self._version_id(params)}", kind="success")

    def _show_failure(self, params: dict) -> None:
        self._status = "failure"
        sound.play("error")
        self._icon.delete("all")
        draw_state_icon(self._icon, theme.sp(72), "error")
        self._set_title("安装失败", theme.DANGER)
        self._subtitle_label.configure(
            text="安装过程中遇到问题，已下载的文件会保留，重试时会自动补齐。"
        )
        error = str(params.get("error") or "未知错误")
        self.notice.show(error, "error")
        self._clear_details()
        self._detail_row(0, "游戏版本", self._version_id(params))
        kind = str(params.get("kind") or "vanilla")
        self._detail_row(1, "加载器", KIND_LABELS.get(kind, kind))
        self._detail_row(2, "建议", "检查网络后点击“重试”；仍失败可更换下载源或减少线程数。")
        self._clear_buttons()
        primary_button(self._buttons, "重试", self._retry, width=theme.sp(120),
                       bg=theme.SURFACE).pack(side="right")
        secondary_button(self._buttons, "返回下载页", self._back_download, width=theme.sp(140),
                         bg=theme.SURFACE).pack(side="right", padx=(0, theme.PAD_SM))
        self.set_status("安装失败", kind="error")

    def _show_cancelled(self, params: dict) -> None:
        self._status = "cancelled"
        sound.play("warn")
        self._icon.delete("all")
        draw_state_icon(self._icon, theme.sp(72), "cancelled")
        self._set_title("安装已取消", theme.WARNING)
        self._subtitle_label.configure(text="安装已停止，已下载的文件会保留在缓存中。")
        self.notice.hide()
        self._clear_details()
        self._detail_row(0, "游戏版本", self._version_id(params))
        self._clear_buttons()
        primary_button(self._buttons, "重新开始", self._again, width=theme.sp(130),
                       bg=theme.SURFACE).pack(side="right")
        secondary_button(self._buttons, "返回下载页", self._back_download, width=theme.sp(140),
                         bg=theme.SURFACE).pack(side="right", padx=(0, theme.PAD_SM))
        self.set_status("安装已取消", kind="warn")

    def _show_idle(self) -> None:
        self._status = "idle"
        self.notice.hide()
        self._icon.delete("all")
        draw_state_icon(self._icon, theme.sp(72), "cancelled")
        self._set_title("暂无安装结果", theme.TEXT)
        self._subtitle_label.configure(text="请先在下载页选择游戏版本与加载器，然后开始安装。")
        self._clear_details()
        self._detail_row(0, "提示", "下载完成后这里会显示安装结果与安装路径。")
        self._clear_buttons()
        ghost_button(self._buttons, "去下载页", self._back_download, width=theme.sp(150),
                     bg=theme.SURFACE).pack(side="right")

    # ------------------------------------------------------------------
    # 按钮回调
    # ------------------------------------------------------------------
    def _params_for_retry(self) -> dict:
        return {
            "version": self._params.get("version"),
            "kind": self._params.get("kind"),
            "loader_version": self._params.get("loader_version"),
            "download_java": self._params.get("download_java", False),
            "custom_version_name": self._params.get("custom_version_name"),
        }

    def _retry(self) -> None:
        self.app.show_page("progress", **self._params_for_retry())  # type: ignore[attr-defined]

    def _again(self) -> None:
        self.app.show_page("download")  # type: ignore[attr-defined]

    def _finish(self) -> None:
        self.app.show_page("download")  # type: ignore[attr-defined]

    def _back_download(self) -> None:
        self.app.show_page("download")  # type: ignore[attr-defined]

    def _open_folder(self) -> None:
        target = self._mc_root()
        opener = getattr(self.services, "open_folder", None)
        if not callable(opener):
            self.set_status("无法打开文件夹（后端未就绪）", kind="warn")
            return
        self.set_status("正在打开安装目录…")

        def worker() -> None:
            try:
                opener(target)
            except Exception as exc:  # noqa: BLE001
                log.warning("打开目录失败: %s", target, exc_info=True)
                self.on_ui(lambda: self.notice.show(f"打开目录失败：{exc}", "error"))
            else:
                self.on_ui(lambda: self.set_status("已打开安装目录", kind="success"))

        threading.Thread(target=worker, daemon=True, name="cubefetch-open-folder").start()


__all__ = ["DonePage"]
