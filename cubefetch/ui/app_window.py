"""CubeFetch 主窗口：顶部顶栏 + 左侧导航 + 页面容器 + 底部状态栏。

对外的三个入口（签名保持不变）：
    AppWindow(master, services)          -- 在已有 Tk 根窗口里搭建界面
    create_app(services=None, config=None) -> (tk.Tk, AppWindow)
    run(services=None)                   -- 创建窗口并进入 mainloop
"""
from __future__ import annotations

import logging
import tkinter as tk
from typing import Any, Callable

from ..config import APP_VERSION, AppConfig
from . import dialogs, dpi, sound, theme
from .pages.done import DonePage
from .pages.download import DownloadPage
from .pages.login import LoginPage
from .pages.progress import ProgressPage
from .pages.settings import SettingsPage
from .widgets import Card, draw_cube_logo, primary_button, secondary_button

log = logging.getLogger("cubefetch.ui.app_window")

APP_TITLE = "CubeFetch 方块速递"

NAV_ITEMS: list[tuple[str, str]] = [
    ("login", "登录"),
    ("download", "下载"),
    ("progress", "进度"),
    ("done", "完成"),
    ("settings", "设置"),
]
NAV_DISABLED = {"progress", "done"}
# 下载流程页面：未登录（没有正版账号）时不得进入。
DOWNLOAD_FLOW_PAGES = frozenset({"download", "progress", "done"})

PAGES: dict[str, type] = {
    "login": LoginPage,
    "download": DownloadPage,
    "progress": ProgressPage,
    "done": DonePage,
    "settings": SettingsPage,
}

STATUS_COLORS = {
    "info": theme.INFO,
    "success": theme.SUCCESS,
    "warn": theme.WARNING,
    "warning": theme.WARNING,
    "error": theme.DANGER,
}


def _norm_path(path: Any) -> str:
    """目录路径归一化（用于 root_versions 与 candidate_roots 对齐）。"""
    try:
        return str(path).replace("/", "\\").rstrip("\\").lower()
    except Exception:  # noqa: BLE001 - 极端对象也不应影响界面
        return str(path).lower()


# ---------------------------------------------------------------------------
# 降级服务对象：仅用于没有后端时预览界面 / 设计调试
# ---------------------------------------------------------------------------
class _FallbackServices:
    """无 services 时的最小替身，保证界面可以打开。"""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.account = None
        self.detection = None
        self.java_exe = None
        self.mirror = None

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)

        def _unavailable(*_args, **_kwargs):
            raise RuntimeError("后端服务尚未就绪，请稍后再试。")

        return _unavailable


# ---------------------------------------------------------------------------
# 左侧导航项（圆角胶囊：选中 = 强调 14% 底 + 强调文字；hover = 白 6% 底）
# ---------------------------------------------------------------------------
class _NavItem(tk.Canvas):
    def __init__(
        self,
        master: tk.Misc,
        key: str,
        label: str,
        command: Callable[[str], None],
        *,
        enabled: bool = True,
    ) -> None:
        super().__init__(master, bg=theme.BG, height=theme.sp(40),
                         highlightthickness=0, bd=0, takefocus=0)
        self.key = key
        self._label = label
        self._command = command
        self._enabled = bool(enabled)
        self._selected = False
        self._hover = False
        self._font = theme.font(theme.FS_BODY, "bold")
        self._font_normal = theme.font(theme.FS_BODY)
        self.bind("<Configure>", lambda _e: self._paint())
        if enabled:
            self.configure(cursor="hand2")
            self.bind("<Enter>", self._on_enter)
            self.bind("<Leave>", self._on_leave)
            self.bind("<Button-1>", self._on_click)

    def _paint(self) -> None:
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 2 else theme.SIDEBAR_WIDTH - theme.PAD_MD
        height = self.winfo_height() if self.winfo_height() > 2 else theme.sp(40)
        if self._selected:
            fill = theme.mix(theme.BG, theme.ACCENT, 0.14)
            fg = theme.mix(theme.TEXT, theme.ACCENT, 0.55)
        elif self._hover and self._enabled:
            fill = theme.mix(theme.BG, theme.WHITE, 0.06)
            fg = theme.TEXT
        else:
            fill = theme.BG
            fg = theme.TEXT_MUTED if self._enabled else theme.TEXT_DIM
        if fill != theme.BG:
            theme.rounded_rect(self, 0, 1, width, height - 1, theme.RADIUS_SM,
                               fill=fill, outline="")
        self.create_text(theme.PAD_MD, height / 2, text=self._label, fill=fg,
                         font=self._font if self._selected else self._font_normal,
                         anchor="w")

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._paint()

    def set_enabled(self, enabled: bool) -> None:
        self._enabled = bool(enabled)
        self._paint()

    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._paint()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._paint()

    def _on_click(self, _event=None) -> None:
        if self._enabled:
            self._command(self.key)


# ---------------------------------------------------------------------------
# 顶栏右侧账号胶囊
# ---------------------------------------------------------------------------
class _AccountPill(tk.Canvas):
    def __init__(self, master: tk.Misc, *, bg: str = theme.BG) -> None:
        super().__init__(master, bg=bg, height=theme.sp(32), width=theme.sp(130),
                         highlightthickness=0, bd=0, takefocus=0)
        self._bg = bg
        self._name = "未登录"
        self._dot_color = theme.TEXT_DIM
        self.bind("<Configure>", lambda _e: self._paint())

    def set_account(self, name: str | None) -> None:
        if name:
            self._name = name
            self._dot_color = theme.SUCCESS
        else:
            self._name = "未登录"
            self._dot_color = theme.TEXT_DIM
        self._paint()

    def _paint(self) -> None:
        self.delete("all")
        width = self.winfo_width() if self.winfo_width() > 2 else theme.sp(130)
        height = self.winfo_height() if self.winfo_height() > 2 else theme.sp(32)
        theme.rounded_rect(self, 0, 1, width, height - 1, theme.RADIUS_SM,
                           fill=theme.SURFACE_ALT, outline=theme.BORDER, width=1)
        r = theme.sp(3)
        dot_x = theme.PAD_SM + r
        self.create_oval(dot_x - r, height / 2 - r, dot_x + r, height / 2 + r,
                         fill=self._dot_color, outline="")
        self.create_text(dot_x + r + theme.PAD_SM, height / 2, text=self._name,
                         fill=theme.TEXT if self._name != "未登录" else theme.TEXT_MUTED,
                         font=theme.font(theme.FS_SMALL), anchor="w")


# ---------------------------------------------------------------------------
# “选择 .minecraft 目录”模态对话框
# ---------------------------------------------------------------------------
class _RootChoiceDialog(tk.Toplevel):
    """检测到多个游戏目录时让用户选择 .minecraft 目录。

    只展示目录路径与版本数，不展示启动器品牌名。
    """

    def __init__(self, app: "AppWindow", detection: Any) -> None:
        super().__init__(app.master)
        self.app = app
        self.detection = detection
        self._closed = False
        self.title("选择 .minecraft 目录")
        self.configure(bg=theme.BG)
        self.resizable(False, False)
        try:
            self.transient(app.master)
        except tk.TclError:  # pragma: no cover
            pass

        candidates = self._collect_candidates(detection)
        default_root = getattr(detection, "default_root", None)
        if not candidates and default_root:
            candidates = [(str(default_root), self._version_count(detection, str(default_root)))]
        if not candidates:
            candidates = [(str(self.app.config.mc_root_path), 0)]

        shell = tk.Frame(self, bg=theme.BG, padx=theme.PAD_XL, pady=theme.PAD_XL)
        shell.pack(fill="both", expand=True)

        header = tk.Frame(shell, bg=theme.BG)
        header.pack(fill="x")
        tk.Label(header, text="检测到多个游戏目录", bg=theme.BG, fg=theme.TEXT,
                 font=theme.bold(theme.FS_TITLE), anchor="w").pack(side="left")
        tk.Label(shell, text="请选择要安装 Minecraft 的 .minecraft 目录；直接关闭窗口将使用默认目录。",
                 bg=theme.BG, fg=theme.TEXT_MUTED, font=theme.font(theme.FS_SMALL),
                 anchor="w", justify="left", wraplength=theme.sp(560)).pack(
            fill="x", pady=(theme.PAD_XS, theme.PAD_LG))

        for path, count in candidates:
            self._build_candidate(shell, path, count)

        footer = tk.Frame(shell, bg=theme.BG)
        footer.pack(fill="x", pady=(theme.PAD_MD, 0))
        if default_root:
            secondary_button(footer, "使用默认目录",
                             lambda: self._choose(str(default_root))).pack(side="left")
        secondary_button(footer, "取消", self._cancel).pack(side="right")

        self.protocol("WM_DELETE_WINDOW", self._cancel)
        self._center(app.master)
        try:
            self.grab_set()
        except tk.TclError:  # pragma: no cover
            pass
        try:
            self.focus_set()
        except tk.TclError:  # pragma: no cover
            pass

    # ---- 构造候选行 ----
    @staticmethod
    def _version_count(detection: Any, path: str) -> int:
        """从 detection.root_versions（若后端已提供）取该目录的版本数。"""
        target = _norm_path(path)
        for item in getattr(detection, "root_versions", None) or []:
            try:
                raw_path, count = item
            except (TypeError, ValueError):
                continue
            if _norm_path(raw_path) == target:
                return max(0, int(count or 0))
        return 0

    def _collect_candidates(self, detection: Any) -> list[tuple[str, int]]:
        """返回 [(目录, 版本数)]；优先用 root_versions，再补 candidate_roots。"""
        candidates: list[tuple[str, int]] = []
        seen: set[str] = set()
        for item in getattr(detection, "root_versions", None) or []:
            try:
                raw_path, count = item
            except (TypeError, ValueError):
                continue
            path = str(raw_path)
            key = _norm_path(path)
            if not path or key in seen:
                continue
            seen.add(key)
            candidates.append((path, max(0, int(count or 0))))
        for root in getattr(detection, "candidate_roots", None) or []:
            path = str(root)
            key = _norm_path(path)
            if not path or key in seen:
                continue
            seen.add(key)
            candidates.append((path, self._version_count(detection, path)))
        return candidates

    def _build_candidate(self, parent: tk.Frame, path: str, count: int = 0) -> None:
        card = Card(parent, bg=theme.SURFACE, border=theme.BORDER, padding=theme.PAD_MD)
        card.pack(fill="x", pady=(0, theme.PAD_SM))
        inner = tk.Frame(card.body, bg=theme.SURFACE)
        inner.pack(fill="x")
        display = f"{path}（{count} 个版本）" if count > 0 else path
        tk.Label(inner, text=display, bg=theme.SURFACE, fg=theme.TEXT,
                 font=theme.font(theme.FS_SMALL, mono=True), anchor="w",
                 justify="left", wraplength=theme.sp(560)).pack(
            side="left", fill="x", expand=True)
        primary_button(inner, "使用此目录", lambda p=path: self._choose(p),
                       width=theme.sp(128)).pack(side="right")

    # ---- 选择 / 关闭 ----
    def _choose(self, path: str) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.app.choose_root(path)
        finally:
            self._destroy()

    def _cancel(self) -> None:
        if self._closed:
            return
        self._closed = True
        default_root = getattr(self.detection, "default_root", None)
        try:
            if default_root:
                self.app.choose_root(str(default_root))
        finally:
            self._destroy()

    def _destroy(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:  # pragma: no cover
            pass
        self.app._root_dialog = None  # type: ignore[attr-defined]
        try:
            self.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def _center(self, master: tk.Misc) -> None:
        self.update_idletasks()
        width = max(theme.sp(600), self.winfo_reqwidth())
        height = max(theme.sp(320), self.winfo_reqheight())
        try:
            x = master.winfo_rootx() + max(0, (master.winfo_width() - width) // 2)
            y = master.winfo_rooty() + max(0, (master.winfo_height() - height) // 2)
        except tk.TclError:  # pragma: no cover
            x, y = 200, 200
        self.geometry(f"{width}x{height}+{x}+{y}")


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class AppWindow:
    """把界面搭建在 master（通常是 tk.Tk）里并管理页面路由。"""

    def __init__(self, master: tk.Tk, services: Any = None, *, config: AppConfig | None = None) -> None:
        self.master = master
        self.services = services if services is not None else _FallbackServices(config)
        self.config = getattr(self.services, "config", None) or config or AppConfig()
        self._pages: dict[str, Any] = {}
        self._current_page = None
        self._current_name = ""
        self._nav_items: dict[str, _NavItem] = {}
        self._status_kind = "info"
        self._close_hooks: list[Callable[[], None]] = []
        self._closing = False
        self._root_dialog: _RootChoiceDialog | None = None
        self._root_dialog_shown = False
        self.selected_version: Any = None
        self.selected_kind: str = "vanilla"
        self.selected_loader_version: str | None = None

        self._build()

    # ------------------------------------------------------------------
    # 构建界面
    # ------------------------------------------------------------------
    def _build(self) -> None:
        self.root_frame = tk.Frame(self.master, bg=theme.BG, bd=0, highlightthickness=0)
        self.root_frame.pack(fill="both", expand=True)

        self._build_header()
        self._build_statusbar()  # 先固定底部，避免被内容挤出
        self._build_body()

    def _build_header(self) -> None:
        header = tk.Frame(self.root_frame, bg=theme.BG, height=theme.HEADER_HEIGHT)
        header.pack(fill="x")
        header.pack_propagate(False)

        left = tk.Frame(header, bg=theme.BG)
        left.pack(side="left", padx=(theme.PAD_XL, 0))
        logo = tk.Canvas(left, width=theme.sp(30), height=theme.sp(30), bg=theme.BG,
                         highlightthickness=0, bd=0)
        logo.pack(side="left", pady=0)
        draw_cube_logo(logo, theme.sp(30))
        tk.Label(left, text=APP_TITLE, bg=theme.BG, fg=theme.TEXT,
                 font=theme.bold(theme.FS_TITLE)).pack(side="left", padx=(theme.PAD_SM, theme.PAD_SM))
        tk.Label(left, text=f"v{APP_VERSION}", bg=theme.SURFACE_ALT, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_TINY, mono=True),
                 padx=theme.sp(8), pady=theme.sp(2)).pack(side="left")

        right = tk.Frame(header, bg=theme.BG)
        right.pack(side="right", padx=(0, theme.PAD_XL))
        self._account_pill = _AccountPill(right, bg=theme.BG)
        self._account_pill.pack(side="right")

        tk.Frame(self.root_frame, height=theme.sp(1), bg=theme.BORDER).pack(fill="x")

    def _build_body(self) -> None:
        body = tk.Frame(self.root_frame, bg=theme.BG)
        body.pack(fill="both", expand=True)

        self.sidebar = tk.Frame(body, bg=theme.BG, width=theme.SIDEBAR_WIDTH)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)

        nav_box = tk.Frame(self.sidebar, bg=theme.BG)
        nav_box.pack(fill="x", padx=theme.PAD_MD, pady=(theme.PAD_MD, 0))
        for key, label in NAV_ITEMS:
            item = _NavItem(nav_box, key, label, self._on_nav_click,
                            enabled=key not in NAV_DISABLED)
            item.pack(fill="x", pady=theme.sp(2))
            self._nav_items[key] = item

        footer = tk.Frame(self.sidebar, bg=theme.BG)
        footer.pack(side="bottom", fill="x", padx=theme.PAD_LG, pady=theme.PAD_LG)
        tk.Frame(footer, height=theme.sp(1), bg=theme.BORDER).pack(fill="x", pady=(0, theme.PAD_SM))
        self._sidebar_info = tk.Label(footer, text="深色模式", bg=theme.BG, fg=theme.TEXT_DIM,
                                      font=theme.font(theme.FS_TINY), anchor="w")
        self._sidebar_info.pack(fill="x")
        tk.Label(footer, text="安装文件保存在本机", bg=theme.BG, fg=theme.TEXT_DIM,
                 font=theme.font(theme.FS_TINY), anchor="w").pack(fill="x")

        tk.Frame(body, width=theme.sp(1), bg=theme.BORDER).pack(side="left", fill="y")

        self.content = tk.Frame(body, bg=theme.BG, bd=0, highlightthickness=0)
        self.content.pack(side="left", fill="both", expand=True)

    def _build_statusbar(self) -> None:
        tk.Frame(self.root_frame, height=theme.sp(1), bg=theme.BORDER).pack(
            fill="x", side="bottom")
        bar = tk.Frame(self.root_frame, bg=theme.BG, height=theme.STATUSBAR_HEIGHT)
        bar.pack(fill="x", side="bottom")
        bar.pack_propagate(False)

        self._status_dot = tk.Canvas(bar, width=theme.sp(8), height=theme.sp(8), bg=theme.BG,
                                     highlightthickness=0, bd=0)
        self._status_dot.pack(side="left", padx=(theme.PAD_XL, theme.PAD_SM))
        self._status_dot.create_oval(theme.sp(1), theme.sp(1), theme.sp(7), theme.sp(7),
                                     fill=theme.INFO, outline="")
        self._status_label = tk.Label(bar, text="就绪", bg=theme.BG, fg=theme.TEXT_MUTED,
                                      font=theme.font(theme.FS_TINY), anchor="w")
        self._status_label.pack(side="left")
        self._info_label = tk.Label(bar, text="", bg=theme.BG, fg=theme.TEXT_DIM,
                                    font=theme.font(theme.FS_TINY, mono=True), anchor="e")
        self._info_label.pack(side="right", padx=(0, theme.PAD_XL))
        self._refresh_info()

    # ------------------------------------------------------------------
    # 启动
    # ------------------------------------------------------------------
    def startup(self) -> None:
        """路由到登录页或下载页（游戏目录已在 bootstrap 中自动选择，无需再弹窗）。"""
        account = getattr(self.services, "account", None)
        if account is not None:
            self._update_account_label(account)
            self.show_page("download")
        else:
            self._update_account_label(None)
            self.show_page("login")

    def show_root_dialog(self, detection: Any) -> None:
        if self._root_dialog is not None:
            try:
                self._root_dialog.lift()
            except tk.TclError:  # pragma: no cover
                pass
            return
        self._root_dialog = _RootChoiceDialog(self, detection)

    def choose_root(self, path: str) -> None:
        """用户（或对话框兜底逻辑）选定 .minecraft 目录。"""
        chooser = getattr(self.services, "choose_root", None)
        if callable(chooser):
            try:
                chooser(path)
                self.set_status(f"已选择 .minecraft 目录：{path}", kind="success")
                self._refresh_info()
                return
            except Exception as exc:  # noqa: BLE001 - 选择目录失败不应打断流程
                log.warning("choose_root 失败: %s", exc, exc_info=True)
                self.set_status(f"保存目录失败：{exc}", kind="error")
                return
        self.config.mc_root = str(path)
        try:
            saver = getattr(self.services, "save_config", None)
            if callable(saver):
                saver()
        except Exception:  # noqa: BLE001
            log.debug("保存目录配置失败", exc_info=True)
        self.set_status(f"已选择 .minecraft 目录：{path}", kind="success")
        self._refresh_info()

    # ------------------------------------------------------------------
    # 页面路由
    # ------------------------------------------------------------------
    @property
    def current_page(self):
        return self._current_page

    @property
    def current_page_name(self) -> str:
        return self._current_name

    def get_page(self, name: str):
        page = self._pages.get(name)
        if page is None:
            cls = PAGES[name]
            page = cls(self.content, self)
            self._pages[name] = page
        return page

    def _ensure_download_access(self, name: str) -> bool:
        """未登录时拦截下载流程页面。

        返回 ``True`` 表示已拦截（调用方应立即返回）；``False`` 表示放行。
        登录页 / 设置页不受限制；``_FallbackServices`` 没有真实账号，
        因此 ``account is None`` 也按未登录处理。
        """
        if name not in DOWNLOAD_FLOW_PAGES:
            return False
        if getattr(self.services, "account", None) is not None:
            return False
        if self._current_name != "login" or self._current_page is None:
            self.show_page("login", notice="请先登录 Microsoft 账号，再开始下载安装。")
        self.set_status("请先登录 Microsoft 账号", kind="warn")
        return True

    def show_page(self, name: str, **kwargs: Any) -> None:
        if name not in PAGES:
            raise ValueError(f"未知页面: {name!r}")
        if self._ensure_download_access(name):
            return
        if self._current_page is not None:
            try:
                self._current_page.on_hide()
            except Exception:  # noqa: BLE001 - 页面切换不应崩溃
                log.warning("页面 %s on_hide 异常", self._current_name, exc_info=True)
            try:
                self._current_page.pause_backdrop()
            except Exception:  # noqa: BLE001
                log.debug("页面 %s 暂停背景动画失败", self._current_name, exc_info=True)
            self._current_page.pack_forget()

        page = self.get_page(name)
        page.pack(fill="both", expand=True)
        self._current_page = page
        self._current_name = name
        self._update_nav(name)
        try:
            page.on_show(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.exception("页面 %s on_show 异常", name)
            self.set_status(f"{page.title} 页面加载失败：{exc}", kind="error")
        try:
            page.resume_backdrop()
        except Exception:  # noqa: BLE001
            log.debug("页面 %s 启动背景动画失败", name, exc_info=True)
        try:
            self.master.title(f"{page.title} · {APP_TITLE}")
        except tk.TclError:  # pragma: no cover
            pass
        self._refresh_info()

    def _update_nav(self, name: str) -> None:
        for key, item in self._nav_items.items():
            item.set_selected(key == name)

    def _on_nav_click(self, key: str) -> None:
        if self._ensure_download_access(key):
            return
        if key in NAV_DISABLED:
            self.set_status("请先完成当前步骤", kind="warn")
            return
        if key == "login" and getattr(self.services, "account", None) is not None:
            self.set_status("当前已登录", kind="success")
            return
        self.show_page(key)

    # ------------------------------------------------------------------
    # 状态 / 账号
    # ------------------------------------------------------------------
    def set_status(self, text: str, kind: str = "info") -> None:
        color = STATUS_COLORS.get(kind, theme.INFO)
        self._status_kind = kind
        if kind in ("warn", "warning", "error"):
            sound.play(kind)
        try:
            self._status_label.configure(text=str(text))
            self._status_dot.delete("all")
            self._status_dot.create_oval(theme.sp(1), theme.sp(1), theme.sp(7),
                                         theme.sp(7), fill=color, outline="")
        except tk.TclError:  # pragma: no cover
            return

    def _refresh_info(self) -> None:
        parts: list[str] = []
        threads = getattr(self.config, "threads", None)
        if threads:
            parts.append(f"线程 {threads}")
        mirror = str(getattr(self.config, "mirror", "") or "")
        if mirror:
            parts.append(f"下载源 {theme.MIRROR_LABELS.get(mirror, mirror)}")
        mc_root = getattr(self.config, "mc_root", None)
        if mc_root:
            parts.append("目录已设置")
        try:
            self._info_label.configure(text=" · ".join(parts))
        except tk.TclError:  # pragma: no cover
            pass

    def _update_account_label(self, account: Any) -> None:
        try:
            if account is None:
                self._account_pill.set_account(None)
            else:
                name = str(getattr(account, "name", "") or "玩家")
                self._account_pill.set_account(name)
        except tk.TclError:  # pragma: no cover
            pass

    def set_account(self, account: Any) -> None:
        self._update_account_label(account)
        if getattr(self.services, "account", None) is None and account is not None:
            try:
                self.services.account = account
            except Exception:  # noqa: BLE001 - 只读属性时忽略
                log.debug("services.account 只读，跳过赋值")

    def logout(self) -> None:
        logout = getattr(self.services, "logout", None)
        if callable(logout):
            try:
                logout()
            except Exception as exc:  # noqa: BLE001
                log.warning("退出登录失败: %s", exc, exc_info=True)
                self.set_status(f"退出登录失败：{exc}", kind="error")
                return
        self._update_account_label(None)
        self.set_status("已退出登录", kind="info")
        self.show_page("login")

    # ------------------------------------------------------------------
    # 关闭
    # ------------------------------------------------------------------
    def add_close_hook(self, hook: Callable[[], None]) -> None:
        self._close_hooks.append(hook)

    def has_running_task(self) -> bool:
        """是否有需要确认的进行中任务（安装 / 下载 / 版本列表刷新）。"""
        for name in ("progress", "download"):
            page = self._pages.get(name)
            if page is None:
                continue
            try:
                if page.is_running:
                    return True
            except Exception:  # noqa: BLE001 - 页面状态异常按无任务处理
                log.debug("%s 页面 is_running 读取失败", name, exc_info=True)
        return False

    def request_close(self) -> bool:
        """WM_DELETE_WINDOW 入口：有进行中任务时先确认，返回是否真正关闭。"""
        if self._closing:
            return True
        if self.has_running_task():
            try:
                confirmed = dialogs.ask_yes_no(
                    "有正在进行的任务",
                    "当前任务尚未完成，关闭窗口后下载进度不会保留。确定要关闭吗？",
                    icon="warning",
                )
            except Exception:  # noqa: BLE001 - 消息框异常时默认不关，避免误杀任务
                log.warning("关闭确认对话框异常", exc_info=True)
                confirmed = False
            if not confirmed:
                self.set_status("已取消关闭：任务仍在进行", kind="info")
                return False
        self.close()
        return True

    def close(self) -> None:
        if self._closing:
            return
        self._closing = True
        for hook in list(self._close_hooks):
            try:
                hook()
            except Exception:  # noqa: BLE001
                log.warning("关闭钩子异常", exc_info=True)
        for page in list(self._pages.values()):
            try:
                page.shutdown()
            except Exception:  # noqa: BLE001
                log.debug("页面 shutdown 异常", exc_info=True)
        closer = getattr(self.services, "shutdown", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                log.debug("services.shutdown 异常", exc_info=True)
        try:
            self.master.destroy()
        except tk.TclError:  # pragma: no cover
            pass


# 兼容任务书里的命名
CubeFetchApp = AppWindow


# ---------------------------------------------------------------------------
# 工厂函数
# ---------------------------------------------------------------------------
def _enable_dpi_awareness() -> None:
    """声明 DPI 感知（保留旧入口，实际实现在 :mod:`cubefetch.ui.dpi`）。"""
    dpi.enable_dpi_awareness()


def _center_geometry(master: tk.Tk, width: int, height: int) -> str:
    try:
        screen_w = master.winfo_screenwidth()
        screen_h = master.winfo_screenheight()
    except tk.TclError:  # pragma: no cover
        return f"{width}x{height}"
    x = max(0, (screen_w - width) // 2)
    y = max(0, (screen_h - height) // 3)
    return f"{width}x{height}+{x}+{y}"


def create_app(services: Any = None, config: AppConfig | None = None) -> tuple[tk.Tk, AppWindow]:
    """创建根窗口和主界面，返回 (root, app)。

    创建 ``tk.Tk()`` 之前先声明 DPI 感知；随后把 Tk 的 scaling 设为
    ``SCALE * 96 / 72``（pt → 物理像素），窗口尺寸 / 最小尺寸用 theme 里
    已按 SCALE 缩放过的常量。
    """
    dpi.enable_dpi_awareness()
    root = tk.Tk()
    root.title(APP_TITLE)
    root.configure(bg=theme.BG)
    try:
        root.tk.call("tk", "scaling", theme.SCALE * 96.0 / 72.0)
    except tk.TclError:  # pragma: no cover
        log.debug("设置 tk scaling 失败", exc_info=True)
    theme.init(root)
    root.geometry(_center_geometry(root, theme.WINDOW_WIDTH, theme.WINDOW_HEIGHT))
    root.minsize(theme.MIN_WIDTH, theme.MIN_HEIGHT)

    app = AppWindow(root, services, config=config)
    root.protocol("WM_DELETE_WINDOW", app.request_close)
    app.startup()
    return root, app


def run(services: Any = None, config: AppConfig | None = None) -> AppWindow:
    """创建窗口并进入 mainloop（供 app.py / 调试入口使用）。"""
    root, app = create_app(services, config)
    root.mainloop()
    return app


__all__ = [
    "APP_TITLE",
    "AppWindow",
    "CubeFetchApp",
    "create_app",
    "run",
]
