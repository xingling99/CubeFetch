"""设置页：分组卡片（Minecraft 目录 / 下载 / 账号与安全 / 关于）。

保存时直接改 ``services.config``（AppConfig）字段并调用
``services.save_config()``；自动检测调用 ``services.detect_launchers()``
（放到工作线程，避免扫描磁盘卡住 Tk 主线程）。
"""
from __future__ import annotations

import logging
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, ttk
from typing import Any

from ...config import APP_NAME_CN, APP_VERSION, default_minecraft_root
from .. import theme
from ..widgets import (
    Card,
    NoticeBar,
    ScrollFrame,
    SegmentedControl,
    ghost_button,
    primary_button,
    secondary_button,
)
from .base import BasePage, error_message

log = logging.getLogger("cubefetch.ui.settings")

MIRROR_OPTIONS = [
    ("自动（优先镜像）", "auto"),
    ("官方源", "official"),
    ("BMCLAPI", "bmclapi"),
]
MIRROR_HINTS = {
    "auto": "优先使用国内镜像并自动测速，镜像不可用时回退官方源。",
    "official": "始终使用 Mojang / Forge 官方源。",
    "bmclapi": "始终使用 BMCLAPI 国内镜像，适合国内网络。",
}

_LABEL_WIDTH = 120


class SettingsPage(BasePage):
    title = "设置"
    nav_key = "settings"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> None:
        self._detection = getattr(self.services, "detection", None)
        self._detect_running = False
        self._root_var = tk.StringVar(value="")
        self._java_var = tk.StringVar(value="")
        self._threads_var = tk.StringVar(value="32")
        self._remember_var = tk.StringVar(value="30")
        self._mirror_value = "auto"
        self._mirror_hint = tk.StringVar(value="")

        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_LG, 0))
        tk.Label(header, text="设置", bg=theme.BG, fg=theme.TEXT,
                 font=theme.font(theme.FS_HEADING, "bold"), anchor="w").pack(side="left")
        tk.Label(header, text="修改后点击右下角“保存设置”生效", bg=theme.BG,
                 fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL),
                 anchor="w").pack(side="left", padx=(theme.PAD_MD, 0),
                                   pady=(theme.sp(6), 0))

        self.notice = NoticeBar(self)
        self.notice.pack(fill="x", padx=theme.CONTENT_PADDING)
        self.notice.hide()

        self._scroll = ScrollFrame(self, bg=theme.BG)
        self._scroll.pack(fill="both", expand=True, padx=theme.CONTENT_PADDING,
                          pady=(theme.PAD_MD, 0))
        body = self._scroll.body

        self._build_root_card(body)
        self._build_download_card(body)
        self._build_account_card(body)
        self._build_about_card(body)

        footer = tk.Frame(self, bg=theme.BG)
        footer.pack(fill="x", padx=theme.CONTENT_PADDING, pady=theme.PAD_LG)
        self._hint_label = tk.Label(
            footer, text="设置保存在 %LOCALAPPDATA%\\CubeFetch\\config.json",
            bg=theme.BG, fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL), anchor="w",
        )
        self._hint_label.pack(side="left")
        self._save_button = primary_button(footer, "保存设置", self.save, width=120,
                                           bg=theme.BG)
        self._save_button.pack(side="right")
        ghost_button(footer, "恢复默认", self.reset_defaults, width=110,
                     bg=theme.BG).pack(side="right", padx=(0, theme.PAD_SM))

    # ---- 分组卡片基础结构 ----
    def _group_card(self, parent: tk.Frame, title: str, subtitle: str = "") -> tk.Frame:
        card = Card(parent, padding=theme.PAD_XL)
        card.pack(fill="x", pady=(0, theme.PAD_MD))
        tk.Label(card.body, text=title, bg=theme.SURFACE, fg=theme.TEXT,
                 font=theme.font(theme.FS_TITLE, "bold"), anchor="w").pack(fill="x")
        if subtitle:
            tk.Label(card.body, text=subtitle, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                     font=theme.font(theme.FS_SMALL), anchor="w", justify="left",
                     wraplength=theme.sp(720)).pack(fill="x", pady=(theme.sp(2), 0))
        rows = tk.Frame(card.body, bg=theme.SURFACE)
        rows.pack(fill="x", pady=(theme.PAD_MD, 0))
        rows.grid_columnconfigure(0, minsize=theme.sp(_LABEL_WIDTH))
        rows.grid_columnconfigure(1, weight=1)
        return rows

    def _row(self, rows: tk.Frame, row: int, label: str) -> tk.Frame:
        tk.Label(rows, text=label, bg=theme.SURFACE, fg=theme.TEXT_MUTED,
                 font=theme.font(theme.FS_BODY), anchor="w").grid(
            row=row, column=0, sticky="nw", pady=(0, theme.PAD_MD))
        control = tk.Frame(rows, bg=theme.SURFACE)
        control.grid(row=row, column=1, sticky="ew", pady=(0, theme.PAD_MD))
        return control

    @staticmethod
    def _hint(parent: tk.Frame, text: str) -> None:
        tk.Label(parent, text=text, bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_SMALL), anchor="w", justify="left",
                 wraplength=theme.sp(620)).pack(fill="x")

    # ---- Minecraft 目录 ----
    def _build_root_card(self, parent: tk.Frame) -> None:
        rows = self._group_card(parent, "Minecraft 目录",
                                "游戏本体、版本和 Mod 都会安装到这个 .minecraft 目录。")
        control = self._row(rows, 0, "目录")
        self._root_entry = ttk.Entry(control, textvariable=self._root_var, style="Dark.TEntry")
        self._root_entry.pack(side="left", fill="x", expand=True, ipady=theme.sp(4))
        secondary_button(control, "浏览…", self.browse_root,
                         height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE).pack(
            side="left", padx=(theme.PAD_SM, 0))
        self._detect_button = secondary_button(
            control, "自动检测", self.detect_launchers,
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )
        self._detect_button.pack(side="left", padx=(theme.PAD_SM, 0))

        self._detect_label = tk.Label(
            rows, text="点击“自动检测”查找本机的游戏目录。", bg=theme.SURFACE,
            fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL), justify="left",
            anchor="w", wraplength=theme.sp(700),
        )
        self._detect_label.grid(row=1, column=1, sticky="w")

    # ---- 下载 ----
    def _build_download_card(self, parent: tk.Frame) -> None:
        rows = self._group_card(parent, "下载", "下载源与并发设置会影响下载速度与稳定性。")

        mirror_control = self._row(rows, 0, "下载源")
        self._mirror_seg = SegmentedControl(
            mirror_control, MIRROR_OPTIONS, value="auto",
            on_change=self._on_mirror_change, bg=theme.SURFACE, height=34,
        )
        self._mirror_seg.pack(side="left")
        tk.Label(rows, textvariable=self._mirror_hint, bg=theme.SURFACE,
                 fg=theme.TEXT_DIM, font=theme.font(theme.FS_SMALL), anchor="w",
                 justify="left", wraplength=theme.sp(620)).grid(
            row=1, column=1, sticky="w", pady=(0, theme.PAD_MD))

        threads_control = self._row(rows, 2, "下载线程数")
        self._threads_spin = ttk.Spinbox(
            threads_control, from_=1, to=64, width=6,
            textvariable=self._threads_var, style="Dark.TSpinbox",
        )
        self._threads_spin.pack(side="left", ipady=theme.sp(3))
        tk.Label(threads_control, text="1 - 64，默认 32；网络不稳定时可适当降低。",
                 bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_SMALL)).pack(side="left", padx=(theme.PAD_SM, 0))

        remember_control = self._row(rows, 3, "记住登录天数")
        self._remember_spin = ttk.Spinbox(
            remember_control, from_=1, to=365, width=6,
            textvariable=self._remember_var, style="Dark.TSpinbox",
        )
        self._remember_spin.pack(side="left", ipady=theme.sp(3))
        tk.Label(remember_control, text="超过该天数后需要重新登录 Microsoft 账号。",
                 bg=theme.SURFACE, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_SMALL)).pack(side="left", padx=(theme.PAD_SM, 0))

    # ---- 账号与安全 ----
    def _build_account_card(self, parent: tk.Frame) -> None:
        rows = self._group_card(parent, "账号与安全",
                                "登录信息使用 Windows DPAPI 加密保存在本机。")
        account_control = self._row(rows, 0, "当前账号")
        self._account_label = tk.Label(account_control, text="未登录", bg=theme.SURFACE,
                                       fg=theme.TEXT, font=theme.font(theme.FS_BODY),
                                       anchor="w")
        self._account_label.pack(side="left")
        logout_control = self._row(rows, 1, "登录状态")
        secondary_button(logout_control, "退出登录", self._logout,
                         height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE).pack(side="left")
        self._hint(logout_control, "退出后需要重新验证 Microsoft 账号。")

    # ---- 关于 ----
    def _build_about_card(self, parent: tk.Frame) -> None:
        rows = self._group_card(parent, "关于")
        self._about_version = self._row(rows, 0, "版本")
        tk.Label(self._about_version, text=f"{APP_NAME_CN} v{APP_VERSION}", bg=theme.SURFACE,
                 fg=theme.TEXT, font=theme.font(theme.FS_BODY)).pack(side="left")
        self._about_ui = self._row(rows, 1, "界面")
        tk.Label(self._about_ui, text="深色现代极简", bg=theme.SURFACE,
                 fg=theme.TEXT, font=theme.font(theme.FS_BODY)).pack(side="left")
        self._about_note = self._row(rows, 2, "说明")
        self._hint(self._about_note, "安装文件保存在本机 .minecraft 目录，不会上传到云端。")

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_show(self, **kwargs) -> None:
        self.notice.hide()
        self._detection = getattr(self.services, "detection", None)
        config = self._config()
        if config is not None:
            root = getattr(config, "mc_root", None)
            if not root:
                root = str(getattr(config, "mc_root_path", "") or "")
            self._root_var.set(str(root or ""))
            self._java_var.set(str(getattr(config, "java_path", None) or ""))
            self._threads_var.set(str(getattr(config, "threads", 32) or 32))
            self._remember_var.set(str(getattr(config, "remember_days", 30) or 30))
            mirror = str(getattr(config, "mirror", "auto") or "auto")
            self._mirror_value = mirror if mirror in dict(MIRROR_OPTIONS).values() else "auto"
            try:
                self._mirror_seg.set(self._mirror_value)
            except Exception:  # noqa: BLE001 - 控件异常不应阻塞页面
                log.debug("设置下载源分段控件失败", exc_info=True)
            self._mirror_hint.set(MIRROR_HINTS.get(self._mirror_value, ""))
        account = self.account
        if account is not None:
            self._account_label.configure(text=f"{getattr(account, 'name', '?')}",
                                          fg=theme.TEXT)
        else:
            self._account_label.configure(text="未登录", fg=theme.TEXT_DIM)
        self._render_detection(self._detection)
        self._detect_button.set_enabled(True)
        self.set_status("设置")

    def on_hide(self) -> None:
        if self._detect_running:
            self.cancel_task()
            self._detect_running = False

    # ------------------------------------------------------------------
    # 字段操作
    # ------------------------------------------------------------------
    def _config(self):
        config = getattr(self.services, "config", None)
        return config if config is not None else getattr(self.app, "config", None)

    def _on_mirror_change(self, value: str) -> None:
        self._mirror_value = value
        self._mirror_hint.set(MIRROR_HINTS.get(value, ""))

    def browse_root(self) -> None:
        current = self._root_var.get().strip()
        initial = current or str(default_minecraft_root())
        try:
            path = filedialog.askdirectory(
                parent=self, title="选择 .minecraft 目录",
                initialdir=initial if Path(initial).is_dir() else str(Path(initial).parent),
                mustexist=False,
            )
        except tk.TclError:  # pragma: no cover - 对话框被关闭
            return
        if path:
            self._root_var.set(str(Path(path)))
            self.set_status(f"已选择目录：{path}", kind="success")

    def browse_java(self) -> None:
        current = self._java_var.get().strip()
        initialdir = str(Path(current).parent) if current and Path(current).parent.is_dir() else "C:\\"
        try:
            path = filedialog.askopenfilename(
                parent=self, title="选择 java.exe / javaw.exe", initialdir=initialdir,
                filetypes=[("Java 可执行文件", "java*.exe"), ("可执行文件", "*.exe"),
                           ("所有文件", "*.*")],
            )
        except tk.TclError:  # pragma: no cover
            return
        if path:
            self._java_var.set(str(Path(path)))
            self.set_status(f"已选择 Java：{path}", kind="success")

    def _logout(self) -> None:
        logout = getattr(self.app, "logout", None)
        if callable(logout):
            logout()
        else:  # pragma: no cover - 兜底
            self.notice.show("当前环境不支持退出登录。", "warn")

    # ------------------------------------------------------------------
    # 自动检测
    # ------------------------------------------------------------------
    def detect_launchers(self) -> None:
        if self._detect_running:
            return
        detect = getattr(self.services, "detect_launchers", None)
        if not callable(detect):
            self.notice.show("当前服务不支持自动检测游戏目录，请手动选择目录。", "warn")
            return
        self._detect_running = True
        self._detect_button.set_enabled(False)
        self._detect_label.configure(text="正在检测本机游戏目录…", fg=theme.TEXT_MUTED)
        self.set_status("正在检测游戏目录…")

        def work():
            return detect()

        self.start_task(work, on_success=self._on_detected,
                        on_error=self._on_detect_error, name="detect")

    def _on_detected(self, detection: Any) -> None:
        self._detect_running = False
        self._detect_button.set_enabled(True)
        self._detection = detection
        self.services.detection = detection  # type: ignore[attr-defined]
        self._render_detection(detection)
        default_root = getattr(detection, "default_root", None)
        if default_root:
            # 检测到目录就直接自动选择最佳目录（版本数最多），用户不用再点一次。
            chosen = str(default_root)
            self._root_var.set(chosen)
            chooser = getattr(self.services, "choose_root", None)
            if callable(chooser):
                try:
                    chooser(chosen)
                except Exception:  # noqa: BLE001 - 自动选择失败不应打断设置页
                    pass
            count = 0
            for item in getattr(detection, "root_versions", None) or []:
                try:
                    if str(item[0]) == chosen:
                        count = int(item[1])
                        break
                except (TypeError, ValueError, IndexError):
                    continue
            label = f"{chosen}（{count} 个版本）" if count else chosen
            self.notice.show(f"已自动选择游戏目录：{label}", "success")
            self.set_status(f"已自动选择目录：{label}", kind="success")
        else:
            self.notice.show("已更新检测结果。", "success")
            self.set_status("游戏目录检测完成", kind="success")

    def _on_detect_error(self, exc: BaseException) -> None:
        self._detect_running = False
        self._detect_button.set_enabled(True)
        self._detect_label.configure(text="检测失败，请手动选择 .minecraft 目录。",
                                     fg=theme.TEXT_MUTED)
        self.notice.show(error_message(exc), "error")
        self.set_status("检测游戏目录失败", kind="error")

    def _render_detection(self, detection: Any) -> None:
        """只列出检测到的游戏目录路径与版本数，不显示启动器品牌名与空行。"""
        if detection is None:
            self._detect_label.configure(
                text="点击“自动检测”查找本机的游戏目录。", fg=theme.TEXT_DIM
            )
            return
        entries: list[tuple[str, int | None]] = []
        seen: set[str] = set()

        def add_path(value: Any, count: int | None = None) -> None:
            if value is None:
                return
            text = str(value).strip()
            if not text:
                return
            key = text.replace("/", "\\").rstrip("\\").lower()
            if key in seen:
                return
            seen.add(key)
            entries.append((text, count))

        # loader-dev 在 DetectionResult 上新增 root_versions: list[(Path, 版本数)]；
        # 旧后端没有该字段时回退到 candidate_roots / probes。
        for item in getattr(detection, "root_versions", None) or []:
            try:
                path, count = item[0], int(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            add_path(path, count)
        for candidate in getattr(detection, "candidate_roots", None) or []:
            add_path(candidate)
        for probe in getattr(detection, "probes", None) or []:
            if bool(getattr(probe, "installed", False)):
                add_path(getattr(probe, "mc_root", None))
        if not entries:
            add_path(getattr(detection, "default_root", None))
        if entries:
            lines = [
                f"{path}（{count} 个版本）" if count else path for path, count in entries
            ]
            text = "\n".join(lines)
            color = theme.TEXT
        else:
            text = "没有找到可用的游戏目录，请点击“浏览…”手动选择。"
            color = theme.TEXT_MUTED
        self._detect_label.configure(text=text, fg=color)

    # ------------------------------------------------------------------
    # 保存 / 重置
    # ------------------------------------------------------------------
    def save(self) -> None:
        config = self._config()
        if config is None:
            self.notice.show("配置对象不可用，无法保存。", "error")
            return
        root_text = self._root_var.get().strip()
        if root_text:
            try:
                Path(root_text)
            except (OSError, ValueError):
                self.notice.show("Minecraft 目录路径无效，请重新选择。", "error")
                return
        java_text = self._java_var.get().strip()
        if java_text and not Path(java_text).exists():
            self.notice.show("Java 路径不存在，请检查后再保存。", "warn")
            return
        threads = self._to_int(self._threads_var.get(), 32, 1, 64)
        remember = self._to_int(self._remember_var.get(), 30, 1, 365)

        try:
            config.mc_root = root_text or None
            config.mirror = self._mirror_value
            config.threads = threads
            config.remember_days = remember
            config.java_path = java_text or None
            sanitize = getattr(config, "sanitize", None)
            if callable(sanitize):
                sanitize()
            saver = getattr(self.services, "save_config", None)
            if callable(saver):
                saver()
            else:  # 兜底：直接调用 AppConfig.save()
                save = getattr(config, "save", None)
                if callable(save):
                    save()
        except Exception as exc:  # noqa: BLE001 - 保存失败提示用户
            log.warning("保存设置失败", exc_info=True)
            self.notice.show(f"保存设置失败：{error_message(exc)}", "error")
            self.set_status("保存设置失败", kind="error")
            return
        self.notice.show("设置已保存。", "success")
        self.set_status("设置已保存", kind="success")

    @staticmethod
    def _to_int(raw: str, default: int, low: int, high: int) -> int:
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def reset_defaults(self) -> None:
        config = self._config()
        self._threads_var.set(str(getattr(config, "threads", 32) or 32))
        self._remember_var.set("30")
        self._mirror_value = "auto"
        try:
            self._mirror_seg.set("auto")
        except Exception:  # noqa: BLE001
            pass
        self._mirror_hint.set(MIRROR_HINTS["auto"])
        self._java_var.set("")
        self._root_var.set(str(default_minecraft_root()))
        self.notice.show("已填入默认值，点击“保存设置”后生效。", "info")
        self.set_status("已恢复默认设置（待保存）")


__all__ = ["SettingsPage"]
