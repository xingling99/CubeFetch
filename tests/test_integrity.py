"""文件级完整性校验单测（完全离线，不联网、不依赖真实打包流程）。

覆盖：
- 源码模式：匹配通过；改一个文件 / 缺文件 / 多文件 / 多出未登记文件 → problem；
- 无清单 / 清单损坏 → ``mode="unverified"`` 语义明确；
- 冻结模式：用 ``marshal.loads(marshal.dumps(code))`` 模拟 PyInstaller 从 PYZ 取回
  code object，验证规范化哈希稳定；并借 mock 覆盖冻结分支的匹配/不匹配；
- 构建脚本 ``build/gen_integrity.py``：可执行、幂等、生成内容与 ``build_manifest`` 一致；
- security 门禁：登记 problems 后 ``require_ticket()/require_active()`` 抛
  ``AuthError(code="integrity")``，清空后恢复。
"""
from __future__ import annotations

import datetime as _datetime
import marshal
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cubefetch.core import integrity as integrity_module
from cubefetch.core.auth import Account
from cubefetch.core.errors import AuthError
from cubefetch.core.integrity import (
    IntegrityReport,
    MANIFEST_REL_PATH,
    build_manifest,
    code_digest,
    compile_module,
    compiled_filename,
    format_manifest_source,
    module_name,
    summarize,
    verify,
)
from cubefetch.core.security import (
    AuthGate,
    clear_active,
    current_ticket,
    integrity_problems,
    require_active,
    require_ticket,
    set_integrity_problems,
)
from cubefetch.core.util import now

REPO_ROOT = Path(__file__).resolve().parents[1]
UUID = "069a79f444e94726a5befca90e38affa"
_ERRORS_REL = "cubefetch/core/errors.py"
_UTIL_REL = "cubefetch/core/util.py"


def _account(name: str = "Steve") -> Account:
    return Account(
        access_token="mc-token",
        refresh_token="rt",
        expires_at=now() + 3600.0,
        uuid=UUID,
        name=name,
        verified_at=now(),
    )


def _copy_package(destination: Path) -> Path:
    """把仓库内 cubefetch 包复制到临时目录（跳过缓存文件），返回根目录。"""
    shutil.copytree(
        REPO_ROOT / "cubefetch",
        destination / "cubefetch",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    return destination


class SourceModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cf_integrity_src_")
        self.root = _copy_package(Path(self._tmp.name))
        self.manifest = build_manifest(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def _verify(self) -> IntegrityReport:
        return verify(self.manifest, root=self.root)

    def test_matching_manifest_passes(self):
        report = self._verify()
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.mode, "source")
        self.assertEqual(report.problems, [])

    def test_modified_file_reported(self):
        target = self.root / _ERRORS_REL
        target.write_text(target.read_text(encoding="utf-8") + "\n# tampered\n", encoding="utf-8")
        report = self._verify()
        self.assertFalse(report.ok)
        self.assertIn(f"文件被修改：{_ERRORS_REL}", report.problems)

    def test_missing_file_reported(self):
        (self.root / _UTIL_REL).unlink()
        report = self._verify()
        self.assertFalse(report.ok)
        self.assertIn(f"文件缺失：{_UTIL_REL}", report.problems)

    def test_unregistered_new_file_reported(self):
        injected = self.root / "cubefetch" / "core" / "injected_backdoor.py"
        injected.write_text("print('pwned')\n", encoding="utf-8")
        report = self._verify()
        self.assertFalse(report.ok)
        self.assertIn("未登记文件：cubefetch/core/injected_backdoor.py", report.problems)

    def test_multiple_problems_reported_together(self):
        errors = self.root / _ERRORS_REL
        errors.write_text(errors.read_text(encoding="utf-8") + "\n# x\n", encoding="utf-8")
        (self.root / _UTIL_REL).unlink()
        (self.root / "cubefetch" / "extra.py").write_text("X = 1\n", encoding="utf-8")
        report = self._verify()
        self.assertFalse(report.ok)
        self.assertGreaterEqual(len(report.problems), 3)
        self.assertIn(f"文件被修改：{_ERRORS_REL}", report.problems)
        self.assertIn(f"文件缺失：{_UTIL_REL}", report.problems)
        self.assertIn("未登记文件：cubefetch/extra.py", report.problems)

    def test_manifest_excludes_itself(self):
        self.assertNotIn(MANIFEST_REL_PATH, self.manifest["FILES"])
        self.assertNotIn("cubefetch.integrity_manifest", self.manifest["CODE"])
        # 复制出来的包里带着清单生成物，但不参与校验（既不报未登记也不报缺失）
        self.assertTrue((self.root / MANIFEST_REL_PATH).is_file())
        self.assertTrue(self._verify().ok)

    def test_manifest_is_deterministic(self):
        self.assertEqual(build_manifest(self.root), self.manifest)
        self.assertEqual(format_manifest_source(build_manifest(self.root)), format_manifest_source(self.manifest))

    def test_pycache_is_ignored(self):
        cache = self.root / "cubefetch" / "core" / "__pycache__"
        cache.mkdir(parents=True, exist_ok=True)
        (cache / "evil.py").write_text("X = 1\n", encoding="utf-8")
        self.assertTrue(self._verify().ok)

    def test_invalid_manifest_marks_unverified(self):
        report = verify({"FILES": {}}, root=self.root)
        self.assertFalse(report.ok)
        self.assertEqual(report.mode, "unverified")
        self.assertTrue(any("格式无效" in item for item in report.problems), report.problems)


class MissingManifestTest(unittest.TestCase):
    def test_missing_manifest_marks_unverified(self):
        with mock.patch.object(integrity_module, "_manifest_module", return_value=None):
            report = verify()
        self.assertFalse(report.ok)
        self.assertEqual(report.mode, "unverified")
        self.assertEqual(len(report.problems), 1)
        self.assertIn("未找到文件完整性清单", report.problems[0])

    def test_summarize_ok_and_unverified(self):
        self.assertIn("通过", summarize(IntegrityReport(ok=True, mode="source")))
        text = summarize(IntegrityReport(ok=False, mode="unverified", problems=["没有清单"]))
        self.assertIn("未校验", text)
        self.assertIn("没有清单", text)


class CanonicalCodeTest(unittest.TestCase):
    def test_marshal_roundtrip_digest_stable(self):
        """模拟 PyInstaller：PYZ 里取出的是 marshal 重建后的 code object。"""
        for rel in ("cubefetch/core/errors.py", "cubefetch/core/isolation.py"):
            data = (REPO_ROOT / rel).read_bytes()
            code = compile_module(data, rel)
            roundtrip = marshal.loads(marshal.dumps(code))
            self.assertEqual(code_digest(code), code_digest(roundtrip), rel)
            # 规范化的幂等性：再次规范化不应改变结果
            self.assertEqual(
                code_digest(code),
                code_digest(integrity_module.canonicalize_code(integrity_module.canonicalize_code(code))),
            )

    def test_frozenset_constant_order_normalized(self):
        # 编译器会把 ``in {"a", "b"}`` 折叠成 frozenset 常量；规范化后应变成定序元组
        code = compile_module(b"def f(x):\n    return x in {'a','b','c'}\n", "cubefetch/_probe.py")
        canonical = integrity_module.canonicalize_code(code)
        found: list[tuple] = []

        def walk(item_code):
            for constant in item_code.co_consts:
                if isinstance(constant, tuple) and constant and all(isinstance(v, str) for v in constant):
                    found.append(constant)
                if isinstance(constant, type(item_code)):
                    walk(constant)

        walk(canonical)
        self.assertIn(("a", "b", "c"), found)

    def test_different_sources_have_different_digests(self):
        first = code_digest(compile_module(b"X = 1\n", "cubefetch/_a.py"))
        second = code_digest(compile_module(b"X = 2\n", "cubefetch/_a.py"))
        self.assertNotEqual(first, second)

    def test_filename_matches_module_name(self):
        self.assertEqual(module_name("cubefetch/__init__.py"), "cubefetch")
        self.assertEqual(module_name("cubefetch/core/http.py"), "cubefetch.core.http")
        self.assertIsNone(module_name("README.py"))
        self.assertEqual(compiled_filename("cubefetch/core/http.py"), os.path.join("cubefetch", "core", "http.py"))


class FrozenModeTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="cf_integrity_frozen_")
        self.root = _copy_package(Path(self._tmp.name))
        sources = dict(integrity_module._iter_package_sources(self.root))
        self.codes = {
            module: compile_module(sources[rel].read_bytes(), rel)
            for module, rel in (
                (module_name(rel), rel) for rel in sources
            )
            if module
        }

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, manifest):
        with (
            mock.patch.object(integrity_module.sys, "frozen", True, create=True),
            mock.patch.object(integrity_module, "_module_code", side_effect=self.codes.get),
        ):
            return verify(manifest, root=self.root)

    def test_frozen_matching_code_passes(self):
        manifest = {
            "BUILD_TIME": "2026-01-01T00:00:00Z",
            "FILES": {},
            "CODE": {name: code_digest(code) for name, code in self.codes.items()},
        }
        report = self._run(manifest)
        self.assertEqual(report.mode, "frozen")
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.skipped, [])

    def test_frozen_tampered_code_reported(self):
        manifest = {
            "FILES": {},
            "CODE": {"cubefetch.core.errors": "0" * 64},
        }
        report = self._run(manifest)
        self.assertFalse(report.ok)
        self.assertEqual(report.mode, "frozen")
        self.assertIn("模块代码被修改：cubefetch.core.errors", report.problems)
        self.assertEqual(report.skipped, [])

    def test_frozen_unbundled_module_is_skipped_not_problem(self):
        # PyInstaller 会裁掉没有静态引用的死模块（如 cubefetch.i18n），
        # 它们取不到 code object，应记入 skipped 而不是被误报为篡改。
        manifest = {
            "FILES": {},
            "CODE": {"cubefetch.not_bundled": "0" * 64},
        }
        report = self._run(manifest)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual(report.mode, "frozen")
        self.assertEqual(report.problems, [])
        self.assertEqual(report.skipped, ["cubefetch.not_bundled"])
        self.assertIn("未参与校验", summarize(report))

    def test_frozen_mixed_tamper_and_unbundled(self):
        manifest = {
            "FILES": {},
            "CODE": {
                "cubefetch.core.errors": "0" * 64,
                "cubefetch.not_bundled": "0" * 64,
            },
        }
        report = self._run(manifest)
        self.assertFalse(report.ok)
        self.assertIn("模块代码被修改：cubefetch.core.errors", report.problems)
        self.assertEqual(report.skipped, ["cubefetch.not_bundled"])


class GenScriptTest(unittest.TestCase):
    def test_gen_script_runs_and_is_idempotent(self):
        with tempfile.TemporaryDirectory(prefix="cf_gen_integrity_") as tmp_name:
            tmp = Path(tmp_name)
            _copy_package(tmp)
            (tmp / "build").mkdir()
            shutil.copy2(REPO_ROOT / "build" / "gen_integrity.py", tmp / "build" / "gen_integrity.py")
            script = tmp / "build" / "gen_integrity.py"
            command = [sys.executable, "-X", "utf8", str(script)]
            first = subprocess.run(command, cwd=tmp, capture_output=True, text=True, timeout=180)
            self.assertEqual(first.returncode, 0, first.stderr)
            manifest_path = tmp / "cubefetch" / "integrity_manifest.py"
            self.assertTrue(manifest_path.is_file())
            content_first = manifest_path.read_text(encoding="utf-8")
            self.assertIn("FILES = {", content_first)
            self.assertIn("CODE = {", content_first)
            self.assertNotIn(f"{MANIFEST_REL_PATH!r}:", content_first)
            second = subprocess.run(command, cwd=tmp, capture_output=True, text=True, timeout=180)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(manifest_path.read_text(encoding="utf-8"), content_first, "生成清单必须幂等")
            # 生成的清单覆盖了全部源码文件
            generated = build_manifest(tmp)
            self.assertEqual(set(generated["FILES"]), set(integrity_module.build_manifest(tmp)["FILES"]))
            self.assertGreaterEqual(len(generated["FILES"]), 30)


class RepoManifestTest(unittest.TestCase):
    def test_repo_verify_passes_with_fresh_manifest(self):
        """仓库内清单若未过期，``verify()`` 应通过（过期则跳过，避免并行改动误报）。"""
        manifest = integrity_module._manifest_module()
        if manifest is None:
            self.skipTest("仓库内尚无 integrity_manifest.py")
        try:
            built = _datetime.datetime.strptime(manifest.BUILD_TIME, "%Y-%m-%dT%H:%M:%SZ").replace(
                tzinfo=_datetime.timezone.utc
            )
        except (AttributeError, ValueError):
            self.skipTest("清单 BUILD_TIME 无法解析")
        sources = integrity_module._iter_package_sources(REPO_ROOT)
        newest = max(path.stat().st_mtime for _rel, path in sources)
        if newest > built.timestamp() + 1:
            self.skipTest("完整性清单已过期（源码在清单生成后被修改），打包前需重新运行 gen_integrity")
        report = verify()
        self.assertEqual(report.mode, "source")
        self.assertTrue(report.ok, summarize(report))


class SecurityGateTest(unittest.TestCase):
    def setUp(self):
        clear_active()
        set_integrity_problems([])

    def tearDown(self):
        set_integrity_problems([])
        clear_active()

    def test_requires_auth_error_when_problems_registered(self):
        set_integrity_problems(["文件被修改：cubefetch/core/errors.py"])
        self.assertEqual(integrity_problems(), ["文件被修改：cubefetch/core/errors.py"])
        for call in (require_active, AuthGate().require_active):
            with self.assertRaises(AuthError) as ctx:
                call()
            self.assertEqual(ctx.exception.code, "integrity")
            self.assertIn("程序文件完整性校验失败，已禁止下载", ctx.exception.user_message)
            self.assertIn("integrity_problems=1", ctx.exception.detail)

    def test_blocks_download_even_after_valid_login(self):
        gate = AuthGate()
        account = _account()
        ticket = gate.issue(account)  # 登录本身不拦截
        self.assertIs(current_ticket(), ticket)
        set_integrity_problems(["模块代码被修改：cubefetch.core.http"])
        with self.assertRaises(AuthError) as ctx:
            gate.require_active()  # install 入口
        self.assertEqual(ctx.exception.code, "integrity")
        with self.assertRaises(AuthError):
            require_ticket()
        with self.assertRaises(AuthError):
            gate.verify(ticket)

    def test_explicit_ticket_also_blocked(self):
        ticket = AuthGate().issue(_account())
        set_integrity_problems(["存在未登记的 cubefetch 文件"])
        with self.assertRaises(AuthError) as ctx:
            require_ticket(ticket)
        self.assertEqual(ctx.exception.code, "integrity")

    def test_clearing_problems_restores_gate(self):
        gate = AuthGate()
        account = _account()
        gate.issue(account)
        set_integrity_problems(["坏文件"])
        with self.assertRaises(AuthError):
            gate.require_active()
        set_integrity_problems([])
        self.assertIs(gate.require_active(), account)

    def test_registered_problems_are_copied(self):
        source = ["问题一"]
        set_integrity_problems(source)
        source.append("问题二")
        self.assertEqual(integrity_problems(), ["问题一"])
        returned = integrity_problems()
        returned.append("外部篡改")
        self.assertEqual(integrity_problems(), ["问题一"])


if __name__ == "__main__":
    unittest.main()
