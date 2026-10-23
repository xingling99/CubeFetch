"""``cubefetch.core.java_runtime`` 的离线单元测试。

全部测试不依赖外网：

- ``FakeHttp`` 提供 ``get_json`` / ``download`` 的内存替身；
- ``InstallOfflineTest`` 用 :class:`tests.test_http.LocalHttpServer` 模拟
  Mojang 索引 / 平台清单 / 运行时文件，走真实 ``HttpSession``（镜像模式，
  ``mirror_base`` 指向本地服务），覆盖 sha1 校验、已存在跳过、进度、取消。
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from cubefetch.core import java_runtime as jr
from cubefetch.core.errors import CancelledError, InstallError, NetworkError
from cubefetch.core.http import HttpSession, Progress, mirror_url
from cubefetch.core.java_runtime import (
    JAVA_COMPONENT_BY_MAJOR,
    JAVA_RUNTIME_INDEX_URL,
    PLATFORM,
    JavaRuntimeInfo,
    JavaRuntimeService,
)
from tests.test_http import LocalHttpServer

FIXTURES = Path(__file__).resolve().parent / "fixtures"
COMPONENT = "java-runtime-gamma"

VJ_1_20_1 = {
    "id": "1.20.1",
    "javaVersion": {"component": "java-runtime-gamma", "majorVersion": 17},
}
VJ_1_7_10 = {
    "id": "1.7.10",
    "javaVersion": {"component": "jre-legacy", "majorVersion": 8},
}


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def payload(relpath: str) -> bytes:
    """与 fixture 生成脚本一致的确定性文件内容。"""
    return f"cf-java-runtime:{relpath}".encode("utf-8")


def sha1_of(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def file_entries(manifest: dict) -> dict[str, dict]:
    """清单中真正需要下载的 file 项。"""
    result: dict[str, dict] = {}
    for relpath, meta in manifest["files"].items():
        if isinstance(meta, dict) and meta.get("type") == "file":
            raw = (meta.get("downloads") or {}).get("raw") or {}
            if raw.get("url"):
                result[relpath] = meta
    return result


def build_index_and_manifest() -> tuple[dict, dict]:
    return load_fixture("java_runtime_all.json"), load_fixture("java_runtime_manifest.json")


def manifest_url_of(index: dict) -> str:
    return index["windows-x64"][COMPONENT][0]["manifest"]["url"]


class FakeHttp:
    """``HttpSession`` 的内存替身：只实现 java_runtime 用到的两个方法。"""

    def __init__(self, *, workers: int = 4) -> None:
        self.mirror = "auto"
        self.workers = workers
        self.json_map: dict[str, dict] = {}
        self.payloads: dict[str, bytes] = {}
        self.fail_json_urls: set[str] = set()
        self.fail_download_urls: set[str] = set()
        self.json_calls: list[str] = []
        self.download_calls: list[str] = []
        self.on_download = None  # Callable[[str], None] | None

    def get_json(self, url: str, *, use_mirror: bool = True) -> dict:
        self.json_calls.append(url)
        if url in self.fail_json_urls:
            raise NetworkError("模拟网络失败", detail=url)
        if url not in self.json_map:
            raise NetworkError("未知 JSON 地址", detail=url)
        return copy.deepcopy(self.json_map[url])

    def download(
        self,
        url: str,
        dest: Path | str,
        *,
        sha1: str | None = None,
        size: int | None = None,
        use_mirror: bool = True,
        timeout: float | None = None,
        progress_cb=None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.download_calls.append(url)
        if self.on_download is not None:
            self.on_download(url)
        if url in self.fail_download_urls:
            raise NetworkError("模拟下载失败", detail=url)
        if cancel is not None and cancel.is_set():
            raise CancelledError("下载已取消")
        data = self.payloads.get(url)
        if data is None:
            raise NetworkError("未知下载地址", detail=url)
        if sha1 is not None and sha1_of(data) != sha1.lower():
            raise NetworkError("模拟 sha1 校验失败", detail=url)
        target = Path(dest)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if progress_cb is not None:
            progress_cb(len(data))


def make_fake_http(
    index: dict | None = None,
    manifest: dict | None = None,
) -> tuple[FakeHttp, dict, dict, str]:
    """组装一个指向 fixture 的 fake http（索引 + 清单 + 文件内容）。"""
    index = copy.deepcopy(index if index is not None else load_fixture("java_runtime_all.json"))
    manifest = copy.deepcopy(
        manifest if manifest is not None else load_fixture("java_runtime_manifest.json")
    )
    url = manifest_url_of(index)
    http = FakeHttp()
    http.json_map[JAVA_RUNTIME_INDEX_URL] = index
    http.json_map[url] = manifest
    for relpath, meta in file_entries(manifest).items():
        raw = meta["downloads"]["raw"]
        http.payloads[raw["url"]] = payload(relpath)
    return http, index, manifest, url


class RuntimeTestCase(unittest.TestCase):
    """公共临时目录与 service 构造。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="cf-java-runtime-")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.mc_root = self.tmp / "mc"
        self.cache_root = self.tmp / "cache"

    def make_service(self, http: FakeHttp | HttpSession, **kwargs) -> JavaRuntimeService:
        kwargs.setdefault("cache_root", self.cache_root)
        service = JavaRuntimeService(http, self.mc_root, **kwargs)
        return service

    def create_installed(self, component: str, version: str) -> Path:
        exe = self.mc_root / "runtime" / component / PLATFORM / component / "bin" / "java.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_bytes(b"installed")
        release = exe.parent.parent / "release"
        release.write_text(f'JAVA_VERSION="{version}"\n', encoding="utf-8")
        return exe


# ---------------------------------------------------------------------------
# 常量 / 映射
# ---------------------------------------------------------------------------


class ConstantsTest(unittest.TestCase):
    def test_index_url_and_platform(self) -> None:
        self.assertTrue(JAVA_RUNTIME_INDEX_URL.startswith("https://launchermeta.mojang.com/"))
        self.assertTrue(JAVA_RUNTIME_INDEX_URL.endswith("/all.json"))
        self.assertEqual(PLATFORM, "windows-x64")

    def test_version_major_parsing(self) -> None:
        cases = {
            "17.0.15": 17,
            "21.0.7": 21,
            "25.0.1": 25,
            "16.0.1.9.1": 16,
            "8u51-cacert462b08": 8,
            "1.8.0_51": 8,
            "": None,
            "release": None,
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                self.assertEqual(jr._version_major(name), expected)

    def test_static_mapping_matches_real_index_versions(self) -> None:
        """静态映射必须与索引里组件的实际大版本一致（防止把 21 映射成 17）。"""
        index = load_fixture("java_runtime_all.json")
        self.assertIn(21, JAVA_COMPONENT_BY_MAJOR)
        self.assertEqual(JAVA_COMPONENT_BY_MAJOR[21], "java-runtime-delta")
        self.assertEqual(JAVA_COMPONENT_BY_MAJOR[25], "java-runtime-epsilon")
        for major, component in JAVA_COMPONENT_BY_MAJOR.items():
            with self.subTest(major=major):
                entry = index["windows-x64"][component][0]
                self.assertEqual(jr._version_major(entry["version"]["name"]), major)

    def test_dataclass_fields(self) -> None:
        info = JavaRuntimeInfo(component=COMPONENT, platform=PLATFORM, version_name="17.0.15")
        self.assertEqual(info.component, COMPONENT)
        self.assertEqual(info.platform, PLATFORM)
        self.assertEqual(info.version_name, "17.0.15")


# ---------------------------------------------------------------------------
# find_installed / 路径
# ---------------------------------------------------------------------------


class FindInstalledTest(RuntimeTestCase):
    def test_paths(self) -> None:
        service = self.make_service(make_fake_http()[0])
        self.assertEqual(service.runtime_root(), self.mc_root / "runtime")
        self.assertEqual(
            service.java_executable(COMPONENT),
            self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT / "bin" / "java.exe",
        )

    def test_missing_returns_none(self) -> None:
        service = self.make_service(make_fake_http()[0])
        self.assertIsNone(service.find_installed(COMPONENT))
        self.assertIsNone(service.find_installed(""))

    def test_installed_returns_path(self) -> None:
        service = self.make_service(make_fake_http()[0])
        exe = self.create_installed(COMPONENT, "17.0.15")
        self.assertEqual(service.find_installed(COMPONENT), exe)


# ---------------------------------------------------------------------------
# resolve
# ---------------------------------------------------------------------------


class ResolveTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.http, self.index, self.manifest, self.manifest_url = make_fake_http()
        self.service = self.make_service(self.http)

    def test_version_json_component_wins(self) -> None:
        info = self.service.resolve(version_json=VJ_1_20_1)
        self.assertIsNotNone(info)
        self.assertEqual(info.component, "java-runtime-gamma")
        self.assertEqual(info.platform, PLATFORM)
        self.assertEqual(info.version_name, "17.0.15")

    def test_major_21_maps_to_delta_not_gamma(self) -> None:
        info = self.service.resolve(21)
        self.assertIsNotNone(info)
        self.assertEqual(info.component, "java-runtime-delta")
        self.assertEqual(jr._version_major(info.version_name), 21)

    def test_major_17_maps_to_gamma(self) -> None:
        self.assertEqual(self.service.resolve(17).component, "java-runtime-gamma")

    def test_major_8_maps_to_jre_legacy(self) -> None:
        self.assertEqual(self.service.resolve(8).component, "jre-legacy")

    def test_major_25_maps_to_epsilon(self) -> None:
        self.assertEqual(self.service.resolve(25).component, "java-runtime-epsilon")

    def test_unknown_major_returns_none(self) -> None:
        self.assertIsNone(self.service.resolve(23))

    def test_no_input_returns_none_without_network(self) -> None:
        self.assertIsNone(self.service.resolve())
        self.assertIsNone(self.service.resolve(None, {"id": "1.0"}))
        self.assertEqual(self.http.json_calls, [])

    def test_version_json_major_only(self) -> None:
        info = self.service.resolve(version_json={"javaVersion": {"majorVersion": 21}})
        self.assertEqual(info.component, "java-runtime-delta")

    def test_installed_runtime_resolved_without_network(self) -> None:
        self.create_installed("java-runtime-gamma", "17.0.15")
        info = self.service.resolve(17)
        self.assertEqual(info.component, "java-runtime-gamma")
        self.assertEqual(info.version_name, "17.0.15")
        self.assertEqual(self.http.json_calls, [])

    def test_installed_version_json_component_without_network(self) -> None:
        self.create_installed("jre-legacy", "8u51-cacert462b08")
        info = self.service.resolve(version_json=VJ_1_7_10)
        self.assertEqual(info.component, "jre-legacy")
        self.assertEqual(self.http.json_calls, [])

    def test_installed_wrong_major_falls_through_to_index(self) -> None:
        self.create_installed("java-runtime-gamma", "17.0.15")
        info = self.service.resolve(21)
        self.assertEqual(info.component, "java-runtime-delta")
        self.assertNotEqual(self.http.json_calls, [])

    def test_index_failure_raises_network_error(self) -> None:
        self.http.fail_json_urls.add(JAVA_RUNTIME_INDEX_URL)
        with self.assertRaises(NetworkError):
            self.service.resolve(17)

    def test_index_failure_uses_stale_cache(self) -> None:
        stale = {
            "fetched_at": time.time() - 30 * 24 * 3600,
            "index": self.index,
        }
        cache_file = self.cache_root / "java_runtime_all.json"
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_text(json.dumps(stale), encoding="utf-8")
        self.http.fail_json_urls.add(JAVA_RUNTIME_INDEX_URL)
        info = self.service.resolve(17)
        self.assertEqual(info.component, "java-runtime-gamma")

    def test_legacy_index_shape_supported(self) -> None:
        index = load_fixture("java_runtime_all.json")
        legacy = {name: {"windows-x64": entries} for name, entries in index["windows-x64"].items()}
        http = FakeHttp()
        http.json_map[JAVA_RUNTIME_INDEX_URL] = legacy
        http.json_map[self.manifest_url] = self.manifest
        service = self.make_service(http)
        self.assertEqual(service.resolve(17).component, "java-runtime-gamma")

    def test_incomplete_entry_skipped(self) -> None:
        index = copy.deepcopy(self.index)
        entries = index["windows-x64"]["java-runtime-gamma"]
        incomplete = copy.deepcopy(entries[0])
        incomplete["availability"] = {"progress": 0}
        incomplete["version"] = {"name": "17.0.0"}
        complete = copy.deepcopy(entries[0])
        complete["availability"] = {"progress": 100}
        index["windows-x64"]["java-runtime-gamma"] = [incomplete, complete]
        http = FakeHttp()
        http.json_map[JAVA_RUNTIME_INDEX_URL] = index
        http.json_map[self.manifest_url] = self.manifest
        info = self.make_service(http).resolve(17)
        self.assertEqual(info.version_name, "17.0.15")

    def test_platform_components_normalizes_both_shapes(self) -> None:
        index = load_fixture("java_runtime_all.json")
        current = JavaRuntimeService._platform_components(index)
        legacy = JavaRuntimeService._platform_components(
            {name: {"windows-x64": entries} for name, entries in index["windows-x64"].items()}
        )
        self.assertEqual(list(current), list(legacy))
        self.assertIn("java-runtime-gamma", current)

    def test_platform_components_tolerates_garbage(self) -> None:
        self.assertEqual(JavaRuntimeService._platform_components({}), {})
        self.assertEqual(
            JavaRuntimeService._platform_components({"windows-x64": "nope"}),
            {},
        )


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


class PlanTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.http, self.index, self.manifest, self.manifest_url = make_fake_http()
        self.service = self.make_service(self.http)

    def test_plan_builds_file_tasks(self) -> None:
        tasks = self.service.plan(COMPONENT)
        expected = file_entries(self.manifest)
        self.assertEqual(len(tasks), len(expected))
        by_relpath = {
            str(task.dest.relative_to(self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT))
            .replace("\\", "/"): task
            for task in tasks
        }
        for relpath, meta in expected.items():
            raw = meta["downloads"]["raw"]
            task = by_relpath[relpath]
            self.assertEqual(task.url, raw["url"])
            self.assertEqual(task.sha1, sha1_of(payload(relpath)))
            self.assertEqual(task.size, len(payload(relpath)))
        # directory / link 不进入下载计划
        self.assertNotIn("bin", by_relpath)
        self.assertNotIn("bin/legacy-link", by_relpath)

    def test_plan_caches_index_and_manifest(self) -> None:
        self.service.plan(COMPONENT)
        first_calls = list(self.http.json_calls)
        self.assertEqual(len(first_calls), 2)
        self.service.plan(COMPONENT)
        self.assertEqual(self.http.json_calls, first_calls)

    def test_plan_unknown_component_raises(self) -> None:
        with self.assertRaises(InstallError):
            self.service.plan("java-runtime-does-not-exist")

    def test_plan_blank_component_raises(self) -> None:
        with self.assertRaises(InstallError):
            self.service.plan("  ")

    def test_plan_skips_unsafe_relative_paths(self) -> None:
        manifest = {
            "files": {
                "../evil.exe": {
                    "type": "file",
                    "downloads": {
                        "raw": {
                            "url": "https://piston-data.mojang.com/v1/objects/aa/evil.exe",
                            "sha1": sha1_of(b"evil"),
                            "size": 4,
                        }
                    },
                },
                "bin/ok.exe": {
                    "type": "file",
                    "downloads": {
                        "raw": {
                            "url": "https://piston-data.mojang.com/v1/objects/bb/ok.exe",
                            "sha1": sha1_of(b"ok"),
                            "size": 2,
                        }
                    },
                },
            }
        }
        http = FakeHttp()
        http.json_map[JAVA_RUNTIME_INDEX_URL] = self.index
        http.json_map[self.manifest_url] = manifest
        tasks = self.make_service(http).plan(COMPONENT)
        self.assertEqual([task.dest.name for task in tasks], ["ok.exe"])

    def test_plan_index_failure_raises_network_error(self) -> None:
        self.http.fail_json_urls.add(JAVA_RUNTIME_INDEX_URL)
        with self.assertRaises(NetworkError):
            self.service.plan(COMPONENT)

    def test_plan_unavailable_component_raises(self) -> None:
        index = copy.deepcopy(self.index)
        index["windows-x64"][COMPONENT][0]["availability"] = {"progress": 0}
        http = FakeHttp()
        http.json_map[JAVA_RUNTIME_INDEX_URL] = index
        http.json_map[self.manifest_url] = self.manifest
        with self.assertRaises(InstallError):
            self.make_service(http).plan(COMPONENT)


# ---------------------------------------------------------------------------
# install（离线 HTTP 服务 + 真实 HttpSession）
# ---------------------------------------------------------------------------


class InstallOfflineTest(RuntimeTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.server = LocalHttpServer().start()
        self.addCleanup(self.server.stop)
        self.index, self.manifest = build_index_and_manifest()
        self.manifest_url = manifest_url_of(self.index)
        self._register_routes(self.index, self.manifest)
        self.session = HttpSession(
            mirror="bmclapi",
            mirror_base=self.server.base,
            timeout=5,
            retries=0,
        )
        self.session.workers = 4
        self.addCleanup(self.session.close)
        self.service = self.make_service(self.session)

    # -- 辅助 -------------------------------------------------------------

    def _server_path(self, url: str) -> str:
        return urlsplit(mirror_url(url, self.server.base)).path

    def _register_routes(self, index: dict, manifest: dict) -> None:
        self.server.add_bytes(
            self._server_path(JAVA_RUNTIME_INDEX_URL),
            json.dumps(index).encode("utf-8"),
        )
        self.server.add_bytes(
            self._server_path(manifest_url_of(index)),
            json.dumps(manifest).encode("utf-8"),
        )
        for relpath, meta in file_entries(manifest).items():
            raw = meta["downloads"]["raw"]
            self.server.add_bytes(self._server_path(raw["url"]), payload(relpath))

    def expected_exe(self) -> Path:
        return self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT / "bin" / "java.exe"

    # -- 用例 -------------------------------------------------------------

    def test_install_downloads_all_files(self) -> None:
        exe = self.service.install(COMPONENT)
        self.assertEqual(exe, self.expected_exe())
        self.assertTrue(exe.is_file())
        self.assertEqual(exe.read_bytes(), payload("bin/java.exe"))
        for relpath in file_entries(self.manifest):
            target = (
                self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT
            ).joinpath(*relpath.split("/"))
            self.assertTrue(target.is_file(), relpath)
            self.assertEqual(target.read_bytes(), payload(relpath))
        # directory 类型创建为空目录
        self.assertTrue(
            (self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT / "conf").is_dir()
        )
        # link 类型不下载
        self.assertFalse(
            (
                self.mc_root / "runtime" / COMPONENT / PLATFORM / COMPONENT / "bin" / "legacy-link"
            ).exists()
        )
        self.assertEqual(self.service.find_installed(COMPONENT), exe)

    def test_install_reports_progress(self) -> None:
        events: list[Progress] = []
        self.service.install(COMPONENT, progress_cb=events.append)
        self.assertTrue(events)
        last = events[-1]
        self.assertEqual(last.message, "正在下载 Java 运行时…")
        self.assertEqual(last.done, len(file_entries(self.manifest)))
        self.assertEqual(last.total, len(file_entries(self.manifest)))
        self.assertEqual(
            last.bytes_total,
            sum(len(payload(rel)) for rel in file_entries(self.manifest)),
        )

    def test_install_second_run_skips_existing_files(self) -> None:
        first = self.service.install(COMPONENT)
        self.server.clear_requests()
        second = self.service.install(COMPONENT)
        self.assertEqual(first, second)
        self.assertEqual(self.server.request_count(), 0)

    def test_install_cancel_before_start(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            self.service.install(COMPONENT, cancel=cancel)
        self.assertEqual(self.server.request_count(), 0)

    def test_install_cancel_mid_download(self) -> None:
        raw = file_entries(self.manifest)["bin/java.exe"]["downloads"]["raw"]
        self.server.add_bytes(
            self._server_path(raw["url"]),
            payload("bin/java.exe"),
            chunk_delay=0.02,
        )
        cancel = threading.Event()

        def on_progress(progress: Progress) -> None:
            if progress.bytes_done > 0:
                cancel.set()

        with self.assertRaises(CancelledError):
            self.service.install(COMPONENT, progress_cb=on_progress, cancel=cancel)
        self.assertFalse(self.expected_exe().is_file())

    def test_install_sha1_mismatch_raises_network_error(self) -> None:
        raw = file_entries(self.manifest)["bin/java.exe"]["downloads"]["raw"]
        self.server.add_bytes(
            self._server_path(raw["url"]), b"broken", honor_range=False
        )
        with self.assertRaises(NetworkError):
            self.service.install(COMPONENT)
        self.assertFalse(self.expected_exe().is_file())

    def test_install_writes_cache_files(self) -> None:
        self.service.plan(COMPONENT)
        self.assertTrue((self.cache_root / "java_runtime_all.json").is_file())
        self.assertTrue(
            (self.cache_root / "java_runtimes" / f"{COMPONENT}-{PLATFORM}.json").is_file()
        )

    def test_install_unknown_component_raises(self) -> None:
        with self.assertRaises(InstallError):
            self.service.install("java-runtime-does-not-exist")


class InstallFakeHttpTest(RuntimeTestCase):
    """不需要真实 HTTP 服务的失败路径（用 FakeHttp 加速）。"""

    def setUp(self) -> None:
        super().setUp()
        self.http, self.index, self.manifest, self.manifest_url = make_fake_http()
        self.service = self.make_service(self.http)

    def test_install_network_failure_raises_network_error(self) -> None:
        url = file_entries(self.manifest)["bin/java.exe"]["downloads"]["raw"]["url"]
        self.http.fail_download_urls.add(url)
        with self.assertRaises(NetworkError):
            self.service.install(COMPONENT)

    def test_install_missing_java_exe_raises_install_error(self) -> None:
        manifest = copy.deepcopy(self.manifest)
        del manifest["files"]["bin/java.exe"]
        http = FakeHttp()
        http.json_map[JAVA_RUNTIME_INDEX_URL] = self.index
        http.json_map[self.manifest_url] = manifest
        for relpath, meta in file_entries(manifest).items():
            http.payloads[meta["downloads"]["raw"]["url"]] = payload(relpath)
        with self.assertRaises(InstallError):
            self.make_service(http).install(COMPONENT)

    def test_install_writes_files_through_fake_http(self) -> None:
        exe = self.service.install(COMPONENT)
        self.assertTrue(exe.is_file())
        self.assertEqual(exe.read_bytes(), payload("bin/java.exe"))


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    unittest.main(verbosity=2)
