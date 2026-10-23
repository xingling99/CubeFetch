"""真实网络验证：点击登录后自动打开浏览器 + 自动复制设备码（v1.1.2-beta）。

不打开真实浏览器：把 cubefetch.ui.pages.login._browser_opener 替换成记录器，
其余全部走真实链路（真实 AppServices → 真实 login_flow(Microsoft 设备码) →
真实 LoginPage.start_login → 真实 Tk 剪贴板）。

用法：
    python -X utf8 tools/smoke_login_autoopen.py
退出码 0 = PASS。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from cubefetch.config import AppConfig  # noqa: E402
from cubefetch.services import AppServices  # noqa: E402
from cubefetch.ui import dpi  # noqa: E402

dpi.enable_dpi_awareness()

import tkinter as tk  # noqa: E402

from cubefetch.ui.app_window import create_app  # noqa: E402
from cubefetch.ui.pages import login as login_mod  # noqa: E402

opened: list[str] = []
login_mod._browser_opener = lambda url, *a, **kw: opened.append(str(url)) or True


def pump(root: tk.Misc, seconds: float) -> None:
    end = time.time() + seconds
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def main() -> int:
    result: dict = {"ok": False}
    tmp = Path(tempfile.mkdtemp(prefix="cf_login_autoopen_"))
    services = AppServices(AppConfig(mc_root=str(tmp), threads=16))
    root = None
    try:
        root, app = create_app(services)
        root.update()
        pages = getattr(app, "_pages", None) or getattr(app, "pages", {})
        page = pages.get("login")
        if page is None:
            app.show_page("login")
            root.update()
            page = pages.get("login")
        if page is None:
            raise RuntimeError(f"没有找到登录页实例（已有页面：{sorted(pages)}）")
        page.start_login()
        deadline = time.time() + 60
        while time.time() < deadline and not opened:
            root.update()
            time.sleep(0.02)
        # 等 UI 处理完 flow 事件（剪贴板/提示文案）
        pump(root, 0.8)
        result["opened"] = list(opened)
        result["uri_var"] = str(page._uri_var.get()) if hasattr(page, "_uri_var") else ""
        result["code_var"] = str(page._code_var.get()) if hasattr(page, "_code_var") else ""
        try:
            result["clipboard"] = str(root.clipboard_get())
        except tk.TclError:
            result["clipboard"] = ""
        result["hint"] = str(page._copy_hint_var.get()) if hasattr(page, "_copy_hint_var") else ""
        code = result["code_var"].strip()
        uri = result["uri_var"].strip()
        checks = {
            "opened_once": len(opened) == 1,
            "opened_verification_uri": bool(uri) and opened and opened[0] == uri,
            "code_copied": bool(code) and result["clipboard"].strip() == code,
            "hint_says_auto_open": "自动打开浏览器" in result["hint"],
            # login.live.com 的设备码是 8 位字母数字（如 Q5NEKKWR），不带连字符。
            "code_format": len(code.replace("-", "")) == 8 and code.replace("-", "").isalnum(),
        }
        result["checks"] = checks
        result["ok"] = all(checks.values())
        page.cancel_login()
        pump(root, 1.5)
    except Exception:  # noqa: BLE001
        result["error"] = traceback.format_exc()
    finally:
        try:
            if root is not None:
                root.destroy()
        except Exception:  # noqa: BLE001
            pass

    print(json.dumps(result, ensure_ascii=False, indent=2))
    print("LOGIN AUTOOPEN SMOKE", "PASS" if result.get("ok") else "FAIL")
    sys.stdout.flush()
    # 直接退出：登录轮询线程持有 requests 连接，避免解释器收尾等待。
    os._exit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    raise SystemExit(main())
