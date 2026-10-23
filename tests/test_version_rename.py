"""tests for cubefetch.core.version_rename（离线，不联网）。"""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from cubefetch.core.errors import InstallError
from cubefetch.core.version_rename import (
    MAX_VERSION_NAME_LENGTH,
    existing_version,
    rename_installed_version,
    validate_version_name,
)


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class ValidateVersionNameTest(unittest.TestCase):
    def test_accepts_simple_name(self) -> None:
        self.assertEqual(validate_version_name("1.20.1-forge-47.2.0"), "1.20.1-forge-47.2.0")

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        self.assertEqual(validate_version_name("  my pack  "), "my pack")
        self.assertEqual(validate_version_name("\t整合包\n"), "整合包")

    def test_accepts_unicode_and_inner_spaces(self) -> None:
        self.assertEqual(
            validate_version_name("1.20.1 · 生存整合 v2"), "1.20.1 · 生存整合 v2"
        )

    def test_accepts_max_length(self) -> None:
        name = "a" * MAX_VERSION_NAME_LENGTH
        self.assertEqual(validate_version_name(name), name)

    def test_rejects_empty_and_whitespace(self) -> None:
        for value in ("", "   ", "\t\n"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_version_name(value)

    def test_rejects_non_string(self) -> None:
        for value in (None, 123, b"abc"):  # type: ignore[arg-type]
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_version_name(value)  # type: ignore[arg-type]

    def test_rejects_reserved_names(self) -> None:
        for value in (".", ".."):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_version_name(value)

    def test_rejects_invalid_characters(self) -> None:
        for char in '\\/:*?"<>|':
            with self.subTest(char=char):
                with self.assertRaises(ValueError) as ctx:
                    validate_version_name(f"name{char}x")
                self.assertIn("不能包含", ctx.exception.args[0])

    def test_rejects_control_characters(self) -> None:
        for value in ("abc\x00", "abc\x01", "a\nb", "a\tb", "a\x7fb"):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_version_name(value)

    def test_rejects_trailing_dot(self) -> None:
        for value in ("name.", "name. ", "  name.  "):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    validate_version_name(value)

    def test_rejects_too_long(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            validate_version_name("a" * (MAX_VERSION_NAME_LENGTH + 1))
        self.assertIn("过长", ctx.exception.args[0])


class RenameInstalledVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / ".minecraft"
        self.versions = self.root / "versions"

    def make_version(
        self,
        version_id: str,
        *,
        payload: dict | None = None,
        raw_text: str | None = None,
    ) -> Path:
        version_dir = self.versions / version_id
        version_dir.mkdir(parents=True, exist_ok=True)
        json_path = version_dir / f"{version_id}.json"
        if raw_text is not None:
            json_path.write_text(raw_text, encoding="utf-8")
        else:
            write_json(json_path, payload if payload is not None else {"id": version_id})
        return version_dir

    def read_version_json(self, version_id: str) -> dict:
        path = self.versions / version_id / f"{version_id}.json"
        return json.loads(path.read_text(encoding="utf-8"))

    # -- 正常重命名 --------------------------------------------------------

    def test_renames_directory_and_json_id(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(
            old,
            payload={
                "id": old,
                "inheritsFrom": "1.20.1",
                "mainClass": "cpw.mods.bootstraplauncher.BootstrapLauncher",
                "libraries": [{"name": "x"}],
            },
        )
        result = rename_installed_version(self.root, old, " 我的整合包 ")

        self.assertEqual(result, "我的整合包")
        self.assertFalse((self.versions / old).exists())
        new_dir = self.versions / "我的整合包"
        self.assertTrue(new_dir.is_dir())
        new_json = new_dir / "我的整合包.json"
        self.assertTrue(new_json.is_file())
        self.assertFalse((new_dir / f"{old}.json").exists())
        data = json.loads(new_json.read_text(encoding="utf-8"))
        self.assertEqual(data["id"], "我的整合包")
        self.assertEqual(data["inheritsFrom"], "1.20.1")
        self.assertEqual(data["mainClass"], "cpw.mods.bootstraplauncher.BootstrapLauncher")
        self.assertEqual(data["libraries"], [{"name": "x"}])

    def test_updates_inherits_from_references(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": old, "inheritsFrom": "1.20.1"})
        self.make_version("1.20.1-child", payload={"id": "1.20.1-child", "inheritsFrom": old})
        self.make_version("1.20.1-child2", payload={"id": "1.20.1-child2", "inheritsFrom": old})

        rename_installed_version(self.root, old, "forge-pack")

        self.assertEqual(self.read_version_json("1.20.1-child")["inheritsFrom"], "forge-pack")
        self.assertEqual(self.read_version_json("1.20.1-child2")["inheritsFrom"], "forge-pack")

    def test_unrelated_inherits_from_is_untouched(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": old, "inheritsFrom": "1.20.1"})
        self.make_version("other", payload={"id": "other", "inheritsFrom": "1.20.1"})

        rename_installed_version(self.root, old, "renamed")

        self.assertEqual(self.read_version_json("other")["inheritsFrom"], "1.20.1")

    def test_broken_sibling_json_is_skipped(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": old, "inheritsFrom": "1.20.1"})
        self.make_version("broken", raw_text="{not json")
        self.make_version("child", payload={"id": "child", "inheritsFrom": old})

        result = rename_installed_version(self.root, old, "renamed")

        self.assertEqual(result, "renamed")
        self.assertEqual((self.versions / "broken" / "broken.json").read_text(), "{not json")
        self.assertEqual(self.read_version_json("child")["inheritsFrom"], "renamed")

    def test_isolation_files_are_preserved(self) -> None:
        old = "1.20.1-forge-47.2.0"
        version_dir = self.make_version(old, payload={"id": old, "inheritsFrom": "1.20.1"})
        pcl_ini = version_dir / "PCL" / "Setup.ini"
        pcl_ini.parent.mkdir(parents=True, exist_ok=True)
        pcl_ini.write_bytes(b"VersionArgumentIndieV2:True\r\n")
        hmcl_cfg = version_dir / "instance-game-settings.json"
        hmcl_cfg.write_text('{"runningDirectory": "", "defaultIsolationType": "ALWAYS"}', encoding="utf-8")

        rename_installed_version(self.root, old, "renamed")

        new_dir = self.versions / "renamed"
        self.assertEqual(
            (new_dir / "PCL" / "Setup.ini").read_bytes(),
            b"VersionArgumentIndieV2:True\r\n",
        )
        self.assertEqual(
            (new_dir / "instance-game-settings.json").read_text(encoding="utf-8"),
            '{"runningDirectory": "", "defaultIsolationType": "ALWAYS"}',
        )

    def test_same_name_is_noop(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": "stale", "inheritsFrom": "1.20.1"})

        self.assertEqual(rename_installed_version(self.root, old, old), old)
        self.assertTrue((self.versions / old).is_dir())
        self.assertEqual(self.read_version_json(old)["id"], old)

    def test_accepts_string_mc_root(self) -> None:
        old = "1.20.1-fabric-0.15.7"
        self.make_version(old, payload={"id": old, "inheritsFrom": "1.20.1"})
        self.assertEqual(
            rename_installed_version(str(self.root), old, "fabric-pack"),  # type: ignore[arg-type]
            "fabric-pack",
        )

    # -- 错误分支 ----------------------------------------------------------

    def test_invalid_new_name_raises_valueerror_and_keeps_old(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": old})
        for new_name in ("", "a/b", "name.", "a" * (MAX_VERSION_NAME_LENGTH + 1)):
            with self.subTest(new_name=new_name):
                with self.assertRaises(ValueError):
                    rename_installed_version(self.root, old, new_name)
                self.assertTrue((self.versions / old).is_dir())

    def test_missing_old_version_raises_install_error(self) -> None:
        with self.assertRaises(InstallError) as ctx:
            rename_installed_version(self.root, "not-installed", "new-name")
        self.assertIn("找不到", ctx.exception.user_message)

    def test_target_already_exists_raises_install_error(self) -> None:
        old = "1.20.1-forge-47.2.0"
        self.make_version(old, payload={"id": old})
        self.make_version("taken", payload={"id": "taken"})

        with self.assertRaises(InstallError) as ctx:
            rename_installed_version(self.root, old, "taken")
        self.assertIn("已存在", ctx.exception.user_message)
        self.assertTrue((self.versions / old).is_dir())
        self.assertTrue((self.versions / "taken").is_dir())

    def test_missing_version_json_raises_and_does_not_move(self) -> None:
        old = "broken-dir"
        (self.versions / old).mkdir(parents=True)
        with self.assertRaises(InstallError) as ctx:
            rename_installed_version(self.root, old, "new-name")
        self.assertIn("缺少版本信息文件", ctx.exception.user_message)
        self.assertTrue((self.versions / old).is_dir())

    def test_corrupt_version_json_raises_and_does_not_move(self) -> None:
        old = "bad-json"
        self.make_version(old, raw_text="{oops")
        with self.assertRaises(InstallError) as ctx:
            rename_installed_version(self.root, old, "new-name")
        self.assertIn("无法解析", ctx.exception.user_message)
        self.assertTrue((self.versions / old).is_dir())

    def test_unsafe_old_id_raises_install_error(self) -> None:
        for old in ("../outside", "a/b", "a\\b", "", "   "):
            with self.subTest(old=old):
                with self.assertRaises(InstallError):
                    rename_installed_version(self.root, old, "new-name")


class ExistingVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name) / ".minecraft"
        self.versions = self.root / "versions"

    def install_fake(self, version_id: str) -> None:
        write_json(self.versions / version_id / f"{version_id}.json", {"id": version_id})

    def test_vanilla(self) -> None:
        self.install_fake("1.20.1")
        self.assertEqual(existing_version(self.root, "vanilla", "1.20.1", None), "1.20.1")
        self.assertIsNone(existing_version(self.root, "vanilla", "1.21.1", None))

    def test_fabric_and_quilt(self) -> None:
        self.install_fake("1.20.1-fabric-0.15.7")
        self.install_fake("1.20.1-quilt-0.30.1")
        self.assertEqual(
            existing_version(self.root, "fabric", "1.20.1", "0.15.7"), "1.20.1-fabric-0.15.7"
        )
        self.assertEqual(
            existing_version(self.root, "quilt", "1.20.1", "0.30.1"), "1.20.1-quilt-0.30.1"
        )
        self.assertIsNone(existing_version(self.root, "fabric", "1.20.1", "0.16.0"))

    def test_forge_predicted_id(self) -> None:
        self.install_fake("1.20.1-forge-47.2.0")
        self.assertEqual(
            existing_version(self.root, "forge", "1.20.1", "47.2.0"), "1.20.1-forge-47.2.0"
        )
        self.assertIsNone(existing_version(self.root, "forge", "1.20.1", "47.3.0"))

    def test_neoforge_prefers_predicted_id(self) -> None:
        self.install_fake("1.21.1-neoforge-21.1.80")
        self.install_fake("neoforge-21.1.80")
        self.assertEqual(
            existing_version(self.root, "neoforge", "1.21.1", "21.1.80"),
            "1.21.1-neoforge-21.1.80",
        )

    def test_neoforge_falls_back_to_installer_id(self) -> None:
        self.install_fake("neoforge-21.1.80")
        self.assertEqual(
            existing_version(self.root, "neoforge", "1.21.1", "21.1.80"), "neoforge-21.1.80"
        )

    def test_neoforge_alias(self) -> None:
        self.install_fake("neoforge-21.1.80")
        self.assertEqual(
            existing_version(self.root, "neo", "1.21.1", "21.1.80"), "neoforge-21.1.80"
        )

    def test_requires_loader_version(self) -> None:
        for kind in ("fabric", "quilt", "forge", "neoforge"):
            with self.subTest(kind=kind):
                self.assertIsNone(existing_version(self.root, kind, "1.20.1", None))
                self.assertIsNone(existing_version(self.root, kind, "1.20.1", "  "))

    def test_unknown_kind_and_empty_game(self) -> None:
        self.assertIsNone(existing_version(self.root, "optifine", "1.20.1", "1.0"))
        self.assertIsNone(existing_version(self.root, "vanilla", "", None))
        self.assertIsNone(existing_version(self.root, "", "1.20.1", None))

    def test_directory_without_json_is_not_installed(self) -> None:
        (self.versions / "1.20.1-fabric-0.15.7").mkdir(parents=True)
        self.assertIsNone(existing_version(self.root, "fabric", "1.20.1", "0.15.7"))

    def test_trimmed_inputs(self) -> None:
        self.install_fake("1.20.1-forge-47.2.0")
        self.assertEqual(
            existing_version(self.root, " Forge ", " 1.20.1 ", " 47.2.0 "),
            "1.20.1-forge-47.2.0",
        )

    def test_empty_root_returns_none(self) -> None:
        self.assertIsNone(existing_version(Path(self._tmp.name) / "missing", "vanilla", "1.20.1", None))


if __name__ == "__main__":
    unittest.main()
