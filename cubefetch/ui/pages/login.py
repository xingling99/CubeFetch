"""登录页：Microsoft 设备码流程（深色现代极简版）。

调用契约 §11 的 AppServices：
    services.login_flow() -> DeviceFlow
    services.finish_login(flow, cancel, status_cb) -> Account
两个调用都在工作线程执行；UI 只消费队列事件。
"""
from __future__ import annotations

import logging
import threading
import tkinter as tk
import webbrowser

from ...core.errors import CancelledError
from .. import theme
from ..widgets import (
    Card,
    LinkLabel,
    NoticeBar,
    Spinner,
    draw_cube_logo,
    ghost_button,
    primary_button,
    secondary_button,
)
from .base import BasePage, error_detail, error_message

log = logging.getLogger("cubefetch.ui.login")

# 打开浏览器的钩子：默认直接调用 webbrowser.open；
# 自测会替换该钩子来断言“拿到设备码后自动打开浏览器”的次数与 URL。
_browser_opener = webbrowser.open

_CARD_WIDTH = 460


class LoginPage(BasePage):
    title = "登录"
    nav_key = "login"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> None:
        self._flow = None
        self._auto_opened_flow = None
        self._state = "idle"
        self._restart_requested = False
        self._status_var = tk.StringVar(value="")
        self._code_var = tk.StringVar(value="ABCD-EFGH")
        self._uri_var = tk.StringVar(value="https://microsoft.com/link")
        self._copy_hint_var = tk.StringVar(value="")

        holder = tk.Frame(self, bg=theme.BG)
        holder.pack(fill="both", expand=True)

        # 极淡的紫/蓝双光晕（静态，无动画）：低饱和、不与正文抢对比。
        self._glow = tk.Canvas(holder, bg=theme.BG, highlightthickness=0, bd=0, takefocus=0)
        self._glow.place(x=0, y=0, relwidth=1, relheight=1)
        self._glow.bind("<Configure>", self._draw_glow)

        for column in (0, 2):
            holder.grid_columnconfigure(column, weight=1)
        for row in (0, 2):
            holder.grid_rowconfigure(row, weight=1)

        center = tk.Frame(holder, bg=theme.BG)
        center.grid(row=1, column=1)

        self.card = Card(center, padding=theme.PAD_XXL, bg=theme.SURFACE,
                         border=theme.BORDER)
        self.card.pack()

        # 提示条不预留空白：隐藏时完全收起，显示时插回卡片上方。
        self.notice = NoticeBar(center, reserve_space=False, before=self.card, bg=theme.BG)
        self.notice.hide()

        self._frames: dict[str, tk.Frame] = {}
        self._build_idle(self.card.body)
        self._show_state("idle")

    # ---- 背景光晕 ----
    def _draw_glow(self, event=None) -> None:
        try:
            width = self._glow.winfo_width()
            height = self._glow.winfo_height()
        except (tk.TclError, AttributeError):
            return
        if width <= 4 or height <= 4:
            return
        self._glow.delete("all")
        blobs = (
            (0.16, 0.06, 0.70, theme.ACCENT),
            (0.90, 0.96, 0.66, theme.ACCENT_2),
            (0.02, 0.96, 0.42, "#2f8faf"),
        )
        for cx_ratio, cy_ratio, radius_ratio, color in blobs:
            cx = width * cx_ratio
            cy = height * cy_ratio
            base = max(width, height) * radius_ratio
            layers = 10
            for index in range(layers):
                fraction = 1.0 - index * (0.78 / layers)
                alpha = 0.008 + 0.026 * (index / (layers - 1))
                rx = base * fraction
                ry = rx * 0.78
                self._glow.create_oval(cx - rx, cy - ry, cx + rx, cy + ry,
                                       fill=theme.mix(theme.BG, color, alpha), outline="")

    # ---- 未登录状态 ----
    def _build_idle(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=theme.SURFACE)
        self._frames["idle"] = frame

        logo = tk.Canvas(frame, width=theme.sp(64), height=theme.sp(64),
                         bg=theme.SURFACE, highlightthickness=0, bd=0)
        logo.pack(pady=(0, theme.PAD_LG))
        draw_cube_logo(logo, theme.sp(64))

        tk.Label(frame, text="登录 Microsoft 账号", bg=theme.SURFACE, fg=theme.TEXT,
                 font=theme.bold(theme.FS_HEADING)).pack()
        tk.Label(
            frame,
            text=(
                "CubeFetch 方块速递需要验证你的 Minecraft 所有权。\n"
                "登录后即可下载安装游戏本体与 Fabric / Quilt / Forge / NeoForge 加载器。"
            ),
            bg=theme.SURFACE, fg=theme.TEXT_MUTED, font=theme.font(theme.FS_BODY),
            justify="center", wraplength=theme.sp(380),
        ).pack(pady=(theme.PAD_SM, theme.PAD_XL))

        self._login_button = primary_button(
            frame, "使用 Microsoft 账号登录", self.start_login,
            width=_CARD_WIDTH - theme.PAD_XXL * 2,
            height=theme.BUTTON_HEIGHT_LG,
            font_size=theme.FS_SUBTITLE,
            bg=theme.SURFACE,
        )
        self._login_button.pack(pady=(0, theme.PAD_MD), fill="x")

        remember_days = int(getattr(self.config, "remember_days", 30) or 30)
        tk.Label(
            frame,
            text=f"登录信息使用 Windows DPAPI 加密保存在本机，{remember_days} 天内免登录。",
            bg=theme.SURFACE, fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL),
            wraplength=theme.sp(380), justify="center",
        ).pack()
        tk.Label(
            frame,
            text="点击后会自动打开浏览器并复制验证码，按提示完成授权即可。",
            bg=theme.SURFACE, fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL),
            wraplength=theme.sp(380), justify="center",
        ).pack(pady=(theme.PAD_XS, 0))

    # ---- 设备码等待状态 ----
    def _build_device(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=theme.SURFACE)
        self._frames["device"] = frame
        tk.Label(frame, text="在浏览器中完成登录", bg=theme.SURFACE, fg=theme.TEXT,
                 font=theme.bold(theme.FS_HEADING)).pack(anchor="w")
        tk.Label(
            frame,
            text="1. 打开下面的链接    2. 输入设备代码    3. 完成授权后本页会自动继续",
            bg=theme.SURFACE, fg=theme.TEXT_MUTED, font=theme.font(theme.FS_BODY),
            justify="left", wraplength=theme.sp(380),
        ).pack(anchor="w", pady=(theme.PAD_SM, theme.PAD_LG))

        code_box = tk.Frame(frame, bg=theme.BG_INPUT, highlightthickness=1,
                            highlightbackground=theme.BORDER_LIGHT, bd=0)
        code_box.pack(fill="x")
        tk.Label(
            code_box, textvariable=self._code_var, bg=theme.BG_INPUT, fg=theme.TEXT,
            font=theme.font(30, "bold", mono=True), pady=theme.sp(14),
        ).pack(expand=True)

        self._copy_hint = tk.Label(
            frame, textvariable=self._copy_hint_var, bg=theme.SURFACE, fg=theme.SUCCESS,
            font=theme.font(theme.FS_SMALL), anchor="w",
        )
        self._copy_hint.pack(fill="x", pady=(theme.PAD_XS, 0))

        actions = tk.Frame(frame, bg=theme.SURFACE)
        actions.pack(fill="x", pady=(theme.PAD_MD, theme.PAD_SM))
        self._copy_button = secondary_button(
            actions, "复制代码", self.copy_code, height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE
        )
        self._copy_button.pack(side="left")
        self._open_button = primary_button(
            actions, "在浏览器中打开", self.open_browser,
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )
        self._open_button.pack(side="left", padx=(theme.PAD_SM, 0))

        uri_row = tk.Frame(frame, bg=theme.SURFACE)
        uri_row.pack(fill="x", pady=(theme.PAD_XS, theme.PAD_MD))
        tk.Label(uri_row, text="授权地址：", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_SMALL)).pack(side="left")
        LinkLabel(uri_row, "https://microsoft.com/link", self.open_browser,
                  bg=theme.SURFACE, font_size=theme.FS_SMALL).pack(side="left")

        status_row = tk.Frame(frame, bg=theme.SURFACE)
        status_row.pack(fill="x")
        self._spinner = Spinner(status_row, size=18, bg=theme.SURFACE)
        self._spinner.pack(side="left", padx=(0, theme.PAD_SM))
        tk.Label(status_row, textvariable=self._status_var, bg=theme.SURFACE,
                 fg=theme.TEXT_MUTED, font=theme.font(theme.FS_SMALL),
                 anchor="w").pack(side="left", fill="x", expand=True)

        bottom = tk.Frame(frame, bg=theme.SURFACE)
        bottom.pack(fill="x", pady=(theme.PAD_MD, 0))
        self._cancel_button = ghost_button(
            bottom, "取消登录", self.cancel_login, height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE
        )
        self._cancel_button.pack(side="left")
        self._regenerate_button = ghost_button(
            bottom, "重新获取验证码", self._restart_login,
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )
        self._regenerate_button.pack(side="right")

    # ---- 已登录状态 ----
    def _build_logged(self, parent: tk.Frame) -> None:
        frame = tk.Frame(parent, bg=theme.SURFACE)
        self._frames["logged"] = frame
        tk.Label(frame, text="已登录", bg=theme.SURFACE, fg=theme.SUCCESS,
                 font=theme.bold(theme.FS_HEADING)).pack(anchor="w")
        self._account_name = tk.Label(frame, text="", bg=theme.SURFACE, fg=theme.TEXT,
                                      font=theme.bold(theme.FS_TITLE))
        self._account_name.pack(anchor="w", pady=(theme.PAD_MD, 2))
        self._account_sub = tk.Label(frame, text="", bg=theme.SURFACE, fg=theme.TEXT_DIM,
                                     font=theme.font(theme.FS_SMALL, mono=True))
        self._account_sub.pack(anchor="w")
        tk.Label(
            frame,
            text="登录信息会加密保存在本机；如需切换账号请点击下方按钮。",
            bg=theme.SURFACE, fg=theme.TEXT_MUTED, font=theme.font(theme.FS_BODY),
            wraplength=theme.sp(380), justify="left",
        ).pack(anchor="w", pady=(theme.PAD_LG, theme.PAD_MD))
        row = tk.Frame(frame, bg=theme.SURFACE)
        row.pack(fill="x")
        primary_button(row, "继续", self._continue, width=theme.sp(160),
                       bg=theme.SURFACE).pack(side="left")
        secondary_button(row, "切换账号", self._switch_account, width=theme.sp(140),
                         bg=theme.SURFACE).pack(side="left", padx=(theme.PAD_SM, 0))

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_show(self, **kwargs) -> None:
        notice = kwargs.get("notice")
        if notice:
            kind = kwargs.get("notice_kind", "error")
            self.notice.show(str(notice), kind)
        else:
            self.notice.hide()
        account = self.account
        if account is not None:
            self._show_state("logged")
            self._account_name.configure(text=f"{getattr(account, 'name', '?')}")
            uuid = getattr(account, "uuid", "") or ""
            self._account_sub.configure(text=uuid if uuid else "Microsoft 账号")
            self._spinner.stop()
        elif self.is_running:
            self._show_state("device")
        else:
            self._show_state("idle")
            self._login_button.set_enabled(True)
        self.set_status("请先登录 Microsoft 账号")

    def on_hide(self) -> None:
        if self.is_running:
            self.cancel_task()
            self._spinner.stop()

    # ------------------------------------------------------------------
    # 状态切换
    # ------------------------------------------------------------------
    def _show_state(self, state: str) -> None:
        self._state = state
        # 设备码/已登录状态按需惰性构建：启动时只创建空闲态，缩短首屏耗时。
        if state not in self._frames:
            builders = {
                "device": self._build_device,
                "logged": self._build_logged,
            }
            builder = builders.get(state)
            if builder is not None:
                builder(self.card.body)
        for name, frame in self._frames.items():
            if name == state:
                frame.pack(fill="x")
            else:
                frame.pack_forget()
        if state != "device" and hasattr(self, "_spinner"):
            self._spinner.stop()

    # ------------------------------------------------------------------
    # 登录流程
    # ------------------------------------------------------------------
    def start_login(self) -> None:
        if self.is_running:
            return
        self.notice.hide()
        self._flow = None
        self._auto_opened_flow = None
        self._restart_requested = False
        self._login_button.set_enabled(False)
        self._show_state("device")
        self._code_var.set("正在获取设备码…")
        self._status_var.set("正在连接 Microsoft…")
        self._copy_hint_var.set("")
        self._spinner.start()
        self._cancel_button.set_enabled(True)
        self._regenerate_button.set_enabled(True)
        self._copy_button.set_enabled(False)
        self._open_button.set_enabled(False)
        self.set_status("正在请求设备代码…")

        def work():
            flow = self.services.login_flow()
            self.post("flow", flow)
            status_cb = lambda message: self.post("status", message)  # noqa: E731
            account = self.services.finish_login(flow, self.cancel_event, status_cb)
            return account

        self.start_task(
            work,
            on_success=self._on_login_success,
            on_error=self._on_login_error,
            on_event=self._on_login_event,
            name="login",
        )

    def _on_login_event(self, kind: str, payload) -> None:
        if kind == "flow":
            self._flow = payload
            code = str(getattr(payload, "user_code", "") or "")
            uri = str(getattr(payload, "verification_uri", "") or "")
            self._code_var.set(code or "--------")
            self._uri_var.set(uri)
            self._copy_button.set_enabled(True)
            self._open_button.set_enabled(True)
            # 设备码一拿到就自动复制到系统剪贴板，方便直接粘贴到浏览器。
            copied = self._copy_to_clipboard(code)
            # 同时自动打开系统浏览器（重新获取验证码时同样会走到这里）。
            if getattr(self, "_auto_opened_flow", None) is not payload:
                self._auto_opened_flow = payload
                self.open_browser()
            if copied:
                self._copy_hint_var.set("验证码已复制，已自动打开浏览器")
                self._status_var.set("验证码已复制，已自动打开浏览器，等待完成授权…")
                self.set_status("验证码已复制，已自动打开浏览器", kind="success")
            else:
                self._copy_hint_var.set("浏览器已自动打开；自动复制失败，请手动复制")
                self._status_var.set("已自动打开浏览器，等待完成授权…")
                self.set_status("已自动打开浏览器，请在浏览器中完成授权")
        elif kind == "status":
            self._status_var.set(str(payload))
            self.set_status(str(payload))

    def _on_login_success(self, account) -> None:
        self._spinner.stop()
        self._show_state("logged")
        name = getattr(account, "name", "玩家")
        self.app.set_account(account)  # type: ignore[attr-defined]
        self.set_status(f"登录成功：{name}", kind="success")
        self.app.show_page("download")  # type: ignore[attr-defined]

    def _on_login_error(self, exc: BaseException) -> None:
        self._spinner.stop()
        if isinstance(exc, CancelledError) and self._restart_requested:
            # 用户点了“重新获取验证码”：取消旧流程后马上重新开始。
            self._restart_requested = False
            self._status_var.set("正在重新获取验证码…")
            self.after(80, self.start_login)
            return
        self._show_state("idle")
        self._login_button.set_enabled(True)
        if isinstance(exc, CancelledError):
            self.notice.show("登录已取消。", "info")
            self.set_status("登录已取消", kind="warn")
            return
        log.info("登录失败: %s", exc)
        message = error_message(exc)
        detail = error_detail(exc)
        if detail:
            message = f"{message}（{detail}）"
        self.notice.show(message, "error")
        self.set_status("登录失败", kind="error")

    # ------------------------------------------------------------------
    # 设备码辅助操作
    # ------------------------------------------------------------------
    def cancel_login(self) -> None:
        if not self.is_running:
            self._show_state("idle")
            return
        self.cancel_task()
        self._cancel_button.set_enabled(False)
        self._status_var.set("正在取消…")
        self.set_status("正在取消登录…", kind="warn")

    def _restart_login(self) -> None:
        """作废当前设备码并重新申请一个新的（新验证码同样会自动复制）。"""
        if not self.is_running:
            self.start_login()
            return
        self._restart_requested = True
        self._regenerate_button.set_enabled(False)
        self._cancel_button.set_enabled(False)
        self.cancel_task()
        self._status_var.set("正在重新获取验证码…")
        self._copy_hint_var.set("")
        self.set_status("正在重新获取设备代码…", kind="warn")

    def _copy_to_clipboard(self, text: str) -> bool:
        """把文本写入系统剪贴板；成功返回 True。"""
        text = str(text or "").strip()
        if not text or "…" in text or text == "--------":
            return False
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except tk.TclError:  # pragma: no cover - 无图形环境时
            return False
        return True

    def copy_code(self) -> None:
        code = self._code_var.get()
        if not self._copy_to_clipboard(code):
            return
        self._copy_hint_var.set("验证码已复制到剪贴板")
        self.set_status(f"已复制设备代码 {code}", kind="success")

    def open_browser(self) -> None:
        flow = self._flow
        uri = str(getattr(flow, "verification_uri", "") or self._uri_var.get())
        if not uri:
            return
        threading.Thread(target=self._open_browser_worker, args=(uri,), daemon=True,
                         name="cubefetch-open-browser").start()

    @staticmethod
    def _open_browser_worker(uri: str) -> None:
        try:
            _browser_opener(uri)
        except Exception:  # noqa: BLE001 - 打开浏览器失败不应崩溃
            log.warning("打开浏览器失败: %s", uri, exc_info=True)

    # ------------------------------------------------------------------
    # 已登录状态操作
    # ------------------------------------------------------------------
    def _continue(self) -> None:
        self.app.show_page("download")  # type: ignore[attr-defined]

    def _switch_account(self) -> None:
        logout = getattr(self.app, "logout", None)
        if callable(logout):
            logout()


__all__ = ["LoginPage"]
