"""账号存储单测：DPAPI 往返、HMAC 篡改检测、30 天边界、降级路径。

全部写入临时目录，不触碰真实账号文件；不联网。
"""
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from cubefetch.config import appdata_dir
from cubefetch.core import account_store as store_module
from cubefetch.core import security as security_module
from cubefetch.core.account_store import AccountStore, MAGIC, dpapi_available
from cubefetch.core.auth import Account
from cubefetch.core.util import now


def _account(verified_days_ago: float = 1.0, *, xuid: str | None = None) -> Account:
    return Account(
        access_token="mc-secret-token",
        refresh_token="msa-refresh-secret",
        expires_at=now() + 3600.0,
        uuid="069a79f444e94726a5befca90e38affa",
        name="Steve",
        xuid=xuid,
        verified_at=now() - verified_days_ago * 86400.0,
    )


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        self.path = self.dir / "account.dat"
        self.store = AccountStore(self.path)

    def _envelope(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _write_envelope(self, doc: dict) -> None:
        self.path.write_text(json.dumps(doc), encoding="utf-8")

    # ------------------------------------------------------------ 路径/基本
    def test_default_path(self):
        self.assertEqual(AccountStore().path, appdata_dir() / "account.dat")

    def test_load_missing_file(self):
        self.assertIsNone(self.store.load())

    def test_save_rejects_non_account(self):
        with self.assertRaises(TypeError):
            self.store.save({"uuid": "x"})  # type: ignore[arg-type]

    def test_clear(self):
        self.store.save(_account())
        self.assertTrue(self.path.exists())
        self.store.clear()
        self.assertFalse(self.path.exists())
        self.assertIsNone(self.store.load())

    # ------------------------------------------------------------ DPAPI
    @unittest.skipUnless(dpapi_available(), "当前环境不支持 Windows DPAPI")
    def test_dpapi_roundtrip(self):
        account = _account()
        try:
            self.store.save(account)
        except OSError as exc:  # pragma: no cover - 例如无用户配置文件的服务账号
            self.skipTest(f"DPAPI 加密不可用：{exc}")
        raw = self.path.read_bytes()
        self.assertNotIn(b"mc-secret-token", raw)  # 明文不得落盘
        self.assertNotIn(b"msa-refresh-secret", raw)
        doc = self._envelope()
        self.assertEqual(doc["magic"], MAGIC)
        self.assertEqual(doc["mode"], "dpapi")
        self.assertFalse(doc["insecure"])
        self.assertIn("mac", doc)
        loaded = self.store.load()
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.to_dict(), account.to_dict())
        self.assertFalse(self.store.insecure)

    @unittest.skipUnless(dpapi_available(), "当前环境不支持 Windows DPAPI")
    def test_dpapi_corrupted_ciphertext_with_valid_mac_is_discarded(self):
        try:
            self.store.save(_account())
        except OSError as exc:  # pragma: no cover
            self.skipTest(f"DPAPI 加密不可用：{exc}")
        doc = self._envelope()
        payload = bytearray(base64.b64decode(doc["data"]))
        payload[len(payload) // 2] ^= 0xFF  # 破坏密文
        payload = bytes(payload)
        doc["data"] = base64.b64encode(payload).decode("ascii")
        doc["mac"] = store_module._payload_mac("dpapi", payload)  # MAC 仍然正确
        self._write_envelope(doc)
        self.assertIsNone(self.store.load())  # 解密失败 → 视为无效
        self.assertFalse(self.path.exists())  # 坏文件被清掉

    # ------------------------------------------------------------ 篡改检测
    def test_tampered_payload_is_discarded(self):
        self.store.save(_account())
        doc = self._envelope()
        payload = bytearray(base64.b64decode(doc["data"]))
        payload[0] ^= 0x01
        doc["data"] = base64.b64encode(bytes(payload)).decode("ascii")
        self._write_envelope(doc)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_tampered_mac_is_discarded(self):
        self.store.save(_account())
        doc = self._envelope()
        doc["mac"] = "0" * 64
        self._write_envelope(doc)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_cross_machine_file_is_discarded(self):
        self.store.save(_account())
        with mock.patch.object(security_module, "machine_id", return_value="other-machine-id"):
            self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_corrupt_json_is_discarded(self):
        self.path.write_bytes(b"{not valid json")
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_wrong_magic_is_discarded(self):
        self.store.save(_account())
        doc = self._envelope()
        doc["magic"] = "OTHER"
        self._write_envelope(doc)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    def test_bad_base64_is_discarded(self):
        self.store.save(_account())
        doc = self._envelope()
        doc["data"] = "!!!not-base64!!!"
        self._write_envelope(doc)
        self.assertIsNone(self.store.load())
        self.assertFalse(self.path.exists())

    # ------------------------------------------------------------ 明文回退
    def test_plaintext_fallback_when_dpapi_unavailable(self):
        old = (store_module._DPAPI, store_module._DPAPI_TRIED)
        store_module._DPAPI = None
        store_module._DPAPI_TRIED = True
        try:
            account = _account()
            self.store.save(account)
            doc = self._envelope()
            self.assertEqual(doc["mode"], "plain")
            self.assertTrue(doc["insecure"])  # 文件内明确标记不安全
            self.assertTrue(self.store.insecure)
            # 明文回退：payload 为 base64 编码的明文 JSON（可逆、无加密）
            self.assertIn(b"mc-secret-token", base64.b64decode(doc["data"]))
            loaded = self.store.load()
            self.assertIsNotNone(loaded)
            assert loaded is not None
            self.assertEqual(loaded.to_dict(), account.to_dict())
            self.assertTrue(self.store.insecure)
        finally:
            store_module._DPAPI, store_module._DPAPI_TRIED = old

    def test_xuid_roundtrip(self):
        account = _account(xuid="1234567890")
        self.store.save(account)
        if dpapi_available():
            loaded = self.store.load()
        else:  # pragma: no cover
            loaded = None
        self.assertIsNotNone(loaded)
        assert loaded is not None
        self.assertEqual(loaded.xuid, "1234567890")
        self.assertEqual(loaded.verified_at, account.verified_at)

    # ------------------------------------------------------------ 30 天规则
    def test_days_since_verified(self):
        self.assertAlmostEqual(self.store.days_since_verified(_account(5)), 5.0, delta=0.01)
        self.assertAlmostEqual(self.store.days_since_verified(_account(0)), 0.0, delta=0.01)
        self.assertEqual(self.store.days_since_verified(Account("t", "r", 0, "u", "n", verified_at=0)), float("inf"))

    def test_needs_reverify_strict_boundary(self):
        base = 1_800_000_000.0
        with mock.patch.object(store_module, "now", return_value=base):
            account = _account(0)
            account.verified_at = base - 30 * 86400.0
            self.assertFalse(self.store.needs_reverify(account))  # 正好 30 天：不强制
            account.verified_at = base - 30 * 86400.0 - 1
            self.assertTrue(self.store.needs_reverify(account))  # 超过 1 秒：强制
            account.verified_at = base - 29 * 86400.0
            self.assertFalse(self.store.needs_reverify(account))
            account.verified_at = base
            self.assertFalse(self.store.needs_reverify(account))
            account.verified_at = 0.0
            self.assertTrue(self.store.needs_reverify(account))  # 从未验证 → 强制

            # 自定义天数
            account.verified_at = base - 8 * 86400.0
            self.assertTrue(self.store.needs_reverify(account, 7))
            account.verified_at = base - 7 * 86400.0
            self.assertFalse(self.store.needs_reverify(account, 7))

    def test_needs_reverify_ignores_bad_days(self):
        account = _account(1)
        self.assertFalse(self.store.needs_reverify(account, "bad"))  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
