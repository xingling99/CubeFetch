"""``cubefetch.core.loader`` 单元测试：离线 fixture + 假 HttpSession / Installer，不联网、不跑 Java。"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from cubefetch.core import installer_processors as processors_module
from cubefetch.core import loader as loader_module
from cubefetch.core.errors import CancelledError, InstallError, NetworkError
from cubefetch.core.http import Progress
from cubefetch.core.loader import (
    FABRIC_META,
    FORGE_MAVEN_BASE,
    FORGE_METADATA_URL,
    FORGE_PROMOTIONS_URL,
    INSTALLER_MAVEN_MIRROR,
    NEOFORGE_MAVEN_BASE,
    QUILT_META,
    LoaderBuild,
    LoaderService,
    neoforge_version_prefix,
)

FABRIC_LIST_URL = f"{FABRIC_META}/versions/loader/1.20.1"
FABRIC_PROFILE_URL = f"{FABRIC_META}/versions/loader/1.20.1/0.15.7/profile/json"
QUILT_LIST_URL = f"{QUILT_META}/versions/loader/1.20.1"
QUILT_PROFILE_URL = f"{QUILT_META}/versions/loader/1.20.1/0.23.2/profile/json"
NEOFORGE_METADATA_URL = f"{NEOFORGE_MAVEN_BASE}/neoforge/maven-metadata.xml"
NEOFORGE_LEGACY_METADATA_URL = f"{NEOFORGE_MAVEN_BASE}/forge/maven-metadata.xml"


def _xml_metadata(artifact: str, versions: list[str]) -> bytes:
    body = "".join(f"<version>{version}</version>" for version in versions)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<metadata><groupId>net.neoforged</groupId><artifactId>{artifact}</artifactId>"
        f"<versioning><latest>{versions[-1] if versions else ''}</latest>"
        f"<versions>{body}</versions></versioning></metadata>"
    ).encode("utf-8")


FABRIC_PROFILE = {
    "id": "fabric-loader-0.15.7-1.20.1",
    "inheritsFrom": "1.20.1",
    "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
    "libraries": [{"name": "net.fabricmc:fabric-loader:0.15.7"}],
}

QUILT_PROFILE = {
    "id": "quilt-loader-0.23.2-1.20.1",
    "inheritsFrom": "1.20.1",
    "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
}


class FakeHttp:
    """只回应预置的 URL；下载写一个假 jar 文件。"""

    def __init__(self) -> None:
        self.json_responses: dict[str, object] = {}
        self.bytes_responses: dict[str, bytes] = {}
        self.downloads: list[tuple[str, Path]] = []
        self.workers = 4

    def get_json(self, url: str, *, use_mirror: bool = True):
        if url in self.json_responses:
            return self.json_responses[url]
        raise AssertionError(f"FakeHttp 未预置 get_json 响应：{url}")

    def get_bytes(self, url: str, *, use_mirror: bool = True) -> bytes:
        if url in self.bytes_responses:
            return self.bytes_responses[url]
        raise AssertionError(f"FakeHttp 未预置 get_bytes 响应：{url}")

    def download(
        self,
        url: str,
        dest,
        *,
        sha1=None,
        size=None,
        use_mirror=True,
        timeout=None,
        progress_cb=None,
        cancel=None,
    ) -> None:
        destination = Path(dest)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"fake-installer-jar")
        self.downloads.append((url, destination))
        if progress_cb is not None:
            progress_cb(destination.stat().st_size)


class FakeInstaller:
    """模拟 ``Installer.install``：写出版本目录和版本 JSON。"""

    def __init__(self) -> None:
        self.requests = []

    def install(self, req):
        self.requests.append(req)
        version_dir = Path(req.mc_root) / "versions" / req.version_id
        version_dir.mkdir(parents=True, exist_ok=True)
        data = dict(req.version_json or {})
        data.setdefault("id", req.version_id)
        (version_dir / f"{req.version_id}.json").write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8"
        )
        return version_dir


class FakeManifest:
    def __init__(self, versions: dict[str, dict] | None = None) -> None:
        self.versions = versions or {
            "1.20.1": {"id": "1.20.1", "type": "release", "libraries": []},
            "1.21.1": {"id": "1.21.1", "type": "release", "libraries": []},
        }

    def fetch_version_json(self, version_id: str) -> dict:
        if version_id not in self.versions:
            raise InstallError(f"未知版本 {version_id}")
        return json.loads(json.dumps(self.versions[version_id]))


def make_service(
    http: FakeHttp | None = None,
    installer: FakeInstaller | None = None,
    manifest: FakeManifest | None = None,
    *,
    use_official_installer: bool | None = None,
) -> LoaderService:
    return LoaderService(
        http or FakeHttp(),
        installer or FakeInstaller(),
        manifest or FakeManifest(),
        use_official_installer=use_official_installer,
    )


class LoaderListTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.http = FakeHttp()

    def test_vanilla_returns_empty(self) -> None:
        service = make_service(http=self.http)
        self.assertEqual(service.list_loaders("vanilla", "1.20.1"), [])
        self.assertIsNone(service.recommend("vanilla", "1.20.1"))

    def test_unknown_kind_and_empty_version_raise(self) -> None:
        service = make_service(http=self.http)
        with self.assertRaises(ValueError):
            service.list_loaders("optifine", "1.20.1")
        with self.assertRaises(ValueError):
            service.list_loaders("fabric", "   ")

    def test_fabric_list_sorted_stable_first_then_version_desc(self) -> None:
        # 官方真实响应：每项是 {"loader": {"version", "stable", ...}, "intermediary": {...}}；
        # 同时兼容扁平的 {"version", "stable"} 形式（旧镜像/早期接口）。
        self.http.json_responses[FABRIC_LIST_URL] = [
            {"loader": {"version": "0.20.1", "stable": False}, "intermediary": {"version": "1.20.1"}},
            {"loader": {"version": "0.16.0", "stable": False}},
            {"loader": {"version": "0.15.10", "stable": True}},
            {"loader": {"version": "0.15.10", "stable": True}},  # 重复项应被去重
            {"version": "0.15.7", "stable": True},
        ]
        service = make_service(http=self.http)
        items = service.list_loaders("fabric", "1.20.1")
        self.assertEqual(
            [item["version"] for item in items], ["0.15.10", "0.15.7", "0.20.1", "0.16.0"]
        )
        self.assertEqual([bool(item["stable"]) for item in items], [True, True, False, False])
        self.assertEqual(service.recommend("fabric", "1.20.1"), "0.15.10")

    def test_quilt_list(self) -> None:
        # Quilt 列表没有 stable 字段：含 alpha/beta/rc 的版本视为预发布，稳定版优先。
        self.http.json_responses[QUILT_LIST_URL] = [
            {"loader": {"version": "0.23.2"}},
            {"loader": {"version": "0.24.0"}},
            {"loader": {"version": "0.31.0-beta.4"}},
            {"loader": {"version": "0.31.0-beta.3"}},
        ]
        service = make_service(http=self.http)
        items = service.list_loaders("quilt", "1.20.1")
        self.assertEqual(
            [item["version"] for item in items],
            ["0.24.0", "0.23.2", "0.31.0-beta.4", "0.31.0-beta.3"],
        )
        self.assertEqual([bool(item["stable"]) for item in items], [True, True, False, False])
        self.assertEqual(service.recommend("quilt", "1.20.1"), "0.24.0")

    def test_forge_promotions_and_maven_merged(self) -> None:
        self.http.json_responses[FORGE_PROMOTIONS_URL] = {
            "promotions": {
                "1.20.1-recommended": "47.2.0",
                "1.20.1-latest": "47.3.0",
                "1.21.1-latest": "52.0.1",
            }
        }
        self.http.bytes_responses[FORGE_METADATA_URL] = _xml_metadata(
            "forge", ["1.20.1-47.1.0", "1.20.1-47.2.0", "1.20.1-47.3.0", "1.21.1-52.0.1"]
        )
        service = make_service(http=self.http)
        items = service.list_loaders("forge", "1.20.1")
        versions = [item["version"] for item in items]
        self.assertEqual(versions, ["47.3.0", "47.2.0", "47.1.0"])
        self.assertTrue(items[0]["stable"] and items[1]["stable"])
        self.assertFalse(items[2]["stable"])
        self.assertEqual(service.recommend("forge", "1.20.1"), "47.2.0")

    def test_forge_promotions_failure_falls_back_to_maven(self) -> None:
        from cubefetch.core.errors import NetworkError

        class PromotionsDownHttp(FakeHttp):
            def get_json(self, url: str, *, use_mirror: bool = True):
                if url == FORGE_PROMOTIONS_URL:
                    raise NetworkError("promotions 不可用")
                return super().get_json(url, use_mirror=use_mirror)

            def get_bytes(self, url: str, *, use_mirror: bool = True) -> bytes:
                if url == FORGE_PROMOTIONS_URL:
                    raise NetworkError("promotions 不可用")
                return super().get_bytes(url, use_mirror=use_mirror)

        http = PromotionsDownHttp()
        http.bytes_responses[FORGE_METADATA_URL] = _xml_metadata("forge", ["1.20.1-47.1.0"])
        service = make_service(http=http)
        self.assertEqual([item["version"] for item in service.list_loaders("forge", "1.20.1")], ["47.1.0"])

    def test_neoforge_version_prefix_mapping(self) -> None:
        cases = {
            "1.21.1": "21.1.",
            "1.21": "21.0.",
            "1.20.2": "20.2.",
            "1.20.4": "20.4.",
            "1.21.4": "21.4.",
            "26.3": "26.3.0.",
            "26.3.0": "26.3.0.",
        }
        for game, expected in cases.items():
            self.assertEqual(neoforge_version_prefix(game), expected, game)

    def test_neoforge_list_uses_mc_mapping(self) -> None:
        self.http.bytes_responses[NEOFORGE_METADATA_URL] = _xml_metadata(
            "neoforge", ["21.0.167", "21.1.80", "21.1.264", "20.4.237"]
        )
        service = make_service(http=self.http)
        items = service.list_loaders("neoforge", "1.21.1")
        self.assertEqual([item["version"] for item in items], ["21.1.264", "21.1.80"])
        self.assertEqual(service.recommend("neoforge", "1.21.1"), "21.1.264")

    def test_neoforge_legacy_1_20_1(self) -> None:
        self.http.bytes_responses[NEOFORGE_LEGACY_METADATA_URL] = _xml_metadata(
            "forge", ["1.20.1-47.1.100", "1.20.1-47.1.106"]
        )
        service = make_service(http=self.http)
        items = service.list_loaders("neoforge", "1.20.1")
        self.assertEqual([item["version"] for item in items], ["47.1.106", "47.1.100"])


class LoaderPrepareTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.http = FakeHttp()

    def test_prepare_vanilla_uses_manifest(self) -> None:
        service = make_service(http=self.http)
        build = service.prepare("vanilla", None, "1.20.1")
        self.assertIsInstance(build, LoaderBuild)
        self.assertEqual(build.kind, "vanilla")
        self.assertIsNone(build.version)
        self.assertEqual(build.profile_id, "1.20.1")
        self.assertEqual(build.version_json["id"], "1.20.1")
        self.assertFalse(build.needs_installer)

    def test_prepare_fabric_downloads_profile_json(self) -> None:
        self.http.json_responses[FABRIC_PROFILE_URL] = FABRIC_PROFILE
        service = make_service(http=self.http)
        build = service.prepare("fabric", "0.15.7", "1.20.1")
        self.assertEqual(build.kind, "fabric")
        self.assertEqual(build.version, "0.15.7")
        self.assertEqual(build.profile_id, "1.20.1-fabric-0.15.7")
        self.assertEqual(build.version_json["id"], build.profile_id)
        self.assertEqual(build.version_json["inheritsFrom"], "1.20.1")
        self.assertFalse(build.needs_installer)

    def test_prepare_quilt_downloads_profile_json(self) -> None:
        self.http.json_responses[QUILT_PROFILE_URL] = QUILT_PROFILE
        service = make_service(http=self.http)
        build = service.prepare("quilt", "0.23.2", "1.20.1")
        self.assertEqual(build.profile_id, "1.20.1-quilt-0.23.2")
        self.assertEqual(build.version_json["inheritsFrom"], "1.20.1")

    def test_prepare_forge_and_neoforge_need_installer(self) -> None:
        service = make_service(http=self.http)
        forge = service.prepare("forge", "47.2.0", "1.20.1")
        self.assertTrue(forge.needs_installer)
        self.assertIsNone(forge.version_json)
        self.assertEqual(forge.profile_id, "1.20.1-forge-47.2.0")
        neoforge = service.prepare("neoforge", "21.1.80", "1.21.1")
        self.assertTrue(neoforge.needs_installer)
        self.assertEqual(neoforge.profile_id, "1.21.1-neoforge-21.1.80")

    def test_prepare_requires_loader_version(self) -> None:
        service = make_service(http=self.http)
        with self.assertRaises(ValueError):
            service.prepare("fabric", None, "1.20.1")
        with self.assertRaises(ValueError):
            service.prepare("forge", "", "1.20.1")


class LoaderInstallTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cubefetch-loader-")
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / "mc"
        self.mc_root.mkdir(parents=True, exist_ok=True)
        self.http = FakeHttp()
        self.installer = FakeInstaller()
        self.service = make_service(http=self.http, installer=self.installer)
        # 官方 --installClient 安装器路径的显式开关版本（默认走 processors）。
        self.official_service = make_service(
            http=self.http, installer=self.installer, use_official_installer=True
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _java(self) -> Path:
        java = self.tmp / "bin" / "java.exe"
        java.parent.mkdir(parents=True, exist_ok=True)
        java.write_bytes(b"fake-java")
        return java

    # -- vanilla / fabric -------------------------------------------------

    def test_install_vanilla_passthrough(self) -> None:
        build = self.service.prepare("vanilla", None, "1.20.1")
        progress: list[Progress] = []
        profile_id = self.service.install(
            build, self.mc_root, progress_cb=progress.append, cancel=threading.Event()
        )
        self.assertEqual(profile_id, "1.20.1")
        self.assertEqual(len(self.installer.requests), 1)
        request = self.installer.requests[0]
        self.assertEqual(request.version_id, "1.20.1")
        self.assertEqual(request.mc_root, self.mc_root)
        self.assertTrue((self.mc_root / "versions" / "1.20.1" / "1.20.1.json").is_file())
        self.assertTrue(progress)

    def test_install_fabric_writes_profile_json_and_hands_to_installer(self) -> None:
        self.http.json_responses[FABRIC_PROFILE_URL] = FABRIC_PROFILE
        build = self.service.prepare("fabric", "0.15.7", "1.20.1")
        profile_id = self.service.install(build, self.mc_root)
        self.assertEqual(profile_id, "1.20.1-fabric-0.15.7")
        json_path = self.mc_root / "versions" / profile_id / f"{profile_id}.json"
        self.assertTrue(json_path.is_file())
        data = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(data["inheritsFrom"], "1.20.1")
        self.assertEqual(data["id"], profile_id)
        self.assertEqual(self.installer.requests[0].version_id, profile_id)
        self.assertEqual(self.installer.requests[0].version_json["inheritsFrom"], "1.20.1")

    def test_install_cancel_raises_before_work(self) -> None:
        build = self.service.prepare("vanilla", None, "1.20.1")
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            self.service.install(build, self.mc_root, cancel=cancel)
        self.assertEqual(self.installer.requests, [])

    # -- Forge ------------------------------------------------------------

    def test_install_forge_downloads_runs_installer_and_detects_id(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        calls: list[dict] = []

        def fake_run(
            java_exe,
            jar_path,
            mc_root,
            *,
            progress_cb=None,
            cancel=None,
            label="Forge",
            timeout=None,
            mirror_url=None,
        ):
            calls.append(
                {
                    "java": Path(java_exe),
                    "jar": Path(jar_path),
                    "root": Path(mc_root),
                    "label": label,
                    "timeout": timeout,
                    "mirror": mirror_url,
                }
            )
            if progress_cb is not None:
                progress_cb(Progress(message="正在安装 Forge：Considering library foo"))
            profile_dir = Path(mc_root) / "versions" / "1.20.1-forge-47.2.0"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "1.20.1-forge-47.2.0.json").write_text(
                json.dumps({"id": "1.20.1-forge-47.2.0", "inheritsFrom": "1.20.1"}),
                encoding="utf-8",
            )
            return ["Considering library foo", "Download completed"]

        progress: list[Progress] = []
        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            profile_id = self.official_service.install(
                build, self.mc_root, java_exe=self._java(), progress_cb=progress.append
            )
        self.assertEqual(profile_id, "1.20.1-forge-47.2.0")
        self.assertEqual(len(calls), 1)
        expected_url = f"{FORGE_MAVEN_BASE}/1.20.1-47.2.0/forge-1.20.1-47.2.0-installer.jar"
        self.assertEqual(self.http.downloads[0][0], expected_url)
        self.assertEqual(calls[0]["mirror"], INSTALLER_MAVEN_MIRROR)
        # 安装器成功后会把 profile 交给 Installer 补齐继承链资源。
        self.assertEqual(len(self.installer.requests), 1)
        self.assertEqual(self.installer.requests[0].version_id, "1.20.1-forge-47.2.0")
        jar = self.mc_root / ".cubefetch" / "installers" / "forge-1.20.1-47.2.0-installer.jar"
        self.assertTrue(jar.is_file())
        self.assertEqual(calls[0]["jar"], jar)
        profiles = json.loads((self.mc_root / "launcher_profiles.json").read_text(encoding="utf-8"))
        self.assertEqual(profiles, {"profiles": {}, "settings": {}, "version": 3})
        self.assertTrue(any("Forge" in item.message for item in progress))

        # 重复安装：已存在的 profile 直接复用，不再下载/运行 installer。
        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            again = self.official_service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(again, "1.20.1-forge-47.2.0")
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.http.downloads), 1)

    def test_install_forge_existing_profile_skips_installer(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        profile_dir = self.mc_root / "versions" / "1.20.1-forge-47.2.0"
        profile_dir.mkdir(parents=True)
        (profile_dir / "1.20.1-forge-47.2.0.json").write_text(
            json.dumps({"id": "1.20.1-forge-47.2.0", "inheritsFrom": "1.20.1"}),
            encoding="utf-8",
        )
        with mock.patch.object(loader_module, "_run_java_installer") as runner:
            profile_id = self.official_service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(profile_id, "1.20.1-forge-47.2.0")
        runner.assert_not_called()
        self.assertEqual(self.http.downloads, [])

    def test_install_forge_missing_generated_json_raises(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        with mock.patch.object(loader_module, "_run_java_installer", return_value=["nothing"]):
            with self.assertRaises(InstallError):
                self.official_service.install(build, self.mc_root, java_exe=self._java())

    def test_install_forge_without_java_raises(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        with mock.patch.object(loader_module, "pick_java", return_value=None):
            with self.assertRaises(InstallError):
                self.official_service.install(build, self.mc_root)

    def test_install_forge_official_mode_omits_mirror(self) -> None:
        self.http.mirror = "official"
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        calls: list[str | None] = []

        def fake_run(java_exe, jar_path, mc_root, *, progress_cb=None, cancel=None, label="Forge", timeout=None, mirror_url=None):
            calls.append(mirror_url)
            profile_dir = Path(mc_root) / "versions" / "1.20.1-forge-47.2.0"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "1.20.1-forge-47.2.0.json").write_text(
                json.dumps({"id": "1.20.1-forge-47.2.0", "inheritsFrom": "1.20.1"}),
                encoding="utf-8",
            )
            return ["ok"]

        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            profile_id = self.service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(profile_id, "1.20.1-forge-47.2.0")
        self.assertEqual(calls, [None])

    def test_install_forge_retries_once_after_installer_failure(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        calls: list[str | None] = []

        def fake_run(java_exe, jar_path, mc_root, *, progress_cb=None, cancel=None, label="Forge", timeout=None, mirror_url=None):
            calls.append(mirror_url)
            if len(calls) == 1:
                raise InstallError("安装器执行失败（退出码 1）", detail="network down")
            profile_dir = Path(mc_root) / "versions" / "1.20.1-forge-47.2.0"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "1.20.1-forge-47.2.0.json").write_text(
                json.dumps({"id": "1.20.1-forge-47.2.0", "inheritsFrom": "1.20.1"}),
                encoding="utf-8",
            )
            return ["ok"]

        progress: list[Progress] = []
        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            profile_id = self.official_service.install(
                build, self.mc_root, java_exe=self._java(), progress_cb=progress.append
            )
        self.assertEqual(profile_id, "1.20.1-forge-47.2.0")
        self.assertEqual(len(calls), 2)
        self.assertTrue(any("重试" in item.message for item in progress))

    # -- NeoForge ---------------------------------------------------------

    def test_install_neoforge_modern_returns_installer_written_id(self) -> None:
        build = self.service.prepare("neoforge", "21.1.80", "1.21.1")
        self.assertEqual(build.profile_id, "1.21.1-neoforge-21.1.80")

        def fake_run(java_exe, jar_path, mc_root, **kwargs):
            profile_dir = Path(mc_root) / "versions" / "neoforge-21.1.80"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "neoforge-21.1.80.json").write_text(
                json.dumps({"id": "neoforge-21.1.80", "inheritsFrom": "1.21.1"}),
                encoding="utf-8",
            )
            return ["installing client"]

        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            profile_id = self.official_service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(profile_id, "neoforge-21.1.80")
        expected_url = (
            f"{NEOFORGE_MAVEN_BASE}/neoforge/21.1.80/neoforge-21.1.80-installer.jar"
        )
        self.assertEqual(self.http.downloads[0][0], expected_url)

    def test_install_neoforge_legacy_1_20_1_uses_forge_artifact(self) -> None:
        build = self.service.prepare("neoforge", "47.1.106", "1.20.1")
        self.assertEqual(build.profile_id, "1.20.1-neoforge-47.1.106")

        def fake_run(java_exe, jar_path, mc_root, **kwargs):
            profile_dir = Path(mc_root) / "versions" / "1.20.1-neoforge-47.1.106"
            profile_dir.mkdir(parents=True, exist_ok=True)
            (profile_dir / "1.20.1-neoforge-47.1.106.json").write_text(
                json.dumps({"id": "1.20.1-neoforge-47.1.106", "inheritsFrom": "1.20.1"}),
                encoding="utf-8",
            )
            return []

        with mock.patch.object(loader_module, "_run_java_installer", side_effect=fake_run):
            profile_id = self.official_service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(profile_id, "1.20.1-neoforge-47.1.106")
        expected_url = (
            f"{NEOFORGE_MAVEN_BASE}/forge/1.20.1-47.1.106/forge-1.20.1-47.1.106-installer.jar"
        )
        self.assertEqual(self.http.downloads[0][0], expected_url)


class LoaderProcessorsTestCase(unittest.TestCase):
    """Forge/NeoForge 默认走「自己执行 install_profile processors」路径。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cubefetch-proc-loader-")
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / "mc"
        self.mc_root.mkdir(parents=True, exist_ok=True)
        self.http = FakeHttp()
        self.installer = FakeInstaller()
        self.service = make_service(http=self.http, installer=self.installer)
        self.official_service = make_service(
            http=self.http, installer=self.installer, use_official_installer=True
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _java(self) -> Path:
        java = self.tmp / "bin" / "java.exe"
        java.parent.mkdir(parents=True, exist_ok=True)
        java.write_bytes(b"fake-java")
        return java

    def _engine(
        self, profile_id: str = "1.20.1-forge-47.2.0", inherits: str = "1.20.1"
    ):
        calls: list[dict] = []

        def fake(http, jar_path, mc_root, **kwargs):
            calls.append(
                {
                    "http": http,
                    "jar": Path(jar_path),
                    "mc_root": Path(mc_root),
                    **kwargs,
                }
            )
            progress_cb = kwargs.get("progress_cb")
            if progress_cb is not None:
                progress_cb(Progress(message=f"正在执行 {kwargs.get('label')} 补丁任务"))
            version_dir = Path(mc_root) / "versions" / profile_id
            version_dir.mkdir(parents=True, exist_ok=True)
            data = {"id": profile_id, "inheritsFrom": inherits}
            (version_dir / f"{profile_id}.json").write_text(
                json.dumps(data), encoding="utf-8"
            )
            return profile_id, data

        return calls, fake

    def test_install_forge_uses_processors_then_installer(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        calls, fake = self._engine()
        progress: list[Progress] = []
        with mock.patch.object(
            processors_module, "install_with_processors", side_effect=fake
        ):
            profile_id = self.service.install(
                build, self.mc_root, java_exe=self._java(), progress_cb=progress.append
            )
        self.assertEqual(profile_id, "1.20.1-forge-47.2.0")
        self.assertEqual(len(calls), 1)
        expected_url = f"{FORGE_MAVEN_BASE}/1.20.1-47.2.0/forge-1.20.1-47.2.0-installer.jar"
        self.assertEqual(self.http.downloads[0][0], expected_url)
        kwargs = calls[0]
        self.assertEqual(kwargs["game_version"], "1.20.1")
        self.assertEqual(kwargs["label"], "Forge")
        self.assertEqual(kwargs["vanilla_version_json"]["id"], "1.20.1")
        self.assertEqual(Path(kwargs["java_exe"]), self._java())
        jar = self.mc_root / ".cubefetch" / "installers" / "forge-1.20.1-47.2.0-installer.jar"
        self.assertEqual(kwargs["jar"], jar)
        # 安装完还要把 profile 交给 Installer 补齐 assets / 继承库。
        self.assertEqual(len(self.installer.requests), 1)
        self.assertEqual(self.installer.requests[0].version_id, profile_id)
        self.assertTrue(any("Forge" in item.message for item in progress))

        # 幂等：已存在 profile 时不再下载/执行 processors。
        with mock.patch.object(
            processors_module, "install_with_processors", side_effect=fake
        ):
            again = self.service.install(build, self.mc_root, java_exe=self._java())
        self.assertEqual(again, profile_id)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.http.downloads), 1)

    def test_install_processors_network_error_maps_to_install_error(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")

        def boom(*args, **kwargs):
            raise NetworkError("无法连接服务器")

        with mock.patch.object(
            processors_module, "install_with_processors", side_effect=boom
        ):
            with self.assertRaises(InstallError) as ctx:
                self.service.install(build, self.mc_root, java_exe=self._java())
        self.assertIn("网络", str(ctx.exception.user_message))

    def test_install_processors_without_java_raises(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        with mock.patch.object(loader_module, "pick_java", return_value=None):
            with mock.patch.object(
                processors_module, "install_with_processors"
            ) as engine:
                with self.assertRaises(InstallError):
                    self.service.install(build, self.mc_root)
        engine.assert_not_called()

    def test_install_processors_cancel_before_work(self) -> None:
        build = self.service.prepare("forge", "47.2.0", "1.20.1")
        cancel = threading.Event()
        cancel.set()
        with mock.patch.object(processors_module, "install_with_processors") as engine:
            with self.assertRaises(CancelledError):
                self.service.install(
                    build, self.mc_root, java_exe=self._java(), cancel=cancel
                )
        engine.assert_not_called()

    def test_install_neoforge_processors_returns_generated_id(self) -> None:
        build = self.service.prepare("neoforge", "21.1.80", "1.21.1")
        calls, fake = self._engine("neoforge-21.1.80", "1.21.1")
        with mock.patch.object(
            processors_module, "install_with_processors", side_effect=fake
        ):
            profile_id = self.service.install(
                build, self.mc_root, java_exe=self._java()
            )
        self.assertEqual(profile_id, "neoforge-21.1.80")
        self.assertEqual(
            self.http.downloads[0][0],
            f"{NEOFORGE_MAVEN_BASE}/neoforge/21.1.80/neoforge-21.1.80-installer.jar",
        )
        self.assertEqual(calls[0]["game_version"], "1.21.1")
        self.assertEqual(calls[0]["label"], "NeoForge")

    def test_should_use_official_installer_switch(self) -> None:
        self.http.mirror = "auto"
        self.assertFalse(self.service.should_use_official_installer())
        self.http.mirror = "official"
        self.assertTrue(self.service.should_use_official_installer())
        self.http.mirror = "bmclapi"
        self.assertTrue(self.official_service.should_use_official_installer())
        forced = make_service(
            http=self.http, installer=self.installer, use_official_installer=False
        )
        self.http.mirror = "official"
        self.assertFalse(forced.should_use_official_installer())


if __name__ == "__main__":
    unittest.main()
