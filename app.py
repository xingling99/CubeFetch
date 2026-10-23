"""CubeFetch（方块速递）入口。"""
from __future__ import annotations

import logging
import logging.handlers
import sys
import traceback


def _setup_logging(verbose: bool = False) -> None:
    from cubefetch.config import log_dir

    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    try:
        logfile = log_dir() / "cubefetch.log"
        handler = logging.handlers.RotatingFileHandler(
            logfile, maxBytes=1024 * 1024, backupCount=3, encoding="utf-8"
        )
        handler.setFormatter(fmt)
        root.addHandler(handler)
    except OSError:
        pass
    # 打包成 windowed exe 后 stderr 可能为 None
    if sys.stderr is not None:
        console = logging.StreamHandler()
        console.setFormatter(fmt)
        root.addHandler(console)


def _enable_dpi_awareness() -> None:
    """在创建任何 Tk 窗口之前声明 DPI 感知。

    不声明时 Windows 会把窗口按位图放大（125%/150% 下字体模糊、布局错位）；
    缩放系数与细节见 ``cubefetch.ui.dpi`` / ``cubefetch.ui.theme``。
    """
    try:
        from cubefetch.ui.dpi import enable_dpi_awareness

        enable_dpi_awareness()
        return
    except Exception:  # noqa: BLE001 - 旧版本/非 Windows 时退化为内联实现
        pass
    if sys.platform != "win32":
        return
    try:
        import ctypes

        user32 = ctypes.windll.user32
        set_context = getattr(user32, "SetProcessDpiAwarenessContext", None)
        if set_context is not None:
            try:
                set_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
                return
            except Exception:  # noqa: BLE001
                pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
            return
        except Exception:  # noqa: BLE001
            pass
        user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _fatal(message: str) -> None:
    logging.getLogger("cubefetch").exception(message)
    try:
        import tkinter as tk
        from tkinter import messagebox

        _enable_dpi_awareness()
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("CubeFetch 启动失败", f"{message}\n\n详情见日志文件。")
        root.destroy()
    except Exception:  # noqa: BLE001  连 GUI 都起不来时只能写日志
        pass


def _write_version() -> None:
    from cubefetch.config import APP_NAME, APP_VERSION

    text = f"{APP_NAME} {APP_VERSION}"
    print(text)
    try:  # windowed exe 没有控制台，写到临时文件便于打包脚本校验
        from cubefetch.config import appdata_dir

        (appdata_dir() / "version.txt").write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass


def _integrity_problems() -> list[str]:
    """启动时的完整性自检（关键模块 / 文件校验 / 打包资源 / 调试器）。"""
    log = logging.getLogger("cubefetch")
    problems: list[str] = []
    try:
        from cubefetch.core.integrity import summarize, verify

        report = verify()
        log.info("文件完整性校验：%s", summarize(report))
        if not report.ok:
            problems.extend(report.problems)
    except Exception:  # noqa: BLE001 - 自检失败不应阻止启动
        log.warning("文件完整性校验执行失败", exc_info=True)
    try:
        from cubefetch.core.security import IntegrityGuard

        problems.extend(IntegrityGuard().check())
    except Exception:  # noqa: BLE001 - 自检失败不应阻止启动
        log.warning("完整性自检执行失败", exc_info=True)
    # 把结果登记给门禁：有篡改时 require_active() 会拒绝下载
    try:
        from cubefetch.core.security import set_integrity_problems

        set_integrity_problems(problems)
    except Exception:  # noqa: BLE001
        log.debug("登记完整性结果失败", exc_info=True)
    return problems


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    verbose = "--verbose" in args or "-v" in args
    _setup_logging(verbose)
    log = logging.getLogger("cubefetch")

    # 必须在创建 Tk 之前声明 DPI 感知（--selftest 与正常启动都会建窗口）。
    _enable_dpi_awareness()

    if "--version" in args:
        _write_version()
        return 0

    if "--selftest" in args:
        # 打包后的冒烟测试入口：完全离线跑一遍界面构造
        try:
            from cubefetch.ui._selftest import run_selftest

            code = run_selftest()
            log.info("UI 自测退出码 %s", code)
            return int(code or 0)
        except Exception:  # noqa: BLE001
            log.exception("UI 自测失败")
            return 1

    import cubefetch

    log.info("CubeFetch %s 启动", cubefetch.__version__)
    try:
        from cubefetch.services import AppServices

        services = AppServices()
        services.bootstrap()
        integrity = _integrity_problems()
        if integrity:
            log.warning("完整性自检发现问题：%s", "；".join(integrity))
        from cubefetch.ui.app_window import create_app

        root, window = create_app(services)
        if integrity:
            window.set_status("完整性自检发现异常：" + "；".join(integrity), kind="warn")
            try:
                from tkinter import messagebox

                messagebox.showwarning(
                    "完整性校验失败",
                    "检测到程序文件被修改或环境异常，出于安全考虑已禁用下载功能。\n\n"
                    + "\n".join(integrity[:6]),
                    parent=root,
                )
            except Exception:  # noqa: BLE001 提示失败不阻止启动
                log.debug("完整性警告弹窗失败", exc_info=True)
        root.mainloop()
        log.info("CubeFetch 退出")
        return 0
    except Exception:  # noqa: BLE001  顶层兜底
        traceback.print_exc()
        _fatal("程序发生了未处理的错误")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
