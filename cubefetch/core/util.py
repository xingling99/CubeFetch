"""CubeFetch 公共工具函数。

本模块被所有其他模块依赖，签名保持稳定（见 docs/CONTRACTS.md 第 2 节）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

log = logging.getLogger("cubefetch.util")

_CHUNK = 1024 * 256


def sha1_file(path: Path) -> str:
    """返回文件 SHA-1（小写十六进制）。文件不存在时返回空串。"""
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha1()
    with p.open("rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def sha256_file(path: Path) -> str:
    """返回文件 SHA-256（小写十六进制）。文件不存在时返回空串。"""
    p = Path(path)
    if not p.is_file():
        return ""
    h = hashlib.sha256()
    with p.open("rb") as f:
        while True:
            block = f.read(_CHUNK)
            if not block:
                break
            h.update(block)
    return h.hexdigest()


def human_size(n: int) -> str:
    """把字节数格式化为易读字符串。"""
    try:
        value = float(n)
    except (TypeError, ValueError):
        return "0 B"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024.0 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.2f} {unit}"
        value /= 1024.0
    return f"{value:.2f} TB"


def ensure_dir(p: Path) -> Path:
    """创建目录（含父目录）并返回。"""
    path = Path(p)
    path.mkdir(parents=True, exist_ok=True)
    return path


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写入：先写同目录临时文件再 os.replace。"""
    target = Path(path)
    ensure_dir(target.parent)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        with tmp.open("wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def atomic_write_text(path: Path, text: str, encoding: str = "utf-8") -> None:
    atomic_write_bytes(path, text.encode(encoding))


def read_json(path: Path, default=None):
    """读取 JSON；文件不存在或损坏时返回 default。"""
    p = Path(path)
    if not p.is_file():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as exc:  # 损坏的缓存不应导致程序退出
        log.warning("读取 JSON 失败 %s: %s", p, exc)
        return default


def write_json(path: Path, obj) -> None:
    atomic_write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def now() -> float:
    return time.time()


def run_process(
    args: list[str],
    cwd: Path | None = None,
    timeout: float | None = None,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    """运行外部进程并捕获输出（UTF-8，忽略解码错误）。

    Windows 下隐藏控制台窗口。超时抛 subprocess.TimeoutExpired。
    """
    kwargs: dict = {
        "cwd": str(cwd) if cwd else None,
        "capture_output": True,
        "text": True,
        "encoding": "utf-8",
        "errors": "replace",
        "timeout": timeout,
        "env": {**os.environ, **(env or {})},
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run([str(a) for a in args], **kwargs)


def _reg_read(key: str, value: str) -> str | None:
    """读取注册表字符串值（仅 Windows）。"""
    if sys.platform != "win32":
        return None
    try:
        import winreg  # noqa: PLC0415  (仅 Windows 存在)
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as handle:
            data, _ = winreg.QueryValueEx(handle, value)
            return str(data)
    except OSError:
        return None


def _pcl_java_folders() -> list[Path]:
    """从 PCL2 的 config.json 的 JavaList 中读取 Java 路径。"""
    appdata = os.environ.get("APPDATA")
    if not appdata:
        return []
    cfg = Path(appdata) / "PCL" / "config.json"
    data = read_json(cfg, None)
    if not isinstance(data, dict):
        return []
    result: list[Path] = []
    for item in data.get("JavaList") or []:
        folder = item.get("Folder") if isinstance(item, dict) else None
        if not folder:
            continue
        p = Path(str(folder))
        exe = p / "java.exe" if p.is_dir() else p
        if exe.is_file():
            result.append(exe)
    return result


def find_java_candidates() -> list[Path]:
    """返回本机可用的 java.exe 候选（去重、保序）。"""
    found: list[Path] = []

    def add(p: Path | str | None) -> None:
        if not p:
            return
        path = Path(p)
        if path.is_dir():
            path = path / "java.exe"
        if path.is_file() and path.name.lower() == "java.exe" and path not in found:
            found.append(path)

    add(shutil.which("java"))
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        add(Path(java_home) / "bin" / "java.exe")

    program_files = [os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)")]
    vendor_globs = [
        "Java/*/bin/java.exe",
        "Eclipse Adoptium/*/bin/java.exe",
        "Eclipse Foundation/*/bin/java.exe",
        "Microsoft/jdk*/*/bin/java.exe",
        "Zulu/*/bin/java.exe",
        "BellSoft/Liberica*/*/bin/java.exe",
        "Amazon Corretto/*/bin/java.exe",
        "Semeru/*/bin/java.exe",
    ]
    for base in program_files:
        if not base:
            continue
        for pattern in vendor_globs:
            for match in sorted(Path(base).glob(pattern), reverse=True):
                add(match)

    appdata = os.environ.get("APPDATA")
    if appdata:
        for runtime in sorted((Path(appdata) / ".minecraft" / "runtime").glob("*/bin/java.exe")):
            add(runtime)

    for exe in _pcl_java_folders():
        add(exe)
    return found


def java_major_version(java_exe: Path) -> int | None:
    """解析 java -version 输出中的主版本号，失败返回 None。"""
    try:
        proc = run_process([str(java_exe), "-version"], timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    text = (proc.stderr or "") + (proc.stdout or "")
    # 常见形式: java version "1.8.0_401" / openjdk version "21.0.2" 2024-01-16
    match = re.search(r'version\s+"(\d+)(?:\.(\d+))?', text)
    if not match:
        return None
    major = int(match.group(1))
    if major == 1 and match.group(2):
        return int(match.group(2))
    return major


def pick_java(required_major: int | None = None) -> Path | None:
    """挑选一个 Java；指定 major 时优先精确匹配，其次不低于要求的最新版本。"""
    candidates = find_java_candidates()
    if not candidates:
        return None
    if required_major is None:
        return candidates[0]
    scored: list[tuple[int, Path]] = []
    for exe in candidates:
        major = java_major_version(exe)
        if major is None:
            continue
        scored.append((major, exe))
    if not scored:
        return candidates[0]
    exact = [p for m, p in scored if m == required_major]
    if exact:
        return exact[0]
    higher = sorted((m, p) for m, p in scored if m >= required_major)
    if higher:
        return higher[0][1]
    return max(scored)[1]


def is_admin() -> bool:
    """当前进程是否以管理员运行。"""
    if sys.platform != "win32":
        return os.geteuid() == 0 if hasattr(os, "geteuid") else False
    try:
        import ctypes  # noqa: PLC0415

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001  权限探测不应影响主流程
        return False


def open_in_explorer(path: Path) -> None:
    """在资源管理器中打开目录（尽力而为）。"""
    target = Path(path)
    try:
        if sys.platform == "win32":
            os.startfile(str(target))  # type: ignore[attr-defined]  # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(target)])  # noqa: S603,S607
        else:
            subprocess.Popen(["xdg-open", str(target)])  # noqa: S603,S607
    except OSError as exc:
        log.warning("打开目录失败 %s: %s", target, exc)
