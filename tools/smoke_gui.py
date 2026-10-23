"""GUI 冒烟：启动 ``app.py``（或 ``dist\\CubeFetch.exe``），等窗口出现后截图并退出。

只用于开发期验证（需要一个可交互桌面）。

用法：
    python -X utf8 tools\\smoke_gui.py              # 源码方式
    python -X utf8 tools\\smoke_gui.py --exe        # 打包后的 exe
    python -X utf8 tools\\smoke_gui.py --exe --out docs/shots/real_exe_login.png
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable

#: 本脚本自己声明 DPI 感知，这样 GetWindowRect 与 ImageGrab 都在物理像素坐标系里。
try:  # noqa: SIM105
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:  # noqa: BLE001  Win7/无 shcore 时退化为不感知
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:  # noqa: BLE001
        pass


def _window_process_name(hwnd: int) -> str:
    """返回窗口所属进程的可执行文件名（如 ``CubeFetch.exe`` / ``python.exe``）。"""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = wt.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if not pid.value:
        return ""
    handle = kernel32.OpenProcess(0x1000, False, pid.value)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wt.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return ""
    finally:
        kernel32.CloseHandle(handle)


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", ctypes.wintypes.DWORD),
        ("biWidth", ctypes.wintypes.LONG),
        ("biHeight", ctypes.wintypes.LONG),
        ("biPlanes", ctypes.wintypes.WORD),
        ("biBitCount", ctypes.wintypes.WORD),
        ("biCompression", ctypes.wintypes.DWORD),
        ("biSizeImage", ctypes.wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.wintypes.LONG),
        ("biYPelsPerMeter", ctypes.wintypes.LONG),
        ("biClrUsed", ctypes.wintypes.DWORD),
        ("biClrImportant", ctypes.wintypes.DWORD),
    ]


def _capture_client(hwnd: int):
    """用 PrintWindow 抓客户区像素（不受其它窗口遮挡影响）；失败返回 None。

    后台进程创建的窗口在前台有其它窗口时可能被遮挡，ImageGrab 只会截到遮挡物，
    因此优先用 PrintWindow(PW_CLIENTONLY) 直接让窗口重绘到内存 DC，再配 ImageGrab 兜底。
    """
    try:
        from PIL import Image
    except Exception:  # noqa: BLE001
        return None
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    user32.GetDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
    gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
    gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
    gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = ctypes.c_void_p
    gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    gdi32.GetDIBits.restype = ctypes.c_int
    gdi32.GetDIBits.argtypes = [
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.c_uint,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint,
    ]
    gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
    gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
    user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]

    client = wt.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        return None
    width, height = int(client.right), int(client.bottom)
    if width <= 0 or height <= 0:
        return None

    hdc_win = user32.GetDC(hwnd)
    if not hdc_win:
        return None
    hdc_mem = gdi32.CreateCompatibleDC(hdc_win)
    hbmp = gdi32.CreateCompatibleBitmap(hdc_win, width, height)
    old = gdi32.SelectObject(hdc_mem, hbmp) if (hdc_mem and hbmp) else None
    try:
        if not hdc_mem or not hbmp:
            return None
        if not user32.PrintWindow(hwnd, hdc_mem, 1):  # PW_CLIENTONLY
            return None
        bmi = _BITMAPINFOHEADER()
        bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        bmi.biWidth = width
        bmi.biHeight = -height  # 负数 = top-down
        bmi.biPlanes = 1
        bmi.biBitCount = 32
        bmi.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        lines = gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)
        if lines != height:
            return None
        img = Image.frombuffer("RGB", (width, height), buf, "raw", "BGRX", 0, 1)
        gray = img.convert("L")
        low, high = gray.getextrema()
        if high - low < 8:  # 全黑/纯色说明没有渲染出来，交给 ImageGrab 兜底
            return None
        return img.copy()
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            if old:
                gdi32.SelectObject(hdc_mem, old)
        except Exception:  # noqa: BLE001
            pass
        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(hwnd, hdc_win)


def _find_window(keyword: str, process_name: str = "") -> int | None:
    found: list[int] = []
    user32 = ctypes.windll.user32

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def _cb(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        if keyword.lower() in buf.value.lower() and user32.IsWindowVisible(hwnd):
            # 必须确认窗口属于我们刚启动的进程，避免误抓标题里含同名的其它窗口
            # （例如标题里带项目名的编辑器/终端窗口）。
            if process_name and _window_process_name(int(hwnd)).lower() != process_name.lower():
                return True
            found.append(int(hwnd))
            return False
        return True

    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CubeFetch GUI 冒烟截图")
    parser.add_argument("--exe", action="store_true", help="启动 dist\\CubeFetch.exe 而不是 app.py")
    parser.add_argument("--out", default=None, help="截图输出路径（默认 docs/shots/real_app.png）")
    parser.add_argument("--wait", type=float, default=3.0, help="窗口出现后等待秒数")
    parser.add_argument("--exe-path", default=None, help="指定 exe 路径（默认 dist\\CubeFetch.exe）")
    args = parser.parse_args(argv)

    out = Path(args.out) if args.out else (
        ROOT / "docs" / "shots" / ("real_exe_login.png" if args.exe else "real_app.png")
    )
    if not out.is_absolute():
        out = (ROOT / out).resolve()

    if args.exe:
        command = [str(Path(args.exe_path) if args.exe_path else ROOT / "dist" / "CubeFetch.exe")]
    else:
        command = [PY, "-X", "utf8", str(ROOT / "app.py")]

    proc = subprocess.Popen(
        command,
        cwd=str(ROOT),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    try:
        hwnd = None
        expected_process = "CubeFetch.exe" if args.exe else "python.exe"
        deadline = time.time() + 30
        while time.time() < deadline and hwnd is None:
            if proc.poll() is not None:
                print("!! 进程提前退出", proc.returncode)
                print(proc.stdout.read() if proc.stdout else "")
                return 1
            hwnd = _find_window("CubeFetch", expected_process)
            if hwnd is None:
                time.sleep(0.3)
        if hwnd is None:
            print("!! 30 秒内没有找到 CubeFetch 窗口")
            return 1
        time.sleep(args.wait)  # 留给 bootstrap 与首屏渲染
        # 优先用 PrintWindow 抓客户区：后台进程窗口被其它窗口遮挡时也能拿到内容。
        try:
            img = _capture_client(hwnd)
            if img is not None:
                out.parent.mkdir(parents=True, exist_ok=True)
                img.save(out)
                print(f"截图已保存 {out}（{img.size[0]}x{img.size[1]}，PrintWindow）")
                return 0
        except Exception as exc:  # noqa: BLE001
            print(f"!! PrintWindow 截图失败，改用屏幕抓取：{exc}")

        user32 = ctypes.windll.user32
        try:
            # 回退路径：HWND_TOPMOST + 置前，减少被终端等窗口遮挡。
            user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)  # NOSIZE | NOMOVE
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.6)
        except Exception:  # noqa: BLE001
            pass
        # 只截客户区（不含标题栏/DWM 阴影）
        client = wt.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(client))
        origin = wt.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(origin))
        box = (origin.x, origin.y, origin.x + client.right, origin.y + client.bottom)
        print(f"窗口句柄 {hwnd} 客户区 {box}")
        result = 1
        try:
            from PIL import ImageGrab

            img = ImageGrab.grab(bbox=box)
            out.parent.mkdir(parents=True, exist_ok=True)
            img.save(out)
            print(f"截图已保存 {out}（{img.size[0]}x{img.size[1]}）")
            result = 0
        except Exception as exc:  # noqa: BLE001
            print(f"!! 截图失败：{exc}")
        return result
    finally:
        # PyInstaller onefile 的父进程会再拉起子进程，必须整棵树杀掉，
        # 否则子进程继续运行、并占住 stdout 管道让读取永久阻塞。
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=15,
            )
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass
        try:
            out_text, _ = proc.communicate(timeout=10)
        except Exception:  # noqa: BLE001
            out_text = ""
        if out_text and out_text.strip():
            print("--- 进程 stdout/stderr ---")
            print(out_text[-4000:])


if __name__ == "__main__":
    raise SystemExit(main())
