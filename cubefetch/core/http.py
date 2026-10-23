"""HTTP 会话、镜像源重写与并发下载。

本模块是安装引擎唯一的网络出口：上层（manifest/install/loader 等）不得直接
使用 requests，所有网络/校验错误都必须转换为 :mod:`cubefetch.core.errors`
中的异常类型（见 docs/CONTRACTS.md §3）。
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import requests

from ..config import MIRRORS
from .errors import CancelledError, CubeFetchError, InstallError, NetworkError
from .util import ensure_dir, sha1_file

log = logging.getLogger("cubefetch.http")

#: BMCLAPI 镜像根地址（官方 bmclapi2 节点）
BMCLAPI_BASE = "https://bmclapi2.bangbang93.com"

#: 镜像重写规则：(官方 URL 前缀, 镜像附加路径)。
#: 官方 host 类规则附加路径为空、保留完整 path；带斜杠的规则把剩余 path 追加到附加路径后。
MIRROR_RULES: tuple[tuple[str, str], ...] = (
    ("https://piston-meta.mojang.com", ""),
    ("https://launchermeta.mojang.com", ""),
    ("https://piston-data.mojang.com", ""),
    ("https://launcher.mojang.com", ""),
    ("https://libraries.minecraft.net/", "/maven/"),
    ("https://resources.download.minecraft.net/", "/assets/"),
    ("https://meta.fabricmc.net/", "/fabric-meta/"),
    ("https://maven.fabricmc.net/", "/maven/"),
    ("https://maven.neoforged.net/releases/", "/maven/"),
)

_CHUNK_SIZE = 256 * 1024
_PROGRESS_INTERVAL = 0.1
_DEFAULT_USER_AGENT = "CubeFetch/1.0"

#: 下载源模式别名（口语化的 "mirror" 等价于纯镜像 "bmclapi"）。
_MIRROR_ALIASES = {"mirror": "bmclapi"}


def _normalize_mirror_mode(mirror: str) -> str:
    """规范化下载源模式：未知值回退 ``auto``，并接受 ``mirror`` 别名。"""
    value = str(mirror or "auto").strip().lower()
    value = _MIRROR_ALIASES.get(value, value)
    if value not in MIRRORS:
        log.warning("未知镜像模式 %r，回退为 auto", mirror)
        return "auto"
    return value


# ---------------------------------------------------------------------------
# 对外数据结构
# ---------------------------------------------------------------------------


@dataclass
class Progress:
    """安装/下载进度。

    可被任意工作线程调用 ``progress_cb`` 更新；``message`` 是给用户看的中文阶段文案。

    口径约定（安装引擎所有阶段一致）：

    - ``done`` / ``total`` 永远是**整体**文件数；
    - ``bytes_done`` / ``bytes_total`` 永远是**整体**字节数；
    - ``stage`` / ``stage_done`` / ``stage_total`` 描述**当前阶段内**的进度
      （例如资源文件阶段 ``stage_done/stage_total = 5056/5057``），
      不改变整体口径，也不应被 UI 当作总进度使用。

    新增字段都有默认值，属于向后兼容的扩展。
    """

    done: int = 0
    total: int = 0
    bytes_done: int = 0
    bytes_total: int = 0
    speed: float = 0.0
    message: str = ""
    stage: str = ""
    stage_done: int = 0
    stage_total: int = 0


@dataclass
class DownloadTask:
    """一个待下载文件。"""

    url: str
    dest: Path
    sha1: str | None = None
    size: int | None = None
    use_mirror: bool = True


# ---------------------------------------------------------------------------
# 镜像重写
# ---------------------------------------------------------------------------


def mirror_url(url: str, base: str | None = None) -> str:
    """把官方 URL 重写为镜像 URL；无法重写时原样返回。

    ``base`` 仅供测试/自定义镜像使用，默认 :data:`BMCLAPI_BASE`。
    """
    if not url:
        return url
    root = (base or BMCLAPI_BASE).rstrip("/")
    best: tuple[str, str] | None = None
    for prefix, extra in MIRROR_RULES:
        if url.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, extra)
    if best is None:
        return url
    prefix, extra = best
    rest = url[len(prefix) :]
    if prefix.endswith("/"):
        return f"{root}{extra}{rest}"
    return f"{root}{rest}"


# ---------------------------------------------------------------------------
# 内部异常：区分可重试（瞬时）与不可重试（永久）
# ---------------------------------------------------------------------------


class _TransientNetworkError(Exception):
    """瞬时错误（超时/连接失败/5xx/429/校验不符），可重试。"""


class _PermanentNetworkError(Exception):
    """永久错误（4xx 等），重试无意义。"""


class _AbortSignal:
    """把「用户取消」与「并发下载时其它任务失败」合并成一个取消信号。"""

    __slots__ = ("_cancel", "_abort")

    def __init__(self, cancel: threading.Event | None, abort: threading.Event) -> None:
        self._cancel = cancel
        self._abort = abort

    def is_set(self) -> bool:
        return self._abort.is_set() or (self._cancel is not None and self._cancel.is_set())

    def wait(self, timeout: float | None = None) -> bool:
        if self.is_set():
            return True
        if self._cancel is not None and self._cancel.wait(timeout):
            return True
        return self._abort.wait(timeout)

    def clear(self) -> None:  # pragma: no cover - 只为兼容 Event 接口
        self._abort.clear()


def _safe_unlink(path: Path) -> None:
    try:
        if path.is_file() or path.is_symlink():
            path.unlink()
    except OSError as exc:  # pragma: no cover - 极端文件系统错误
        log.debug("删除文件失败 %s: %s", path, exc)


def _safe_progress(callback: Callable[[Progress], None] | None, progress: Progress) -> None:
    if callback is None:
        return
    try:
        callback(progress)
    except Exception:  # noqa: BLE001 - 用户回调异常不影响安装
        log.debug("进度回调抛出异常", exc_info=True)


# ---------------------------------------------------------------------------
# HTTP 会话
# ---------------------------------------------------------------------------


class HttpSession:
    """带镜像回落、自动重试与断点续传的 HTTP 会话。

    下载源语义（见 task-7 / 用户需求「下载源尽量用镜像源」）：

    - ``auto``：优先 BMCLAPI 镜像；镜像超时/连接失败/429/5xx 或 403/404 时
      自动回落官方源。每次请求都从镜像开始，不会因为官方成功过就改走官方；
    - ``official``：只请求官方源；
    - ``bmclapi`` / ``mirror``：只请求镜像源（``mirror`` 是 ``bmclapi`` 的别名）。
    """

    def __init__(
        self,
        mirror: str = "auto",
        timeout: float = 25,
        retries: int = 3,
        user_agent: str = _DEFAULT_USER_AGENT,
        *,
        mirror_base: str | None = None,
        workers: int = 32,
        pool_size: int | None = None,
    ) -> None:
        self.mirror = _normalize_mirror_mode(mirror)
        self.timeout = float(timeout)
        self.retries = max(0, int(retries))
        self.user_agent = user_agent or _DEFAULT_USER_AGENT
        self.workers = max(1, int(workers))
        self._mirror_base = (mirror_base or BMCLAPI_BASE).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update(
            {
                "User-Agent": self.user_agent,
                "Accept": "*/*",
                "Accept-Encoding": "identity",
            }
        )
        pool = max(10, int(pool_size or self.workers))
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=pool, pool_maxsize=pool, max_retries=0
        )
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    # -- 镜像状态 ---------------------------------------------------------

    def set_mirror(self, mirror: str) -> None:
        """切换镜像模式；未知值回退为 auto。"""
        self.mirror = _normalize_mirror_mode(mirror)

    def clear_mirror_memo(self) -> None:
        """保留接口兼容：auto 模式固定镜像优先，不存在「记住官方」的偏好。"""

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:  # noqa: BLE001 - 关闭失败不影响退出
            log.debug("关闭 HTTP 会话失败", exc_info=True)

    def __enter__(self) -> HttpSession:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- 源选择 -----------------------------------------------------------

    def _sources(self, url: str, use_mirror: bool) -> list[str]:
        official = url
        mirrored = mirror_url(url, self._mirror_base)
        if not use_mirror or mirrored == official:
            return [official]
        if self.mirror == "official":
            return [official]
        if self.mirror == "bmclapi":
            return [mirrored]
        # auto：每次请求都优先镜像；镜像失败（超时/连接/429/5xx/403/404）后
        # 在同一轮重试里立刻回落官方，不记忆「官方优先」。
        return [mirrored, official]

    # -- 通用请求 ---------------------------------------------------------

    def get_json(self, url: str, *, use_mirror: bool = True) -> dict:
        data = self.get_bytes(url, use_mirror=use_mirror)
        try:
            obj = json.loads(data.decode("utf-8-sig"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NetworkError(
                "服务器返回的版本数据格式异常", detail=f"{url}: {exc}"
            ) from exc
        if not isinstance(obj, dict):
            raise NetworkError("服务器返回的版本数据格式异常", detail=f"{url}: 不是 JSON 对象")
        return obj

    def get_bytes(self, url: str, *, use_mirror: bool = True) -> bytes:
        sources = self._sources(url, use_mirror)
        last_error = ""
        permanent: set[str] = set()
        attempt = 0
        # 按来源顺序尝试（auto 模式镜像在前）：某个来源失败立刻换下一个；
        # 永久性错误（4xx）的来源不再重试，瞬时错误在下一轮重试。见 task-7。
        while attempt <= self.retries and len(permanent) < len(sources):
            retry_later = False
            for source in sources:
                if source in permanent:
                    continue
                try:
                    data = self._get_bytes_once(source, url)
                except _TransientNetworkError as exc:
                    last_error = str(exc)
                    retry_later = True
                    log.info("GET %s 失败（第 %d 轮）：%s", source, attempt + 1, exc)
                except _PermanentNetworkError as exc:
                    last_error = str(exc)
                    permanent.add(source)
                    log.warning("GET %s 失败：%s", source, exc)
                else:
                    return data
            if not retry_later:
                break
            attempt += 1
            if attempt <= self.retries:
                self._wait(0.3 * (2 ** (attempt - 1)), None)
        raise NetworkError(
            "无法连接服务器，请检查网络或切换下载镜像", detail=f"{url} ({last_error})"
        )

    def _get_bytes_once(self, source: str, original: str) -> bytes:
        try:
            response = self.session.get(source, timeout=self.timeout)
        except requests.RequestException as exc:
            raise _TransientNetworkError(f"网络错误：{exc}") from exc
        try:
            status = int(response.status_code)
            if status == 429:
                raise _TransientNetworkError("HTTP 429 请求过于频繁")
            if status >= 500:
                raise _TransientNetworkError(f"HTTP {status}")
            if status >= 400:
                raise _PermanentNetworkError(f"HTTP {status}")
            return response.content
        except requests.RequestException as exc:
            raise _TransientNetworkError(f"读取响应失败：{exc}") from exc
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    # -- 文件下载 ---------------------------------------------------------

    def download(
        self,
        url: str,
        dest: Path | str,
        *,
        sha1: str | None = None,
        size: int | None = None,
        use_mirror: bool = True,
        timeout: float | None = None,
        progress_cb: Callable[[int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        """下载 ``url`` 到 ``dest``。

        - 目标文件已存在且校验通过时直接返回；
        - 支持 ``.part`` 断点续传（服务器支持 Range 时）与原子替换；
        - ``progress_cb`` 收到「已写入字节数」（含续传的已有字节）；
        - 取消时抛出 :class:`~cubefetch.core.errors.CancelledError`。
        """
        target = Path(dest)
        normalized_sha1 = sha1.lower() if sha1 else None
        if self._file_ok(target, normalized_sha1, size):
            if progress_cb is not None:
                try:
                    progress_cb(target.stat().st_size)
                except Exception:  # noqa: BLE001
                    pass
            return
        if target.exists():
            _safe_unlink(target)

        last_error = ""
        sources = self._sources(url, use_mirror)
        permanent: set[str] = set()
        attempt = 0
        while attempt <= self.retries and len(permanent) < len(sources):
            self._check_cancel(cancel)
            retry_later = False
            for source in sources:
                if source in permanent:
                    continue
                self._check_cancel(cancel)
                try:
                    self._download_once(
                        source,
                        target,
                        normalized_sha1,
                        size,
                        timeout,
                        progress_cb,
                        cancel,
                    )
                except CancelledError:
                    raise
                except _TransientNetworkError as exc:
                    last_error = str(exc)
                    retry_later = True
                    log.info("下载 %s 失败（第 %d 轮）：%s", source, attempt + 1, exc)
                except _PermanentNetworkError as exc:
                    last_error = str(exc)
                    permanent.add(source)
                    log.warning("下载 %s 失败：%s", source, exc)
                else:
                    return
            if not retry_later:
                break
            attempt += 1
            if attempt <= self.retries:
                self._wait(0.4 * (2 ** (attempt - 1)), cancel)
        raise NetworkError(
            "文件下载失败，请检查网络或切换下载镜像",
            detail=f"{url} -> {target} ({last_error})",
        )

    def _download_once(
        self,
        source: str,
        target: Path,
        sha1: str | None,
        size: int | None,
        timeout: float | None,
        progress_cb: Callable[[int], None] | None,
        cancel: threading.Event | None,
    ) -> None:
        part = target.with_name(target.name + ".part")
        try:
            offset = part.stat().st_size if part.is_file() else 0
        except OSError:
            offset = 0
        if size is not None and offset > size:
            _safe_unlink(part)
            offset = 0

        headers = {"Range": f"bytes={offset}-"} if offset > 0 else None
        try:
            response = self.session.get(
                source,
                timeout=timeout or self.timeout,
                stream=True,
                headers=headers,
            )
        except requests.RequestException as exc:
            raise _TransientNetworkError(f"网络错误：{exc}") from exc

        try:
            status = int(response.status_code)
            if status == 416:
                _safe_unlink(part)
                raise _TransientNetworkError("服务器拒绝断点续传 (HTTP 416)")
            if status == 429:
                raise _TransientNetworkError("HTTP 429 请求过于频繁")
            if status >= 500:
                raise _TransientNetworkError(f"HTTP {status}")
            if status >= 400:
                raise _PermanentNetworkError(f"HTTP {status}")

            if status == 206 and offset > 0:
                start = _content_range_start(response.headers.get("Content-Range"))
                if start != offset:
                    _safe_unlink(part)
                    raise _TransientNetworkError("断点续传响应与本地文件不匹配")
                mode = "ab"
                written = offset
            else:
                offset = 0
                mode = "wb"
                written = 0

            hasher = hashlib.sha1() if sha1 else None
            if hasher is not None and mode == "ab":
                try:
                    with open(part, "rb") as existing:
                        while True:
                            chunk = existing.read(_CHUNK_SIZE)
                            if not chunk:
                                break
                            hasher.update(chunk)
                except OSError as exc:
                    _safe_unlink(part)
                    raise _TransientNetworkError(f"读取续传文件失败：{exc}") from exc

            try:
                ensure_dir(target.parent)
                with open(part, mode) as handle:
                    for chunk in response.iter_content(chunk_size=_CHUNK_SIZE):
                        if cancel is not None and cancel.is_set():
                            raise CancelledError("下载已取消")
                        if not chunk:
                            continue
                        try:
                            handle.write(chunk)
                        except OSError as exc:
                            raise InstallError(
                                "写入下载文件失败，请检查磁盘空间与权限",
                                detail=f"{target}: {exc}",
                            ) from exc
                        if hasher is not None:
                            hasher.update(chunk)
                        written += len(chunk)
                        if progress_cb is not None:
                            try:
                                progress_cb(written)
                            except Exception:  # noqa: BLE001
                                log.debug("下载进度回调抛出异常", exc_info=True)
            except requests.RequestException as exc:
                raise _TransientNetworkError(f"读取响应流失败：{exc}") from exc

            if size is not None and written != size:
                _safe_unlink(part)
                raise _TransientNetworkError(f"文件大小不匹配：期望 {size}，实际 {written}")
            if hasher is not None and hasher.hexdigest() != sha1:
                _safe_unlink(part)
                raise _TransientNetworkError("SHA1 校验失败，文件已损坏")

            try:
                os.replace(part, target)
            except OSError as exc:
                raise InstallError(
                    "保存下载文件失败，请检查磁盘空间与权限",
                    detail=f"{target}: {exc}",
                ) from exc
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001
                pass

    # -- 辅助 -------------------------------------------------------------

    @staticmethod
    def _file_ok(path: Path, sha1: str | None, size: int | None) -> bool:
        try:
            if not path.is_file():
                return False
            if size is not None and path.stat().st_size != size:
                return False
        except OSError:
            return False
        if sha1 is not None and sha1_file(path) != sha1:
            return False
        return True

    @staticmethod
    def _check_cancel(cancel: threading.Event | None) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("操作已取消")

    @staticmethod
    def _wait(seconds: float, cancel: threading.Event | None) -> None:
        delay = max(0.0, float(seconds))
        if cancel is None:
            time.sleep(delay)
            return
        deadline = time.monotonic() + delay
        while True:
            if cancel.is_set():
                raise CancelledError("操作已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            if cancel.wait(remaining):
                raise CancelledError("操作已取消")


def _content_range_start(value: str | None) -> int | None:
    if not value:
        return None
    try:
        unit, _, spec = value.partition(" ")
        if unit.lower() != "bytes":
            return None
        start, _, _ = spec.partition("-")
        return int(start)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# 并发下载
# ---------------------------------------------------------------------------


class ThreadedDownloader:
    """线程池批量下载；单个文件失败重试 2 次后抛 :class:`NetworkError`。"""

    task_retries = 2

    def __init__(self, http: HttpSession, workers: int = 32) -> None:
        self.http = http
        self.workers = max(1, int(workers))

    def run(
        self,
        tasks: Sequence[DownloadTask],
        *,
        progress_cb: Callable[[Progress], None] | None = None,
        cancel: threading.Event | None = None,
        stage: str = "",
    ) -> None:
        task_list = list(tasks)
        if not task_list:
            return
        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消")

        total_files = len(task_list)
        total_bytes = sum(int(task.size or 0) for task in task_list)
        message = stage or "正在下载文件"
        abort = threading.Event()
        signal = _AbortSignal(cancel, abort)
        started_at = time.monotonic()
        lock = threading.Lock()
        task_bytes = [0] * total_files
        state = {"done": 0, "bytes": 0, "last_emit": 0.0, "known_total": total_bytes}
        finished = threading.Event()

        def emit(force: bool = False) -> None:
            now = time.monotonic()
            with lock:
                if not force and now - state["last_emit"] < _PROGRESS_INTERVAL:
                    return
                state["last_emit"] = now
                # 未知大小的文件会让实际字节数超过声明的总字节数：动态抬高
                # bytes_total，保证 0 <= bytes_done <= bytes_total。
                if state["bytes"] > state["known_total"]:
                    state["known_total"] = state["bytes"]
                if finished.is_set():
                    # 收尾事件必须正好 100%：以实际字节数为准。
                    state["known_total"] = state["bytes"]
                done = min(state["done"], total_files)
                snapshot = Progress(
                    done=done,
                    total=total_files,
                    bytes_done=min(state["bytes"], state["known_total"]),
                    bytes_total=state["known_total"],
                    speed=state["bytes"] / max(now - started_at, 1e-6),
                    message=message,
                    stage=message,
                    stage_done=done,
                    stage_total=total_files,
                )
            _safe_progress(progress_cb, snapshot)

        def make_on_bytes(index: int) -> Callable[[int], None]:
            def on_bytes(value: int) -> None:
                with lock:
                    delta = int(value) - task_bytes[index]
                    if delta <= 0:
                        return
                    task_bytes[index] += delta
                    state["bytes"] += delta
                emit()

            return on_bytes

        def work(index: int, task: DownloadTask) -> None:
            if signal.is_set():
                raise CancelledError("下载已取消")
            last_exc: CubeFetchError | None = None
            for attempt in range(self.task_retries + 1):
                if signal.is_set():
                    raise CancelledError("下载已取消")
                try:
                    self.http.download(
                        task.url,
                        task.dest,
                        sha1=task.sha1,
                        size=task.size,
                        use_mirror=task.use_mirror,
                        progress_cb=make_on_bytes(index),
                        cancel=signal,  # type: ignore[arg-type]
                    )
                    _finalize(index, task)
                    return
                except CancelledError:
                    raise
                except NetworkError as exc:
                    last_exc = exc
                    if attempt < self.task_retries:
                        log.info(
                            "下载任务失败，准备重试 %s（第 %d 次）：%s",
                            task.url,
                            attempt + 1,
                            exc,
                        )
                        _abortable_sleep(0.5 * (attempt + 1), signal)
                        continue
                    raise
                except InstallError:
                    raise
                except CubeFetchError:
                    raise
            if last_exc is not None:
                raise last_exc

        def _finalize(index: int, task: DownloadTask) -> None:
            try:
                actual = task.dest.stat().st_size
            except OSError:
                actual = int(task.size or 0)
            with lock:
                delta = actual - task_bytes[index]
                if delta > 0:
                    task_bytes[index] = actual
                    state["bytes"] += delta
                state["done"] += 1
            emit(force=True)

        emit(force=True)
        first_error: CubeFetchError | None = None
        pool = ThreadPoolExecutor(
            max_workers=min(self.workers, total_files), thread_name_prefix="cf-download"
        )
        try:
            futures: list[Future[None]] = [
                pool.submit(work, index, task) for index, task in enumerate(task_list)
            ]
            for future in as_completed(futures):
                try:
                    future.result()
                except CancelledError:
                    if cancel is not None and cancel.is_set():
                        abort.set()
                        raise
                    # 其它任务失败导致的取消：错误已记录，忽略即可。
                except CubeFetchError as exc:
                    if first_error is None:
                        first_error = exc
                        abort.set()
                except Exception as exc:  # noqa: BLE001 - 兜底，不向外抛裸异常
                    if first_error is None:
                        first_error = NetworkError(
                            "下载文件失败，请重试", detail=f"{exc.__class__.__name__}: {exc}"
                        )
                        abort.set()
        finally:
            abort.set()
            pool.shutdown(wait=True, cancel_futures=True)

        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消")
        if first_error is not None:
            raise first_error
        finished.set()
        emit(force=True)


def _abortable_sleep(seconds: float, signal: _AbortSignal) -> None:
    if signal.is_set():
        raise CancelledError("下载已取消")
    if signal.wait(max(0.0, seconds)):
        raise CancelledError("下载已取消")
