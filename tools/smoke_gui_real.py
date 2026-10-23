"""真实后端 GUI 冒烟：用 AppServices（真实网络/真实检测）+ 伪造账号，驱动真实窗口并截图。

只用于开发期验证，不会写任何账号数据（账号只在内存里）。
用法：

    python -X utf8 tools\\smoke_gui_real.py --root <已安装过版本的 .minecraft> [--out docs/shots]
"""
from __future__ import annotations

import argparse
import ctypes
import sys
import time
import tkinter as tk
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 旧系统/非 Windows 忽略
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def _fake_account():
    from cubefetch.core.auth import Account

    now = time.time()
    return Account(
        access_token="smoke-token",
        refresh_token="smoke-refresh",
        expires_at=now + 24 * 3600,
        uuid="00000000000000000000000000000000",
        name="SmokePlayer",
        verified_at=now,
    )


def _shot(root: tk.Tk, path: Path) -> bool:
    try:
        from PIL import ImageGrab

        # 后台进程创建的窗口默认可能被其它窗口遮挡，截图前强制置顶并刷新一次。
        try:
            root.attributes("-topmost", True)
            root.lift()
            root.focus_force()
        except tk.TclError:
            pass
        root.update_idletasks()
        root.update()
        time.sleep(0.5)
        root.update()
        x = root.winfo_rootx()
        y = root.winfo_rooty()
        w = root.winfo_width()
        h = root.winfo_height()
        ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True).save(path)
        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"!! 截图失败：{type(exc).__name__}: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CubeFetch 真实后端 GUI 冒烟")
    parser.add_argument("--root", required=True)
    parser.add_argument("--out", default="docs/shots")
    parser.add_argument("--threads", type=int, default=32)
    parser.add_argument("--install", default=None,
                        help="可选：驱动一次真实安装的游戏版本 id（如 26.2），用于截取真实进度页")
    parser.add_argument("--install-kind", default="fabric",
                        choices=["vanilla", "fabric", "quilt", "forge", "neoforge"])
    parser.add_argument("--shot1-ms", type=int, default=8000, help="第一张进度截图延迟（毫秒）")
    parser.add_argument("--shot2-ms", type=int, default=45000, help="第二张进度截图延迟（毫秒）")
    args = parser.parse_args(argv)

    _dpi_aware()

    from cubefetch.config import AppConfig
    from cubefetch.services import AppServices
    from cubefetch.ui.app_window import create_app

    out_dir = (ROOT / args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    config = AppConfig(mc_root=str(Path(args.root)), threads=args.threads)
    services = AppServices(config)
    services.bootstrap()
    services.account = _fake_account()
    services.ticket = services.gate.issue(services.account)

    detection = services.detection
    print(f"== 检测：needs_choice={getattr(detection, 'needs_choice', None)} "
          f"candidates={[str(p) for p in getattr(detection, 'candidate_roots', [])]}")
    print(f"== 账号：{getattr(services.account, 'name', None)} 目录：{config.mc_root}")

    root, app = create_app(services)
    results: dict[str, bool] = {}
    started = time.time()

    def step1() -> None:
        # 下载页（版本+加载器合并页）：等 4 秒让 manifest 网络请求完成
        app.show_page("download")
        root.after(4000, step2)

    def step2() -> None:
        page = app.current_page
        data = getattr(page, "_data", None)
        counts = {k: len(v) for k, v in data.items()} if isinstance(data, dict) else None
        print(f"[{time.time() - started:5.1f}s] 当前页={app.current_page_name!r} 版本分类统计={counts}")
        results["download"] = _shot(root, out_dir / "real_download.png")
        root.after(400, step3)

    def step3() -> None:
        app.show_page("settings")
        root.after(900, step4)

    def step4() -> None:
        print(f"[{time.time() - started:5.1f}s] 当前页={app.current_page_name!r}")
        rv = getattr(detection, "root_versions", None)
        if rv:
            print("== 有效安装根（版本数）：" + "；".join(f"{p}（{n}）" for p, n in rv))
        results["settings"] = _shot(root, out_dir / "real_settings.png")
        root.after(300, step5)

    def step5() -> None:
        if not args.install:
            finish()
            return
        # 驱动一次真实安装，截取真实进度页（验证 v1.1.1 进度口径与 DPI）。
        entries = [e for lst in services.versions_categories().values() for e in lst
                   if str(getattr(e, "id", "")) == args.install]
        if not entries:
            print(f"!! 版本 {args.install} 不在清单中，跳过进度截图")
            finish()
            return
        kind = args.install_kind
        loader = services.recommend_loader(kind, args.install) if kind != "vanilla" else None
        print(f"[{time.time() - started:5.1f}s] 开始真实安装 {args.install} / {kind} {loader}")
        app.show_page("progress", version=entries[0], kind=kind, loader_version=loader)
        root.after(args.shot1_ms, step6)

    def step6() -> None:
        page = app.current_page
        print(f"[{time.time() - started:5.1f}s] 进度页运行中={getattr(page, 'is_running', None)}")
        results["progress1"] = _shot(root, out_dir / "real_progress_1.png")
        root.after(max(1000, args.shot2_ms - args.shot1_ms), step7)

    def step7() -> None:
        page = app.current_page
        print(f"[{time.time() - started:5.1f}s] 进度页运行中={getattr(page, 'is_running', None)}")
        results["progress2"] = _shot(root, out_dir / "real_progress_2.png")
        try:
            if getattr(page, "is_running", False):
                page.cancel_task()
                print("== 已请求取消安装")
        except Exception as exc:  # noqa: BLE001
            print(f"!! 取消失败：{exc}")
        root.after(2500, finish)

    def finish() -> None:
        print(f"== 截图结果：{results}")
        try:
            app.close()
        except Exception as exc:  # noqa: BLE001
            print(f"!! 关闭异常：{exc}")
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    root.after(600, step1)
    root.after(args.shot2_ms + 15000 if args.install else 20000, finish)  # 兜底超时
    root.mainloop()
    ok = all(results.values()) and len(results) >= 2
    print("GUI SMOKE PASS" if ok else "GUI SMOKE FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
