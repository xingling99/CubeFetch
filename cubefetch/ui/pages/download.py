"""下载页：游戏版本选择 + 加载器选择合并在一个界面里完成。

用户只在这一页完成全部选择：
    1. 左侧：正式版 / 预览版 / 远古版 / 愚人节版 + 搜索 + 游戏版本列表；
    2. 右侧：原版 / Fabric / Quilt / Forge / NeoForge + 加载器版本下拉；
    3. 底部：“开始下载”。

数据全部来自契约 §11 补充接口，UI 不自己发明接口：
    services.versions_categories(refresh=False) -> dict[str, list[VersionEntry]]
    services.loaders_for(kind, game_version) -> list[dict]      # 每项含 "version"
    services.recommend_loader(kind, game_version) -> str | None
点击“开始下载”后切到进度页，由进度页在工作线程里调用 services.install()。
"""
from __future__ import annotations

import logging
import re
import tkinter as tk
from tkinter import ttk
from typing import Any, Callable

from ...core.errors import AuthError
from ...core.manifest import CATEGORY_APRIL, CATEGORY_OLD, CATEGORY_RELEASE, CATEGORY_SNAPSHOT
from .. import dialogs, theme
from ..widgets import (
    Badge,
    Card,
    EmptyState,
    NoticeBar,
    PageHeader,
    ScrollFrame,
    SegmentedControl,
    Spinner,
    VersionRow,
    bind_recursive,
    ghost_button,
    primary_button,
    secondary_button,
)
from .base import BasePage, error_detail, error_message

log = logging.getLogger("cubefetch.ui.download")

CATEGORY_ORDER = [CATEGORY_RELEASE, CATEGORY_SNAPSHOT, CATEGORY_OLD, CATEGORY_APRIL]
CATEGORY_LABELS = {
    CATEGORY_RELEASE: "正式版",
    CATEGORY_SNAPSHOT: "预览版",
    CATEGORY_OLD: "远古版",
    CATEGORY_APRIL: "愚人节版",
}

KIND_ORDER = ["vanilla", "fabric", "quilt", "forge", "neoforge"]
KIND_LABELS = {
    "vanilla": "原版",
    "fabric": "Fabric",
    "quilt": "Quilt",
    "forge": "Forge",
    "neoforge": "NeoForge",
}
KIND_TAGS = {
    "vanilla": "纯净",
    "fabric": "推荐",
    "quilt": "轻量",
    "forge": "经典",
    "neoforge": "新",
}

PAGE_SIZE = 60
SEARCH_DEBOUNCE_MS = 150
RIGHT_WIDTH = 340

# 每次进程启动后第一次进入下载页时强制联网刷新一次版本列表（之后走缓存）。
_auto_refresh_done = False

# 版本名非法字符（与启动器/Windows 目录命名保持一致）。
_INVALID_VERSION_CHARS = re.compile(r'[\\/:*?"<>|]')


def _date_text(entry) -> str:
    try:
        release_date = getattr(entry, "release_date", None)
        if callable(release_date):
            return release_date().strftime("%Y-%m-%d")
    except (TypeError, ValueError, AttributeError):  # pragma: no cover - 防御坏数据
        pass
    raw = str(getattr(entry, "release_time", "") or "")
    return raw[:10] if len(raw) >= 10 else raw


def _entry_id(entry) -> str:
    return str(getattr(entry, "id", "") or "")


def _version_of(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("version", "") or "")
    return str(item or "")


class _LoaderOption(tk.Canvas):
    """2 列加载器选择卡片（名称 + 小字标签，选中强调色描边 + 淡强调底）。"""

    def __init__(
        self,
        master: tk.Misc,
        kind: str,
        title: str,
        tag: str = "",
        *,
        tag_color: str = theme.SURFACE_ALT,
        tag_fg: str = theme.TEXT_DIM,
        selected: bool = False,
        on_click: Callable[[], None] | None = None,
        bg: str = theme.SURFACE,
    ) -> None:
        super().__init__(master, height=theme.LOADER_CARD_HEIGHT, width=theme.sp(150),
                         bg=bg, highlightthickness=0, bd=0, cursor="hand2")
        self.kind = kind
        self._bg = bg
        self._selected = bool(selected)
        self._hover = False
        self._on_click = on_click
        self._title = title
        self._tag = tag
        self.bind("<Configure>", lambda _e: self._redraw())
        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_press)

    # ---- 对外 API（与旧实现保持一致） ----
    def invoke(self) -> None:
        if self._on_click is not None:
            self._on_click()

    def set_selected(self, selected: bool) -> None:
        self._selected = bool(selected)
        self._redraw()

    # ---- 交互 ----
    def _on_enter(self, _event=None) -> None:
        self._hover = True
        self._redraw()

    def _on_leave(self, _event=None) -> None:
        self._hover = False
        self._redraw()

    def _on_press(self, _event=None) -> None:
        self.invoke()

    # ---- 绘制 ----
    def _redraw(self) -> None:
        try:
            width = max(theme.sp(120), self.winfo_width())
            height = theme.sp(theme.LOADER_CARD_HEIGHT)
        except tk.TclError:  # pragma: no cover
            return
        self.delete("all")
        self.configure(height=height)
        if self._selected:
            fill = theme.mix(self._bg, theme.ACCENT, 0.12)
            outline = theme.ACCENT
        elif self._hover:
            fill = theme.SURFACE_HOVER
            outline = theme.BORDER_LIGHT
        else:
            fill = theme.SURFACE_ALT
            outline = theme.BORDER
        theme.rounded_rect(self, 0.5, 0.5, width - 0.5, height - 0.5,
                           theme.RADIUS_MD, fill=fill, outline=outline)
        pad = theme.PAD_MD
        self.create_text(pad, theme.sp(20), text=self._title, anchor="w",
                         fill=theme.TEXT, font=theme.font(theme.FS_BODY, "bold"))
        if self._tag:
            tag_fg = theme.ACCENT if self._selected else theme.TEXT_DIM
            self.create_text(pad, height - theme.sp(17), text=self._tag, anchor="w",
                             fill=tag_fg, font=theme.font(theme.FS_TINY))
        if self._selected:
            cx, cy = width - theme.sp(16), theme.sp(16)
            self.create_oval(cx - theme.sp(4), cy - theme.sp(4),
                             cx + theme.sp(4), cy + theme.sp(4),
                             fill=theme.ACCENT, outline="")


class DownloadPage(BasePage):
    """版本 + 加载器合并后的下载页。"""

    title = "下载"
    nav_key = "download"

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------
    def build(self) -> None:
        self._data: dict[str, list[Any]] = {}
        self._rows: dict[str, VersionRow] = {}
        self._selected: Any = None
        self._category = CATEGORY_RELEASE
        self._query = ""
        self._page = 1
        self._search_after: str | None = None
        self._loading = False
        self._loaded_once = False

        # 加载器侧状态
        self._kind = "vanilla"
        self._loader_version: str | None = None
        self._loader_versions: list[str] = []
        self._loader_recommend: str | None = None
        self._loader_loading: tuple[str, str] | None = None
        self._loader_errors: dict[tuple[str, str], str] = {}
        self._cache: dict[tuple[str, str], tuple[list[str], str | None]] = {}
        self._combo_map: dict[str, str] = {}
        self._option_rows: dict[str, _LoaderOption] = {}

        self._build_header()
        self._build_toolbar()
        self._build_body()
        self._build_footer()
        self._sync_loader_control()

    def _build_header(self) -> None:
        header = tk.Frame(self, bg=theme.BG)
        header.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_LG, 0))
        self.page_header = PageHeader(header, "下载游戏", "选择游戏版本与加载器，一键安装")
        self.page_header.pack(side="left", fill="x", expand=True)
        self._refresh_button = secondary_button(
            header,
            "刷新列表",
            self._on_refresh_clicked,
            height=theme.BUTTON_HEIGHT_SM,
            bg=theme.BG,
        )
        self._refresh_button.pack(side="right")

        self.notice = NoticeBar(self)
        self.notice.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_MD, 0))
        self.notice.hide()

    def _build_toolbar(self) -> None:
        toolbar = tk.Frame(self, bg=theme.BG)
        toolbar.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(theme.PAD_MD, theme.PAD_SM))
        self._segments = SegmentedControl(
            toolbar,
            [(CATEGORY_LABELS[key], key) for key in CATEGORY_ORDER],
            value=self._category,
            on_change=self._on_category_change,
            bg=theme.BG,
        )
        self._segments.pack(side="left")

        search_box = tk.Frame(toolbar, bg=theme.BG)
        search_box.pack(side="right")
        tk.Label(
            search_box,
            text="搜索",
            bg=theme.BG,
            fg=theme.TEXT_DIM,
            font=theme.font(theme.FS_SMALL),
        ).pack(side="left", padx=(0, theme.PAD_SM))
        self._search_var = tk.StringVar()
        self._search_entry = ttk.Entry(
            search_box,
            textvariable=self._search_var,
            width=16,
            style="Dark.TEntry",
        )
        self._search_entry.pack(side="left", ipady=theme.sp(5))
        self._search_entry.bind("<KeyRelease>", self._on_search_key)
        self._search_entry.bind("<Return>", lambda _e: self._apply_search_now())
        self._clear_button = ghost_button(
            search_box, "清除", self._clear_search, height=theme.BUTTON_HEIGHT_SM, bg=theme.BG
        )
        self._clear_button.pack(side="left", padx=(theme.PAD_SM, 0))
        self._clear_button.set_enabled(False)

    def _build_body(self) -> None:
        body = tk.Frame(self, bg=theme.BG)
        body.pack(fill="both", expand=True, padx=theme.CONTENT_PADDING, pady=(0, theme.PAD_SM))
        body.grid_columnconfigure(0, weight=1)
        body.grid_columnconfigure(1, weight=0, minsize=theme.sp(RIGHT_WIDTH))
        body.grid_rowconfigure(0, weight=1)

        # 左侧：游戏版本列表
        left = tk.Frame(body, bg=theme.BG)
        left.grid(row=0, column=0, sticky="nsew")
        self._list_card = Card(left, padding=theme.PAD_SM)
        self._list_card.pack(fill="both", expand=True)
        self._scroll = ScrollFrame(self._list_card.body, bg=theme.SURFACE)
        self._scroll.pack(fill="both", expand=True)

        self._state_holder = tk.Frame(self._list_card.body, bg=theme.SURFACE)
        self._state = EmptyState(self._state_holder, bg=theme.SURFACE)
        self._state.pack(fill="both", expand=True)
        self._retry_button = self._state.add_action("重新加载", lambda: self._load(refresh=True))

        self._loading_holder = tk.Frame(self._state_holder, bg=theme.SURFACE)
        loading_wrap = tk.Frame(self._loading_holder, bg=theme.SURFACE)
        loading_wrap.pack(expand=True)
        self._spinner = Spinner(loading_wrap, size=26, bg=theme.SURFACE)
        self._spinner.pack(pady=(0, theme.PAD_MD))
        self._loading_label = tk.Label(
            loading_wrap,
            text="正在获取版本列表…",
            bg=theme.SURFACE,
            fg=theme.TEXT_MUTED,
            font=theme.font(theme.FS_BODY),
        )
        self._loading_label.pack()

        # 右侧：加载器选择（组合区固定可见，选项区可滚动）
        right = tk.Frame(body, bg=theme.BG)
        right.grid(row=0, column=1, sticky="nsew", padx=(theme.PAD_LG, 0))
        self._panel = Card(right, padding=theme.PAD_LG)
        self._panel.pack(fill="both", expand=True)
        self._build_loader_panel(self._panel.body)

    def _build_loader_panel(self, parent: tk.Frame) -> None:
        head = tk.Frame(parent, bg=theme.SURFACE)
        head.pack(fill="x", pady=(0, theme.PAD_SM))
        tk.Label(head, text="加载器", bg=theme.SURFACE, fg=theme.TEXT,
                 font=theme.font(theme.FS_TITLE, "bold"), anchor="w").pack(side="left")
        self._loader_refresh_button = ghost_button(
            head, "刷新版本", self._refresh_loader_versions,
            height=theme.BUTTON_HEIGHT_SM, bg=theme.SURFACE,
        )
        self._loader_refresh_button.pack(side="right")
        self._loader_refresh_button.set_enabled(False)
        self._loader_spinner = Spinner(head, size=14, bg=theme.SURFACE)
        self._loader_spinner_visible = False

        # 底部固定区先用 side="bottom" 由下往上排，保证加载器版本下拉始终可见
        self._loader_hint = tk.Label(
            parent,
            text="",
            bg=theme.SURFACE,
            fg=theme.TEXT_DIM,
            font=theme.font(theme.FS_TINY),
            justify="left",
            anchor="w",
            wraplength=theme.sp(RIGHT_WIDTH) - theme.sp(56),
        )
        self._loader_hint.pack(side="bottom", fill="x", pady=(0, theme.PAD_XS))

        self._combo_var = tk.StringVar(value="")
        self._combo = ttk.Combobox(
            parent,
            textvariable=self._combo_var,
            state="disabled",
            style="Dark.TCombobox",
            width=18,
        )
        self._combo.pack(side="bottom", fill="x", pady=(0, theme.PAD_SM))
        self._combo.bind("<<ComboboxSelected>>", self._on_combo_selected)

        version_head = tk.Frame(parent, bg=theme.SURFACE)
        version_head.pack(side="bottom", fill="x", pady=(0, theme.PAD_XS))
        tk.Label(
            version_head,
            text="加载器版本",
            bg=theme.SURFACE,
            fg=theme.TEXT_MUTED,
            font=theme.font(theme.FS_SMALL),
            anchor="w",
        ).pack(side="left")
        self._game_label = tk.Label(
            version_head,
            text="",
            bg=theme.SURFACE,
            fg=theme.TEXT_DIM,
            font=theme.font(theme.FS_TINY, mono=True),
            anchor="w",
        )
        self._game_label.pack(side="left", padx=(theme.PAD_SM, 0))

        tk.Frame(parent, height=theme.sp(1), bg=theme.BORDER).pack(
            side="bottom", fill="x", pady=theme.PAD_SM)

        # 2 列选择卡片（可滚动看到全部 5 个）
        options_scroll = ScrollFrame(parent, bg=theme.SURFACE)
        options_scroll.pack(fill="both", expand=True)
        options = options_scroll.body
        options.grid_columnconfigure(0, weight=1)
        options.grid_columnconfigure(1, weight=1)
        for index, kind in enumerate(KIND_ORDER):
            card = _LoaderOption(
                options,
                kind,
                KIND_LABELS[kind],
                KIND_TAGS.get(kind, ""),
                selected=kind == self._kind,
                on_click=lambda k=kind: self._select_kind(k),
                bg=theme.SURFACE,
            )
            card.grid(row=index // 2, column=index % 2, sticky="nsew",
                      padx=theme.sp(4), pady=theme.sp(4))
            self._option_rows[kind] = card

    def _build_footer(self) -> None:
        footer = tk.Frame(self, bg=theme.BG)
        footer.pack(fill="x", padx=theme.CONTENT_PADDING, pady=(0, theme.PAD_LG))
        tk.Frame(footer, height=theme.sp(1), bg=theme.BORDER).pack(
            fill="x", pady=(0, theme.PAD_MD))
        bar = tk.Frame(footer, bg=theme.BG)
        bar.pack(fill="x")
        self._summary_label = tk.Label(
            bar,
            text="尚未选择游戏版本",
            bg=theme.BG,
            fg=theme.TEXT_MUTED,
            font=theme.font(theme.FS_BODY),
            anchor="w",
        )
        self._summary_label.pack(side="left")
        self._start_button = primary_button(bar, "开始下载", self._start, width=160, bg=theme.BG)
        self._start_button.pack(side="right")
        self._start_button.set_enabled(False)

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def on_show(self, **kwargs) -> None:
        if kwargs.get("version") is not None:
            self._selected = kwargs["version"]
        notice = kwargs.get("notice")
        if notice:
            self.notice.show(str(notice), kwargs.get("notice_kind", "error"))
        elif not self._loading:
            self.notice.hide()
        if not self._data and not self._loading:
            # 首次进入下载页强制联网刷新；「刷新列表」按钮同样 refresh=True。
            force_refresh = not _auto_refresh_done or bool(kwargs.get("refresh", False))
            self._load(refresh=force_refresh)
        else:
            self._render()
        self._sync_panel()
        self._update_footer()
        if not self._loading:
            self.set_status(self._status_text())

    def on_hide(self) -> None:
        if self._loading or self._loader_loading is not None:
            self.cancel_task()

    def shutdown(self) -> None:
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._search_after = None
        super().shutdown()

    # ------------------------------------------------------------------
    # 版本列表加载
    # ------------------------------------------------------------------
    def _on_refresh_clicked(self) -> None:
        if self._loader_loading is not None:
            self.set_status("正在获取加载器版本，请稍候…", kind="warn")
            return
        self._load(refresh=True)

    def _load(self, refresh: bool = False) -> None:
        if self._loading:
            return
        if self._loader_loading is not None:
            # 版本列表刷新会占用页面唯一的任务槽，先取消加载器请求。
            self.cancel_task()
            self._loader_loading = None
            self._loader_spinner.stop()
        self._loading = True
        self.notice.hide()
        self._refresh_button.set_enabled(False)
        self._segments.set_enabled(False)
        if not self._data:
            self._show_loading(True)
        self.set_status("正在刷新版本列表…" if refresh else "正在加载版本列表…")

        def work():
            return self.services.versions_categories(refresh=refresh)

        self.start_task(
            work,
            on_success=self._on_loaded,
            on_error=self._on_load_error,
            name="versions",
        )

    def _on_loaded(self, data: dict[str, list[Any]]) -> None:
        global _auto_refresh_done
        _auto_refresh_done = True
        self._loading = False
        self._loaded_once = True
        self._data = dict(data or {})
        for category in CATEGORY_ORDER:
            self._data.setdefault(category, [])
        self._show_loading(False)
        self._refresh_button.set_enabled(True)
        self._segments.set_enabled(True)
        self.notice.hide()
        self._page = 1
        self._render()
        total = sum(len(items) for items in self._data.values())
        self.set_status(f"已加载 {total} 个版本", kind="success")
        # 如果当前选择还在列表里，顺带补拉加载器版本
        if self._selected is not None and self._kind != "vanilla":
            key = (_entry_id(self._selected), self._kind)
            if key not in self._cache:
                self._request_loader_versions(self._kind, _entry_id(self._selected))

    def _on_load_error(self, exc: BaseException) -> None:
        self._loading = False
        self._refresh_button.set_enabled(True)
        self._segments.set_enabled(True)
        self._show_loading(False)
        log.info("版本列表加载失败: %s", exc)
        if self.is_cancelled(exc):
            self.set_status("已取消加载", kind="warn")
            return
        if isinstance(exc, AuthError):
            self.set_status("登录已过期，请重新登录", kind="warn")
            self.app.show_page("login", notice="登录已过期，请重新登录 Microsoft 账号。")  # type: ignore[attr-defined]
            return
        message = error_message(exc)
        detail = error_detail(exc)
        if detail:
            message = f"{message}（{detail}）"
        self.notice.show(message, "error")
        if self._data:
            self._show_state_only(False)
            self._render()
        else:
            self._state.set_glyph("alert")
            self._state.set("版本列表加载失败", "请检查网络连接后重试；也可稍后刷新列表。")
            self._show_state_only(True)
        self.set_status("版本列表加载失败", kind="error")

    # ------------------------------------------------------------------
    # 版本列表渲染
    # ------------------------------------------------------------------
    def _render(self) -> None:
        self._rows.clear()
        self._scroll.clear()
        entries = self._filtered_entries()
        total = len(entries)
        shown = min(total, self._page * PAGE_SIZE)
        if total == 0:
            self._show_state_only(True)
            if self._query:
                self._retry_button.pack_forget()
                self._state.set_glyph("search")
                self._state.set("没有匹配的版本", f"没有找到与“{self._query}”匹配的版本，换个关键词试试。")
            else:
                self._retry_button.pack()
                self._state.set_glyph("box")
                self._state.set("这个分类暂时没有版本", "可以点击下方按钮刷新列表后重试。")
            self._update_header(0, 0)
            return
        self._show_state_only(False)
        selected_id = _entry_id(self._selected) if self._selected is not None else ""
        for entry in entries[:shown]:
            version_id = _entry_id(entry)
            row = VersionRow(
                self._scroll.body,
                version_id,
                getattr(entry, "category", self._category),
                _date_text(entry),
                selected=version_id == selected_id,
                on_click=self._make_row_handler(entry),
                bg=theme.SURFACE,
            )
            row.pack(fill="x", pady=(0, 2))
            self._rows[version_id] = row
        if shown < total:
            more = secondary_button(
                self._scroll.body,
                f"显示更多（剩余 {total - shown} 个）",
                self._show_more,
                bg=theme.SURFACE,
                height=theme.BUTTON_HEIGHT_SM,
            )
            more.pack(pady=theme.PAD_SM)
        self._scroll.scroll_to_top()
        self._update_header(shown, total)
        self.set_status(self._status_text())

    def _filtered_entries(self) -> list[Any]:
        query = self._query.strip().lower()
        if query:
            result: list[Any] = []
            for category in CATEGORY_ORDER:
                for entry in self._data.get(category, []):
                    if query in _entry_id(entry).lower():
                        result.append(entry)
            result.sort(key=lambda item: str(getattr(item, "release_time", "")), reverse=True)
            return result
        return list(self._data.get(self._category, []))

    def _make_row_handler(self, entry):
        def handler() -> None:
            self._select(entry)

        return handler

    def _select(self, entry) -> None:
        self._selected = entry
        version_id = _entry_id(entry)
        for row_id, row in self._rows.items():
            row.set_selected(row_id == version_id)
        try:
            self.app.selected_version = entry  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 只读属性时忽略
            pass
        self._sync_panel()
        self._update_footer()
        self.set_status(f"已选择游戏版本 {version_id}")

    def _show_more(self) -> None:
        self._page += 1
        self._render()

    # ------------------------------------------------------------------
    # 分类 / 搜索
    # ------------------------------------------------------------------
    def _on_category_change(self, value: str) -> None:
        self._category = value
        self._page = 1
        if self._query:
            if self._search_after is not None:
                try:
                    self.after_cancel(self._search_after)
                except (tk.TclError, ValueError):  # pragma: no cover
                    pass
                self._search_after = None
            self._query = ""
            self._search_var.set("")
            self._clear_button.set_enabled(False)
        self._render()

    def _on_search_key(self, event: tk.Event) -> None:
        if event.keysym in ("Return", "Escape"):
            if event.keysym == "Escape":
                self._clear_search()
            return
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
        self._search_after = self.after(SEARCH_DEBOUNCE_MS, self._apply_search_now)

    def _apply_search_now(self) -> None:
        self._search_after = None
        query = self._search_var.get().strip()
        if query == self._query:
            return
        self._query = query
        self._clear_button.set_enabled(bool(query))
        self._page = 1
        self._render()

    def _clear_search(self) -> None:
        if self._search_after is not None:
            try:
                self.after_cancel(self._search_after)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._search_after = None
        self._search_var.set("")
        self._query = ""
        self._clear_button.set_enabled(False)
        self._page = 1
        self._render()

    # ------------------------------------------------------------------
    # 加载器选择
    # ------------------------------------------------------------------
    def _select_kind(self, kind: str) -> None:
        if kind not in KIND_ORDER:
            return
        if kind != "vanilla" and self._selected is None:
            self.notice.show("请先在左侧选择一个 Minecraft 游戏版本。", "warn")
            self.set_status("请先选择游戏版本", kind="warn")
            return
        if self._loader_loading is not None:
            self.cancel_task()
            self._loader_loading = None
            self._loader_spinner.stop()
        self._kind = kind
        self._loader_version = None
        self._loader_versions = []
        self._loader_recommend = None
        for key, row in self._option_rows.items():
            row.set_selected(key == kind)
        if kind == "vanilla":
            self.notice.hide()
            self._sync_panel()
            self._update_footer()
            self.set_status("已选择原版（无加载器）")
            return
        version_id = _entry_id(self._selected)
        key = (version_id, kind)
        cached = self._cache.get(key)
        if cached is not None:
            self._apply_loader_versions(version_id, kind, cached[0], cached[1])
            return
        self._request_loader_versions(kind, version_id)

    def _request_loader_versions(self, kind: str, version_id: str) -> None:
        if not version_id:
            return
        if self._loading:
            # 版本列表还在加载，等它完成后再拉加载器版本。
            return
        self._loader_loading = (version_id, kind)
        self._loader_versions = []
        self._loader_recommend = None
        self._sync_loader_control()
        self.set_status(f"正在获取 {KIND_LABELS[kind]} 版本列表…")

        def work():
            raw_versions = self.services.loaders_for(kind, version_id)
            versions = [item for item in (_version_of(entry) for entry in (raw_versions or [])) if item]
            try:
                recommended = self.services.recommend_loader(kind, version_id)
            except Exception:  # noqa: BLE001 - 推荐失败不影响列表展示
                log.warning("获取加载器推荐版本失败: %s %s", kind, version_id, exc_info=True)
                recommended = None
            return version_id, kind, versions, recommended

        self.start_task(
            work,
            on_success=self._on_loader_versions,
            on_error=lambda exc: self._on_loader_error(kind, version_id, exc),
            name="loaders",
        )

    def _on_loader_versions(self, payload) -> None:
        version_id, kind, versions, recommended = payload
        self._loader_loading = None
        self._loader_spinner.stop()
        self._cache[(version_id, kind)] = (list(versions), recommended)
        if self._selected is None or _entry_id(self._selected) != version_id or self._kind != kind:
            return
        self._apply_loader_versions(version_id, kind, versions, recommended)

    def _apply_loader_versions(self, version_id: str, kind: str, versions: list[str], recommended: str | None) -> None:
        self._loader_versions = list(versions)
        self._loader_recommend = recommended
        if not versions:
            self._loader_version = None
        elif self._loader_version not in versions:
            self._loader_version = recommended if recommended in versions else versions[0]
        self._loader_errors.pop((version_id, kind), None)
        self._sync_loader_control()
        self._update_footer()
        self.set_status(
            f"{KIND_LABELS[kind]} 共 {len(versions)} 个版本"
            if versions
            else f"该游戏版本没有可用的 {KIND_LABELS[kind]} 版本",
            kind="success" if versions else "warn",
        )

    def _on_loader_error(self, kind: str, version_id: str, exc: BaseException) -> None:
        if self._loader_loading == (version_id, kind):
            self._loader_loading = None
        self._loader_spinner.stop()
        log.info("加载器版本列表获取失败 (%s %s): %s", kind, version_id, exc)
        if self.is_cancelled(exc):
            self.set_status("已取消获取加载器版本", kind="warn")
            self._sync_loader_control()
            return
        if isinstance(exc, AuthError):
            self.set_status("登录已过期，请重新登录", kind="warn")
            self.app.show_page("login", notice="登录已过期，请重新登录 Microsoft 账号。")  # type: ignore[attr-defined]
            return
        message = error_message(exc)
        detail = error_detail(exc)
        if detail:
            message = f"{message}（{detail}）"
        self._loader_errors[(version_id, kind)] = message
        if self._selected is not None and _entry_id(self._selected) == version_id and self._kind == kind:
            self.notice.show(message, "error")
            self._sync_loader_control()
            self._update_footer()
            self.set_status(f"{KIND_LABELS[kind]} 版本列表获取失败", kind="error")

    def _refresh_loader_versions(self) -> None:
        if self._kind == "vanilla" or self._selected is None:
            return
        if self._loader_loading is not None or self._loading:
            return
        version_id = _entry_id(self._selected)
        self._cache.pop((version_id, self._kind), None)
        self._loader_errors.pop((version_id, self._kind), None)
        self._request_loader_versions(self._kind, version_id)

    def _on_combo_selected(self, _event=None) -> None:
        raw = self._combo_map.get(self._combo_var.get())
        if raw is None:
            return
        self._loader_version = raw
        self._update_footer()
        self.set_status(f"已选择 {KIND_LABELS.get(self._kind, self._kind)} {raw}")

    # ------------------------------------------------------------------
    # 右侧面板状态
    # ------------------------------------------------------------------
    def _sync_panel(self) -> None:
        for kind, row in self._option_rows.items():
            row.set_selected(kind == self._kind)
        if self._selected is None:
            self._game_label.configure(text="", fg=theme.TEXT_DIM)
        else:
            self._game_label.configure(text=f"· 游戏 {_entry_id(self._selected)}", fg=theme.TEXT_DIM)
        self._sync_loader_control()

    def _available_loader_versions(self) -> list[str]:
        if self._selected is None:
            return []
        if self._loader_loading == (_entry_id(self._selected), self._kind):
            return []
        if self._loader_versions:
            return self._loader_versions
        cached = self._cache.get((_entry_id(self._selected), self._kind))
        return list(cached[0]) if cached else []

    def _sync_loader_control(self) -> None:
        kind = self._kind
        kind_label = KIND_LABELS.get(kind, kind)
        spinner_visible = False
        if self._selected is None:
            self._set_combo([], "请先选择游戏版本", enabled=False)
            self._loader_hint.configure(text="先在左侧选择一个游戏版本，再挑选加载器。", fg=theme.TEXT_DIM)
            self._loader_refresh_button.set_enabled(False)
        elif kind == "vanilla":
            self._set_combo([], "无需加载器版本", enabled=False)
            self._loader_hint.configure(text="原版无需加载器版本。", fg=theme.TEXT_DIM)
            self._loader_refresh_button.set_enabled(False)
        elif self._loader_loading == (_entry_id(self._selected), kind):
            self._set_combo([], "正在获取…", enabled=False)
            self._loader_hint.configure(text=f"正在获取 {kind_label} 版本列表…", fg=theme.TEXT_MUTED)
            self._loader_refresh_button.set_enabled(False)
            spinner_visible = True
        else:
            error = self._loader_errors.get((_entry_id(self._selected), kind))
            versions = self._available_loader_versions()
            if error and not versions:
                self._set_combo([], "获取失败", enabled=False)
                self._loader_hint.configure(text=f"{kind_label} 版本获取失败，可点“刷新”重试。", fg=theme.DANGER)
                self._loader_refresh_button.set_enabled(True)
            elif versions:
                recommended = self._loader_recommend
                cached = self._cache.get((_entry_id(self._selected), kind))
                if cached is not None:
                    recommended = cached[1]
                displays: list[str] = []
                mapping: dict[str, str] = {}
                for index, version in enumerate(versions):
                    marks: list[str] = []
                    if version == recommended:
                        marks.append("推荐")
                    if index == 0:
                        marks.append("最新")
                    display = f"{version}（{' · '.join(marks)}）" if marks else version
                    displays.append(display)
                    mapping[display] = version
                self._combo_map = mapping
                if self._loader_version not in versions:
                    if recommended in versions:
                        self._loader_version = recommended
                    else:
                        self._loader_version = versions[0]
                selected_display = next(
                    (display for display, raw in mapping.items() if raw == self._loader_version), versions[0]
                )
                self._set_combo(displays, selected_display, enabled=True)
                if recommended:
                    self._loader_hint.configure(
                        text=f"已自动选中推荐版本 {recommended}。",
                        fg=theme.TEXT_DIM,
                    )
                else:
                    self._loader_hint.configure(
                        text=f"共 {len(versions)} 个版本，默认选中最新的。", fg=theme.TEXT_DIM
                    )
                self._loader_refresh_button.set_enabled(True)
            else:
                self._set_combo([], "暂无可用版本", enabled=False)
                self._loader_hint.configure(
                    text=f"没有可用的 {kind_label} 版本，可换游戏版本或选原版。",
                    fg=theme.WARNING,
                )
                self._loader_refresh_button.set_enabled(True)

        if spinner_visible:
            if not self._loader_spinner_visible:
                self._loader_spinner.pack(side="right", padx=(theme.PAD_SM, 0))
                self._loader_spinner_visible = True
            self._loader_spinner.start()
        else:
            self._loader_spinner.stop()
            if self._loader_spinner_visible:
                self._loader_spinner.pack_forget()
                self._loader_spinner_visible = False

        if kind != "vanilla" and self._selected is not None:
            start_ready = bool(self._loader_version) and self._loader_loading is None
        else:
            start_ready = self._selected is not None
        self._start_button.set_enabled(start_ready)

    def _set_combo(self, values: list[str], current: str, *, enabled: bool) -> None:
        try:
            self._combo.configure(values=values)
            self._combo_var.set(current)
            self._combo.configure(state="readonly" if enabled else "disabled")
        except tk.TclError:  # pragma: no cover - 控件销毁中
            pass

    # ------------------------------------------------------------------
    # 底部摘要 / 开始下载
    # ------------------------------------------------------------------
    def _update_footer(self) -> None:
        if self._selected is None:
            self._summary_label.configure(text="尚未选择游戏版本", fg=theme.TEXT_MUTED)
            self._start_button.set_enabled(False)
            return
        version_id = _entry_id(self._selected)
        kind_label = KIND_LABELS.get(self._kind, self._kind)
        if self._kind == "vanilla":
            self._summary_label.configure(text=f"当前选择：{version_id} · 原版（无加载器）", fg=theme.TEXT)
            self._start_button.set_enabled(self._loader_loading is None)
        elif self._loader_version:
            self._summary_label.configure(
                text=f"当前选择：{version_id} · {kind_label} {self._loader_version}", fg=theme.TEXT
            )
            self._start_button.set_enabled(self._loader_loading is None)
        else:
            self._summary_label.configure(
                text=f"当前选择：{version_id} · {kind_label}（请选择加载器版本）", fg=theme.TEXT_MUTED
            )
            self._start_button.set_enabled(False)

    def _java_required(self, version_id: str) -> dict[str, Any]:
        """读取 Java 状态（不联网）。检查失败时按“无需下载”处理，不阻断安装。"""
        getter = getattr(self.services, "java_status", None)
        if not callable(getter):
            return {}
        try:
            status = getter(version_id)
        except Exception as exc:  # noqa: BLE001 - Java 检查失败不应阻断下载
            log.warning("Java 状态检查失败: %s", exc, exc_info=True)
            return {}
        return status if isinstance(status, dict) else {}

    def _ask_new_version_name(self, existing: str) -> tuple[bool, str | None]:
        """已存在同名版本时询问新名字。返回 (是否继续, 新名字 | None)。"""
        prompt = (
            f"检测到版本 {existing} 已存在（可能被重命名过）。\n"
            "请输入新的版本名；留空则使用现有版本。"
        )
        parent = self.winfo_toplevel()
        value = dialogs.ask_string("版本名称已存在", prompt, initialvalue="", parent=parent)
        if value is None:
            return False, None
        name = value.strip()
        while name and _INVALID_VERSION_CHARS.search(name):
            dialogs.show_warning(
                "版本名称无效",
                '版本名不能包含以下字符：\\ / : * ? " < > |，也不能全是空白。',
                parent=parent,
            )
            value = dialogs.ask_string("版本名称已存在", prompt, initialvalue="", parent=parent)
            if value is None:
                return False, None
            name = value.strip()
        return True, (name or None)

    def _start(self) -> None:
        if self._selected is None:
            self.notice.show("请先选择一个 Minecraft 游戏版本。", "warn")
            return
        if self._kind != "vanilla" and not self._loader_version:
            self.notice.show("请先选择一个加载器版本。", "warn")
            return
        version_id = _entry_id(self._selected)

        # 1) Java 检查：缺少所需 Java 时询问是否下载官方运行时。
        download_java = False
        java_status = self._java_required(version_id)
        if java_status.get("need_download"):
            major = java_status.get("required_major") or 17
            confirmed = dialogs.ask_yes_no(
                "需要 Java 运行时",
                f"未检测到 Minecraft {version_id} 需要的 Java {major}，是否现在下载官方 Java 运行时？"
                "（约 100MB，安装到 .minecraft\\runtime）",
                icon="warning",
                parent=self.winfo_toplevel(),
            )
            if confirmed:
                download_java = True
            else:
                dialogs.show_info(
                    "跳过 Java 运行时下载",
                    "已跳过官方 Java 运行时下载。若安装 Forge / NeoForge 时本机缺少可用的 Java，"
                    "安装可能失败，可稍后重新开始安装并选择下载 Java。",
                    parent=self.winfo_toplevel(),
                )

        # 2) 已存在版本检查：可能被重命名过，询问新版本名。
        custom_version_name: str | None = None
        find_existing = getattr(self.services, "existing_version", None)
        if callable(find_existing):
            try:
                existing = find_existing(self._kind, self._loader_version, version_id)
            except Exception as exc:  # noqa: BLE001 - 检查失败不阻断安装
                log.warning("检查已存在版本失败: %s", exc, exc_info=True)
                existing = None
            if existing:
                proceed, custom_version_name = self._ask_new_version_name(str(existing))
                if not proceed:
                    self.set_status("已取消下载", kind="info")
                    return

        self.set_status(f"准备安装 {version_id}（{KIND_LABELS.get(self._kind, self._kind)}）", kind="success")
        try:
            self.app.selected_version = self._selected  # type: ignore[attr-defined]
            self.app.selected_kind = self._kind  # type: ignore[attr-defined]
            self.app.selected_loader_version = self._loader_version  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - 只读属性时忽略
            pass
        self.app.show_page(  # type: ignore[attr-defined]
            "progress",
            version=self._selected,
            kind=self._kind,
            loader_version=self._loader_version,
            download_java=download_java,
            custom_version_name=custom_version_name,
        )

    # ------------------------------------------------------------------
    # 界面状态
    # ------------------------------------------------------------------
    def _show_loading(self, show: bool) -> None:
        if show:
            self._scroll.pack_forget()
            self._state_holder.pack(fill="both", expand=True)
            self._state.pack_forget()
            self._loading_holder.pack(fill="both", expand=True)
            self._spinner.start()
        else:
            self._spinner.stop()
            self._loading_holder.pack_forget()
            self._state_holder.pack_forget()
            self._scroll.pack(fill="both", expand=True)

    def _show_state_only(self, show: bool) -> None:
        if show:
            self._scroll.pack_forget()
            self._state_holder.pack(fill="both", expand=True)
            self._loading_holder.pack_forget()
            self._state.pack(fill="both", expand=True)
        else:
            self._state_holder.pack_forget()
            self._scroll.pack(fill="both", expand=True)

    def _update_header(self, shown: int, total: int) -> None:
        label = CATEGORY_LABELS.get(self._category, self._category)
        if self._query:
            if total == 0:
                self.page_header.set_subtitle(f"“{self._query}” · 没有找到匹配的版本")
            else:
                self.page_header.set_subtitle(f"“{self._query}” · 找到 {total} 个版本")
        else:
            suffix = f"（显示 {shown}）" if shown < total else ""
            self.page_header.set_subtitle(f"{label} · 共 {total} 个版本{suffix}")

    def _status_text(self) -> str:
        if self._query:
            return f"搜索：{self._query}"
        if self._loaded_once:
            total = len(self._data.get(self._category, []))
            return f"{CATEGORY_LABELS.get(self._category, self._category)} · {total} 个版本"
        return "准备加载版本列表"


__all__ = ["DownloadPage"]
