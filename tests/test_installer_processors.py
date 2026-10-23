"""``cubefetch.core.installer_processors`` 单元测试。

全部离线：installer jar 用内存里构造的最小 zip 伪造，HttpSession 用假对象，
``run_process`` 用 mock 替换；不联网、不启动 Java。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from cubefetch.core import installer_processors as ip
from cubefetch.core.errors import CancelledError, InstallError, NetworkError
from cubefetch.core.http import Progress

TOOL_COORD = "net.minecraftforge:installertools:1.3.0"
TOOL_MAIN = "net.minecraftforge.installertools.ConsoleTool"
TOOL_PATH = "net/minecraftforge/installertools/1.3.0/installertools-1.3.0.jar"
PATCHED_COORD = "net.minecraftforge:forge:1.20.1-47.2.0:client"
MOJMAPS_COORD = "net.minecraft:client:1.20.1-20230612.114412:mappings@txt"
GAME_VERSION = "1.20.1"
PROFILE_ID = "1.20.1-forge-47.2.0"


def make_tool_jar_bytes(main_class: str = TOOL_MAIN) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "META-INF/MANIFEST.MF",
            f"Manifest-Version: 1.0\r\nMain-Class: {main_class}\r\n\r\n".encode("utf-8"),
        )
    return buffer.getvalue()


TOOL_JAR = make_tool_jar_bytes()

PROFILE = {
    "spec": 1,
    "minecraft": GAME_VERSION,
    "profile": "forge",
    "data": {
        "MCP_VERSION": "'1.20.1-20230612.114412'",
        "MC_SLIM": "[net.minecraft:client:1.20.1-20230612.114412:slim]",
        "BINPATCH": "/data/client.lzma",
        "MOJMAPS": f"[{MOJMAPS_COORD}]",
        "PATCHED": f"[{PATCHED_COORD}]",
    },
    "libraries": [
        {
            "name": "test.libs:alpha:1.0",
            "downloads": {
                "artifact": {
                    "path": "test/libs/alpha/1.0/alpha-1.0.jar",
                    "url": "https://maven.example/alpha-1.0.jar",
                    "size": 9,
                }
            },
        },
        # 没有 downloads.artifact 时用 name 推导路径。
        {"name": "test.libs:beta:2.0"},
    ],
    "processors": [
        {"sides": ["server"], "jar": TOOL_COORD, "args": ["--server"]},
        {
            "args": [
                "DOWNLOAD_MOJMAPS",
                "--version",
                GAME_VERSION,
                "--side",
                "{SIDE}",
                "--output",
                "{MOJMAPS}",
            ]
        },
        {
            "jar": TOOL_COORD,
            "classpath": [f"{TOOL_COORD}@jar"],
            "args": [
                "--input",
                "{MINECRAFT_JAR}",
                "--clean",
                "{MC_SLIM}",
                "--output",
                "{PATCHED}",
                "--apply",
                "{BINPATCH}",
                "--version",
                "{MCP_VERSION}",
            ],
        },
        {"sides": ["client"], "jar": TOOL_COORD, "args": ["--client-only", "{SIDE}"]},
    ],
}

VERSION_JSON = {
    "id": PROFILE_ID,
    "inheritsFrom": GAME_VERSION,
    "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
}


def make_installer_jar(path: Path, *, tool_jar: bytes = TOOL_JAR) -> Path:
    """构造一个最小 installer jar：install_profile + version + data + 工具 jar。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(ip.PROFILE_ENTRY, json.dumps(PROFILE))
        archive.writestr(ip.VERSION_ENTRY, json.dumps(VERSION_JSON))
        archive.writestr("data/client.lzma", b"LZMA-DATA")
        archive.writestr("libraries/" + TOOL_PATH, tool_jar)
    return path


VANILLA_JSON = {
    "id": GAME_VERSION,
    "downloads": {
        "client": {
            "url": "https://piston-data.mojang.com/v1/objects/client.jar",
            "sha1": "0" * 40,
            "size": 10,
        },
        "client_mappings": {
            "url": "https://piston-data.mojang.com/v1/objects/client.txt",
            "sha1": "1" * 40,
            "size": 20,
        },
    },
}


class FakeHttp:
    """假 HttpSession：download 写本地文件，可注入失败 URL。"""

    def __init__(self) -> None:
        self.mirror = "auto"
        self.workers = 2
        self.downloads: list[tuple[str, Path]] = []
        self.fail_urls: set[str] = set()
        self.payload_by_name: dict[str, bytes] = {"installertools-1.3.0.jar": TOOL_JAR}
        self.default_payload = b"downloaded-bytes"

    def download(
        self,
        url: str,
        destination: Path,
        *,
        sha1: str | None = None,
        size: int | None = None,
        use_mirror: bool = True,
        timeout: float | None = None,
        progress_cb=None,
        cancel=None,
    ) -> None:
        if cancel is not None and cancel.is_set():
            raise CancelledError("操作已取消")
        if url in self.fail_urls:
            raise NetworkError(f"无法连接服务器：{url}", detail=url)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        data = self.payload_by_name.get(destination.name, self.default_payload)
        destination.write_bytes(data)
        self.downloads.append((url, destination))
        if progress_cb is not None:
            progress_cb(len(data))


def fake_run_process(calls: list[list[str]] | None = None, *, returncode: int = 0):
    """假 run_process：记录命令，并按 ``--output`` 写出产物文件。"""

    def runner(command, cwd=None, timeout=None, env=None):
        if calls is not None:
            calls.append(list(command))
        output: str | None = None
        for index, token in enumerate(command):
            if token == "--output" and index + 1 < len(command):
                output = command[index + 1]
        if output:
            target = Path(output)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"patched-client")
        return subprocess.CompletedProcess(
            command, returncode, stdout="patch ok\n", stderr=""
        )

    return runner


class MavenPathTestCase(unittest.TestCase):
    def test_maven_path_variants(self) -> None:
        self.assertEqual(
            ip.maven_path("net.neoforged:AutoRenamingTool:2.0.3:all"),
            "net/neoforged/AutoRenamingTool/2.0.3/AutoRenamingTool-2.0.3-all.jar",
        )
        self.assertEqual(
            ip.maven_path("net.neoforged.installertools:installertools:2.1.2@jar"),
            "net/neoforged/installertools/installertools/2.1.2/installertools-2.1.2.jar",
        )
        self.assertEqual(
            ip.maven_path(MOJMAPS_COORD),
            "net/minecraft/client/1.20.1-20230612.114412/client-1.20.1-20230612.114412-mappings.txt",
        )

    def test_maven_path_invalid(self) -> None:
        with self.assertRaises(ValueError):
            ip.maven_path("")
        with self.assertRaises(ValueError):
            ip.maven_path("only:two")


class RequiredJavaTestCase(unittest.TestCase):
    def test_required_java_major_mapping(self) -> None:
        cases = {
            "1.21.1": 21,
            "1.20.6": 21,
            "1.20.5": 21,
            "1.20.4": 17,
            "1.20.1": 17,
            "1.19.4": 17,
            "1.18.2": 17,
            "1.17.1": 16,
            "1.16.5": 8,
            "1.12.2": 8,
            "23w45a": 17,
            "24w14a": 21,
            "26.3": 25,
        }
        for version, expected in cases.items():
            self.assertEqual(ip.required_java_major(version), expected, version)


class ReadInstallerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-read-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_read_installer_ok(self) -> None:
        jar = make_installer_jar(self.tmp / "forge-installer.jar")
        profile = ip.read_installer(jar)
        self.assertEqual(profile.minecraft, GAME_VERSION)
        self.assertEqual(profile.profile_id, PROFILE_ID)
        self.assertEqual(profile.version_json["inheritsFrom"], GAME_VERSION)
        self.assertEqual(profile.jar_path, jar)

    def test_read_installer_missing_file(self) -> None:
        with self.assertRaises(InstallError):
            ip.read_installer(self.tmp / "missing.jar")

    def test_read_installer_bad_zip(self) -> None:
        jar = self.tmp / "bad.jar"
        jar.write_bytes(b"not-a-zip")
        with self.assertRaises(InstallError):
            ip.read_installer(jar)

    def test_read_installer_missing_entries(self) -> None:
        jar = self.tmp / "empty.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr("readme.txt", b"x")
        with self.assertRaises(InstallError):
            ip.read_installer(jar)

    def test_read_installer_bad_json(self) -> None:
        jar = self.tmp / "badjson.jar"
        with zipfile.ZipFile(jar, "w") as archive:
            archive.writestr(ip.PROFILE_ENTRY, b"{not json")
            archive.writestr(ip.VERSION_ENTRY, json.dumps(VERSION_JSON))
        with self.assertRaises(InstallError):
            ip.read_installer(jar)


class LibraryEnumerationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-lib-")
        self.tmp = Path(self._tmp.name)
        self.jar = make_installer_jar(self.tmp / "installer.jar")
        self.profile = ip.read_installer(self.jar)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_library_items(self) -> None:
        items = ip.library_items(self.profile)
        paths = {item.path for item in items}
        self.assertIn("test/libs/alpha/1.0/alpha-1.0.jar", paths)
        self.assertIn("test/libs/beta/2.0/beta-2.0.jar", paths)
        alpha = next(item for item in items if item.path.endswith("alpha-1.0.jar"))
        self.assertEqual(alpha.url, "https://maven.example/alpha-1.0.jar")
        self.assertEqual(alpha.size, 9)

    def test_extra_tool_items_adds_plain_processor_tokens(self) -> None:
        items = ip.library_items(self.profile)
        tools = ip.extra_tool_items(self.profile, {item.path for item in items})
        self.assertEqual([item.path for item in tools], [TOOL_PATH])
        # 已知路径时不重复添加。
        known = {item.path for item in items} | {TOOL_PATH}
        self.assertEqual(ip.extra_tool_items(self.profile, known), [])


class ProcessorHelpersTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-help-")
        self.tmp = Path(self._tmp.name)
        self.jar = make_installer_jar(self.tmp / "installer.jar")
        self.profile = ip.read_installer(self.jar)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_processor_included(self) -> None:
        server = {"sides": ["server"]}
        self.assertFalse(ip.processor_included(server))
        self.assertTrue(ip.processor_included(server, side="server"))
        self.assertTrue(ip.processor_included({"sides": ["client"]}))
        self.assertTrue(ip.processor_included({}))
        self.assertTrue(ip.processor_included({"sides": []}))

    def test_processor_task_name_and_mojmaps(self) -> None:
        profile = self.profile.profile
        self.assertEqual(ip.processor_task_name(profile["processors"][1]), "DOWNLOAD_MOJMAPS")
        self.assertTrue(ip.is_download_mojmaps(profile["processors"][1]))
        self.assertFalse(ip.is_download_mojmaps(profile["processors"][2]))
        self.assertEqual(ip.processor_task_name({"args": ["--task", "MCP_DATA"]}), "MCP_DATA")

    def test_resolve_variables(self) -> None:
        variables = ip.resolve_variables(self.profile, mc_root=self.tmp / "mc")
        self.assertEqual(variables["SIDE"], "client")
        self.assertEqual(variables["ROOT"], str(self.tmp / "mc"))
        self.assertEqual(variables["MINECRAFT_VERSION"], GAME_VERSION)
        self.assertEqual(variables["MCP_VERSION"], "1.20.1-20230612.114412")
        self.assertTrue(variables["MOJMAPS"].endswith("client-1.20.1-20230612.114412-mappings.txt"))
        self.assertTrue(variables["MOJMAPS"].startswith(str(self.tmp / "mc")))
        # lzma 数据从 installer jar 解压到 .cubefetch/installers 下。
        binpatch = Path(variables["BINPATCH"])
        self.assertTrue(binpatch.is_file())
        self.assertEqual(binpatch.read_bytes(), b"LZMA-DATA")
        self.assertIn(".cubefetch", str(binpatch))

    def test_resolve_token(self) -> None:
        variables = {"SIDE": "client", "ROOT": "C:/mc"}
        self.assertEqual(ip.resolve_token("{SIDE}", variables, self.tmp), "client")
        self.assertEqual(ip.resolve_token("{ROOT}", variables, self.tmp), "C:/mc")
        self.assertEqual(
            ip.resolve_token("'literal value'", variables, self.tmp), "literal value"
        )
        self.assertTrue(
            ip.resolve_token(f"[{MOJMAPS_COORD}]", variables, self.tmp).endswith(
                "client-1.20.1-20230612.114412-mappings.txt"
            )
        )

    def test_read_main_class(self) -> None:
        tool_path = self.tmp / "tool.jar"
        tool_path.write_bytes(TOOL_JAR)
        self.assertEqual(ip.read_main_class(tool_path), TOOL_MAIN)
        # 带折叠续行的 MANIFEST。
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(
                "META-INF/MANIFEST.MF",
                b"Manifest-Version: 1.0\r\nMain-Class: com.example.\r\n Tool\r\n\r\n",
            )
        folded = self.tmp / "folded.jar"
        folded.write_bytes(buffer.getvalue())
        self.assertEqual(ip.read_main_class(folded), "com.example.Tool")
        with self.assertRaises(InstallError):
            ip.read_main_class(self.tmp / "missing.jar")

    def test_build_processor_command(self) -> None:
        mc_root = self.tmp / "mc"
        variables = ip.resolve_variables(self.profile, mc_root=mc_root)
        tool_path = mc_root / "libraries" / Path(TOOL_PATH)
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_bytes(TOOL_JAR)
        java = self.tmp / "bin" / "java.exe"
        command, jar_path = ip.build_processor_command(
            self.profile,
            self.profile.profile["processors"][2],
            variables,
            mc_root=mc_root,
            java_exe=java,
        )
        self.assertEqual(command[0], str(java))
        self.assertEqual(command[1], "-cp")
        self.assertEqual(Path(command[2]).name, "installertools-1.3.0.jar")
        self.assertEqual(command[3], TOOL_MAIN)
        self.assertIn("--output", command)
        self.assertIn(str(Path(variables["PATCHED"])), command)
        self.assertEqual(jar_path, tool_path)
        # 工具 jar 不存在时给出中文 InstallError。
        tool_path.unlink()
        with self.assertRaises(InstallError):
            ip.build_processor_command(
                self.profile,
                self.profile.profile["processors"][2],
                variables,
                mc_root=mc_root,
                java_exe=java,
            )

    def test_verify_processor_outputs(self) -> None:
        mc_root = self.tmp / "mc"
        variables = ip.resolve_variables(self.profile, mc_root=mc_root)
        patched = Path(variables["PATCHED"])
        patched.parent.mkdir(parents=True, exist_ok=True)
        patched.write_bytes(b"patched")
        processor = {"outputs": {"{PATCHED}": "{PATCHED}"}}
        checked = ip.verify_processor_outputs(processor, variables, mc_root=mc_root)
        self.assertEqual(checked, [str(patched)])
        # 缺失产物。
        with self.assertRaises(InstallError):
            ip.verify_processor_outputs(
                {"outputs": {"{MC_SLIM}": "{MC_SLIM}"}}, variables, mc_root=mc_root
            )
        # sha1 不符。
        expected = hashlib.sha1(b"other").hexdigest()
        with self.assertRaises(InstallError):
            ip.verify_processor_outputs(
                {"outputs": {"{PATCHED}": expected}}, variables, mc_root=mc_root
            )
        self.assertEqual(ip.verify_processor_outputs({}, variables, mc_root=mc_root), [])


class PreseedTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-preseed-")
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / "mc"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_preseed_downloads_and_skips_existing(self) -> None:
        http = FakeHttp()
        payload = b"library-data"
        items = [
            ip.LibraryItem(
                name="test.libs:alpha:1.0",
                path="test/libs/alpha/1.0/alpha-1.0.jar",
                sha1=hashlib.sha1(payload).hexdigest(),
            )
        ]
        http.default_payload = payload
        report = ip.preseed_libraries(http, self.mc_root, items)
        self.assertEqual(report.skipped, 0)
        self.assertEqual(report.missing, [])
        target = self.mc_root / "libraries" / Path(items[0].path)
        self.assertTrue(target.is_file())
        self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(len(http.downloads), 1)
        # 第二次全部命中 sha1，跳过且不再下载。
        report = ip.preseed_libraries(http, self.mc_root, items)
        self.assertEqual(report.skipped, 1)
        self.assertEqual(len(http.downloads), 1)

    def test_preseed_tolerates_download_failure(self) -> None:
        http = FakeHttp()
        broken = ip.LibraryItem(
            name="test.libs:broken:1.0",
            path="test/libs/broken/1.0/broken-1.0.jar",
            url="https://maven.example/broken-1.0.jar",
        )
        http.fail_urls.add(
            f"https://bmclapi2.bangbang93.com/maven/{broken.path}"
        )
        http.fail_urls.add(broken.url)
        report = ip.preseed_libraries(http, self.mc_root, [broken])
        self.assertEqual(report.missing, [broken.name])
        self.assertFalse(
            (self.mc_root / "libraries" / Path(broken.path)).exists()
        )


class MojmapsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-moj-")
        self.tmp = Path(self._tmp.name)
        self.jar = make_installer_jar(self.tmp / "installer.jar")
        self.profile = ip.read_installer(self.jar)
        self.mc_root = self.tmp / "mc"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_client_mappings_download(self) -> None:
        mappings = ip.client_mappings_download(VANILLA_JSON)
        self.assertEqual(
            mappings["url"], "https://piston-data.mojang.com/v1/objects/client.txt"
        )
        self.assertIsNone(ip.client_mappings_download({}))
        self.assertIsNone(ip.client_mappings_download({"downloads": {}}))

    def test_download_mojmaps_writes_target(self) -> None:
        http = FakeHttp()
        variables = ip.resolve_variables(self.profile, mc_root=self.mc_root)
        mappings = ip.client_mappings_download(VANILLA_JSON)
        progress: list[Progress] = []
        target = ip.download_mojmaps(
            http, variables, mappings, progress_cb=progress.append, label="Forge"
        )
        self.assertTrue(target.is_file())
        self.assertEqual(str(target), variables["MOJMAPS"])
        self.assertEqual(len(http.downloads), 1)
        self.assertTrue(any("混淆映射" in item.message for item in progress))
        # 再次调用：sha1 不匹配假数据，但文件存在时也会重新下载（fake 忽略 sha1），
        # 只断言不抛异常且目标存在。
        ip.download_mojmaps(http, variables, mappings, label="Forge")
        self.assertTrue(target.is_file())

    def test_download_mojmaps_missing_variable(self) -> None:
        http = FakeHttp()
        with self.assertRaises(InstallError):
            ip.download_mojmaps(
                http, {}, ip.client_mappings_download(VANILLA_JSON) or {}
            )


class RunClientProcessorsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-run-")
        self.tmp = Path(self._tmp.name)
        self.jar = make_installer_jar(self.tmp / "installer.jar")
        self.profile = ip.read_installer(self.jar)
        self.mc_root = self.tmp / "mc"
        self.java = self.tmp / "bin" / "java.exe"
        self.java.parent.mkdir(parents=True, exist_ok=True)
        self.java.write_bytes(b"java")
        self.variables = ip.resolve_variables(self.profile, mc_root=self.mc_root)
        tool_path = self.mc_root / "libraries" / Path(TOOL_PATH)
        tool_path.parent.mkdir(parents=True, exist_ok=True)
        tool_path.write_bytes(TOOL_JAR)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_run_client_processors_handles_mojmaps_and_client_processors(self) -> None:
        http = FakeHttp()
        mappings = ip.client_mappings_download(VANILLA_JSON)
        calls: list[list[str]] = []
        progress: list[Progress] = []
        with mock.patch.object(ip, "run_process", fake_run_process(calls)):
            lines = ip.run_client_processors(
                http,
                self.profile,
                self.variables,
                mc_root=self.mc_root,
                java_exe=self.java,
                mappings=mappings,
                progress_cb=progress.append,
                label="Forge",
            )
        # server 侧 processor 被过滤；mojmaps 不执行 java；只剩 2 个 client processor。
        self.assertEqual(len(calls), 2)
        self.assertTrue(all(call[0] == str(self.java) for call in calls))
        self.assertTrue(Path(self.variables["PATCHED"]).is_file())
        self.assertEqual(len(http.downloads), 1)
        self.assertIn("https://piston-data.mojang.com", http.downloads[0][0])
        self.assertTrue(any("混淆映射" in item.message for item in progress))
        self.assertTrue(any("补丁任务" in item.message for item in progress))
        self.assertIsInstance(lines, list)

    def test_run_client_processors_missing_mappings_raises(self) -> None:
        with mock.patch.object(ip, "run_process", fake_run_process()):
            with self.assertRaises(InstallError):
                ip.run_client_processors(
                    FakeHttp(),
                    self.profile,
                    self.variables,
                    mc_root=self.mc_root,
                    java_exe=self.java,
                    mappings=None,
                )

    def test_processor_failure_maps_to_install_error(self) -> None:
        http = FakeHttp()
        mappings = ip.client_mappings_download(VANILLA_JSON)

        def runner(command, cwd=None, timeout=None, env=None):
            return subprocess.CompletedProcess(
                command, 1, stdout="", stderr="Exception in thread main"
            )

        with mock.patch.object(ip, "run_process", runner):
            with self.assertRaises(InstallError) as ctx:
                ip.run_client_processors(
                    http,
                    self.profile,
                    self.variables,
                    mc_root=self.mc_root,
                    java_exe=self.java,
                    mappings=mappings,
                )
        self.assertIn("执行失败", str(ctx.exception.user_message))


class InstallWithProcessorsTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cf-ip-e2e-")
        self.tmp = Path(self._tmp.name)
        self.mc_root = self.tmp / "mc"
        self.jar = make_installer_jar(self.tmp / "forge-installer.jar")
        self.java = self.tmp / "bin" / "java.exe"
        self.java.parent.mkdir(parents=True, exist_ok=True)
        self.java.write_bytes(b"java")

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_install_with_processors_end_to_end(self) -> None:
        http = FakeHttp()
        calls: list[list[str]] = []
        progress: list[Progress] = []
        with mock.patch.object(ip, "run_process", fake_run_process(calls)):
            profile_id, version_json = ip.install_with_processors(
                http,
                self.jar,
                self.mc_root,
                game_version=GAME_VERSION,
                vanilla_version_json=VANILLA_JSON,
                java_exe=self.java,
                label="Forge",
                progress_cb=progress.append,
            )
        self.assertEqual(profile_id, PROFILE_ID)
        self.assertEqual(version_json["id"], PROFILE_ID)
        # 最终版本 JSON。
        version_path = self.mc_root / "versions" / PROFILE_ID / f"{PROFILE_ID}.json"
        self.assertTrue(version_path.is_file())
        data = json.loads(version_path.read_text(encoding="utf-8"))
        self.assertEqual(data["inheritsFrom"], GAME_VERSION)
        # 原版基础文件。
        self.assertTrue(
            (self.mc_root / "versions" / GAME_VERSION / f"{GAME_VERSION}.json").is_file()
        )
        self.assertTrue(
            (self.mc_root / "versions" / GAME_VERSION / f"{GAME_VERSION}.jar").is_file()
        )
        # 预置的依赖库 / 工具 jar / 补丁产物。
        self.assertTrue((self.mc_root / "libraries" / Path(TOOL_PATH)).is_file())
        self.assertTrue(
            (self.mc_root / "libraries" / "test" / "libs" / "alpha" / "1.0" / "alpha-1.0.jar").is_file()
        )
        patched = self.mc_root / "libraries" / Path(
            ip.maven_path(PATCHED_COORD)
        )
        self.assertTrue(patched.is_file())
        self.assertGreater(patched.stat().st_size, 0)
        # data 内的 lzma 已解压。
        self.assertTrue(
            (
                self.mc_root
                / ".cubefetch"
                / "installers"
                / "forge-installer.data"
                / "data"
                / "client.lzma"
            ).is_file()
        )
        self.assertTrue(any("补丁安装完成" in item.message for item in progress))
        self.assertTrue(calls)

    def test_install_with_processors_cancel_raises(self) -> None:
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(CancelledError):
            ip.install_with_processors(
                FakeHttp(),
                self.jar,
                self.mc_root,
                game_version=GAME_VERSION,
                vanilla_version_json=VANILLA_JSON,
                java_exe=self.java,
                cancel=cancel,
            )

    def test_install_with_processors_missing_tool_raises(self) -> None:
        http = FakeHttp()
        http.fail_urls.add(
            f"https://bmclapi2.bangbang93.com/maven/{TOOL_PATH}"
        )
        with self.assertRaises(InstallError):
            ip.install_with_processors(
                http,
                self.jar,
                self.mc_root,
                game_version=GAME_VERSION,
                vanilla_version_json=VANILLA_JSON,
                java_exe=self.java,
            )


if __name__ == "__main__":
    unittest.main()
