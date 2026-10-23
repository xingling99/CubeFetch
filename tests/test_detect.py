"""``cubefetch.core.detect`` 单元测试：启动器探测规则（离线、临时目录）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cubefetch.core.detect import (
    DetectionResult,
    LauncherDetector,
    LauncherProbe,
    _expand_pcl_path,
    _parse_pcl_folders,
    _read_pcl_ini,
)


def make_minecraft_root(base: Path, versions: tuple[str, ...] = ("1.20.1",)) -> Path:
    """在 ``base/.minecraft`` 下造一个含若干已安装版本的 MC 根目录。"""
    root = base / ".minecraft"
    for version in versions:
        version_dir = root / "versions" / version
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / f"{version}.json").write_text(
            json.dumps({"id": version, "type": "release"}), encoding="utf-8"
        )
    return root


def write_pcl_setup(launcher_dir: Path, lines: list[str]) -> Path:
    setup = launcher_dir / "PCL" / "Setup.ini"
    setup.parent.mkdir(parents=True, exist_ok=True)
    setup.write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
    return setup


def write_hmcl_modern_config(home: Path, root: Path) -> None:
    config_dir = home / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "game-directories.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.glavo.site/hmcl/game-directories/1.0.1",
                "gameDirectories": [
                    {"id": "3f2a0c9e-1b2d-4e5f-9a8b-7c6d5e4f3a2b", "path": str(root), "name": "MC"}
                ],
            }
        ),
        encoding="utf-8",
    )
    (config_dir / "launcher-settings.json").write_text(
        json.dumps(
            {
                "$schema": "https://schemas.glavo.site/hmcl/launcher-settings/1.0.0",
                "selectedGameDirectory": "3f2a0c9e-1b2d-4e5f-9a8b-7c6d5e4f3a2b",
            }
        ),
        encoding="utf-8",
    )


class DetectTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cubefetch-detect-")
        self.tmp = Path(self._tmp.name)
        self.appdata = self.tmp / "Roaming"
        self.appdata.mkdir(parents=True, exist_ok=True)
        self.user_home = self.tmp / "home"
        self.user_home.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def detector(self, **kwargs) -> LauncherDetector:
        kwargs.setdefault("appdata", self.appdata)
        kwargs.setdefault("user_home", self.user_home)
        kwargs.setdefault("scan_roots", [])
        kwargs.setdefault("use_registry", False)
        return LauncherDetector(**kwargs)

    # -- 无启动器 ----------------------------------------------------------

    def test_no_launcher_uses_appdata_minecraft(self) -> None:
        result = self.detector().detect()
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual([probe.kind for probe in result.probes], ["pcl", "hmcl"])
        self.assertFalse(any(probe.installed for probe in result.probes))
        self.assertFalse(result.needs_choice)
        self.assertEqual(result.default_root, self.appdata / ".minecraft")
        # 目录不存在时没有候选（UI 会提示手动选择）
        self.assertEqual(result.candidate_roots, [])
        self.assertIn("未检测到", result.reason)

    def test_no_launcher_existing_appdata_minecraft_is_candidate(self) -> None:
        make_minecraft_root(self.appdata)
        result = self.detector().detect()
        self.assertEqual(result.default_root, self.appdata / ".minecraft")
        self.assertEqual(result.candidate_roots, [self.appdata / ".minecraft"])

    # -- 单启动器 ----------------------------------------------------------

    def test_single_pcl_uses_its_root(self) -> None:
        launcher = self.tmp / "PCL2"
        root = make_minecraft_root(launcher, ("1.20.1", "1.20.2"))
        write_pcl_setup(launcher, ["LaunchFolderSelect:$.minecraft\\"])

        result = self.detector(scan_roots=[launcher]).detect()
        pcl, hmcl = result.probes
        self.assertEqual(pcl.kind, "pcl")
        self.assertTrue(pcl.installed)
        self.assertEqual(pcl.mc_root, root)
        self.assertEqual(pcl.managed_versions, 2)
        self.assertEqual(pcl.config_path, launcher / "PCL" / "Setup.ini")
        self.assertFalse(hmcl.installed)
        self.assertFalse(result.needs_choice)
        self.assertEqual(result.default_root, root)
        self.assertIn(root, result.candidate_roots)
        self.assertIn("PCL2", result.reason)

    def test_single_hmcl_modern_config(self) -> None:
        root = make_minecraft_root(self.tmp / "hmclmc", ("1.20.1",))
        hmcl_home = self.appdata / "hmcl"
        write_hmcl_modern_config(hmcl_home, root)

        result = self.detector().detect()
        pcl, hmcl = result.probes
        self.assertTrue(hmcl.installed)
        self.assertEqual(hmcl.mc_root, root)
        self.assertFalse(pcl.installed)
        self.assertFalse(result.needs_choice)
        self.assertEqual(result.default_root, root)

    def test_single_hmcl_legacy_hmcl_json(self) -> None:
        root = make_minecraft_root(self.tmp / "hmcllegacy")
        hmcl_home = self.appdata / "hmcl"
        hmcl_home.mkdir(parents=True, exist_ok=True)
        (hmcl_home / "config.json").write_text(
            json.dumps(
                {
                    "configurations": {"Main": {"gameDir": str(root)}},
                    "selectedProfile": "Main",
                }
            ),
            encoding="utf-8",
        )
        result = self.detector().detect()
        hmcl = next(probe for probe in result.probes if probe.kind == "hmcl")
        self.assertTrue(hmcl.installed)
        self.assertEqual(hmcl.mc_root, root)

    # -- 双启动器 ----------------------------------------------------------

    def test_two_launchers_require_choice(self) -> None:
        pcl_launcher = self.tmp / "PCL2"
        pcl_root = make_minecraft_root(pcl_launcher, ("1.20.1",))
        write_pcl_setup(pcl_launcher, ["LaunchFolderSelect:$.minecraft\\"])

        hmcl_root = make_minecraft_root(self.tmp / "hmclmc", ("1.19.2", "1.20.1"))
        write_hmcl_modern_config(self.appdata / "hmcl", hmcl_root)

        result = self.detector(scan_roots=[pcl_launcher]).detect()
        self.assertTrue(all(probe.installed for probe in result.probes))
        self.assertTrue(result.needs_choice)
        self.assertIn("两个启动器", result.reason)
        self.assertIn(pcl_root, result.candidate_roots)
        self.assertIn(hmcl_root, result.candidate_roots)
        self.assertIn(result.default_root, result.candidate_roots)

    # -- PCLCE / LaunchFolders --------------------------------------------

    def test_pclce_config_launch_folders(self) -> None:
        root = make_minecraft_root(self.tmp / "pclcemc", ("1.20.1",))
        pclce = self.appdata / "PCLCE"
        pclce.mkdir(parents=True, exist_ok=True)
        (pclce / "config.v1.json").write_text(
            json.dumps({"FileVersion": 1, "LaunchFolders": f".minecraft>{root}|other>{root}"}),
            encoding="utf-8",
        )
        result = self.detector().detect()
        pcl = next(probe for probe in result.probes if probe.kind == "pcl")
        self.assertTrue(pcl.installed)
        self.assertEqual(pcl.mc_root, root)
        self.assertIn("社区版", result.reason)

    def test_pcl_launch_folders_prefers_root_with_most_versions(self) -> None:
        launcher = self.tmp / "PCL2"
        root_a = make_minecraft_root(self.tmp / "a", ("1.20.1",))
        root_b = make_minecraft_root(self.tmp / "b", ("1.19.2", "1.20.1", "1.20.2"))
        write_pcl_setup(
            launcher,
            ["LaunchFolders:a>" + str(root_a) + "|b>" + str(root_b)],
        )
        result = self.detector(scan_roots=[launcher]).detect()
        pcl = next(probe for probe in result.probes if probe.kind == "pcl")
        self.assertEqual(pcl.mc_root, root_b)
        self.assertEqual(pcl.managed_versions, 3)

    def test_pcl_ce_counts_as_pcl_not_second_launcher(self) -> None:
        # PCL2 与 PCLCE 同时存在时仍算一个启动器（kind 都是 pcl），不应要求选择。
        root = make_minecraft_root(self.tmp / "pclmc", ("1.20.1",))
        write_pcl_setup(self.tmp / "PCL2", ["LaunchFolderSelect:" + str(root)])
        pclce = self.appdata / "PCLCE"
        pclce.mkdir(parents=True, exist_ok=True)
        (pclce / "config.v1.json").write_text(
            json.dumps({"LaunchFolders": f".minecraft>{root}"}), encoding="utf-8"
        )
        result = self.detector(scan_roots=[self.tmp / "PCL2"]).detect()
        self.assertEqual(sum(1 for probe in result.probes if probe.installed), 1)
        self.assertFalse(result.needs_choice)

    # -- 健壮性 ------------------------------------------------------------

    def test_corrupt_configs_do_not_crash(self) -> None:
        (self.appdata / "PCL").mkdir(parents=True, exist_ok=True)
        (self.appdata / "PCL" / "config.json").write_text("{not json", encoding="utf-8")
        (self.appdata / "hmcl").mkdir(parents=True, exist_ok=True)
        (self.appdata / "hmcl" / "config.json").write_text("also broken", encoding="utf-8")
        result = self.detector().detect()  # 不应抛异常
        self.assertEqual(len(result.probes), 2)
        self.assertIsInstance(result.default_root, Path)

    def test_managed_versions_only_counts_json_dirs(self) -> None:
        root = make_minecraft_root(self.tmp / "mc", ("1.20.1",))
        (root / "versions" / "no-json").mkdir()
        (root / "versions" / "stray.json").write_text("{}", encoding="utf-8")
        launcher = self.tmp / "PCL2"
        write_pcl_setup(launcher, ["LaunchFolderSelect:" + str(root)])
        result = self.detector(scan_roots=[launcher]).detect()
        pcl = next(probe for probe in result.probes if probe.kind == "pcl")
        self.assertEqual(pcl.managed_versions, 1)

    def test_default_detector_does_not_crash(self) -> None:
        # 不传 appdata/扫描根目录时读取真实环境；只要求不崩且结构正确。
        detector = LauncherDetector(scan_roots=[], use_registry=False)
        result = detector.detect()
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(len(result.probes), 2)
        self.assertIsInstance(result.needs_choice, bool)
        self.assertIsInstance(result.default_root, Path)

    # -- 模块级辅助函数 ----------------------------------------------------

    def test_read_pcl_ini_key_value_parsing(self) -> None:
        ini = self.tmp / "Setup.ini"
        ini.write_text("LaunchFolderSelect:first\r\nOther:value:with:colons\r\nOther:second\r\n", encoding="utf-8")
        parsed = _read_pcl_ini(ini)
        self.assertEqual(parsed["LaunchFolderSelect"], "first")
        self.assertEqual(parsed["Other"], "second")  # 重复键后者覆盖

    def test_expand_pcl_path_and_launch_folders(self) -> None:
        launcher = self.tmp / "PCL2"
        expanded = _expand_pcl_path("$.minecraft\\", launcher)
        self.assertEqual(expanded, launcher / ".minecraft")
        roots = _parse_pcl_folders(f"a>{self.tmp / 'one'}|b>{self.tmp / 'two'}", launcher)
        self.assertEqual(roots, [self.tmp / "one", self.tmp / "two"])
        self.assertEqual(_parse_pcl_folders(123, launcher), [])

    def test_probe_dataclass_defaults(self) -> None:
        probe = LauncherProbe(kind="pcl", installed=False, exe_hint=None, config_path=None, mc_root=None)
        self.assertEqual(probe.managed_versions, 0)

    # -- 安装根扫描（有效 versions/<id>/<id>.json）-------------------------

    def test_scan_finds_root_without_launcher_config(self) -> None:
        # 模拟用户反馈：只有 D:\pcl2\.minecraft\versions 文件，没有任何启动器配置。
        disk = self.tmp / "disk"
        root = make_minecraft_root(disk / "pcl2", ("1.20.1", "1.20.2"))
        result = self.detector(scan_roots=[disk]).detect()
        self.assertFalse(any(probe.installed for probe in result.probes))
        self.assertFalse(result.needs_choice)
        self.assertEqual(result.root_versions, [(root, 2)])
        self.assertEqual(result.default_root, root)
        self.assertIn("扫描", result.reason)

    def test_launcher_minecraft_dir_without_config_is_detected(self) -> None:
        # 启动器目录里的 .minecraft 即使配置丢失也能被扫描到。
        launcher = self.tmp / "PCL2"
        root = make_minecraft_root(launcher, ("1.19.2",))
        result = self.detector(scan_roots=[launcher]).detect()
        self.assertFalse(result.probes[0].installed)  # 没有 exe / Setup.ini，不算 PCL 已安装
        self.assertEqual(result.root_versions, [(root, 1)])
        self.assertEqual(result.default_root, root)

    def test_root_versions_sorted_by_count_desc(self) -> None:
        disk = self.tmp / "disk"
        root_a = make_minecraft_root(disk / "a", ("1.20.1",))
        root_b = make_minecraft_root(disk / "b", ("1.19.2", "1.20.1", "1.20.2"))
        result = self.detector(scan_roots=[disk]).detect()
        self.assertEqual(result.root_versions, [(root_b, 3), (root_a, 1)])
        self.assertEqual(result.default_root, root_b)
        # 有效安装根在候选里排最前（版本多的优先）
        self.assertEqual(result.candidate_roots[:2], [root_b, root_a])

    def test_root_version_counts_property(self) -> None:
        root = make_minecraft_root(self.tmp / "mc", ("1.20.1", "1.20.2"))
        result = self.detector(scan_roots=[self.tmp]).detect()
        self.assertEqual(result.root_version_counts, {str(root): 2})

    def test_empty_minecraft_is_not_a_valid_install_root(self) -> None:
        launcher = self.tmp / "PCL2"
        (launcher / ".minecraft").mkdir(parents=True)  # 空目录：没有 versions/<id>/<id>.json
        write_pcl_setup(launcher, ["LaunchFolderSelect:$.minecraft\\"])
        result = self.detector(scan_roots=[launcher]).detect()
        pcl = result.probes[0]
        self.assertTrue(pcl.installed)
        self.assertEqual(pcl.managed_versions, 0)
        self.assertEqual(result.root_versions, [])
        # 没有有效安装根时回退到 %APPDATA%\.minecraft，而不是空目录
        self.assertEqual(result.default_root, self.appdata / ".minecraft")

    def test_single_launcher_invalid_root_falls_back_to_scanned_root(self) -> None:
        launcher = self.tmp / "PCL2"
        (launcher / ".minecraft").mkdir(parents=True)  # 配置指向的目录没有有效版本
        write_pcl_setup(launcher, ["LaunchFolderSelect:$.minecraft\\"])
        games = self.tmp / "games"
        scanned = make_minecraft_root(games / "mc", ("1.20.1", "1.20.2"))
        result = self.detector(scan_roots=[launcher, games]).detect()
        self.assertTrue(result.probes[0].installed)
        self.assertEqual(result.default_root, scanned)
        self.assertEqual(result.root_versions, [(scanned, 2)])

    def test_two_launchers_default_prefers_most_versions(self) -> None:
        pcl_launcher = self.tmp / "PCL2"
        pcl_root = make_minecraft_root(pcl_launcher, ("1.20.1",))
        write_pcl_setup(pcl_launcher, ["LaunchFolderSelect:$.minecraft\\"])
        hmcl_root = make_minecraft_root(self.tmp / "hmclmc", ("1.19.2", "1.20.1"))
        write_hmcl_modern_config(self.appdata / "hmcl", hmcl_root)
        result = self.detector(scan_roots=[pcl_launcher]).detect()
        self.assertTrue(result.needs_choice)
        self.assertEqual(result.root_versions[0], (hmcl_root, 2))
        self.assertEqual(result.default_root, hmcl_root)

    def test_scan_roots_injection_does_not_escape(self) -> None:
        outside = make_minecraft_root(self.tmp / "outside", ("1.20.1",))
        scan = self.tmp / "scan"
        root = make_minecraft_root(scan / "mc", ("1.19.2",))
        result = self.detector(scan_roots=[scan]).detect()
        self.assertEqual(result.root_versions, [(root, 1)])
        self.assertNotIn(outside, [path for path, _ in result.root_versions])

    def test_scan_skips_system_directories(self) -> None:
        disk = self.tmp / "disk"
        make_minecraft_root(disk / "Windows", ("1.20.1",))
        result = self.detector(scan_roots=[disk]).detect()
        self.assertEqual(result.root_versions, [])

    def test_redetect_picks_up_new_roots(self) -> None:
        detector = self.detector(scan_roots=[self.tmp / "disk"])
        self.assertEqual(detector.detect().root_versions, [])
        root = make_minecraft_root(self.tmp / "disk" / "new", ("1.20.1",))
        result = detector.detect()  # 同一实例重新检测：不能命中旧的扫描缓存
        self.assertEqual(result.root_versions, [(root, 1)])


if __name__ == "__main__":
    unittest.main()
