"""CubeFetch 界面离线自测。

运行：
    python -m cubefetch.ui._selftest

用 FakeServices 桩完全离线驱动 5 个页面：登录（设备码 + 自动复制）→ 下载
（版本列表 + 加载器合并页，首次进入强制刷新）→ 进度（推进度事件 / 取消 /
错误态 / Java 运行时阶段）→ 完成 → 设置；并打桩 messagebox / simpledialog 验证
Java 缺失询问、版本重命名、关闭确认等分支，检查提醒音效与剪贴板，最后在真实
渲染后用 Pillow 截图到 docs/shots/。全程不联网、不写用户配置。
"""
from __future__ import annotations

import logging
import os
import sys
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..core.errors import CancelledError, InstallError
from . import theme

REPO_ROOT = Path(__file__).resolve().parents[2]
_SHOT_SUBDIR = os.environ.get("CUBEFETCH_SHOT_SUBDIR", "").strip()
SHOTS_DIR = (
    REPO_ROOT / "docs" / "shots" / _SHOT_SUBDIR
    if _SHOT_SUBDIR
    else REPO_ROOT / "docs" / "shots"
)
PIL_OK = False
ImageGrab: Any = None


def _load_pillow() -> None:
    """导入 Pillow；失败时按任务约定等待 60 秒再重试一次。"""
    global PIL_OK, ImageGrab
    try:
        from PIL import ImageGrab as _grab  # type: ignore
    except Exception:  # noqa: BLE001
        print("[shot] Pillow 暂不可用，等待 60 秒后重试一次…")
        time.sleep(60)
        try:
            from PIL import ImageGrab as _grab  # type: ignore
        except Exception:  # noqa: BLE001
            print("[shot] SKIP 截图：Pillow 仍不可用")
            return
    PIL_OK = True
    ImageGrab = _grab


# ---------------------------------------------------------------------------
# Fake 数据对象（字段与契约 §3 / §8 / §9 一致）
# ---------------------------------------------------------------------------
class FakeAccount:
    def __init__(self, name: str = "Steve") -> None:
        self.name = name
        self.uuid = "00000000-0000-4000-8000-000000000001"
        self.xuid = "1234567890"
        self.access_token = "fake-access-token"
        self.refresh_token = "fake-refresh-token"
        self.expires_at = time.time() + 3600


class FakeDeviceFlow:
    user_code = "ABCD-EFGH"
    verification_uri = "https://example.invalid/device"
    expires_in = 900
    interval = 5
    device_code = "fake-device-code"


class FakeVersionEntry:
    def __init__(self, version_id: str, category: str, release_time: str) -> None:
        self.id = version_id
        self.type = category
        self.url = f"https://example.invalid/mojang/{version_id}.json"
        self.release_time = release_time
        self.category = category

    def release_date(self) -> datetime:
        return datetime.fromisoformat(self.release_time.replace("Z", "+00:00"))


class FakeProgress:
    def __init__(self, **kwargs: Any) -> None:
        self.done = int(kwargs.get("done", 0))
        self.total = int(kwargs.get("total", 0))
        self.bytes_done = int(kwargs.get("bytes_done", 0))
        self.bytes_total = int(kwargs.get("bytes_total", 0))
        self.speed = float(kwargs.get("speed", 0.0))
        self.message = str(kwargs.get("message", ""))
        # task-14 之后 Progress 会带阶段字段；这里同时用于验证 UI 的阶段进度显示
        self.stage = str(kwargs.get("stage", ""))
        self.stage_done = int(kwargs.get("stage_done", 0))
        self.stage_total = int(kwargs.get("stage_total", 0))


class FakeProbe:
    def __init__(self, kind: str, installed: bool, mc_root: Any) -> None:
        self.kind = kind
        self.installed = installed
        self.mc_root = mc_root
        self.exe_hint = None
        self.config_path = None
        self.managed_versions: list[str] = []


class FakeDetection:
    def __init__(
        self,
        probes: list[FakeProbe] | None = None,
        candidate_roots: list[Path] | None = None,
        default_root: Path | None = None,
        needs_choice: bool = False,
        reason: str = "",
        root_versions: list[tuple[Any, int]] | None = None,
    ) -> None:
        self.probes = probes or []
        self.candidate_roots = candidate_roots or []
        self.default_root = default_root
        self.needs_choice = needs_choice
        self.reason = reason
        self.root_versions = root_versions or []


def _build_versions() -> dict[str, list[FakeVersionEntry]]:
    release = ["1.20.4", "1.20.3", "1.20.2", "1.20.1", "1.19.4", "1.19.2", "1.18.2", "1.16.5"]
    snapshot = ["24w09a", "23w45a", "23w31a", "22w42a"]
    old = ["1.7.10", "1.5.2", "1.2.5"]
    april = ["15w14a", "3D Shareware v1.34"]
    data: dict[str, list[FakeVersionEntry]] = {}
    base = datetime(2024, 3, 1, 12, 0, tzinfo=timezone.utc)
    for category, ids in (
        ("release", release),
        ("snapshot", snapshot),
        ("old", old),
        ("aprilfool", april),
    ):
        items = []
        for index, version_id in enumerate(ids):
            stamp = base.replace(month=max(1, 12 - index % 12))
            stamp = stamp.replace(day=min(28, index + 1))
            items.append(FakeVersionEntry(version_id, category, stamp.isoformat()))
        data[category] = items
    return data


class FakeServices:
    """完全离线的 AppServices 替身（字段/方法名与 Lead 锁定的 API 一致）。"""

    def __init__(self) -> None:
        self.config = AppConfig()
        self.account: FakeAccount | None = None
        self.detection: FakeDetection | None = FakeDetection(
            probes=[FakeProbe("pcl", True, Path("C:/Users/Test/PCL/.minecraft"))],
            candidate_roots=[Path("C:/Users/Test/PCL/.minecraft")],
            default_root=Path("C:/Users/Test/PCL/.minecraft"),
            needs_choice=False,
        )
        self.java_exe: Path | None = None
        self.mirror = "auto"

        self.versions_calls = 0
        self.version_refresh_flags: list[bool] = []
        self.loader_calls: list[tuple[str, str]] = []
        self.install_calls: list[tuple[str, Any, str]] = []
        self.custom_version_names: list[Any] = []
        self.java_status_calls = 0
        self.java_status_value: dict[str, Any] = {
            "required_major": 17,
            "java_path": None,
            "component": None,
            "need_download": False,
        }
        self.java_download_calls: list[str] = []
        self.existing_version_value: str | None = None
        self.existing_version_calls: list[tuple[str, Any, str]] = []
        self.save_config_calls = 0
        self.detect_calls = 0
        self.chosen_roots: list[str] = []
        self.open_folder_calls: list[str] = []
        self.login_status_calls = 0
        self.install_steps = 10
        self.install_delay = 0.12
        self.fail_next_install = False

    # ---- 登录 ----
    def login_flow(self) -> FakeDeviceFlow:
        time.sleep(0.03)
        return FakeDeviceFlow()

    def finish_login(self, flow: Any, cancel: Any = None, status_cb: Any = None) -> FakeAccount:
        for index in range(3):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise CancelledError()
            if callable(status_cb):
                status_cb(f"正在验证账号（{index + 1}/3）…")
                self.login_status_calls += 1
            time.sleep(0.03)
        self.account = FakeAccount()
        return self.account

    def logout(self) -> None:
        self.account = None

    # ---- 版本 ----
    def versions_categories(self, refresh: bool = False) -> dict[str, list[FakeVersionEntry]]:
        self.versions_calls += 1
        self.version_refresh_flags.append(bool(refresh))
        time.sleep(0.05)
        return _build_versions()

    # ---- 加载器 ----
    def loaders_for(self, kind: str, game_version: str) -> list[dict]:
        self.loader_calls.append((kind, game_version))
        time.sleep(0.03)
        table = {
            "fabric": ["0.15.7", "0.15.6", "0.15.5"],
            "quilt": ["0.23.2", "0.23.1", "0.22.0"],
            "forge": ["49.0.30", "49.0.19", "48.1.0"],
            "neoforge": ["20.4.237", "20.4.190"],
            "vanilla": [],
        }
        return [{"version": item} for item in table.get(kind, [])]

    def recommend_loader(self, kind: str, game_version: str) -> str | None:
        table = {
            "fabric": "0.15.7",
            "quilt": "0.23.2",
            "forge": "49.0.30",
            "neoforge": "20.4.237",
        }
        return table.get(kind)

    # ---- 安装 ----
    def install(
        self,
        kind: str,
        loader_version: Any,
        game_version: str,
        progress_cb: Any = None,
        cancel: Any = None,
        custom_version_name: Any = None,
    ) -> str:
        self.install_calls.append((kind, loader_version, game_version))
        self.custom_version_names.append(custom_version_name)
        if self.fail_next_install:
            self.fail_next_install = False
            raise InstallError("模拟安装失败：无法下载资源索引文件。", "simulated failure")
        total = self.install_steps
        for index in range(1, total + 1):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise CancelledError()
            if callable(progress_cb):
                progress_cb(
                    FakeProgress(
                        done=index,
                        total=total,
                        bytes_done=index * 2 * 1024 * 1024,
                        bytes_total=total * 2 * 1024 * 1024,
                        speed=4.2 * 1024 * 1024,
                        message=f"下载文件 assets/{index:02d}.jar",
                        stage="下载资源文件",
                        stage_done=index,
                        stage_total=total,
                    )
                )
            time.sleep(self.install_delay)
        return f"{game_version}-{kind}"

    # ---- Java / 已存在版本 ----
    def java_status(self, game_version: str) -> dict[str, Any]:
        self.java_status_calls += 1
        return dict(self.java_status_value)

    def download_java(self, game_version: str, progress_cb: Any = None, cancel: Any = None) -> Path:
        self.java_download_calls.append(str(game_version))
        steps = 3
        for index in range(1, steps + 1):
            if cancel is not None and getattr(cancel, "is_set", lambda: False)():
                raise CancelledError()
            if callable(progress_cb):
                progress_cb(
                    FakeProgress(
                        done=index,
                        total=steps,
                        bytes_done=index * 8 * 1024 * 1024,
                        bytes_total=steps * 8 * 1024 * 1024,
                        speed=9.0 * 1024 * 1024,
                        message="正在下载 Java 运行时…",
                    )
                )
            time.sleep(0.03)
        return Path("C:/Users/Test/PCL/.minecraft/runtime/java-runtime-delta/bin/java.exe")

    def existing_version(self, kind: str, loader_version: Any, game_version: str) -> str | None:
        self.existing_version_calls.append((kind, loader_version, game_version))
        return self.existing_version_value

    # ---- 其他 ----
    def ensure_java(self, required_major: int = 17) -> Path | None:
        return None

    def detect_launchers(self) -> FakeDetection:
        self.detect_calls += 1
        time.sleep(0.03)
        return FakeDetection(
            probes=[
                FakeProbe("pcl", True, Path("C:/Users/Test/PCL/.minecraft")),
                FakeProbe("hmcl", True, Path("D:/Games/HMCL/.minecraft")),
            ],
            candidate_roots=[
                Path("C:/Users/Test/PCL/.minecraft"),
                Path("D:/Games/HMCL/.minecraft"),
            ],
            default_root=Path("C:/Users/Test/PCL/.minecraft"),
            needs_choice=True,
            reason="检测到多个启动器",
            root_versions=[
                (Path("C:/Users/Test/PCL/.minecraft"), 12),
                (Path("D:/Games/HMCL/.minecraft"), 5),
            ],
        )

    def choose_root(self, path: Any) -> None:
        text = str(path)
        self.chosen_roots.append(text)
        self.config.mc_root = text

    def save_config(self) -> None:
        self.save_config_calls += 1

    def open_folder(self, path: Any) -> None:
        self.open_folder_calls.append(str(path))

    def bootstrap(self) -> None:
        return None

    def shutdown(self) -> None:
        return None


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------
def pump(root: tk.Tk, seconds: float = 0.0, until=None, timeout: float = 10.0) -> bool:
    """处理 Tk 事件；until 为 None 时等待 seconds，否则等到条件成立。"""
    start = time.monotonic()
    while True:
        try:
            root.update()
        except tk.TclError:
            return False
        if until is not None and until():
            return True
        elapsed = time.monotonic() - start
        if until is None:
            if elapsed >= seconds:
                return True
        elif elapsed >= timeout:
            return False
        time.sleep(0.01)


class Checker:
    def __init__(self) -> None:
        self.failures: list[str] = []
        self.passed = 0

    def check(self, condition: Any, label: str) -> bool:
        if condition:
            self.passed += 1
            print(f"  [ok] {label}")
            return True
        self.failures.append(label)
        print(f"  [FAIL] {label}")
        return False


def _shot(root: tk.Misc, name: str, checker: Checker, box: tuple[int, int, int, int] | None = None) -> Path | None:
    if not PIL_OK or ImageGrab is None:
        print(f"[shot] SKIP {name}（无 Pillow）")
        return None
    SHOTS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if box is None:
            root.update_idletasks()
            root.update()
            try:
                root.lift()
                root.attributes("-topmost", True)
                root.update()
            except tk.TclError:
                pass
            time.sleep(0.15)
            x = int(root.winfo_rootx())
            y = int(root.winfo_rooty())
            box = (x, y, x + max(1, int(root.winfo_width())), y + max(1, int(root.winfo_height())))
        image = ImageGrab.grab(bbox=box)
        path = SHOTS_DIR / f"{name}.png"
        image.save(path)
        print(f"[shot] {path}")
        return path
    except Exception as exc:  # noqa: BLE001 - 截图失败不阻断自测
        print(f"[shot] SKIP {name}: {exc}")
        return None
    finally:
        try:
            root.attributes("-topmost", False)
        except tk.TclError:
            pass


def _widget_box(widget: tk.Misc) -> tuple[int, int, int, int]:
    widget.update_idletasks()
    x = int(widget.winfo_rootx())
    y = int(widget.winfo_rooty())
    return (x, y, x + max(1, int(widget.winfo_width())), y + max(1, int(widget.winfo_height())))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    _load_pillow()
    print("== CubeFetch UI 自测 ==")
    print(f"Python: {sys.version.split()[0]}  Pillow: {'yes' if PIL_OK else 'no'}  Tk: {tk.TkVersion}")
    checker = Checker()
    shots: list[Path] = []
    services = FakeServices()

    root: tk.Tk | None = None
    app = None
    try:
        from .app_window import NAV_ITEMS, PAGES, create_app
        from .dpi import enable_dpi_awareness

        # DPI 感知必须在创建 tk.Tk() 之前声明，否则 Windows 下会被系统位图拉伸。
        enable_dpi_awareness()
        print(f"DPI: SCALE={theme.SCALE}  截图画布尺寸={theme.WINDOW_WIDTH}x{theme.WINDOW_HEIGHT}")

        root, app = create_app(services)
        pump(root, 0.4)
        checker.check(app.current_page_name == "login", "启动路由：未登录 → 登录页")
        checker.check(root.winfo_width() >= theme.MIN_WIDTH, f"窗口宽度 ≥ {theme.MIN_WIDTH}")
        checker.check(root.winfo_height() >= theme.MIN_HEIGHT, f"窗口高度 ≥ {theme.MIN_HEIGHT}")
        checker.check(theme.SCALE >= 1.0, f"DPI 缩放系数已生效：SCALE={theme.SCALE}")
        checker.check(
            theme.WINDOW_WIDTH == int(round(1080 * theme.SCALE))
            and theme.MIN_WIDTH == int(round(960 * theme.SCALE)),
            f"窗口常量按 SCALE 缩放：{theme.WINDOW_WIDTH} / {theme.MIN_WIDTH}",
        )
        checker.check(
            abs(root.winfo_width() - theme.WINDOW_WIDTH) <= 2
            and abs(root.winfo_height() - theme.WINDOW_HEIGHT) <= 2,
            f"实际窗口尺寸 {root.winfo_width()}x{root.winfo_height()} 与缩放后常量一致",
        )
        try:
            tk_scaling = float(root.tk.call("tk", "scaling"))
        except (tk.TclError, ValueError):  # pragma: no cover
            tk_scaling = 0.0
        checker.check(
            abs(tk_scaling - theme.SCALE * 96.0 / 72.0) < 0.5,
            f"tk scaling 已按 SCALE 设置：{tk_scaling:.2f}",
        )
        checker.check(bool(root.title()), f"窗口标题：{root.title()}")
        # ---- 新视觉：静态双光晕 / 圆角卡片底板 ----
        login_page = app.get_page("login")
        checker.check(
            bool(login_page._glow.find_all()),
            "登录页静态紫蓝双光晕已绘制",
        )
        checker.check(
            bool(login_page.card._panel.find_all()),
            "卡片圆角底板已绘制",
        )
        nav_keys = [key for key, _label in NAV_ITEMS]
        checker.check(
            nav_keys == ["login", "download", "progress", "done", "settings"],
            f"左侧导航 5 项：{nav_keys}",
        )
        checker.check(
            set(PAGES) == {"login", "download", "progress", "done", "settings"},
            "页面表只含 5 个页面（版本/加载器已合并为下载）",
        )

        # ---- 未登录保护：没有正版账号不进下载界面 ----
        for target in ("download", "progress", "done"):
            app._on_nav_click(target)
            pump(root, 0.05)
            checker.check(
                app.current_page_name == "login",
                f"未登录时导航“{target}”被拦截回登录页",
            )
        checker.check(
            "请先登录" in app._status_label.cget("text"),
            "未登录拦截时状态栏提示登录",
        )
        app.show_page("download")
        pump(root, 0.05)
        checker.check(app.current_page_name == "login", "未登录时 show_page('download') 同样被拦截")
        app.show_page("download", version="1.20.4")
        pump(root, 0.05)
        checker.check(app.current_page_name == "login", "未登录时带参 show_page('download') 也被拦截")
        app.show_page("settings")
        pump(root, 0.05)
        checker.check(app.current_page_name == "settings", "未登录仍可进入设置页")
        app.show_page("login")
        pump(root, 0.1)
        checker.check(app.current_page_name == "login", "未登录可返回登录页")

        # ---- 登录页（设备码自动复制） ----
        path = _shot(root, "login", checker)
        if path:
            shots.append(path)
        login = app.get_page("login")
        from .pages import login as login_module

        browser_calls: list[str] = []
        original_browser_opener = login_module._browser_opener
        login_module._browser_opener = lambda url: browser_calls.append(str(url)) or True
        login.start_login()
        ok = pump(
            root,
            until=lambda: services.account is not None and app.current_page_name == "download",
            timeout=15.0,
        )
        checker.check(ok, "设备码登录流程完成后自动进入下载页")
        checker.check(services.login_status_calls > 0, "登录状态回调已送达 UI")
        checker.check(app._account_pill._name == "Steve", "顶部显示账号名")
        try:
            clipboard = root.clipboard_get()
        except tk.TclError:
            clipboard = ""
        checker.check(clipboard == "ABCD-EFGH", f"设备码已自动复制到剪贴板：{clipboard!r}")
        checker.check(
            "验证码已复制" in login._copy_hint_var.get(),
            "登录页提示“验证码已复制”",
        )
        pump(root, 0.2)  # 等待自动打开浏览器的 daemon 线程执行
        checker.check(
            len(browser_calls) == 1 and browser_calls[0] == "https://example.invalid/device",
            f"拿到设备码后自动打开浏览器 1 次：{browser_calls}",
        )
        # 重新获取验证码：新的设备码同样会自动再打开一次浏览器
        login._on_login_event("flow", FakeDeviceFlow())
        pump(root, 0.3)
        checker.check(
            len(browser_calls) == 2
            and browser_calls[1].startswith("https://example.invalid/device"),
            f"重新获取验证码后再次自动打开浏览器：{browser_calls}",
        )
        # 打开浏览器抛异常不应崩溃
        login_module._browser_opener = lambda url: (_ for _ in ()).throw(RuntimeError("boom"))
        login._on_login_event("flow", FakeDeviceFlow())
        pump(root, 0.2)
        checker.check(True, "打开浏览器失败时静默降级、不崩溃")
        login_module._browser_opener = original_browser_opener
        checker.check(bool(login._glow.find_all()), "登录页光晕保持静态绘制（无后台动画）")

        # ---- 下载页（版本列表 + 加载器合并） ----
        download = app.get_page("download")
        checker.check(
            pump(root, until=lambda: download._loaded_once, timeout=15.0),
            "版本列表异步加载完成",
        )
        checker.check(bool(download._data.get("release")), "正式版分类有数据")
        checker.check(
            bool(services.version_refresh_flags) and services.version_refresh_flags[0] is True,
            "下载页首次进入强制 refresh=True 拉取版本列表",
        )
        checker.check(len(download._rows) > 0, "版本行已渲染")
        first = download._data["release"][0]
        download._select(first)
        pump(root, 0.1)
        checker.check(download._start_button.enabled, "选中版本后“开始下载”启用")
        download._on_category_change("snapshot")
        pump(root, 0.1)
        checker.check(bool(download._rows), "切换到预览版分类后重新渲染")
        download._on_category_change("release")
        pump(root, 0.1)
        download._select(first)
        pump(root, 0.05)
        checker.check("quilt" in download._option_rows, "加载器选项包含 Quilt")
        download._select_kind("fabric")
        checker.check(
            pump(root, until=lambda: (first.id, "fabric") in download._cache, timeout=15.0),
            "按所选 MC 版本拉取 Fabric 版本列表",
        )
        pump(root, 0.15)
        checker.check(len(download._available_loader_versions()) >= 3, "加载器版本下拉已填充")
        checker.check(download._loader_version == "0.15.7", "加载器版本默认选中推荐值 0.15.7")
        checker.check("0.15.7" in download._combo_var.get(), "下拉显示推荐加载器版本")
        path = _shot(root, "download", checker)
        if path:
            shots.append(path)

        # ---- 进度页：进度事件 + 成功 ----
        download._start()
        pump(root, 0.2)
        checker.check(app.current_page_name == "progress", "开始下载进入进度页")
        checker.check(app.selected_version is first, f"进度页沿用已选版本 {first.id}")
        progress = app.get_page("progress")
        checker.check(
            pump(
                root,
                until=lambda: progress._percent_label.cget("text") not in ("0%", "--%", ""),
                timeout=10.0,
            ),
            "进度事件已推进（百分比更新）",
        )
        checker.check("/" in progress._size_label.cget("text"), "已下载/总大小已显示")
        checker.check(bool(progress._log.text.get("1.0", "end").strip()), "安装日志有内容")
        checker.check(
            "本阶段" in progress._stage_count_label.cget("text"),
            f"安装过程中显示阶段进度：{progress._stage_count_label.cget('text')}",
        )
        path = _shot(root, "progress", checker)
        if path:
            shots.append(path)
        ok = pump(root, until=lambda: app.current_page_name == "done", timeout=30.0)
        checker.check(ok, "安装完成后自动进入完成页")
        done = app.get_page("done")
        pump(root, 0.3)
        checker.check(done._status == "success", "完成页处于成功态")
        checker.check("安装完成" in done._title_label.cget("text"), "完成页标题为安装成功")
        path = _shot(root, "done", checker)
        if path:
            shots.append(path)

        # ---- 进度页：错误态 ----
        services.fail_next_install = True
        second = download._data["release"][1]
        app.show_page("progress", version=second, kind="vanilla", loader_version=None)
        ok = pump(root, until=lambda: app.current_page_name == "done", timeout=15.0)
        pump(root, 0.3)
        checker.check(ok and done._status == "failure", "安装异常时完成页进入失败态")
        checker.check(services.fail_next_install is False, "错误只触发一次")
        path = _shot(root, "done_error", checker)
        if path:
            shots.append(path)

        # ---- 进度页：取消 ----
        app.show_page("progress", version=second, kind="vanilla", loader_version=None)
        ok = pump(root, until=lambda: progress.is_running, timeout=10.0)
        checker.check(ok, "取消测试：安装任务已启动")
        pump(root, 0.25)
        progress.cancel_install()
        ok = pump(root, until=lambda: app.current_page_name == "done", timeout=20.0)
        pump(root, 0.3)
        checker.check(ok and done._status == "cancelled", "取消后进入“安装已取消”状态")
        checker.check(
            done._title_label.cget("text") == "安装已取消",
            "取消态文案正确",
        )

        # ---- 设置页 ----
        app.show_page("settings")
        pump(root, 0.3)
        settings = app.get_page("settings")
        checker.check(bool(settings._root_var.get()), "设置页读取到 .minecraft 目录")
        checker.check(settings._mirror_value == "auto", "下载源默认仍为 auto（界面文案：自动（优先镜像））")
        settings.detect_launchers()
        ok = pump(root, until=lambda: services.detect_calls > 0 and not settings._detect_running, timeout=10.0)
        checker.check(ok, "自动检测游戏目录完成")
        checker.check(settings._detection is not None, "检测结果已回填")
        detect_text = settings._detect_label.cget("text")
        checker.check("启动器" not in detect_text, "目录检测结果不出现“启动器”字样")
        checker.check(
            "PCL2 启动器" not in detect_text and "HMCL 启动器" not in detect_text,
            "目录检测结果不出现 PCL2/HMCL 品牌名",
        )
        checker.check("\n\n" not in detect_text.strip(), "目录检测结果没有空行")
        checker.check(
            str(Path("C:/Users/Test/PCL/.minecraft")) in detect_text
            and str(Path("D:/Games/HMCL/.minecraft")) in detect_text,
            "目录检测结果列出检测到的目录路径",
        )
        checker.check(
            "（12 个版本）" in detect_text and "（5 个版本）" in detect_text,
            f"目录检测结果显示版本数：{detect_text!r}",
        )
        settings._threads_var.set("16")
        settings._remember_var.set("45")
        settings._mirror_value = "bmclapi"
        settings._on_mirror_change("bmclapi")
        settings.save()
        pump(root, 0.2)
        checker.check(services.save_config_calls > 0, "保存设置调用了 services.save_config()")
        checker.check(
            services.config.threads == 16
            and services.config.remember_days == 45
            and services.config.mirror == "bmclapi",
            "设置写入 config（线程 16 / 记住 45 天 / BMCLAPI）",
        )
        path = _shot(root, "settings", checker)
        if path:
            shots.append(path)

        # ---- 双启动器目录选择对话框 ----
        detection = FakeDetection(
            probes=[
                FakeProbe("pcl", True, Path("C:/Users/Test/PCL/.minecraft")),
                FakeProbe("hmcl", True, Path("D:/Games/HMCL/.minecraft")),
            ],
            candidate_roots=[
                Path("C:/Users/Test/PCL/.minecraft"),
                Path("D:/Games/HMCL/.minecraft"),
            ],
            default_root=Path("C:/Users/Test/PCL/.minecraft"),
            needs_choice=True,
            reason="检测到多个启动器",
            root_versions=[
                (Path("C:/Users/Test/PCL/.minecraft"), 12),
                (Path("D:/Games/HMCL/.minecraft"), 5),
            ],
        )
        services.detection = detection
        services.config.mc_root = None
        app._root_dialog_shown = False
        app.show_root_dialog(detection)
        pump(root, 0.4)
        dialog = app._root_dialog
        checker.check(dialog is not None, "检测到多个启动器时弹出选择对话框")
        if dialog is not None:
            checker.check(
                any(count == 12 for _path, count in dialog._collect_candidates(detection)),
                "目录选择对话框显示版本数",
            )
            dpath = _shot(dialog, "dialog", checker, box=_widget_box(dialog))
            if dpath:
                shots.append(dpath)
            target = str(detection.candidate_roots[1])
            dialog._choose(target)
            pump(root, 0.3)
            checker.check(services.chosen_roots and services.chosen_roots[-1] == target, "选择目录后调用 choose_root")
            checker.check(app._root_dialog is None, "选择后对话框关闭")

        # ---- Java 检查 / 版本重命名 / 音效（自绘 dialogs 打桩） ----
        from . import dialogs as dialogs_module

        orig_ask_yes_no = dialogs_module.ask_yes_no
        orig_ask_string = dialogs_module.ask_string
        orig_show_info = dialogs_module.show_info
        orig_show_warning = dialogs_module.show_warning
        dialog_calls: dict[str, list] = {"yesno": [], "askstring": [], "showinfo": [], "showwarning": []}
        dialog_answers: dict[str, Any] = {"yesno": True, "askstring": []}

        def fake_askyesno(title, message, *, icon="warning", sound_kind="warn", parent=None):
            dialog_calls["yesno"].append((title, message, icon))
            return bool(dialog_answers["yesno"])

        def fake_askstring(title, prompt, *, initialvalue="", sound_kind="info", parent=None):
            dialog_calls["askstring"].append((title, prompt, initialvalue))
            queue = dialog_answers["askstring"]
            return queue.pop(0) if queue else ""

        def fake_showinfo(title, message, *, parent=None):
            dialog_calls["showinfo"].append((title, message))
            return None

        def fake_showwarning(title, message, *, parent=None):
            dialog_calls["showwarning"].append((title, message))
            return None

        dialogs_module.ask_yes_no = fake_askyesno
        dialogs_module.ask_string = fake_askstring
        dialogs_module.show_info = fake_showinfo
        dialogs_module.show_warning = fake_showwarning

        # ② Java 缺失：选择“是” → 先下载 Java 运行时再安装
        download = app.get_page("download")
        app.show_page("download")
        pump(root, 0.25)
        services.java_status_value = {
            "required_major": 17,
            "java_path": None,
            "component": "java-runtime-delta",
            "need_download": True,
        }
        dialog_answers["yesno"] = True
        dialog_calls["yesno"].clear()
        before_installs = len(services.install_calls)
        before_java = len(services.java_download_calls)
        download._select(first)
        download._select_kind("vanilla")
        pump(root, 0.15)
        download._start()
        pump(root, 0.4)
        progress = app.get_page("progress")
        checker.check(bool(dialog_calls["yesno"]), "Java 缺失时弹出询问框")
        checker.check(
            "Java" in dialog_calls["yesno"][-1][1] and "runtime" in dialog_calls["yesno"][-1][1],
            "Java 询问文案包含版本与 runtime 信息",
        )
        checker.check(
            app.current_page_name == "progress" and progress._params.get("download_java") is True,
            "Java 分支：进度页收到 download_java=True",
        )
        ok = pump(
            root,
            until=lambda: len(services.java_download_calls) > before_java and app.current_page_name == "done",
            timeout=40.0,
        )
        checker.check(ok, "Java 运行时下载完成后继续安装并进入完成页")
        checker.check(len(services.install_calls) > before_installs, "Java 分支：最终调用了 install")

        # ③ Java 缺失：选择“否” → 不下载 Java，但仍继续安装
        services.java_status_value["need_download"] = True
        dialog_answers["yesno"] = False
        dialog_calls["showinfo"].clear()
        before_java = len(services.java_download_calls)
        app.show_page("download")
        pump(root, 0.25)
        download._select(first)
        download._select_kind("vanilla")
        pump(root, 0.15)
        download._start()
        pump(root, 0.35)
        progress = app.get_page("progress")
        checker.check(
            progress._params.get("download_java") is False,
            "Java 分支：选择“否”时 download_java=False",
        )
        checker.check(bool(dialog_calls["showinfo"]), "Java 分支：拒绝下载时提示可能影响 Forge/NeoForge")
        checker.check(len(services.java_download_calls) == before_java, "Java 分支：拒绝下载时不调用 download_java")
        pump(root, until=lambda: progress.is_running, timeout=5.0)
        progress.cancel_install()
        pump(root, until=lambda: app.current_page_name == "done", timeout=20.0)

        # ④ 已存在版本：非法字符重试 → 自定义版本名
        services.java_status_value["need_download"] = False
        services.existing_version_value = "1.20.4"
        dialog_answers["askstring"] = ["bad:name", "我的版本"]
        dialog_calls["askstring"].clear()
        dialog_calls["showwarning"].clear()
        app.show_page("download")
        pump(root, 0.25)
        download._select(first)
        download._select_kind("vanilla")
        pump(root, 0.15)
        before_installs = len(services.install_calls)
        download._start()
        pump(root, 0.4)
        checker.check(len(dialog_calls["askstring"]) >= 2, "版本重命名：非法字符会重新询问")
        checker.check(bool(dialog_calls["showwarning"]), "版本重命名：非法字符给出警告")
        checker.check(
            pump(root, until=lambda: len(services.install_calls) > before_installs, timeout=15.0),
            "版本重命名：确认后开始安装",
        )
        checker.check(
            services.custom_version_names and services.custom_version_names[-1] == "我的版本",
            "版本重命名：custom_version_name 传给 install",
        )
        pump(root, until=lambda: app.current_page_name == "done", timeout=40.0)

        # ⑤ 已存在版本：留空 → 使用现有版本
        dialog_answers["askstring"] = [""]
        app.show_page("download")
        pump(root, 0.25)
        download._select(first)
        download._select_kind("vanilla")
        pump(root, 0.15)
        before_installs = len(services.install_calls)
        download._start()
        checker.check(
            pump(root, until=lambda: len(services.install_calls) > before_installs, timeout=15.0),
            "版本重命名：留空后继续安装",
        )
        checker.check(services.custom_version_names[-1] is None, "版本重命名：留空使用现有版本")
        pump(root, until=lambda: progress.is_running, timeout=5.0)
        progress.cancel_install()
        pump(root, until=lambda: app.current_page_name == "done", timeout=20.0)

        # ⑥ 已存在版本：取消输入 → 不开始安装
        dialog_answers["askstring"] = [None]
        before_installs = len(services.install_calls)
        app.show_page("download")
        pump(root, 0.25)
        download._select(first)
        download._select_kind("vanilla")
        pump(root, 0.15)
        download._start()
        pump(root, 0.3)
        checker.check(app.current_page_name == "download", "版本重命名：取消输入后停留在下载页")
        checker.check(len(services.install_calls) == before_installs, "版本重命名：取消输入不开始安装")

        # ⑦ 音效模块（真实 winsound.Async；非 Windows 静默）
        from . import sound

        sound.reset()
        played = [sound.play(kind) for kind in ("info", "warn", "error", "success")]
        checker.check(all(played), f"音效模块四种提示音均可调用：{played}")
        sound.reset()
        first_play = sound.play("info")
        second_play = sound.play("info")
        checker.check(
            first_play is True and second_play is False,
            "音效节流：0.3 秒内同类提示不重复播放",
        )
        time.sleep(0.32)
        checker.check(sound.play("info") is True, "音效节流：超过 0.3 秒后可再次播放")

        refreshed = [flag for flag in services.version_refresh_flags if flag]
        checker.check(
            len(refreshed) == 1,
            f"仅首次进入下载页强制联网刷新（refresh=True 次数：{len(refreshed)}）",
        )

        # ---- 依次切换全部 5 个页面 ----
        app.selected_version = None
        app.selected_kind = "vanilla"
        for name in PAGES:
            app.show_page(name)
            pump(root, 0.18)
            checker.check(app.current_page_name == name, f"页面切换：{name}")
        checker.check(set(PAGES).issubset(set(app._pages)), "5 个页面均已创建")

        # ---- 进度显示口径：字节优先、整数百分比、阶段进度独立显示 ----
        # 用独立实例验证，避免真实安装线程仍在排队的事件覆盖断言值
        from .pages.progress import ProgressPage

        display = ProgressPage(root, app)
        display._on_progress(
            FakeProgress(
                done=5154,
                total=5155,
                bytes_done=int(586.23 * 1024 * 1024),
                bytes_total=int(586.80 * 1024 * 1024),
                speed=4.2 * 1024 * 1024,
                message="正在下载资源文件",
                stage="资源文件",
                stage_done=5056,
                stage_total=5057,
            )
        )
        closing_percent = display._percent_label.cget("text")
        checker.check(closing_percent == "100%", f"收尾误差按 100% 显示：{closing_percent}")
        checker.check("." not in closing_percent, "百分比为整数格式（不出现小数）")
        checker.check(
            display._count_label.cget("text") == "5154 / 5155",
            f"文件进度显示整体 done/total：{display._count_label.cget('text')}",
        )
        stage_text = display._stage_count_label.cget("text")
        checker.check(
            "本阶段" in stage_text and "5056 / 5057" in stage_text,
            f"阶段内进度单独显示：{stage_text}",
        )
        size_text = display._size_label.cget("text")
        checker.check(
            "586.23" in size_text and "586.80" in size_text,
            f"已下载/总大小按字节显示：{size_text}",
        )
        display._on_progress(
            FakeProgress(
                done=4860,
                total=5155,
                bytes_done=int(552.4 * 1024 * 1024),
                bytes_total=int(586.8 * 1024 * 1024),
                speed=4.2 * 1024 * 1024,
                message="正在下载资源文件",
                stage="资源文件",
                stage_done=4858,
                stage_total=5155,
            )
        )
        mid_percent = display._percent_label.cget("text")
        checker.check(
            mid_percent.endswith("%") and "." not in mid_percent,
            f"中间进度整数显示：{mid_percent}",
        )
        checker.check(
            int(mid_percent.rstrip("%")) == 94,
            f"字节口径百分比正确：{mid_percent}",
        )
        # 转场保护：done/total 暂时到 100% 而字节口径才刚开始时，应以字节为准
        display._on_progress(
            FakeProgress(
                done=100,
                total=100,
                bytes_done=50,
                bytes_total=500,
                speed=1.0,
                message="正在下载资源文件",
                stage="资源文件",
                stage_done=5,
                stage_total=50,
            )
        )
        transition_percent = display._percent_label.cget("text")
        checker.check(
            transition_percent == "10%",
            f"转场时按字节算百分比（避免 done/total 假 100%）：{transition_percent}",
        )
        checker.check(
            display._count_label.cget("text") == "100 / 100",
            f"文件进度仍显示整体 done/total：{display._count_label.cget('text')}",
        )
        # 旧后端没有阶段分母时只显示占位，不拼接错误数字
        display._on_progress(
            FakeProgress(
                done=4860,
                total=5155,
                bytes_done=int(552.4 * 1024 * 1024),
                bytes_total=int(586.8 * 1024 * 1024),
                speed=4.2 * 1024 * 1024,
                message="正在下载资源文件",
                stage="资源文件",
                stage_done=0,
                stage_total=0,
            )
        )
        checker.check(
            display._stage_count_label.cget("text") in ("本阶段 —", ""),
            f"无阶段分母时显示占位：{display._stage_count_label.cget('text')}",
        )
        display.destroy()

        # ---- 关闭确认：有进行中任务时先询问（request_close 替代直接 close） ----
        progress = app.get_page("progress")
        progress.start_task(lambda: time.sleep(1.5), name="fake-close")
        checker.check(
            pump(root, until=lambda: progress.is_running, timeout=5.0),
            "关闭确认：模拟进行中任务",
        )
        dialog_answers["yesno"] = False
        dialog_calls["yesno"].clear()
        closed = app.request_close()
        checker.check(closed is False, "关闭确认：选“否”不关闭窗口")
        alive = False
        try:
            alive = bool(root.winfo_exists())
        except tk.TclError:
            alive = False
        checker.check(alive, "关闭确认：选“否”后窗口仍存在")
        checker.check(
            bool(dialog_calls["yesno"]) and dialog_calls["yesno"][-1][0] == "有正在进行的任务",
            "关闭确认：提示标题为“有正在进行的任务”",
        )
        checker.check(
            "不会保留" in dialog_calls["yesno"][-1][1],
            "关闭确认：提示文案说明下载进度不会保留",
        )
        progress.cancel_task()
        pump(root, until=lambda: not progress.is_running, timeout=8.0)
        dialog_answers["yesno"] = True
        closed = app.request_close()
        checker.check(closed is True, "关闭确认：选“是”后关闭窗口")
        alive = False
        try:
            alive = bool(root.winfo_exists())
        except tk.TclError:
            alive = False
        checker.check(not alive, "WM_DELETE / close() 销毁窗口")
        root = None

        dialogs_module.ask_yes_no = orig_ask_yes_no
        dialogs_module.ask_string = orig_ask_string
        dialogs_module.show_info = orig_show_info
        dialogs_module.show_warning = orig_show_warning

        # ---- create_app() 无 services 的降级路径 ----
        try:
            theme.reset()
            root2, app2 = create_app(None, AppConfig())
            pump(root2, 0.3)
            checker.check(app2.current_page_name == "login", "create_app(None) 降级到登录页")
            app2.close()
            pump(root2, 0.05)
        except Exception as exc:  # noqa: BLE001
            checker.check(False, f"create_app(None) 降级路径异常：{exc}")

        if PIL_OK:
            checker.check(len(shots) >= 6, f"截图数量 {len(shots)}（至少 6 张）")
    except Exception as exc:  # noqa: BLE001 - 自测自身异常也要给出退出码
        import traceback

        traceback.print_exc()
        checker.check(False, f"自测流程异常：{type(exc).__name__}: {exc}")
    finally:
        if root is not None:
            try:
                root.destroy()
            except tk.TclError:
                pass

    print()
    print(f"== 结果：{checker.passed} 项通过，{len(checker.failures)} 项失败 ==")
    if shots:
        print("截图：")
        for path in shots:
            print(f"  - {path}")
    if checker.failures:
        print("失败项：")
        for label in checker.failures:
            print(f"  - {label}")
        return 1
    print("SELFTEST PASS")
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())

#: ``app.py --selftest`` 与打包后冒烟入口使用的别名。
run_selftest = main
