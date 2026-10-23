"""滚轮事件健壮性回归测试。

背景：Windows 上 ``<MouseWheel>`` 事件的 ``event.num`` 可能是字符串 ``"??"``
（tkinter 在部分滚轮事件里给出非数字值），旧实现 ``int(event.num)`` 直接抛
``ValueError``，在真实 exe 里表现为大量 "Exception in Tkinter callback"。
本测试锁定修复行为。

为了不依赖 Canvas 实际滚动区域/窗口布局（那会随运行环境偶发失败），
这里把 ``canvas.yview_scroll`` 替换成记录器，直接断言滚动方向与不抛异常。
Tk 根窗口整个测试类只创建一次（并带重试），减少 CI/高负载下 Tcl 初始化
偶发失败（"Can't find a usable init.tcl"）带来的假失败。
"""
from __future__ import annotations

import time
import tkinter as tk
import unittest

from cubefetch.ui import theme
from cubefetch.ui.widgets import ScrollFrame


class _FakeEvent:
    def __init__(self, **kwargs) -> None:
        self.__dict__.update(kwargs)


class ScrollFrameWheelTest(unittest.TestCase):
    root: tk.Tk

    @classmethod
    def setUpClass(cls) -> None:
        last_error: Exception | None = None
        for _ in range(8):
            try:
                cls.root = tk.Tk()
                cls.root.withdraw()
                return
            except tk.TclError as exc:  # pragma: no cover - 环境偶发
                last_error = exc
                time.sleep(0.25)
        raise unittest.SkipTest(f"Tk 初始化失败（环境问题，跳过 UI 测试）：{last_error}")

    @classmethod
    def tearDownClass(cls) -> None:
        try:
            cls.root.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def setUp(self) -> None:
        self.frame = ScrollFrame(self.root)
        self.frame.pack(fill="both", expand=True)
        self.root.update_idletasks()
        # 绕开"指针在窗口内"与"内容可滚动"两个前置条件，聚焦事件解析本身。
        self.frame._pointer_inside = lambda: True  # type: ignore[method-assign]
        self.frame._scrollable = lambda: True  # type: ignore[method-assign]
        self.calls: list[tuple[int, str]] = []
        self.frame.canvas.yview_scroll = (  # type: ignore[method-assign]
            lambda step, what: self.calls.append((int(step), str(what)))
        )

    def tearDown(self) -> None:
        try:
            self.frame.destroy()
        except tk.TclError:  # pragma: no cover
            pass

    def test_non_numeric_num_does_not_raise(self) -> None:
        """delta=0 且 num="??" 时应安静返回，不抛异常、不滚动。"""
        self.frame._on_wheel(_FakeEvent(delta=0, num="??"))
        self.assertEqual(self.calls, [])

    def test_negative_delta_scrolls_down(self) -> None:
        """delta=-120 时向下滚动（yview_scroll 正数 units）。"""
        self.frame._on_wheel(_FakeEvent(delta=-120, num="??"))
        self.assertEqual(self.calls, [(60, "units")])

    def test_positive_delta_scrolls_up(self) -> None:
        """delta=+120 时向上滚动（yview_scroll 负数 units）。"""
        self.frame._on_wheel(_FakeEvent(delta=120, num="??"))
        self.assertEqual(self.calls, [(-60, "units")])

    def test_legacy_num_4_and_5(self) -> None:
        """旧式滚轮事件用 num=4/5（无 delta）也能滚动。"""
        self.frame._on_wheel(_FakeEvent(delta=0, num=4))
        self.frame._on_wheel(_FakeEvent(delta=0, num=5))
        self.assertEqual(
            self.calls,
            [(-theme.sp(40), "units"), (theme.sp(40), "units")],
        )

    def test_missing_attributes_do_not_raise(self) -> None:
        """事件对象缺少 delta/num 属性时也不应抛异常。"""
        self.frame._on_wheel(_FakeEvent())
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
