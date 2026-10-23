"""http.py 单元测试：镜像重写、重试、断点续传、SHA1 校验、并发与取消。

全部测试使用本地临时 HTTP 服务（离线、快速、可重复），不依赖外网。
"""
from __future__ import annotations

import hashlib
import http.server
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Callable
from unittest import mock

from cubefetch.core.errors import CancelledError, NetworkError
from cubefetch.core.http import (
    BMCLAPI_BASE,
    DownloadTask,
    HttpSession,
    Progress,
    ThreadedDownloader,
    mirror_url,
)


# ---------------------------------------------------------------------------
# 本地测试 HTTP 服务
# ---------------------------------------------------------------------------


def _send_bytes(
    handler: http.server.BaseHTTPRequestHandler,
    data: bytes,
    *,
    honor_range: bool = True,
    chunk_delay: float = 0.0,
    chunk_count: int = 16,
) -> None:
    """按请求的 Range 返回数据（文件下载测试用）。"""
    raw_range = handler.headers.get("Range")
    start = 0
    if raw_range and honor_range:
        try:
            start = int(raw_range.split("=", 1)[1].split("-", 1)[0])
        except (ValueError, IndexError):
            start = 0
        start = max(0, min(start, len(data)))
    payload = data[start:]
    status = 206 if (raw_range and honor_range and start > 0) else 200
    handler.send_response(status)
    handler.send_header("Content-Type", "application/octet-stream")
    handler.send_header("Content-Length", str(len(payload)))
    if status == 206:
        handler.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
    handler.end_headers()
    try:
        if chunk_delay > 0 and payload:
            step = max(1, len(payload) // chunk_count)
            for offset in range(0, len(payload), step):
                handler.wfile.write(payload[offset : offset + step])
                handler.wfile.flush()
                time.sleep(chunk_delay)
        elif payload:
            handler.wfile.write(payload)
    except (BrokenPipeError, ConnectionResetError):  # pragma: no cover - 客户端取消
        pass


class _FakeResponse:
    """最小化的 requests 响应替身，供「下载源顺序」测试离线模拟。"""

    def __init__(self, status: int, body: bytes = b"") -> None:
        self.status_code = status
        self.content = body
        self.headers: dict[str, str] = {}

    def close(self) -> None:
        pass


class LocalHttpServer:
    """极简离线 HTTP 服务，供 http/install 测试复用。"""

    def __init__(self) -> None:
        self.routes: dict[str, Callable[[http.server.BaseHTTPRequestHandler], None]] = {}
        self.requests: list[dict] = []
        self.lock = threading.Lock()
        self._httpd = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), self._make_handler()
        )
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="test-http", daemon=True
        )

    # -- 生命周期 ---------------------------------------------------------

    def start(self) -> "LocalHttpServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    @property
    def base(self) -> str:
        host, port = self._httpd.server_address[:2]
        return f"http://{host}:{port}"

    def url(self, path: str) -> str:
        if not path.startswith("/"):
            path = "/" + path
        return self.base + path

    # -- 请求记录 ---------------------------------------------------------

    def request_count(self, path: str | None = None) -> int:
        with self.lock:
            if path is None:
                return len(self.requests)
            return sum(1 for item in self.requests if item["path"] == path)

    def requests_for(self, path: str) -> list[dict]:
        with self.lock:
            return [item for item in self.requests if item["path"] == path]

    def clear_requests(self) -> None:
        with self.lock:
            self.requests.clear()

    # -- 路由注册 ---------------------------------------------------------

    def add_bytes(self, path: str, data: bytes, **kwargs) -> None:
        def handler(request: http.server.BaseHTTPRequestHandler) -> None:
            _send_bytes(request, data, **kwargs)

        self.routes[path] = handler

    def add_status(self, path: str, status: int, body: bytes = b"") -> None:
        def handler(request: http.server.BaseHTTPRequestHandler) -> None:
            request.send_response(status)
            request.send_header("Content-Length", str(len(body)))
            request.end_headers()
            if body:
                request.wfile.write(body)

        self.routes[path] = handler

    def add_flaky_bytes(self, path: str, data: bytes, failures: list[int]) -> None:
        """前 ``len(failures)`` 次请求依次返回 failures 里的状态码，之后正常返回数据。"""
        pending = list(failures)

        def handler(request: http.server.BaseHTTPRequestHandler) -> None:
            with self.lock:
                status = pending.pop(0) if pending else 200
            if status == 200:
                _send_bytes(request, data)
                return
            request.send_response(status)
            request.send_header("Content-Length", "0")
            request.end_headers()

        self.routes[path] = handler

    # -- 内部 -------------------------------------------------------------

    def _make_handler(self) -> type[http.server.BaseHTTPRequestHandler]:
        server = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002
                pass

            def do_GET(self) -> None:  # noqa: N802 - 标准库命名
                with server.lock:
                    server.requests.append(
                        {
                            "path": self.path,
                            "range": self.headers.get("Range"),
                            "user_agent": self.headers.get("User-Agent"),
                        }
                    )
                    route = server.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                route(self)

        return Handler


# ---------------------------------------------------------------------------
# 测试基类
# ---------------------------------------------------------------------------


class ServerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.server = LocalHttpServer().start()
        self._tmp = TemporaryDirectory(prefix="cf-http-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self.server.stop)

    def make_session(self, **kwargs) -> HttpSession:
        kwargs.setdefault("mirror", "official")
        kwargs.setdefault("timeout", 5)
        kwargs.setdefault("retries", 1)
        kwargs.setdefault("mirror_base", self.server.base)
        session = HttpSession(**kwargs)
        self.addCleanup(session.close)
        return session


# ---------------------------------------------------------------------------
# 镜像重写
# ---------------------------------------------------------------------------


class MirrorUrlTest(unittest.TestCase):
    def test_official_hosts_keep_full_path(self) -> None:
        cases = {
            "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json": "/mc/game/version_manifest_v2.json",
            "https://launchermeta.mojang.com/mc/game/version_manifest.json": "/mc/game/version_manifest.json",
            "https://piston-data.mojang.com/v1/objects/aa/bb/client.jar": "/v1/objects/aa/bb/client.jar",
            "https://launcher.mojang.com/v1/objects/aa/client.jar": "/v1/objects/aa/client.jar",
        }
        for official, suffix in cases.items():
            with self.subTest(url=official):
                self.assertEqual(mirror_url(official), BMCLAPI_BASE + suffix)

    def test_maven_and_asset_prefixes(self) -> None:
        cases = {
            "https://libraries.minecraft.net/net/java/jinput/jinput/2.0.5/jinput-2.0.5.jar": "/maven/net/java/jinput/jinput/2.0.5/jinput-2.0.5.jar",
            "https://resources.download.minecraft.net/ab/abcdef123": "/assets/ab/abcdef123",
            "https://meta.fabricmc.net/v2/versions/loader/1.20.1/0.15.11/profile/json": "/fabric-meta/v2/versions/loader/1.20.1/0.15.11/profile/json",
            "https://maven.fabricmc.net/net/fabricmc/fabric-loader/0.15.11/fabric-loader-0.15.11.jar": "/maven/net/fabricmc/fabric-loader/0.15.11/fabric-loader-0.15.11.jar",
            "https://maven.neoforged.net/releases/net/neoforged/neoforge/21.1.0/neoforge-21.1.0.jar": "/maven/net/neoforged/neoforge/21.1.0/neoforge-21.1.0.jar",
        }
        for official, suffix in cases.items():
            with self.subTest(url=official):
                self.assertEqual(mirror_url(official), BMCLAPI_BASE + suffix)

    def test_unknown_url_unchanged(self) -> None:
        for url in (
            "https://example.com/file.jar",
            "https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json",
        ):
            self.assertEqual(mirror_url(url), url)

    def test_custom_base(self) -> None:
        self.assertEqual(
            mirror_url("https://libraries.minecraft.net/a/b.jar", "https://mirror.test"),
            "https://mirror.test/maven/a/b.jar",
        )


# ---------------------------------------------------------------------------
# 会话与源选择
# ---------------------------------------------------------------------------


class HttpSessionTest(ServerTestCase):
    def test_get_json(self) -> None:
        self.server.add_bytes("/data.json", b'{"hello": "world"}')
        session = self.make_session()
        self.assertEqual(session.get_json(self.server.url("/data.json")), {"hello": "world"})

    def test_get_json_invalid_payload(self) -> None:
        self.server.add_bytes("/bad.json", b"not json at all")
        session = self.make_session()
        with self.assertRaises(NetworkError):
            session.get_json(self.server.url("/bad.json"))

    def test_user_agent_sent(self) -> None:
        self.server.add_bytes("/ua.json", b"{}")
        session = self.make_session(user_agent="CubeFetch-Test/9.9")
        session.get_json(self.server.url("/ua.json"))
        self.assertEqual(self.server.requests[-1]["user_agent"], "CubeFetch-Test/9.9")

    def test_retry_transient_5xx(self) -> None:
        self.server.add_flaky_bytes("/retry.json", b'{"ok": 1}', [500, 503])
        session = self.make_session(retries=3)
        self.assertEqual(session.get_json(self.server.url("/retry.json")), {"ok": 1})
        self.assertEqual(self.server.request_count("/retry.json"), 3)

    def test_permanent_404_not_retried(self) -> None:
        session = self.make_session(retries=3)
        with self.assertRaises(NetworkError):
            session.get_json(self.server.url("/missing.json"))
        self.assertEqual(self.server.request_count("/missing.json"), 1)

    def test_bmclapi_mode_rewrites_official_url(self) -> None:
        official = "https://libraries.minecraft.net/java/objc/test/lib-1.0.jar"
        self.server.add_bytes("/maven/java/objc/test/lib-1.0.jar", b"jar-bytes")
        session = self.make_session(mirror="bmclapi")
        self.assertEqual(session.get_bytes(official), b"jar-bytes")
        self.assertEqual(self.server.request_count("/maven/java/objc/test/lib-1.0.jar"), 1)

    def test_auto_uses_mirror_first_and_never_official_when_mirror_ok(self) -> None:
        # 镜像可用时只请求镜像，且请求路径是镜像重写后的路径。
        self.server.add_bytes("/maven/java/objc/test/lib-1.0.jar", b"jar-bytes")
        official = "https://libraries.minecraft.net/java/objc/test/lib-1.0.jar"
        session = self.make_session(mirror="auto", retries=1)
        self.assertEqual(session.get_bytes(official), b"jar-bytes")
        self.assertEqual(self.server.request_count("/maven/java/objc/test/lib-1.0.jar"), 1)
        self.assertEqual(self.server.requests[0]["path"], "/maven/java/objc/test/lib-1.0.jar")
        mirror = mirror_url(official, self.server.base)
        self.assertEqual(session._sources(official, True), [mirror, official])

    def test_auto_falls_back_to_official_after_mirror_404(self) -> None:
        official = "https://libraries.minecraft.net/a/b/lib-1.0.jar"
        mirror = mirror_url(official, self.server.base)
        session = self.make_session(mirror="auto", retries=0)
        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            if url == mirror:
                return _FakeResponse(404)
            return _FakeResponse(200, b"jar-bytes")

        with mock.patch.object(session.session, "get", side_effect=fake_get):
            self.assertEqual(session.get_bytes(official), b"jar-bytes")
        self.assertEqual(calls, [mirror, official])

    def test_auto_does_not_remember_official(self) -> None:
        # 镜像失败回落官方成功后，下一次请求仍然从镜像开始（memo 不得反转优先级）。
        official = "https://libraries.minecraft.net/a/b/lib-2.0.jar"
        mirror = mirror_url(official, self.server.base)
        session = self.make_session(mirror="auto", retries=0)
        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            if url == mirror:
                return _FakeResponse(404)
            return _FakeResponse(200, b"jar-bytes")

        with mock.patch.object(session.session, "get", side_effect=fake_get):
            session.get_bytes(official)
            session.get_bytes(official)
        self.assertEqual(calls, [mirror, official, mirror, official])

    def test_fallback_switches_source_without_exhausting_retries(self) -> None:
        # 前一个来源不可达时，应在同一轮重试里立刻尝试下一来源，而不是先耗光重试次数。
        self.server.add_flaky_bytes("/primary.json", b'{"ok": 1}', [500, 500, 500, 500])
        self.server.add_bytes("/secondary.json", b'{"ok": 1}')
        session = self.make_session(mirror="auto", retries=3)
        primary = self.server.url("/primary.json")
        secondary = self.server.url("/secondary.json")
        with mock.patch.object(session, "_sources", return_value=[primary, secondary]):
            self.assertEqual(session.get_json("https://example.com/data.json"), {"ok": 1})
        self.assertEqual(self.server.request_count("/primary.json"), 1)
        self.assertEqual(self.server.request_count("/secondary.json"), 1)

    def test_official_mode_never_requests_mirror(self) -> None:
        official = "https://libraries.minecraft.net/a/b/lib-3.0.jar"
        session = self.make_session(mirror="official", retries=0)
        calls: list[str] = []

        def fake_get(url: str, **kwargs) -> _FakeResponse:
            calls.append(url)
            return _FakeResponse(200, b"official-bytes")

        with mock.patch.object(session.session, "get", side_effect=fake_get):
            self.assertEqual(session.get_bytes(official), b"official-bytes")
        self.assertEqual(calls, [official])

    def test_pure_mirror_modes_never_request_official(self) -> None:
        official = "https://libraries.minecraft.net/a/b/lib-4.0.jar"
        mirror = mirror_url(official, self.server.base)
        for mode in ("bmclapi", "mirror"):
            with self.subTest(mode=mode):
                session = self.make_session(mirror=mode, retries=0)
                calls: list[str] = []

                def fake_get(url: str, **kwargs) -> _FakeResponse:
                    calls.append(url)
                    return _FakeResponse(200, b"mirror-bytes")

                with mock.patch.object(session.session, "get", side_effect=fake_get):
                    self.assertEqual(session.get_bytes(official), b"mirror-bytes")
                self.assertEqual(session.mirror, "bmclapi")
                self.assertEqual(calls, [mirror])

    def test_set_mirror_switches_modes(self) -> None:
        session = self.make_session(mirror="auto")
        session.set_mirror("official")
        self.assertEqual(session.mirror, "official")
        session.set_mirror("mirror")  # 别名等价 bmclapi
        self.assertEqual(session.mirror, "bmclapi")
        session.set_mirror("bogus")  # 非法值回退 auto
        self.assertEqual(session.mirror, "auto")

    def test_workers_attribute(self) -> None:
        session = self.make_session(workers=7)
        self.assertEqual(session.workers, 7)


# ---------------------------------------------------------------------------
# 单文件下载
# ---------------------------------------------------------------------------


class DownloadTest(ServerTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.data = bytes(range(256)) * 4096  # 1 MiB
        self.sha1 = hashlib.sha1(self.data).hexdigest()

    def _route(self, path: str = "/client.jar", **kwargs) -> None:
        self.server.add_bytes(path, self.data, **kwargs)

    def test_download_writes_file_and_verifies(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        progress: list[int] = []
        session = self.make_session()
        session.download(
            self.server.url("/client.jar"),
            dest,
            sha1=self.sha1,
            size=len(self.data),
            progress_cb=progress.append,
        )
        self.assertEqual(dest.read_bytes(), self.data)
        self.assertFalse(dest.with_name(dest.name + ".part").exists())
        self.assertTrue(progress)
        self.assertEqual(progress[-1], len(self.data))

    def test_existing_valid_file_skipped(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        dest.write_bytes(self.data)
        self.server.clear_requests()
        session = self.make_session()
        session.download(self.server.url("/client.jar"), dest, sha1=self.sha1, size=len(self.data))
        self.assertEqual(self.server.request_count(), 0)

    def test_resume_from_part_file_with_range(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        part = dest.with_name(dest.name + ".part")
        offset = 123_456
        part.write_bytes(self.data[:offset])
        session = self.make_session()
        session.download(self.server.url("/client.jar"), dest, sha1=self.sha1, size=len(self.data))
        self.assertEqual(dest.read_bytes(), self.data)
        self.assertFalse(part.exists())
        request = self.server.requests_for("/client.jar")[-1]
        self.assertEqual(request["range"], f"bytes={offset}-")

    def test_server_ignores_range_restarts_cleanly(self) -> None:
        self._route(honor_range=False)
        dest = self.tmp / "client.jar"
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(self.data[:1000])
        session = self.make_session()
        session.download(self.server.url("/client.jar"), dest, sha1=self.sha1, size=len(self.data))
        self.assertEqual(dest.read_bytes(), self.data)

    def test_sha1_mismatch_raises_and_removes_part(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        session = self.make_session(retries=0)
        with self.assertRaises(NetworkError):
            session.download(
                self.server.url("/client.jar"),
                dest,
                sha1="0" * 40,
                size=len(self.data),
            )
        self.assertFalse(dest.exists())
        self.assertFalse(dest.with_name(dest.name + ".part").exists())

    def test_size_mismatch_raises(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        session = self.make_session(retries=0)
        with self.assertRaises(NetworkError):
            session.download(
                self.server.url("/client.jar"),
                dest,
                sha1=self.sha1,
                size=len(self.data) + 1,
            )
        self.assertFalse(dest.exists())

    def test_cancel_before_start(self) -> None:
        self._route()
        dest = self.tmp / "client.jar"
        cancel = threading.Event()
        cancel.set()
        session = self.make_session()
        with self.assertRaises(CancelledError):
            session.download(self.server.url("/client.jar"), dest, cancel=cancel)
        self.assertEqual(self.server.request_count(), 0)
        self.assertFalse(dest.exists())

    def test_cancel_mid_download(self) -> None:
        self._route(chunk_delay=0.02)
        dest = self.tmp / "client.jar"
        cancel = threading.Event()
        seen = threading.Event()
        session = self.make_session()

        def on_progress(_written: int) -> None:
            seen.set()

        errors: list[BaseException] = []

        def run() -> None:
            try:
                session.download(
                    self.server.url("/client.jar"), dest, cancel=cancel, progress_cb=on_progress
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        self.assertTrue(seen.wait(timeout=5), "未能观察到下载进度")
        cancel.set()
        thread.join(timeout=10)
        self.assertTrue(errors, "取消后应抛异常")
        self.assertIsInstance(errors[0], CancelledError)
        self.assertFalse(dest.exists())


# ---------------------------------------------------------------------------
# 并发下载
# ---------------------------------------------------------------------------


class ThreadedDownloaderTest(ServerTestCase):
    def make_tasks(self, count: int = 6) -> tuple[list[DownloadTask], list[bytes]]:
        tasks: list[DownloadTask] = []
        payloads: list[bytes] = []
        for index in range(count):
            payload = f"payload-{index}".encode() * 500
            payloads.append(payload)
            path = f"/file-{index}.bin"
            self.server.add_bytes(path, payload)
            tasks.append(
                DownloadTask(
                    url=self.server.url(path),
                    dest=self.tmp / "downloads" / f"file-{index}.bin",
                    sha1=hashlib.sha1(payload).hexdigest(),
                    size=len(payload),
                )
            )
        return tasks, payloads

    def test_downloads_all_files_and_reports_progress(self) -> None:
        tasks, payloads = self.make_tasks()
        progress_events: list[Progress] = []
        lock = threading.Lock()

        def on_progress(progress: Progress) -> None:
            with lock:
                progress_events.append(progress)

        downloader = ThreadedDownloader(self.make_session(), workers=4)
        downloader.run(tasks, progress_cb=on_progress, stage="正在下载游戏文件")
        for task, payload in zip(tasks, payloads):
            self.assertEqual(task.dest.read_bytes(), payload)
        self.assertTrue(progress_events)
        self.assertEqual(progress_events[-1].done, len(tasks))
        self.assertEqual(progress_events[-1].total, len(tasks))
        self.assertEqual(progress_events[-1].bytes_done, sum(len(item) for item in payloads))
        self.assertEqual(progress_events[-1].message, "正在下载游戏文件")
        self.assertEqual(progress_events[-1].stage, "正在下载游戏文件")
        self.assertEqual(progress_events[-1].stage_done, len(tasks))
        self.assertEqual(progress_events[-1].stage_total, len(tasks))

    def _assert_invariants(self, events: list[Progress], total_files: int) -> None:
        """进度流不变量：范围、单调性、整体口径与 100% 收尾。"""
        self.assertTrue(events, "至少应有首尾两个进度事件")
        prev_done = 0
        prev_bytes = 0
        prev_stage = 0
        prev_total = -1
        prev_bytes_total = -1
        for event in events:
            self.assertGreaterEqual(event.done, 0)
            self.assertLessEqual(event.done, event.total)
            self.assertGreaterEqual(event.bytes_done, 0)
            self.assertLessEqual(event.bytes_done, event.bytes_total)
            self.assertGreaterEqual(event.stage_done, 0)
            self.assertLessEqual(event.stage_done, event.stage_total)
            self.assertGreaterEqual(event.done, prev_done)
            self.assertGreaterEqual(event.bytes_done, prev_bytes)
            self.assertGreaterEqual(event.stage_done, prev_stage)
            self.assertGreaterEqual(event.total, prev_total)
            self.assertGreaterEqual(event.bytes_total, prev_bytes_total)
            self.assertEqual(event.total, total_files)
            self.assertEqual(event.stage_total, total_files)
            prev_done, prev_bytes, prev_stage = event.done, event.bytes_done, event.stage_done
            prev_total, prev_bytes_total = event.total, event.bytes_total
        last = events[-1]
        self.assertEqual(last.done, total_files)
        self.assertEqual(last.bytes_done, last.bytes_total)
        self.assertEqual(last.stage_done, last.stage_total)

    def test_progress_stream_satisfies_invariants(self) -> None:
        tasks, payloads = self.make_tasks()
        events: list[Progress] = []
        downloader = ThreadedDownloader(self.make_session(), workers=4)
        downloader.run(tasks, progress_cb=events.append, stage="正在下载游戏文件")
        self._assert_invariants(events, len(tasks))
        self.assertEqual(events[0].done, 0)
        self.assertEqual(events[0].bytes_done, 0)
        self.assertEqual(events[-1].bytes_done, sum(len(item) for item in payloads))

    def test_progress_unknown_sizes_keeps_bounds(self) -> None:
        tasks: list[DownloadTask] = []
        total = 0
        for index in range(3):
            payload = f"unknown-{index}".encode() * 300
            total += len(payload)
            path = f"/unknown-{index}.bin"
            self.server.add_bytes(path, payload)
            tasks.append(
                DownloadTask(url=self.server.url(path), dest=self.tmp / "unknown" / f"{index}.bin")
            )
        events: list[Progress] = []
        downloader = ThreadedDownloader(self.make_session(), workers=2)
        downloader.run(tasks, progress_cb=events.append, stage="正在下载游戏文件")
        self._assert_invariants(events, len(tasks))
        self.assertEqual(events[-1].bytes_done, total)
        self.assertEqual(events[-1].bytes_total, total)

    def test_progress_backward_compatible_defaults(self) -> None:
        empty = Progress()
        self.assertEqual(empty.stage, "")
        self.assertEqual(empty.stage_done, 0)
        self.assertEqual(empty.stage_total, 0)
        # 旧代码按位置参数只传 6 个字段仍可用
        legacy = Progress(1, 2, 3, 4, 5.0, "旧消息")
        self.assertEqual((legacy.done, legacy.total, legacy.bytes_done), (1, 2, 3))
        self.assertEqual((legacy.bytes_total, legacy.speed, legacy.message), (4, 5.0, "旧消息"))
        self.assertEqual((legacy.stage, legacy.stage_done, legacy.stage_total), ("", 0, 0))


    def test_failure_raises_network_error(self) -> None:
        self.server.add_status("/broken.bin", 404)
        tasks = [
            DownloadTask(url=self.server.url("/broken.bin"), dest=self.tmp / "broken.bin"),
        ]
        downloader = ThreadedDownloader(self.make_session(retries=0), workers=1)
        with self.assertRaises(NetworkError):
            downloader.run(tasks, stage="正在下载游戏文件")
        # 单任务失败：首次 + 2 次任务级重试
        self.assertEqual(self.server.request_count("/broken.bin"), 3)

    def test_cancel_raises(self) -> None:
        tasks, _ = self.make_tasks(count=2)
        cancel = threading.Event()
        cancel.set()
        downloader = ThreadedDownloader(self.make_session(), workers=2)
        with self.assertRaises(CancelledError):
            downloader.run(tasks, cancel=cancel, stage="正在下载游戏文件")

    def test_empty_tasks_noop(self) -> None:
        downloader = ThreadedDownloader(self.make_session(), workers=2)
        downloader.run([], stage="空任务")  # 不应抛异常


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
