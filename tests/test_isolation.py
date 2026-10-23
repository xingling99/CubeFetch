"""``cubefetch.core.isolation`` 单元测试：写入、幂等、保留原键、跳过未安装启动器。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cubefetch.core.isolation import VersionIsolation


class VersionIsolationTestCase(unittest.TestCase):
    VERSION = "1.20.1"

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="cubefetch-isolation-")
        self.tmp = Path(self._tmp.name)
        self.root = self.tmp / "mc"
        self.version_dir = self.root / "versions" / self.VERSION
        self.version_dir.mkdir(parents=True, exist_ok=True)
        (self.version_dir / f"{self.VERSION}.json").write_text(
            json.dumps({"id": self.VERSION}), encoding="utf-8"
        )
        self.pcl_setup = self.version_dir / "PCL" / "Setup.ini"
        self.hmcl_instance = self.version_dir / "instance-game-settings.json"
        self.hmcl_legacy = self.version_dir / "hmclversion.cfg"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- PCL --------------------------------------------------------------

    def test_pcl_write_enable_preserves_unknown_keys(self) -> None:
        self.pcl_setup.parent.mkdir(parents=True, exist_ok=True)
        original = "VersionVanilla:1.20.1\r\nUnknownKey:keep-me\r\nVersionArgumentIndieV2:False\r\n"
        # 用字节写入，避免 Windows 文本模式把 \r\n 再转换成 \r\r\n
        self.pcl_setup.write_bytes(original.encode("utf-8"))

        notes = VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        self.assertTrue(notes)
        self.assertTrue(any("启用" in note for note in notes))

        text = self.pcl_setup.read_text(encoding="utf-8")
        self.assertIn("VersionArgumentIndieV2:True", text)
        self.assertIn("VersionArgumentIndie:1", text)
        # 未知键与原有键必须保留
        self.assertIn("UnknownKey:keep-me", text)
        self.assertIn("VersionVanilla:1.20.1", text)
        # 原文件已备份
        backup = self.pcl_setup.with_name("Setup.ini.bak")
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), original.encode("utf-8"))
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))

    def test_pcl_second_apply_is_idempotent(self) -> None:
        VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        first = self.pcl_setup.read_text(encoding="utf-8")
        backup = self.pcl_setup.with_name("Setup.ini.bak")
        backup_text = backup.read_text(encoding="utf-8") if backup.is_file() else None

        notes = VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        self.assertEqual(self.pcl_setup.read_text(encoding="utf-8"), first)
        self.assertTrue(any("无需修改" in note for note in notes))
        if backup_text is not None:
            self.assertEqual(backup.read_text(encoding="utf-8"), backup_text)

    def test_pcl_disable_and_toggle_back(self) -> None:
        VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        backup = self.pcl_setup.with_name("Setup.ini.bak")
        original_backup = backup.read_text(encoding="utf-8") if backup.is_file() else None

        VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=False)
        text = self.pcl_setup.read_text(encoding="utf-8")
        self.assertIn("VersionArgumentIndieV2:False", text)
        self.assertIn("VersionArgumentIndie:2", text)
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))
        # 备份只做一次，不被后续写入覆盖
        if original_backup is not None:
            self.assertEqual(backup.read_text(encoding="utf-8"), original_backup)

    def test_pcl_creates_setup_ini_when_missing(self) -> None:
        VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        self.assertTrue(self.pcl_setup.is_file())
        self.assertFalse(self.pcl_setup.with_name("Setup.ini.bak").is_file())
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))

    def test_pcl_legacy_key_only(self) -> None:
        self.pcl_setup.parent.mkdir(parents=True, exist_ok=True)
        self.pcl_setup.write_text("VersionArgumentIndie:1\r\n", encoding="utf-8")
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))
        self.pcl_setup.write_text("VersionArgumentIndie:2\r\n", encoding="utf-8")
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))
        # 新键存在时优先于旧键
        self.pcl_setup.write_text(
            "VersionArgumentIndieV2:False\r\nVersionArgumentIndie:1\r\n", encoding="utf-8"
        )
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))

    # -- HMCL -------------------------------------------------------------

    def test_hmcl_writes_modern_and_legacy_preserving_keys(self) -> None:
        self.hmcl_instance.write_text(
            json.dumps(
                {
                    "$schema": "https://schemas.glavo.site/hmcl/instance-game-settings/1.0.1",
                    "parent": "my-preset",
                    "javaType": "java21",
                    "runningDirectory": "",
                    "defaultIsolationType": "MODDED",
                }
            ),
            encoding="utf-8",
        )
        self.hmcl_legacy.write_text(
            json.dumps({"gameDirType": 0, "gameDir": "", "other": "keep"}), encoding="utf-8"
        )

        notes = VersionIsolation.apply(self.root, self.VERSION, ["hmcl"], enabled=True)
        self.assertTrue(notes)
        modern = json.loads(self.hmcl_instance.read_text(encoding="utf-8"))
        self.assertEqual(modern["defaultIsolationType"], "ALWAYS")
        self.assertEqual(modern["runningDirectory"], "")
        self.assertEqual(modern["parent"], "my-preset")
        self.assertEqual(modern["javaType"], "java21")
        legacy = json.loads(self.hmcl_legacy.read_text(encoding="utf-8"))
        self.assertEqual(legacy["gameDirType"], 1)
        self.assertEqual(legacy["other"], "keep")
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["hmcl"]))
        self.assertTrue(self.hmcl_instance.with_name("instance-game-settings.json.bak").is_file())
        self.assertTrue(self.hmcl_legacy.with_name("hmclversion.cfg.bak").is_file())

        VersionIsolation.apply(self.root, self.VERSION, ["hmcl"], enabled=False)
        modern = json.loads(self.hmcl_instance.read_text(encoding="utf-8"))
        legacy = json.loads(self.hmcl_legacy.read_text(encoding="utf-8"))
        self.assertEqual(modern["defaultIsolationType"], "NEVER")
        self.assertEqual(legacy["gameDirType"], 0)
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["hmcl"]))

    def test_hmcl_custom_running_directory_is_preserved(self) -> None:
        custom = str(self.tmp / "custom-game-dir")
        self.hmcl_instance.write_text(
            json.dumps({"runningDirectory": custom, "defaultIsolationType": "MODDED"}),
            encoding="utf-8",
        )
        notes = VersionIsolation.apply(self.root, self.VERSION, ["hmcl"], enabled=True)
        self.assertTrue(any("自定义运行目录" in note for note in notes))
        data = json.loads(self.hmcl_instance.read_text(encoding="utf-8"))
        self.assertEqual(data["runningDirectory"], custom)
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["hmcl"]))

    def test_hmcl_corrupt_files_are_skipped_not_overwritten(self) -> None:
        self.hmcl_instance.write_text("not a json object", encoding="utf-8")
        self.hmcl_legacy.write_text("also broken", encoding="utf-8")
        notes = VersionIsolation.apply(self.root, self.VERSION, ["hmcl"], enabled=True)
        self.assertTrue(notes)
        self.assertEqual(self.hmcl_instance.read_text(encoding="utf-8"), "not a json object")
        self.assertEqual(self.hmcl_legacy.read_text(encoding="utf-8"), "also broken")
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["hmcl"]))

    # -- 通用规则 ----------------------------------------------------------

    def test_both_launchers_and_shared_isolation(self) -> None:
        notes = VersionIsolation.apply(self.root, self.VERSION, ["pcl", "hmcl"], enabled=True)
        self.assertTrue(self.pcl_setup.is_file())
        self.assertTrue(self.hmcl_instance.is_file())
        self.assertTrue(self.hmcl_legacy.is_file())
        self.assertTrue(any("PCL2" in note for note in notes))
        self.assertTrue(any("HMCL" in note for note in notes))
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl", "hmcl"]))
        # 其它启动器没配置时，只要有一个隔离就算隔离
        self.assertTrue(VersionIsolation.is_isolated(self.root, self.VERSION, ["unknown", "pcl"]))

    def test_invalid_version_and_missing_directory_are_skipped(self) -> None:
        self.assertTrue(VersionIsolation.apply(self.root, "..", ["pcl"]))
        self.assertEqual(VersionIsolation.apply(self.root, "..", ["pcl"])[0].count("版本号无效"), 1)
        missing = "9.9.9"
        self.assertTrue(any("版本目录不存在" in note for note in VersionIsolation.apply(self.root, missing, ["pcl"])))
        self.assertFalse(VersionIsolation.is_isolated(self.root, missing, ["pcl"]))
        self.assertFalse(VersionIsolation.is_isolated(self.root, "", ["pcl"]))
        self.assertFalse(VersionIsolation.is_isolated(self.root, "../x", ["hmcl"]))

    def test_unknown_and_empty_kinds(self) -> None:
        notes = VersionIsolation.apply(self.root, self.VERSION, ["optifine"], enabled=True)
        self.assertTrue(any("未知启动器" in note for note in notes))
        notes = VersionIsolation.apply(self.root, self.VERSION, [], enabled=True)
        self.assertTrue(any("未检测到" in note for note in notes))
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, []))

    def test_version_removed_after_write_is_not_isolated(self) -> None:
        VersionIsolation.apply(self.root, self.VERSION, ["pcl"], enabled=True)
        import shutil

        shutil.rmtree(self.version_dir)
        self.assertFalse(VersionIsolation.is_isolated(self.root, self.VERSION, ["pcl"]))


if __name__ == "__main__":
    unittest.main()
