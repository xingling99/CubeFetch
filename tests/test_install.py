"""install.py 单元/集成测试：规则、继承链、natives、资源路径与完整安装。

集成测试使用本地临时 HTTP 服务 + 动态生成的小型版本 JSON / natives zip，
完全离线、快速、可重复。
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import io
import json
import re
import threading
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock
from urllib.parse import urlsplit

from cubefetch.core.errors import AuthError, CancelledError, CubeFetchError, InstallError, NetworkError
from cubefetch.core.http import HttpSession, Progress, mirror_url
from cubefetch.core.install import (
    ASSET_OBJECT_BASE,
    InstallRequest,
    Installer,
    asset_object_path,
    asset_object_relpath,
    asset_object_url,
    asset_virtual_dir,
    detect_os,
    evaluate_rules,
    extract_natives,
    library_artifact,
    library_native_artifact,
    maven_path_from_name,
    merge_version_chain,
    native_classifier,
    natives_extracted,
    relevant_libraries,
)

from tests.test_http import LocalHttpServer

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def make_zip(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


class FakeManifest:
    """不联网的 ManifestService 替身。"""

    def __init__(self, versions: dict[str, dict] | None = None, fail: bool = False) -> None:
        self.versions = dict(versions or {})
        self.calls: list[tuple[str, str]] = []
        self.fail = fail

    def load_cached_version_json(self, version_id: str) -> dict | None:
        self.calls.append(("cached", version_id))
        if self.fail:
            raise NetworkError("网络不可用", detail=version_id)
        data = self.versions.get(version_id)
        return copy.deepcopy(data) if data is not None else None

    def fetch_version_json(self, version_id: str) -> dict:
        self.calls.append(("fetch", version_id))
        if self.fail or version_id not in self.versions:
            raise NetworkError("网络不可用", detail=version_id)
        return copy.deepcopy(self.versions[version_id])


class PureInstallerTest(unittest.TestCase):
    """不依赖 HTTP 服务的纯逻辑测试基类。"""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="cf-install-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / ".minecraft"
        self.http = HttpSession(mirror="official", timeout=5, retries=0)
        self.addCleanup(self.http.close)
        self.manifest = FakeManifest()
        self.installer = Installer(self.http, self.manifest)


# ---------------------------------------------------------------------------
# 规则求值
# ---------------------------------------------------------------------------


class RuleEvaluationTest(unittest.TestCase):
    def test_no_rules_allows(self) -> None:
        self.assertTrue(evaluate_rules(None, "windows", "x86_64", "10.0"))
        self.assertTrue(evaluate_rules([], "windows", "x86_64", "10.0"))

    def test_allow_os_name(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows"}}]
        self.assertTrue(evaluate_rules(rules, "windows", "x86_64", "10.0"))
        self.assertFalse(evaluate_rules(rules, "linux", "x86_64", "6.0"))

    def test_last_matching_rule_wins(self) -> None:
        rules = [{"action": "allow"}, {"action": "disallow", "os": {"name": "linux"}}]
        self.assertFalse(evaluate_rules(rules, "linux", "x86_64", "6.0"))
        self.assertTrue(evaluate_rules(rules, "windows", "x86_64", "10.0"))
        self.assertTrue(evaluate_rules(rules, "osx", "arm64", "14.0"))

    def test_arch_matching(self) -> None:
        x86_rule = [{"action": "allow", "os": {"name": "windows", "arch": "x86"}}]
        self.assertFalse(evaluate_rules(x86_rule, "windows", "x86_64", "10.0"))
        self.assertTrue(evaluate_rules(x86_rule, "windows", "x86", "10.0"))
        arm_rule = [{"action": "allow", "os": {"name": "osx", "arch": "arm64"}}]
        self.assertTrue(evaluate_rules(arm_rule, "osx", "arm64", "14.0"))
        self.assertFalse(evaluate_rules(arm_rule, "osx", "x86_64", "14.0"))

    def test_os_version_regex(self) -> None:
        rules = [{"action": "allow", "os": {"name": "windows", "version": r"^10\."}}]
        self.assertTrue(evaluate_rules(rules, "windows", "x86_64", "10.0.19045"))
        self.assertFalse(evaluate_rules(rules, "windows", "x86_64", "11.0"))

    def test_features(self) -> None:
        rules = [{"action": "allow", "features": {"is_demo_user": True}}]
        self.assertFalse(evaluate_rules(rules, "windows", "x86_64", "10.0"))
        self.assertTrue(
            evaluate_rules(rules, "windows", "x86_64", "10.0", features={"is_demo_user": True})
        )

    def test_1_20_1_real_arch_rule(self) -> None:
        version = load_fixture("version_1_20_1.json")
        xss_rule = None
        for argument in version["arguments"]["jvm"]:
            if isinstance(argument, dict) and "-Xss1M" in argument.get("value", []):
                xss_rule = argument["rules"]
        self.assertIsNotNone(xss_rule)
        self.assertFalse(evaluate_rules(xss_rule, "windows", "x86_64", "10.0"))
        self.assertTrue(evaluate_rules(xss_rule, "windows", "x86", "10.0"))


# ---------------------------------------------------------------------------
# 继承链合并
# ---------------------------------------------------------------------------


class MergeChainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.parent = {
            "id": "parent",
            "type": "release",
            "mainClass": "parent.Main",
            "downloads": {"client": {"url": "https://example.com/client.jar", "sha1": "a" * 40}},
            "assetIndex": {"id": "5"},
            "libraries": [{"name": "a:lib:1.0"}, {"name": "b:lib:1.0"}],
            "arguments": {"game": ["--parent"], "jvm": ["-Xp"]},
        }
        self.child = {
            "id": "child",
            "inheritsFrom": "parent",
            "type": "release",
            "mainClass": "child.Main",
            "libraries": [{"name": "a:lib:2.0"}, {"name": "c:lib:1.0"}],
            "arguments": {"game": ["--child"], "jvm": [], "extra": {"k": 1}},
        }

    def test_child_overrides_scalar_fields(self) -> None:
        merged = merge_version_chain([self.child, self.parent])
        self.assertEqual(merged["id"], "child")
        self.assertEqual(merged["mainClass"], "child.Main")
        self.assertEqual(merged["inheritsFrom"], "parent")
        self.assertEqual(merged["downloads"]["client"]["sha1"], "a" * 40)
        self.assertEqual(merged["assetIndex"]["id"], "5")

    def test_libraries_child_first_and_deduplicated(self) -> None:
        merged = merge_version_chain([self.child, self.parent])
        names = [lib["name"] for lib in merged["libraries"]]
        self.assertEqual(names, ["a:lib:2.0", "c:lib:1.0", "b:lib:1.0"])

    def test_arguments_parent_first_and_deep_merged(self) -> None:
        merged = merge_version_chain([self.child, self.parent])
        self.assertEqual(merged["arguments"]["game"], ["--parent", "--child"])
        self.assertEqual(merged["arguments"]["jvm"], ["-Xp"])
        self.assertEqual(merged["arguments"]["extra"], {"k": 1})

    def test_parent_main_class_kept_when_child_has_none(self) -> None:
        child = dict(self.child)
        child.pop("mainClass")
        merged = merge_version_chain([child, self.parent])
        self.assertEqual(merged["mainClass"], "parent.Main")

    def test_empty_chain_raises(self) -> None:
        with self.assertRaises(InstallError):
            merge_version_chain([])

    def test_deep_merge_nested_dict(self) -> None:
        merged = merge_version_chain(
            [{"id": "c", "a": {"x": 2, "y": 2}}, {"id": "p", "a": {"x": 1, "z": 1}}]
        )
        self.assertEqual(merged["a"], {"x": 2, "y": 2, "z": 1})

    def test_native_variants_survive_child_override(self) -> None:
        parent = {
            "id": "p",
            "libraries": [
                {"name": "org.lwjgl:lwjgl:3.3.1"},
                {"name": "org.lwjgl:lwjgl:3.3.1:natives-windows"},
            ],
        }
        child = {"id": "c", "libraries": [{"name": "org.lwjgl:lwjgl:3.3.1"}]}
        merged = merge_version_chain([child, parent])
        names = [lib["name"] for lib in merged["libraries"]]
        self.assertIn("org.lwjgl:lwjgl:3.3.1:natives-windows", names)
        self.assertEqual(names.count("org.lwjgl:lwjgl:3.3.1"), 1)


# ---------------------------------------------------------------------------
# 库/资源解析
# ---------------------------------------------------------------------------


class LibraryResolutionTest(unittest.TestCase):
    def test_maven_path_from_name(self) -> None:
        self.assertEqual(
            maven_path_from_name("org.lwjgl:lwjgl:3.3.1"),
            "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1.jar",
        )
        self.assertEqual(
            maven_path_from_name("org.lwjgl:lwjgl:3.3.1:natives-windows"),
            "org/lwjgl/lwjgl/3.3.1/lwjgl-3.3.1-natives-windows.jar",
        )
        self.assertEqual(
            maven_path_from_name("org.example:thing:1.0@zip"),
            "org/example/thing/1.0/thing-1.0.zip",
        )
        with self.assertRaises(InstallError):
            maven_path_from_name("bad-name")

    def test_fabric_library_without_downloads(self) -> None:
        fabric = load_fixture("fabric_profile_1_20_1.json")
        lib = fabric["libraries"][0]
        artifact = library_artifact(lib)
        self.assertIsNotNone(artifact)
        self.assertTrue(artifact["url"].startswith("https://maven.fabricmc.net/"))
        self.assertTrue(artifact["path"].endswith(".jar"))

    def test_artifact_from_downloads_block(self) -> None:
        lib = {
            "name": "org.example:lib:1.0",
            "downloads": {
                "artifact": {
                    "url": "https://example.com/lib.jar",
                    "path": "org/example/lib/1.0/lib-1.0.jar",
                }
            },
        }
        self.assertEqual(library_artifact(lib)["url"], "https://example.com/lib.jar")

    def test_old_style_natives_library(self) -> None:
        libs = {lib["name"]: lib for lib in load_fixture("version_1_7_10.json")["libraries"]}
        platform = libs["org.lwjgl.lwjgl:lwjgl-platform:2.9.1"]
        self.assertIsNone(library_artifact(platform))
        parsed = library_native_artifact(platform, "windows", "x86_64")
        self.assertIsNotNone(parsed)
        classifier, artifact = parsed
        self.assertEqual(classifier, "natives-windows")
        self.assertTrue(artifact["path"].endswith("natives-windows.jar"))

    def test_native_classifier_values(self) -> None:
        libs = {lib["name"]: lib for lib in load_fixture("version_1_7_10.json")["libraries"]}
        platform = libs["org.lwjgl.lwjgl:lwjgl-platform:2.9.1"]
        self.assertEqual(native_classifier(platform, "windows", "x86_64"), "natives-windows")
        self.assertEqual(native_classifier(platform, "osx", "arm64"), "natives-osx")
        twitch = libs["tv.twitch:twitch-platform:5.16"]
        self.assertEqual(native_classifier(twitch, "windows", "x86_64"), "natives-windows-64")
        self.assertEqual(native_classifier(twitch, "windows", "x86"), "natives-windows-32")
        self.assertIsNone(native_classifier(libs["com.google.code.gson:gson:2.2.4"], "windows", "x86_64"))

    def test_twitch_missing_platform_classifier_returns_none(self) -> None:
        libs = {lib["name"]: lib for lib in load_fixture("version_1_7_10.json")["libraries"]}
        external = libs["tv.twitch:twitch-external-platform:4.5"]
        self.assertIsNone(library_native_artifact(external, "linux", "x86_64"))

    def test_relevant_libraries_1_20_1_windows(self) -> None:
        version = load_fixture("version_1_20_1.json")
        names = {
            lib["name"] for lib in relevant_libraries(version, "windows", "x86_64", "10.0")
        }
        expected = {
            "com.mojang:blocklist:1.0.10",
            "org.apache.logging.log4j:log4j-api:2.19.0",
            "org.lwjgl:lwjgl-glfw:3.3.1",
            "org.lwjgl:lwjgl-glfw:3.3.1:natives-windows",
            "org.lwjgl:lwjgl-glfw:3.3.1:natives-windows-arm64",
            "org.lwjgl:lwjgl-glfw:3.3.1:natives-windows-x86",
            "org.lwjgl:lwjgl:3.3.1",
            "org.lwjgl:lwjgl:3.3.1:natives-windows",
            "org.lwjgl:lwjgl:3.3.1:natives-windows-arm64",
            "org.lwjgl:lwjgl:3.3.1:natives-windows-x86",
        }
        self.assertEqual(names, expected)

    def test_relevant_libraries_1_20_1_linux(self) -> None:
        version = load_fixture("version_1_20_1.json")
        names = {
            lib["name"] for lib in relevant_libraries(version, "linux", "x86_64", "6.0")
        }
        expected = {
            "com.mojang:blocklist:1.0.10",
            "org.apache.logging.log4j:log4j-api:2.19.0",
            "org.lwjgl:lwjgl-glfw:3.3.1",
            "org.lwjgl:lwjgl-glfw:3.3.1:natives-linux",
            "org.lwjgl:lwjgl:3.3.1",
            "org.lwjgl:lwjgl:3.3.1:natives-linux",
            "io.netty:netty-transport-native-epoll:4.1.82.Final:linux-aarch_64",
            "io.netty:netty-transport-native-epoll:4.1.82.Final:linux-x86_64",
        }
        self.assertEqual(names, expected)

    def test_detect_os_values(self) -> None:
        os_name, os_arch, os_version = detect_os()
        self.assertIn(os_name, {"windows", "osx", "linux"})
        self.assertTrue(os_arch)
        self.assertIsInstance(os_version, str)


class AssetPathTest(unittest.TestCase):
    def test_object_relpath_and_path(self) -> None:
        digest = "abcdef0123456789abcdef0123456789abcdef01"
        self.assertEqual(asset_object_relpath(digest), f"ab/{digest}")
        root = Path("D:/mc")
        self.assertEqual(
            asset_object_path(root, digest),
            root / "assets" / "objects" / "ab" / digest,
        )

    def test_object_url(self) -> None:
        digest = "abcdef0123456789abcdef0123456789abcdef01"
        self.assertTrue(asset_object_url(digest).startswith(ASSET_OBJECT_BASE))
        self.assertEqual(
            asset_object_url(digest, "https://mirror.example/objects/"),
            f"https://mirror.example/objects/ab/{digest}",
        )

    def test_invalid_object_hash_raises(self) -> None:
        with self.assertRaises(InstallError):
            asset_object_relpath("x")

    def test_virtual_dir(self) -> None:
        self.assertEqual(
            asset_virtual_dir(Path("D:/mc"), "legacy"),
            Path("D:/mc") / "assets" / "virtual" / "legacy",
        )


# ---------------------------------------------------------------------------
# natives 解压
# ---------------------------------------------------------------------------


class NativeExtractTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="cf-natives-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.jar = self.tmp / "natives.jar"
        self.jar.write_bytes(
            make_zip(
                {
                    "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0",
                    "natives/foo.dll": b"dll-bytes",
                    "readme.txt": b"hello",
                    "../evil.dll": b"evil",
                }
            )
        )
        self.natives_dir = self.tmp / "natives"

    def test_extract_with_exclude(self) -> None:
        count = extract_natives(self.natives_dir, [(self.jar, ("META-INF/",))])
        self.assertEqual(count, 2)
        self.assertEqual((self.natives_dir / "natives" / "foo.dll").read_bytes(), b"dll-bytes")
        self.assertTrue((self.natives_dir / "readme.txt").is_file())
        self.assertFalse((self.natives_dir / "META-INF" / "MANIFEST.MF").exists())
        self.assertFalse((self.tmp / "evil.dll").exists())
        self.assertTrue(natives_extracted(self.natives_dir, [(self.jar, ("META-INF/",))]))

    def test_natives_extracted_detects_missing_file(self) -> None:
        extract_natives(self.natives_dir, [(self.jar, ())])
        self.assertTrue(natives_extracted(self.natives_dir, [(self.jar, ())]))
        (self.natives_dir / "readme.txt").unlink()
        self.assertFalse(natives_extracted(self.natives_dir, [(self.jar, ())]))

    def test_extract_cancel(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            extract_natives(self.natives_dir, [(self.jar, ())], cancel=cancel)

    def test_bad_zip_raises_install_error(self) -> None:
        bad = self.tmp / "bad.jar"
        bad.write_bytes(b"not a zip")
        with self.assertRaises(InstallError):
            extract_natives(self.natives_dir, [(bad, ())])


# ---------------------------------------------------------------------------
# 计划（plan）与版本链
# ---------------------------------------------------------------------------


class PlanTest(PureInstallerTest):
    def write_parent_json(self, version: dict, version_id: str | None = None) -> Path:
        target_id = version_id or str(version["id"])
        path = self.mc_root / "versions" / target_id / f"{target_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(version, ensure_ascii=False), encoding="utf-8")
        return path

    def test_plan_1_20_1_static_files(self) -> None:
        version = load_fixture("version_1_20_1.json")
        req = InstallRequest(mc_root=self.mc_root, version_id="1.20.1", version_json=version)
        tasks = self.installer.plan(req)
        destinations = {task.dest for task in tasks}
        self.assertIn(self.mc_root / "versions" / "1.20.1" / "1.20.1.jar", destinations)
        self.assertIn(
            self.mc_root / "libraries" / "org" / "lwjgl" / "lwjgl" / "3.3.1" / "lwjgl-3.3.1.jar",
            destinations,
        )
        self.assertIn(
            self.mc_root / "assets" / "log_configs" / "client-1.12.xml", destinations
        )
        self.assertIn(self.mc_root / "assets" / "indexes" / "5.json", destinations)
        os_name, os_arch, os_version = detect_os()
        expected_libs = relevant_libraries(version, os_name, os_arch, os_version)
        self.assertEqual(len(tasks), 1 + len(expected_libs) + 2)
        self.assertEqual(self.manifest.calls, [])

    def test_plan_1_7_10_native_classifiers(self) -> None:
        version = load_fixture("version_1_7_10.json")
        req = InstallRequest(mc_root=self.mc_root, version_id="1.7.10", version_json=version)
        tasks = self.installer.plan(req)
        destinations = {str(task.dest) for task in tasks}
        self.assertTrue(
            any("lwjgl-platform-2.9.1-natives-windows.jar" in dest for dest in destinations)
        )
        self.assertTrue(
            any("jinput-platform-2.0.5-natives-windows.jar" in dest for dest in destinations)
        )
        self.assertTrue(
            any("twitch-platform-5.16-natives-windows-64.jar" in dest for dest in destinations)
        )
        self.assertTrue(
            any(
                "twitch-external-platform-4.5-natives-windows-64.jar" in dest
                for dest in destinations
            )
        )
        self.assertFalse(any("natives-windows-32" in dest for dest in destinations))
        self.assertFalse(any("natives-linux" in dest for dest in destinations))
        self.assertIn(str(self.mc_root / "versions" / "1.7.10" / "1.7.10.jar"), destinations)

    def test_plan_fabric_inherits_parent_jar_directory(self) -> None:
        parent = load_fixture("version_1_20_1.json")
        child = load_fixture("fabric_profile_1_20_1.json")
        self.write_parent_json(parent)
        req = InstallRequest(
            mc_root=self.mc_root,
            version_id="1.20.1-fabric-0.15.11",
            version_json=child,
        )
        tasks = self.installer.plan(req)
        destinations = {str(task.dest) for task in tasks}
        self.assertIn(str(self.mc_root / "versions" / "1.20.1" / "1.20.1.jar"), destinations)
        self.assertNotIn(
            str(self.mc_root / "versions" / "1.20.1-fabric-0.15.11" / "1.20.1-fabric-0.15.11.jar"),
            destinations,
        )
        self.assertTrue(any("asm-9.6.jar" in dest for dest in destinations))
        self.assertTrue(any("sponge-mixin" in dest for dest in destinations))
        self.assertTrue(any("lwjgl-3.3.1.jar" in dest for dest in destinations))
        self.assertEqual(self.manifest.calls, [])

    def test_plan_uses_manifest_cache_for_parent_without_network(self) -> None:
        parent = load_fixture("version_1_20_1.json")
        child = load_fixture("fabric_profile_1_20_1.json")
        self.manifest.versions["1.20.1"] = parent
        req = InstallRequest(
            mc_root=self.mc_root,
            version_id="1.20.1-fabric-0.15.11",
            version_json=child,
        )
        tasks = self.installer.plan(req)
        destinations = {str(task.dest) for task in tasks}
        self.assertIn(str(self.mc_root / "versions" / "1.20.1" / "1.20.1.jar"), destinations)
        # plan 只用本地缓存，绝不发起网络请求。
        self.assertEqual(self.manifest.calls, [("cached", "1.20.1")])

    def test_plan_missing_parent_raises(self) -> None:
        child = load_fixture("fabric_profile_1_20_1.json")
        req = InstallRequest(
            mc_root=self.mc_root,
            version_id="1.20.1-fabric-0.15.11",
            version_json=child,
        )
        with self.assertRaises(InstallError):
            self.installer.plan(req)

    def test_plan_missing_version_json_raises(self) -> None:
        req = InstallRequest(mc_root=self.mc_root, version_id="x", version_json={})
        with self.assertRaises(InstallError):
            self.installer.plan(req)

    def test_install_request_defaults(self) -> None:
        req = InstallRequest(mc_root=self.mc_root, version_id="x", version_json={"id": "x"})
        self.assertEqual(req.workers, 32)
        self.assertIsNone(req.progress_cb)
        self.assertIsNone(req.cancel)


# ---------------------------------------------------------------------------
# 完整安装（本地 HTTP 集成测试）
# ---------------------------------------------------------------------------


class InstallerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.server = LocalHttpServer().start()
        self.addCleanup(self.server.stop)
        self._tmp = TemporaryDirectory(prefix="cf-install-int-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / ".minecraft"
        self.http = HttpSession(mirror="official", timeout=5, retries=0, mirror_base=self.server.base)
        self.addCleanup(self.http.close)
        self.manifest = FakeManifest()
        self.installer = Installer(self.http, self.manifest)
        self.version_id = "test-mc"
        self.register_routes()
        self._lock = threading.Lock()

    # -- 数据准备 ---------------------------------------------------------

    def register_routes(self) -> None:
        self.client_bytes = b"fake-client-jar-bytes-" * 64
        self.lib_bytes = b"fake-library-bytes-" * 32
        self.native_jar_bytes = make_zip(
            {
                "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0",
                "natives/foo.dll": b"dll-bytes",
                "readme.txt": b"hello-native",
            }
        )
        self.asset1 = b"asset-one"
        self.asset2 = b"asset-two"
        self.hash1 = sha1_bytes(self.asset1)
        self.hash2 = sha1_bytes(self.asset2)
        self.log_bytes = b"<Configuration/>"
        self.server.add_bytes("/client.jar", self.client_bytes)
        self.server.add_bytes("/libs/lib.jar", self.lib_bytes)
        self.server.add_bytes("/libs/native.jar", self.native_jar_bytes)
        self.server.add_bytes("/client-slow.jar", self.client_bytes, chunk_delay=0.02)
        self.server.add_bytes("/assets/log.xml", self.log_bytes)
        for data, digest in ((self.asset1, self.hash1), (self.asset2, self.hash2)):
            self.server.add_bytes(f"/objects/{digest[:2]}/{digest}", data)

    def register_index(self, virtual: bool, index_id: str) -> None:
        self.index_bytes = json.dumps(
            {
                "objects": {
                    "lang/en_US.lang": {"hash": self.hash1, "size": len(self.asset1)},
                    "icons/icon_16x16.png": {"hash": self.hash2, "size": len(self.asset2)},
                    "pack.mcmeta": {"hash": self.hash2, "size": len(self.asset2)},
                },
                "virtual": virtual,
            }
        ).encode("utf-8")
        self.index_id = index_id
        self.server.add_bytes("/assets/index.json", self.index_bytes)

    def build_version(
        self,
        version_id: str | None = None,
        *,
        virtual: bool = True,
        assets_field: str | None = None,
        client_url_path: str = "/client.jar",
        with_asset_index: bool = True,
    ) -> dict:
        version_id = version_id or self.version_id
        index_id = "legacy" if virtual else "test-assets"
        self.register_index(virtual, index_id)
        version: dict = {
            "id": version_id,
            "type": "release",
            "mainClass": "net.minecraft.client.main.Main",
            "releaseTime": "2024-01-01T00:00:00+00:00",
            "time": "2024-01-01T00:00:00+00:00",
            "minimumLauncherVersion": 21,
            "assets": assets_field if assets_field is not None else index_id,
            "downloads": {
                "client": {
                    "url": self.server.url(client_url_path),
                    "sha1": sha1_bytes(self.client_bytes),
                    "size": len(self.client_bytes),
                }
            },
            "javaVersion": {"component": "jre-legacy", "majorVersion": 8},
            "libraries": [
                {
                    "name": "org.example:lib:1.0",
                    "downloads": {
                        "artifact": {
                            "url": self.server.url("/libs/lib.jar"),
                            "path": "org/example/lib/1.0/lib-1.0.jar",
                            "sha1": sha1_bytes(self.lib_bytes),
                            "size": len(self.lib_bytes),
                        }
                    },
                },
                {
                    "name": "org.example:native:1.0",
                    "downloads": {
                        "classifiers": {
                            "natives-windows": {
                                "url": self.server.url("/libs/native.jar"),
                                "path": "org/example/native/1.0/native-1.0-natives-windows.jar",
                                "sha1": sha1_bytes(self.native_jar_bytes),
                                "size": len(self.native_jar_bytes),
                            },
                        }
                    },
                    "natives": {"windows": "natives-windows"},
                    "extract": {"exclude": ["META-INF/"]},
                },
            ],
            "logging": {
                "client": {
                    "argument": "-Dlog4j.configurationFile=${path}",
                    "type": "log4j2-xml",
                    "file": {
                        "id": "client-test.xml",
                        "url": self.server.url("/assets/log.xml"),
                        "sha1": sha1_bytes(self.log_bytes),
                        "size": len(self.log_bytes),
                    },
                }
            },
            "arguments": {
                "game": ["--username", "${auth_player_name}"],
                "jvm": ["-Djava.library.path=${natives_directory}"],
            },
        }
        if with_asset_index:
            version["assetIndex"] = {
                "id": index_id,
                "url": self.server.url("/assets/index.json"),
                "sha1": sha1_bytes(self.index_bytes),
                "size": len(self.index_bytes),
                "totalSize": len(self.asset1) + len(self.asset2),
            }
        return version

    def run_install(self, version: dict, *, version_id: str | None = None, **kwargs) -> Path:
        target_id = version_id or str(version.get("id") or self.version_id)
        req = InstallRequest(
            mc_root=self.mc_root,
            version_id=target_id,
            version_json=version,
            **kwargs,
        )
        with mock.patch(
            "cubefetch.core.install.ASSET_OBJECT_BASE", self.server.base + "/objects/"
        ):
            return self.installer.install(req)

    # -- 测试 -------------------------------------------------------------

    def test_full_install_layout(self) -> None:
        version = self.build_version()
        progress: list[Progress] = []
        result = self.run_install(version, workers=4, progress_cb=progress.append)

        version_dir = self.mc_root / "versions" / "test-mc"
        self.assertEqual(result, version_dir)
        self.assertEqual((version_dir / "test-mc.jar").read_bytes(), self.client_bytes)
        written = json.loads((version_dir / "test-mc.json").read_text(encoding="utf-8"))
        self.assertEqual(written, version)
        # libraries
        self.assertEqual(
            (self.mc_root / "libraries" / "org" / "example" / "lib" / "1.0" / "lib-1.0.jar").read_bytes(),
            self.lib_bytes,
        )
        self.assertTrue(
            (
                self.mc_root
                / "libraries"
                / "org"
                / "example"
                / "native"
                / "1.0"
                / "native-1.0-natives-windows.jar"
            ).is_file()
        )
        # natives
        natives_dir = version_dir / "test-mc-natives"
        self.assertEqual((natives_dir / "natives" / "foo.dll").read_bytes(), b"dll-bytes")
        self.assertTrue((natives_dir / "readme.txt").is_file())
        self.assertFalse((natives_dir / "META-INF").exists())
        # asset index / objects / log / virtual
        self.assertEqual(
            (self.mc_root / "assets" / "indexes" / "legacy.json").read_bytes(), self.index_bytes
        )
        self.assertEqual(
            (self.mc_root / "assets" / "objects" / self.hash1[:2] / self.hash1).read_bytes(),
            self.asset1,
        )
        self.assertEqual(
            (self.mc_root / "assets" / "objects" / self.hash2[:2] / self.hash2).read_bytes(),
            self.asset2,
        )
        self.assertEqual(
            (self.mc_root / "assets" / "log_configs" / "client-test.xml").read_bytes(),
            self.log_bytes,
        )
        virtual = self.mc_root / "assets" / "virtual" / "legacy"
        self.assertEqual((virtual / "lang" / "en_US.lang").read_bytes(), self.asset1)
        self.assertEqual((virtual / "icons" / "icon_16x16.png").read_bytes(), self.asset2)
        self.assertEqual((virtual / "pack.mcmeta").read_bytes(), self.asset2)
        # 相同哈希的对象只下载一次
        self.assertEqual(self.server.request_count(f"/objects/{self.hash2[:2]}/{self.hash2}"), 1)
        # 进度
        self.assertTrue(progress)
        messages = [item.message for item in progress]
        self.assertIn("正在下载游戏文件", messages)
        self.assertTrue(any(message.startswith("正在下载资源文件") for message in messages))
        self.assertIn("正在解压 natives", messages)
        self.assertIn("正在整理旧版资源文件", messages)
        self.assertIn("安装完成", messages)
        final = progress[-1]
        self.assertEqual(final.message, "安装完成")
        self.assertEqual(final.done, final.total)
        self.assertGreater(final.total, 0)
        self.assertEqual(final.bytes_done, final.bytes_total)

    def test_install_is_idempotent(self) -> None:
        version = self.build_version()
        self.run_install(version)
        self.server.clear_requests()
        self.run_install(version)
        self.assertEqual(self.server.request_count(), 0, "第二次安装不应重复下载")

    def test_cancel_before_start(self) -> None:
        version = self.build_version()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            self.run_install(version, cancel=cancel)
        self.assertEqual(self.server.request_count(), 0)

    def test_cancel_mid_download(self) -> None:
        version = self.build_version(client_url_path="/client-slow.jar")
        cancel = threading.Event()
        triggered = threading.Event()

        def on_progress(progress: Progress) -> None:
            if progress.bytes_done > 0 and not triggered.is_set():
                triggered.set()
                cancel.set()

        with self.assertRaises(CancelledError):
            self.run_install(version, progress_cb=on_progress, cancel=cancel)
        self.assertGreater(self.server.request_count(), 0)

    def test_gate_require_active_called(self) -> None:
        calls: list[int] = []

        class Gate:
            def require_active(self) -> None:
                calls.append(1)

        installer = Installer(self.http, self.manifest, gate=Gate())
        version = self.build_version()
        with mock.patch(
            "cubefetch.core.install.ASSET_OBJECT_BASE", self.server.base + "/objects/"
        ):
            installer.install(
                InstallRequest(mc_root=self.mc_root, version_id="test-mc", version_json=version)
            )
        self.assertEqual(calls, [1])

    def test_gate_inactive_blocks_install(self) -> None:
        class Gate:
            def require_active(self) -> None:
                raise AuthError("未登录")

        installer = Installer(self.http, self.manifest, gate=Gate())
        version = self.build_version()
        with self.assertRaises(AuthError):
            installer.install(
                InstallRequest(mc_root=self.mc_root, version_id="test-mc", version_json=version)
            )
        self.assertEqual(self.server.request_count(), 0)

    def test_install_non_virtual_assets(self) -> None:
        version = self.build_version(virtual=False)
        self.run_install(version)
        self.assertFalse((self.mc_root / "assets" / "virtual").exists())

    def test_install_legacy_assets_field_triggers_virtual(self) -> None:
        version = self.build_version(virtual=False, assets_field="legacy")
        self.run_install(version)
        virtual = self.mc_root / "assets" / "virtual" / "test-assets"
        self.assertEqual((virtual / "lang" / "en_US.lang").read_bytes(), self.asset1)

    def test_install_without_asset_index(self) -> None:
        version = self.build_version(with_asset_index=False)
        self.run_install(version)
        self.assertTrue((self.mc_root / "versions" / "test-mc" / "test-mc.jar").is_file())
        self.assertFalse((self.mc_root / "assets" / "indexes").exists())

    def test_install_writes_nothing_when_no_version_json(self) -> None:
        with self.assertRaises(InstallError):
            self.installer.install(
                InstallRequest(mc_root=self.mc_root, version_id="test-mc", version_json={})
            )

    def test_child_without_parent_raises(self) -> None:
        child = {
            "id": "fabric-test",
            "inheritsFrom": "vanilla-missing",
            "type": "release",
            "mainClass": "x.Y",
            "libraries": [],
        }
        with self.assertRaises(InstallError):
            self.run_install(child)

    def test_child_install_merges_parent_and_places_jar_in_parent_dir(self) -> None:
        parent = self.build_version("vanilla-test")
        self.manifest.versions["vanilla-test"] = parent
        mod_bytes = b"fabric-mod-bytes" * 20
        self.server.add_bytes("/maven/org/example/mod/1.0/mod-1.0.jar", mod_bytes)
        child = {
            "id": "fabric-test",
            "inheritsFrom": "vanilla-test",
            "type": "release",
            "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
            "releaseTime": "2024-02-01T00:00:00+00:00",
            "time": "2024-02-01T00:00:00+00:00",
            "libraries": [{"name": "org.example:mod:1.0", "url": self.server.base + "/maven/"}],
            "arguments": {
                "game": [],
                "jvm": ["-DFabricMcEmu= net.minecraft.client.main.Main "],
            },
        }
        progress: list[Progress] = []
        result = self.run_install(child, version_id="fabric-test", progress_cb=progress.append)

        child_dir = self.mc_root / "versions" / "fabric-test"
        self.assertEqual(result, child_dir)
        self.assertEqual(
            (self.mc_root / "versions" / "vanilla-test" / "vanilla-test.jar").read_bytes(),
            self.client_bytes,
        )
        self.assertFalse((child_dir / "fabric-test.jar").exists())
        written = json.loads((child_dir / "fabric-test.json").read_text(encoding="utf-8"))
        self.assertEqual(written["inheritsFrom"], "vanilla-test")
        self.assertEqual(written["id"], "fabric-test")
        self.assertEqual(
            (
                self.mc_root
                / "libraries"
                / "org"
                / "example"
                / "mod"
                / "1.0"
                / "mod-1.0.jar"
            ).read_bytes(),
            mod_bytes,
        )
        self.assertTrue(
            (
                self.mc_root / "versions" / "vanilla-test" / "vanilla-test.json"
            ).is_file()
        )
        natives_dir = child_dir / "fabric-test-natives"
        self.assertEqual((natives_dir / "natives" / "foo.dll").read_bytes(), b"dll-bytes")
        self.assertTrue(progress)


# ---------------------------------------------------------------------------
# 进度流不变量（真实裁剪 fixture：1.20.1 现代 / 1.7.10 legacy 资源）
# ---------------------------------------------------------------------------


class ProgressInvariantTest(unittest.TestCase):
    """用真实版本 JSON 跑完整安装，校验整体口径与阶段口径的进度不变量。"""

    def setUp(self) -> None:
        self.server = LocalHttpServer().start()
        self.addCleanup(self.server.stop)
        self._tmp = TemporaryDirectory(prefix="cf-progress-")
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / ".minecraft"
        self.http = HttpSession(
            mirror="bmclapi",
            timeout=5,
            retries=0,
            mirror_base=self.server.base,
        )
        self.addCleanup(self.http.close)
        self.installer = Installer(self.http, FakeManifest())

    # -- 数据准备 ---------------------------------------------------------

    def server_path(self, url: str) -> str:
        return urlsplit(mirror_url(url, self.server.base)).path

    def serve(self, meta: dict, payload: bytes) -> None:
        """登记下载地址并把 sha1/size 改成与本次提供的字节一致。"""
        self.server.add_bytes(self.server_path(meta["url"]), payload)
        meta["sha1"] = sha1_bytes(payload)
        meta["size"] = len(payload)

    def prepare_version(
        self, fixture: str, version_id: str, *, virtual: bool
    ) -> tuple[dict, int]:
        """把 fixture 版本 JSON 引用的所有文件映射到本地 HTTP 服务。"""
        version = copy.deepcopy(load_fixture(fixture))
        version["id"] = version_id
        self.serve(version["downloads"]["client"], f"client:{version_id}".encode("utf-8"))

        native_zip = make_zip(
            {
                "META-INF/MANIFEST.MF": b"Manifest-Version: 1.0",
                "foo.dll": b"native-dll",
            }
        )
        for lib in version.get("libraries", []):
            downloads = lib.get("downloads") or {}
            artifact = downloads.get("artifact")
            if isinstance(artifact, dict) and artifact.get("url"):
                name = str(lib.get("name") or "lib")
                self.serve(artifact, ("lib:" + name).encode("utf-8"))
            for meta in (downloads.get("classifiers") or {}).values():
                if not isinstance(meta, dict) or not meta.get("url"):
                    continue
                if lib.get("natives"):
                    self.serve(meta, native_zip)
                else:
                    name = str(lib.get("name") or "lib")
                    self.serve(meta, ("lib:" + name).encode("utf-8"))

        logging_client = (version.get("logging") or {}).get("client") or {}
        log_file = logging_client.get("file")
        if isinstance(log_file, dict) and log_file.get("url"):
            self.serve(log_file, b"<Configuration/>")

        if virtual:
            index = copy.deepcopy(load_fixture("asset_index_legacy.json"))
        else:
            index = {"objects": {name: {} for name in (
                "minecraft/sounds/random/click.ogg",
                "lang/en_US.lang",
                "icons/icon_16x16.png",
            )}}
        for name, obj in index["objects"].items():
            payload = f"asset:{name}".encode("utf-8")
            obj["hash"] = sha1_bytes(payload)
            obj["size"] = len(payload)
            self.server.add_bytes(f"/objects/{obj['hash'][:2]}/{obj['hash']}", payload)
        index_payload = json.dumps(index).encode("utf-8")
        asset_index = version.get("assetIndex")
        if isinstance(asset_index, dict):
            self.serve(asset_index, index_payload)
        return version, len(index["objects"])

    # -- 断言 -------------------------------------------------------------

    def assert_progress_stream(
        self, events: list[Progress], static_count: int, asset_count: int
    ) -> None:
        expected_total = static_count + asset_count
        self.assertTrue(events, "安装过程应产生进度事件")
        prev_done = 0
        prev_bytes = 0
        prev_total = -1
        prev_bytes_total = -1
        prev_stage = None
        prev_stage_done = 0
        for event in events:
            # 范围不变量
            self.assertGreaterEqual(event.done, 0)
            self.assertLessEqual(event.done, event.total)
            self.assertGreaterEqual(event.bytes_done, 0)
            self.assertLessEqual(event.bytes_done, event.bytes_total)
            self.assertGreaterEqual(event.stage_done, 0)
            self.assertLessEqual(event.stage_done, event.stage_total)
            # 整体口径单调不减
            self.assertGreaterEqual(event.done, prev_done)
            self.assertGreaterEqual(event.bytes_done, prev_bytes)
            self.assertGreaterEqual(event.total, prev_total)
            self.assertGreaterEqual(event.bytes_total, prev_bytes_total)
            # 阶段内进度单调不减；切换阶段时重新开始计数
            if event.stage != prev_stage:
                prev_stage = event.stage
                prev_stage_done = 0
            self.assertGreaterEqual(event.stage_done, prev_stage_done)
            # 阶段文案不再携带“分母/分子”这种会与整体口径打架的数字
            self.assertNotRegex(event.message, r"\d+\s*/\s*\d+")
            self.assertNotRegex(event.message, r"\d")
            prev_done, prev_bytes = event.done, event.bytes_done
            prev_total, prev_bytes_total = event.total, event.bytes_total
            prev_stage_done = event.stage_done

        last = events[-1]
        self.assertEqual(last.message, "安装完成")
        self.assertEqual(last.done, expected_total)
        self.assertEqual(last.total, expected_total)
        self.assertEqual(last.bytes_done, last.bytes_total)
        self.assertGreater(last.bytes_total, 0)

        # 首事件：0 进度、整体口径是静态文件数
        first = events[0]
        self.assertEqual(first.done, 0)
        self.assertEqual(first.bytes_done, 0)
        self.assertEqual(first.total, static_count)

        # 资源阶段：整体 total 覆盖“静态 + 资源”，阶段口径独立
        stage_events = [event for event in events if event.stage == "正在下载资源文件"]
        self.assertTrue(stage_events)
        for event in stage_events:
            self.assertEqual(event.total, expected_total)
            self.assertEqual(event.stage_total, asset_count)
            self.assertEqual(event.stage_done, event.done - static_count)
        self.assertEqual(stage_events[-1].stage_done, asset_count)
        self.assertEqual(stage_events[-1].done, expected_total)

    # -- 用例 -------------------------------------------------------------

    def _run_fixture(
        self, fixture: str, version_id: str, *, virtual: bool
    ) -> tuple[list[Progress], int, int]:
        version, asset_count = self.prepare_version(fixture, version_id, virtual=virtual)
        events: list[Progress] = []
        req = InstallRequest(
            mc_root=self.mc_root,
            version_id=version_id,
            version_json=version,
            workers=4,
            progress_cb=events.append,
        )
        static_count = len(self.installer.plan(req))
        with mock.patch(
            "cubefetch.core.install.ASSET_OBJECT_BASE", self.server.base + "/objects/"
        ):
            self.installer.install(req)
        return events, static_count, asset_count

    def test_modern_1_20_1_progress_stream(self) -> None:
        events, static_count, asset_count = self._run_fixture(
            "version_1_20_1.json", "1.20.1", virtual=False
        )
        self.assertGreater(static_count, 0)
        self.assertGreater(asset_count, 0)
        self.assert_progress_stream(events, static_count, asset_count)
        self.assertIn("正在下载游戏文件", [event.message for event in events])
        self.assertTrue(
            (self.mc_root / "versions" / "1.20.1" / "1.20.1.jar").is_file()
        )
        self.assertFalse((self.mc_root / "assets" / "virtual").exists())

    def test_legacy_1_7_10_progress_stream(self) -> None:
        events, static_count, asset_count = self._run_fixture(
            "version_1_7_10.json", "1.7.10", virtual=True
        )
        self.assertGreater(static_count, 0)
        self.assertGreater(asset_count, 0)
        self.assert_progress_stream(events, static_count, asset_count)
        messages = [event.message for event in events]
        self.assertIn("正在解压 natives", messages)
        self.assertIn("正在整理旧版资源文件", messages)
        # natives 解压与 legacy virtual 资源确实完成
        natives = self.mc_root / "versions" / "1.7.10" / "1.7.10-natives"
        self.assertTrue((natives / "foo.dll").is_file())
        virtual = self.mc_root / "assets" / "virtual" / "1.7.10"
        self.assertTrue((virtual / "icons" / "icon_16x16.png").is_file())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
