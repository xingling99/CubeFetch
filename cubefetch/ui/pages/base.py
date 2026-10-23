"""页面基类：线程 + queue.Queue + after(50) 轮询的 UI 更新模式（契约 §10）。"""
from __future__ import annotations

import logging
import queue
import threading
import tkinter as tk
from typing import Any, Callable

from .. import theme
from ...core.errors import CancelledError, CubeFetchError

log = logging.getLogger("cubefetch.ui.pages")

POLL_INTERVAL_MS = 50


def error_message(exc: BaseException) -> str:
    """把任意异常转换成面向用户的中文文字。"""
    if isinstance(exc, CubeFetchError):
        return exc.user_message or "发生错误"
    return f"发生未预期的错误：{exc}"


def error_detail(exc: BaseException) -> str:
    if isinstance(exc, CubeFetchError):
        return exc.detail or ""
    return str(exc)


class BasePage(tk.Frame):
    """所有页面的基类。

    子类实现 `build()` 构建控件，并可覆写 `on_show(**kwargs)` / `on_hide()`。
    耗时操作通过 `start_task()` 放到工作线程；工作线程只能通过 `post()`
    把事件放进队列，UI 线程用 `after(50)` 轮询消化（禁止在 worker 里碰控件）。
    """

    title = ""
    nav_key = ""

    def __init__(self, master: tk.Misc, app) -> None:
        super().__init__(master, bg=theme.BG, bd=0, highlightthickness=0)
        self.app = app
        self.services = app.services
        self.config = getattr(app, "config", None) or getattr(self.services, "config", None)
        self._queue: queue.Queue[tuple[str, Any]] = queue.Queue()
        self._cancel_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._poll_after: str | None = None
        self._callbacks: dict[str, Callable | None] = {}
        self._task_seq = 0
        self.build()
        self._schedule_poll()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def build(self) -> None:  # pragma: no cover - 抽象方法由子类实现
        raise NotImplementedError

    def on_show(self, **kwargs: Any) -> None:
        """页面被显示时调用（UI 线程）。"""

    def on_hide(self) -> None:
        """页面被隐藏时调用（UI 线程）。"""

    def resume_backdrop(self) -> None:
        """页面显示钩子（保留给 AppWindow 调用；新设计无背景动画）。"""

    def pause_backdrop(self) -> None:
        """页面隐藏钩子（保留给 AppWindow 调用；新设计无背景动画）。"""

    def shutdown(self) -> None:
        """窗口关闭时调用：停止轮询并取消正在执行的任务。"""
        self.cancel_task()
        self.pause_backdrop()
        if self._poll_after is not None:
            try:
                self.after_cancel(self._poll_after)
            except (tk.TclError, ValueError):  # pragma: no cover
                pass
            self._poll_after = None

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------
    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start_task(
        self,
        work: Callable[[], Any],
        *,
        on_success: Callable[[Any], None] | None = None,
        on_error: Callable[[BaseException], None] | None = None,
        on_event: Callable[[str, Any], None] | None = None,
        name: str = "task",
    ) -> None:
        """在新线程执行 work()；结果通过队列回到 UI 线程。"""
        self._cancel_event = threading.Event()
        self._task_seq += 1
        task_id = self._task_seq
        self._callbacks = {
            "on_success": on_success,
            "on_error": on_error,
            "on_event": on_event,
        }

        def runner() -> None:
            try:
                result = work()
            except BaseException as exc:  # noqa: BLE001 - 统一交回 UI 线程处理
                self._queue.put(("__error__", (task_id, exc)))
            else:
                self._queue.put(("__success__", (task_id, result)))

        self._thread = threading.Thread(target=runner, daemon=True, name=f"cubefetch-ui-{name}-{task_id}")
        self._thread.start()

    def post(self, kind: str, payload: Any = None) -> None:
        """工作线程向 UI 线程投递自定义事件（线程安全）。"""
        self._queue.put((kind, payload))

    def cancel_task(self) -> None:
        """请求取消当前任务（线程安全：只是 set 一个 Event）。"""
        self._cancel_event.set()

    def on_ui(self, func: Callable[[], None]) -> None:
        """把回调安排到 UI 线程执行（供任意线程调用）。"""

        def runner() -> None:
            try:
                func()
            except tk.TclError:  # pragma: no cover - 窗口已销毁
                pass

        try:
            self.after(0, runner)
        except (tk.TclError, RuntimeError):  # pragma: no cover
            pass

    # ------------------------------------------------------------------
    # 轮询
    # ------------------------------------------------------------------
    def _schedule_poll(self) -> None:
        try:
            self._poll_after = self.after(POLL_INTERVAL_MS, self._poll)
        except tk.TclError:  # pragma: no cover
            self._poll_after = None

    def _poll(self) -> None:
        self._poll_after = None
        try:
            while True:
                kind, payload = self._queue.get_nowait()
                self._dispatch(kind, payload)
        except queue.Empty:
            pass
        if self._alive():
            self._schedule_poll()

    def _dispatch(self, kind: str, payload: Any) -> None:
        if kind == "__success__":
            task_id, result = payload
            if task_id != self._task_seq:
                return
            callback = self._callbacks.get("on_success")
            self._callbacks = {}
            self._thread = None
            if callback is not None:
                callback(result)
        elif kind == "__error__":
            task_id, exc = payload
            if task_id != self._task_seq:
                return
            callback = self._callbacks.get("on_error")
            self._callbacks = {}
            self._thread = None
            if callback is not None:
                callback(exc)
            else:  # pragma: no cover - 页面没注册错误处理时记录日志
                log.error("界面任务未处理异常: %s", exc, exc_info=exc)
        else:
            callback = self._callbacks.get("on_event")
            if callback is not None:
                callback(kind, payload)

    def _alive(self) -> bool:
        try:
            return bool(self.winfo_exists())
        except tk.TclError:  # pragma: no cover
            return False

    # ------------------------------------------------------------------
    # 常用小工具
    # ------------------------------------------------------------------
    @property
    def account(self):
        return getattr(self.services, "account", None)

    def set_status(self, text: str, kind: str = "info") -> None:
        setter = getattr(self.app, "set_status", None)
        if callable(setter):
            setter(text, kind=kind)

    def report_cancelled(self, message: str = "操作已取消") -> None:
        self.set_status(message, kind="warn")

    def is_cancelled(self, exc: BaseException) -> bool:
        return isinstance(exc, CancelledError)
