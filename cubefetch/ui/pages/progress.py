"""安装进度页：大号百分比、渐变进度条、统计、当前文件、可折叠日志（深色现代极简版）。

安装在工作线程执行：
    services.install(kind, loader_version, game_version,
                     progress_cb=..., cancel=threading.Event()) -> profile_id
"""
from __future__ import annotations

import logging
import time
import tkinter as tk
from typing import Any

from ...core.util import human_size
from .. import theme
from ..widgets import (
    Button,
    Card,
    LogView,
    NoticeBar,
    PageHeader,
    ProgressBar,
    Spinner,
    ghost_button,
    truncate,
)
from .base import BasePage, error_detail, error_message

log = logging.getLogger("cubefetch.ui.progress")

KIND_LABELS = {
    "vanilla": "原版",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}


def _format_elapsed(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes} 分 {sec:02d} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes:02d} 分"


class ProgressPage(BasePage):
    title = "进度"
    nav_key = "progress"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> None:
        self._params: dict[str, Any] = {}
        self._start_time = 0.0
        self._tick_after: str | None = None
        self._cancelled = False
        self._last_message = ""
        self._log_expanded = True

        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_LG, 0))
        self.page_header = PageHeader(header, "正在安装", "等待开始…")
        self.page_header.pack(side="left", fill="x", expand=True)

        self.notice = NoticeBar(self)
        self.notice.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_MD, 0))
        self.notice.hide()

        # ---------------- 进度卡片 ----------------
        card = Card(self, padding=theme.PAD_XL)
        card.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_MD, theme.PAD_MD))
        body = card.body

        top = tk.Frame(body, bg=theme.SURFACE)
        top.pack(fill="x")

        stage_box = tk.Frame(top, bg=theme.SURFACE)
        stage_box.pack(side="left", fill="x", expand=True)
        self._spinner = Spinner(stage_box, size=18, bg=theme.SURFACE)
        self._spinner.pack(side="left", padx=(0, theme.PAD_SM))
        stage_col = tk.Frame(stage_box, bg=theme.SURFACE)
        stage_col.pack(side="left", fill="x", expand=True)
        self._stage_label = tk.Label(
            stage_col, text="等待开始", bg=theme.SURFACE, fg=theme.TEXT,
            font=theme.font(theme.FS_HEADING, "bold"), anchor="w",
        )
        self._stage_label.pack(fill="x")

        # 大号百分比：48px ≈ 36pt（DPI 由 tk scaling 统一处理）
        self._percent_label = tk.Label(
            top, text="0%", bg=theme.SURFACE, fg=theme.TEXT,
            font=theme.font(36, slant="roman"), width=5, anchor="e",
        )
        self._percent_label.pack(side="right", padx=(theme.PAD_MD, 0))

        self._bar = ProgressBar(body, height=6, radius=3)
        self._bar.pack(fill="x", pady=(theme.PAD_LG, theme.PAD_LG))

        # 统计内嵌块（二级浮层，不加额外描边）
        stats = tk.Frame(body, bg=theme.SURFACE_ALT)
        stats.pack(fill="x")
        for column, weight in enumerate((1, 2, 1, 1, 1)):
            stats.grid_columnconfigure(column, weight=weight)
        self._speed_label = self._stat_cell(stats, 0, "速度")
        self._size_label = self._stat_cell(stats, 1, "已下载 / 总大小")
        self._count_label = self._stat_cell(stats, 2, "文件进度")
        self._stage_inline_label = self._stat_cell(stats, 3, "本阶段")
        self._elapsed_label = self._stat_cell(stats, 4, "耗时")
        for column in range(5):
            stats.grid_columnconfigure(column, uniform="stats")

        # 阶段内进度已并入上方统计列（“本阶段”），该标签只保留给自测断言，不再重复显示。
        self._stage_count_label = tk.Label(
            body, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
            font=theme.font(theme.FS_SMALL), anchor="w",
        )

        self._file_label = tk.Label(
            body, text="当前文件：—", bg=theme.SURFACE, fg=theme.TEXT_MUTED,
            font=theme.font(theme.FS_SMALL, mono=True), anchor="w", justify="left",
        )
        self._file_label.pack(fill="x", pady=(theme.PAD_SM, 0))

        actions = tk.Frame(body, bg=theme.SURFACE)
        actions.pack(fill="x", pady=(theme.PAD_MD, 0))
        self._hint_label = tk.Label(
            actions, text="安装过程中请保持网络连接。", bg=theme.SURFACE,
            fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL), anchor="w",
        )
        self._hint_label.pack(side="left")
        self._cancel_button = Button(
            actions, "取消安装", self.cancel_install, kind="danger_soft",
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )
        self._cancel_button.pack(side="right")
        self._cancel_button.set_enabled(False)
        self._back_button = ghost_button(
            actions, "返回下载页", self._back_to_download,
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )

        # ---------------- 可折叠日志卡片 ----------------
        log_card = Card(self, padding=theme.PAD_MD)
        log_card.pack(fill="both", expand=True, padx=theme.CONTENT_PADDING,
                      pady=(0, theme.PAD_LG))
        log_head = tk.Frame(log_card.body, bg=theme.SURFACE)
        log_head.pack(fill="x")
        tk.Label(log_head, text="安装日志", bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                 font=theme.bold(theme.FS_SMALL), anchor="w").pack(side="left")
        self._log_toggle = ghost_button(
            log_head, "收起", self._toggle_log, height=theme.BUTTON_HEIGHT_SM,
            bg=theme.SURFACE,
        )
        self._log_toggle.pack(side="right")
        self._log_holder = tk.Frame(log_card.body, bg=theme.SURFACE)
        self._log_holder.pack(fill="both", expand=True, pady=(theme.PAD_SM, 0))
        self._log = LogView(self._log_holder, height=6, bg=theme.BG_DEEP, fg=theme.TEXT_MUTED)
        self._log.pack(fill="both", expand=True)

        self._set_idle()

    def _stat_cell(self, parent: tk.Frame, column: int, caption: str) -> tk.Label:
        cell = tk.Frame(parent, bg=theme.SURFACE_ALT)
        cell.grid(row=0, column=column, sticky="nsew",
                  padx=(theme.PAD_MD, 0) if column else (theme.PAD_MD, 0),
                  pady=theme.PAD_SM)
        tk.Label(cell, text=caption, bg=theme.SURFACE_ALT, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_TINY), anchor="w").pack(fill="x")
        value = tk.Label(cell, text="—", bg=theme.SURFACE_ALT, fg=theme.TEXT,
                         font=theme.font(theme.FS_BODY, "bold"), anchor="w")
        value.pack(fill="x")
        return value

    def _toggle_log(self) -> None:
        self._log_expanded = not self._log_expanded
        if self._log_expanded:
            self._log_holder.pack(fill="both", expand=True, pady=(theme.PAD_SM, 0))
            self._log_toggle.set_text("收起")
        else:
            self._log_holder.pack_forget()
            self._log_toggle.set_text("展开")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_show(self, **kwargs) -> None:
        self.notice.hide()
        version = kwargs.get("version") or getattr(self.app, "selected_version", None)
        kind = str(kwargs.get("kind") or getattr(self.app, "selected_kind", None) or "vanilla")
        loader_version = kwargs.get(
            "loader_version", getattr(self.app, "selected_loader_version", None)
        )
        if version is None:
            self._set_idle()
            self.page_header.set_subtitle("尚未选择版本")
            self.set_status("尚未开始安装")
            return
        auto_start = bool(kwargs.get("auto_start", True))
        download_java = bool(kwargs.get("download_java", False))
        self._params = {
            "version": version,
            "kind": kind,
            "loader_version": loader_version,
            "download_java": download_java,
            "custom_version_name": kwargs.get("custom_version_name"),
        }
        version_id = str(getattr(version, "id", "") or "")
        label = KIND_LABELS.get(kind, kind)
        suffix = f" {loader_version}" if loader_version else ""
        java_suffix = " · 含 Java 运行时" if download_java else ""
        self.page_header.set_title(f"正在安装 {version_id}")
        self.page_header.set_subtitle(f"{label}{suffix} · 游戏版本 {version_id}{java_suffix}")
        if not self._log_expanded:
            self._log_expanded = True
            self._toggle_log()
        if auto_start:
            self._start_install()
        else:
            self._set_idle()

    def on_hide(self) -> None:
        # 离开进度页时取消仍在执行的安装，避免后台继续写盘
        if self.is_running:
            self.cancel_task()

    def shutdown(self) -> None:
        self._stop_tick()
        super().shutdown()

    # ------------------------------------------------------------------
    # 安装
    # ------------------------------------------------------------------
    def _start_install(self) -> None:
        if self.is_running:
            return
        params = self._params
        version = params.get("version")
        version_id = str(getattr(version, "id", "") or "")
        kind = str(params.get("kind") or "vanilla")
        loader_version = params.get("loader_version")
        download_java = bool(params.get("download_java"))
        custom_version_name = params.get("custom_version_name")
        self._cancelled = False
        self._last_message = ""
        self._start_time = time.monotonic()
        self.notice.hide()
        self._spinner.start()
        self._percent_label.configure(text="0%", fg=theme.TEXT)
        first_stage = "正在下载 Java 运行时…" if download_java else "准备安装…"
        self._stage_label.configure(text=first_stage, fg=theme.TEXT)
        self._bar.reset()
        self._bar.indeterminate(True)
        self._speed_label.configure(text="—")
        self._size_label.configure(text="—")
        self._count_label.configure(text="—")
        self._stage_inline_label.configure(text="—")
        self._stage_count_label.configure(text="")
        self._elapsed_label.configure(text="0 秒")
        self._file_label.configure(text="当前文件：—")
        self._hint_label.configure(text="安装过程中请保持网络连接。", fg=theme.TEXT_DIM)
        self._cancel_button.set_enabled(True)
        self._back_button.pack_forget()
        self._log.clear()
        self._log.append(
            f"[{time.strftime('%H:%M:%S')}] "
            + ("开始下载 Java 运行时并安装 " if download_java else "开始安装 ")
            + f"{version_id}（{KIND_LABELS.get(kind, kind)}）"
        )
        if download_java:
            self._log.append(f"[{time.strftime('%H:%M:%S')}] 正在下载 Java 运行时…")
        if custom_version_name:
            self._log.append(f"[{time.strftime('%H:%M:%S')}] 新版本名：{custom_version_name}")
        self.set_status("正在准备安装…")
        self._schedule_tick()

        def work():
            if download_java:
                downloader = getattr(self.services, "download_java", None)
                if callable(downloader):
                    self.post("stage", "正在下载 Java 运行时…")
                    downloader(
                        version_id,
                        progress_cb=lambda progress: self.post("progress", progress),
                        cancel=self.cancel_event,
                    )
                    self.post("stage", "Java 运行时已就绪，开始安装游戏…")
                else:  # pragma: no cover - 后端版本过旧时跳过
                    self.post("stage", "当前版本不支持下载 Java，继续安装游戏…")
            return self.services.install(
                kind,
                loader_version,
                version_id,
                progress_cb=lambda progress: self.post("progress", progress),
                cancel=self.cancel_event,
                custom_version_name=custom_version_name,
            )

        self.start_task(
            work,
            on_success=self._on_install_success,
            on_error=self._on_install_error,
            on_event=self._on_event,
            name="install",
        )

    def _on_event(self, kind: str, payload) -> None:
        if kind == "progress":
            self._on_progress(payload)
        elif kind == "stage":
            self._on_stage(str(payload or ""))

    def _on_stage(self, text: str) -> None:
        """工作线程发来的阶段提示（例如“正在下载 Java 运行时…”）。"""
        if not text:
            return
        self._stage_label.configure(text=truncate(text, 46), fg=theme.TEXT)
        self._file_label.configure(text=f"当前文件：{truncate(text, 96)}")
        self._log.append(f"[{time.strftime('%H:%M:%S')}] {text}")
        self.set_status(truncate(text, 70))

    def _on_progress(self, progress) -> None:
        message = str(getattr(progress, "message", "") or "")
        done = int(getattr(progress, "done", 0) or 0)
        total = int(getattr(progress, "total", 0) or 0)
        bytes_done = int(getattr(progress, "bytes_done", 0) or 0)
        bytes_total = int(getattr(progress, "bytes_total", 0) or 0)
        speed = float(getattr(progress, "speed", 0.0) or 0.0)
        # task-14 之后 Progress 会带阶段字段，旧后端没有时保持兼容
        stage = str(getattr(progress, "stage", "") or "")
        stage_done = int(getattr(progress, "stage_done", 0) or 0)
        stage_total = int(getattr(progress, "stage_total", 0) or 0)

        fraction: float | None = None
        # 统一口径：总字节可用时按字节算（与“已下载/总大小”一致），否则回退到文件数。
        if bytes_total > 0:
            fraction = bytes_done / bytes_total
        elif total > 0:
            fraction = done / total
        if fraction is None:
            self._bar.indeterminate(True)
            self._percent_label.configure(text="—")
        else:
            fraction = max(0.0, min(1.0, fraction))
            if fraction >= 0.9995:  # 收尾误差按 100% 显示
                fraction = 1.0
            self._bar.indeterminate(False)
            self._bar.set(fraction)
            percent = min(100, int(round(fraction * 100)))
            self._percent_label.configure(text=f"{percent}%")

        if speed > 0:
            self._speed_label.configure(text=f"{human_size(int(speed))}/s")
        if bytes_total > 0:
            self._size_label.configure(text=f"{human_size(bytes_done)} / {human_size(bytes_total)}")
        else:
            self._size_label.configure(text=human_size(bytes_done) if bytes_done else "—")
        # “文件进度”始终显示整体 done/total；阶段内进度单独一行。
        if total > 0:
            self._count_label.configure(text=f"{done} / {total}")
        else:
            self._count_label.configure(text="—")
        if stage_total > 0:
            stage_prefix = f"{stage} " if stage else ""
            stage_text = f"{stage_done} / {stage_total}"
            self._stage_inline_label.configure(text=stage_text)
            self._stage_count_label.configure(text=f"本阶段 {stage_prefix}{stage_done} / {stage_total}")
        elif stage:
            # 旧后端/加载器自身进度可能没有阶段分母：不拼接数字，只显示占位
            self._stage_inline_label.configure(text="—")
            self._stage_count_label.configure(text="本阶段 —")
        else:
            self._stage_inline_label.configure(text="—")
            self._stage_count_label.configure(text="")
        if message:
            self._stage_label.configure(text=truncate(message, 46), fg=theme.TEXT)
            self._file_label.configure(text=f"当前文件：{truncate(message, 96)}")
            if message != self._last_message:
                self._last_message = message
                self._log.append(f"[{time.strftime('%H:%M:%S')}] {message}")
                self.set_status(truncate(message, 70))

    # ------------------------------------------------------------------
    # 计时
    # ------------------------------------------------------------------
    def _schedule_tick(self) -> None:
        self._stop_tick()
        try:
            self._tick_after = self.after(500, self._tick)
        except tk.TclError:  # pragma: no cover
            self._tick_after = None

    def _stop_tick(self) -> None:
        if self._tick_after is not None:
            try:
                self.after_cancel(self._tick_after)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._tick_after = None

    def _tick(self) -> None:
        self._tick_after = None
        elapsed = time.monotonic() - self._start_time
        self._elapsed_label.configure(text=_format_elapsed(elapsed))
        if self.is_running:
            self._schedule_tick()

    # ------------------------------------------------------------------
    # 结束状态
    # ------------------------------------------------------------------
    def _on_install_success(self, profile_id) -> None:
        elapsed = time.monotonic() - self._start_time
        self._stop_tick()
        self._spinner.stop()
        self._bar.indeterminate(False)
        self._bar.set(1.0)
        self._percent_label.configure(text="100%", fg=theme.SUCCESS)
        self._stage_label.configure(text="安装完成", fg=theme.SUCCESS)
        self._stage_inline_label.configure(text="—")
        self._stage_count_label.configure(text="")
        self._elapsed_label.configure(text=_format_elapsed(elapsed))
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 安装完成，配置名：{profile_id}")
        self.set_status("安装完成", kind="success")
        self._cancel_button.set_enabled(False)
        self._show_back_button()
        params = self._params
        self.app.show_page(  # type: ignore[attr-defined]
            "done",
            success=True,
            version=params.get("version"),
            kind=params.get("kind"),
            loader_version=params.get("loader_version"),
            profile_id=profile_id,
            elapsed=elapsed,
        )

    def _on_install_error(self, exc: BaseException) -> None:
        elapsed = time.monotonic() - self._start_time
        self._stop_tick()
        self._spinner.stop()
        self._cancel_button.set_enabled(False)
        self._show_back_button()
        elapsed_text = _format_elapsed(elapsed)
        self._elapsed_label.configure(text=elapsed_text)
        params = self._params
        if self.is_cancelled(exc):
            self._cancelled = True
            self._bar.indeterminate(False)
            self._stage_label.configure(text="已取消安装", fg=theme.WARNING)
            self._hint_label.configure(text="安装已取消，可以返回下载页重新开始。",
                                       fg=theme.WARNING)
            self.notice.show("安装已取消，未完成的文件会在下次安装时自动补齐。", "info")
            self._log.append(f"[{time.strftime('%H:%M:%S')}] 安装已取消")
            self.set_status("安装已取消", kind="warn")
            self.app.show_page(  # type: ignore[attr-defined]
                "done",
                success=False,
                cancelled=True,
                version=params.get("version"),
                kind=params.get("kind"),
                loader_version=params.get("loader_version"),
                error="安装已取消",
                elapsed=elapsed,
            )
            return
        log.info("安装失败: %s", exc, exc_info=True)
        self._stage_label.configure(text="安装失败", fg=theme.DANGER)
        self._bar.indeterminate(False)
        message = error_message(exc)
        detail = error_detail(exc)
        if detail:
            message = f"{message}（{detail}）"
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 安装失败：{message}")
        self.set_status("安装失败", kind="error")
        self.app.show_page(  # type: ignore[attr-defined]
            "done",
            success=False,
            version=params.get("version"),
            kind=params.get("kind"),
            loader_version=params.get("loader_version"),
            error=message,
            elapsed=elapsed,
        )

    def _show_back_button(self) -> None:
        if not self._back_button.winfo_manager():
            self._back_button.pack(side="right", padx=(0, theme.PAD_SM))
        self._back_button.set_enabled(True)

    def _set_idle(self) -> None:
        self._stop_tick()
        self._spinner.stop()
        self._bar.indeterminate(False)
        self._bar.reset()
        self._percent_label.configure(text="0%", fg=theme.TEXT)
        self._stage_label.configure(text="尚未开始安装", fg=theme.TEXT)
        self._file_label.configure(text="当前文件：—")
        self._speed_label.configure(text="—")
        self._size_label.configure(text="—")
        self._count_label.configure(text="—")
        self._stage_inline_label.configure(text="—")
        self._stage_count_label.configure(text="")
        self._elapsed_label.configure(text="—")
        self._hint_label.configure(text="在下载页完成选择后会自动开始安装。",
                                   fg=theme.TEXT_DIM)
        self._cancel_button.set_enabled(False)
        self._show_back_button()
        if not self._log.text.get("1.0", "end").strip():
            self._log.append("等待安装任务开始…")

    # ------------------------------------------------------------------
    # 用户操作
    # ------------------------------------------------------------------
    def cancel_install(self) -> None:
        if not self.is_running or self._cancelled:
            return
        self._cancelled = True
        self.cancel_task()
        self._cancel_button.set_enabled(False)
        self._stage_label.configure(text="正在取消…", fg=theme.WARNING)
        self._hint_label.configure(text="正在通知下载线程停止…", fg=theme.WARNING)
        self._log.append(f"[{time.strftime('%H:%M:%S')}] 用户请求取消安装")
        self.set_status("正在取消安装…", kind="warn")

    def _back_to_download(self) -> None:
        self.app.show_page("download")  # type: ignore[attr-defined]


__all__ = ["ProgressPage"]
