"""安全门禁单测：machine_id、Ticket 验签/过期、AuthGate 状态机、完整性自检。

不联网、不依赖真实安装流程。
"""
from __future__ import annotations

import hashlib
import sys
import unittest
from unittest import mock

from cubefetch.core import security as security_module
from cubefetch.core.auth import Account
from cubefetch.core.errors import AuthError
from cubefetch.core.security import (
    DEFAULT_TICKET_TTL,
    AuthGate,
    IntegrityGuard,
    Ticket,
    clear_active,
    current_ticket,
    machine_id,
    require_active,
    require_ticket,
)
from cubefetch.core.util import now

UUID = "069a79f444e94726a5befca90e38affa"


def _account(name: str = "Steve", uuid_value: str = UUID) -> Account:
    return Account(
        access_token="mc-token",
        refresh_token="rt",
        expires_at=now() + 3600.0,
        uuid=uuid_value,
        name=name,
        verified_at=now(),
    )


class MachineIdTest(unittest.TestCase):
    def setUp(self):
        self._old_cache = security_module._machine_id_cache
        security_module._machine_id_cache = None

    def tearDown(self):
        security_module._machine_id_cache = self._old_cache

    def test_stable_and_hash_format(self):
        first = machine_id()
        self.assertEqual(first, machine_id())
        self.assertEqual(len(first), 64)
        int(first, 16)  # 合法十六进制

    def test_registry_guid_priority(self):
        with mock.patch.object(security_module, "_registry_machine_guid", return_value="guid-aaa"):
            security_module._machine_id_cache = None
            first = machine_id()
        expected = hashlib.sha256(
            security_module.MACHINE_SALT + b"|guid|guid-aaa"
        ).hexdigest()
        self.assertEqual(first, expected)
        with mock.patch.object(security_module, "_registry_machine_guid", return_value="guid-bbb"):
            security_module._machine_id_cache = None
            second = machine_id()
        self.assertNotEqual(first, second)

    def test_volume_serial_fallback(self):
        with (
            mock.patch.object(security_module, "_registry_machine_guid", return_value=None),
            mock.patch.object(security_module, "_volume_serial", return_value="DEADBEEF"),
        ):
            security_module._machine_id_cache = None
            value = machine_id()
        expected = hashlib.sha256(
            security_module.MACHINE_SALT + b"|volume|DEADBEEF"
        ).hexdigest()
        self.assertEqual(value, expected)

    def test_node_hash_last_resort(self):
        with (
            mock.patch.object(security_module, "_registry_machine_guid", return_value=None),
            mock.patch.object(security_module, "_volume_serial", return_value=None),
        ):
            security_module._machine_id_cache = None
            value = machine_id()
        self.assertEqual(value, security_module._node_hash())


class TicketTest(unittest.TestCase):
    def test_create_valid(self):
        account = _account()
        ticket = Ticket.create(account, ttl=60.0)
        self.assertEqual(ticket.uuid, UUID)
        self.assertEqual(ticket.name, "Steve")
        self.assertAlmostEqual(ticket.expires_at - now(), 60.0, delta=2.0)
        self.assertFalse(ticket.is_expired())
        self.assertTrue(ticket.is_valid())

    def test_default_ttl(self):
        ticket = Ticket.create(_account())
        self.assertAlmostEqual(ticket.expires_at - now(), DEFAULT_TICKET_TTL, delta=2.0)

    def test_expired_ticket_invalid(self):
        ticket = Ticket.create(_account(), ttl=-1.0)
        self.assertTrue(ticket.is_expired())
        self.assertFalse(ticket.is_valid())

    def test_tampered_fields_invalid(self):
        ticket = Ticket.create(_account())
        self.assertFalse(Ticket(ticket.uuid, "Mallory", ticket.expires_at, ticket.mac).is_valid())
        self.assertFalse(Ticket(ticket.uuid, ticket.name, ticket.expires_at + 3600, ticket.mac).is_valid())
        self.assertFalse(Ticket(ticket.uuid, ticket.name, ticket.expires_at, "0" * 64).is_valid())
        self.assertFalse(Ticket("", ticket.name, ticket.expires_at, ticket.mac).is_valid())

    def test_ticket_bound_to_machine(self):
        ticket = Ticket.create(_account())
        with mock.patch.object(security_module, "machine_id", return_value="some-other-machine"):
            self.assertFalse(ticket.is_valid())


class AuthGateTest(unittest.TestCase):
    def setUp(self):
        clear_active()
        self.gate = AuthGate()

    def tearDown(self):
        clear_active()

    def test_issue_activates_ticket_and_account(self):
        account = _account()
        ticket = self.gate.issue(account)
        self.assertIsInstance(ticket, Ticket)
        self.assertIs(current_ticket(), ticket)
        self.assertIs(self.gate.current_ticket(), ticket)
        self.assertIs(self.gate.active_account(), account)
        self.assertIs(require_active(), account)
        self.assertIs(require_ticket(), account)
        self.assertIs(require_ticket(ticket), account)
        self.assertIs(self.gate.verify(ticket), account)

    def test_require_active_without_ticket_raises_chinese_auth_error(self):
        with self.assertRaises(AuthError) as ctx:
            self.gate.require_active()
        self.assertEqual(ctx.exception.code, "no_ticket")
        self.assertIn("尚未登录 Minecraft 账号", ctx.exception.user_message)
        self.assertIn("no_active_ticket", ctx.exception.detail)

    def test_module_require_ticket_without_ticket_raises(self):
        with self.assertRaises(AuthError) as ctx:
            require_ticket()
        self.assertEqual(ctx.exception.code, "no_ticket")

    def test_clear_after_issue(self):
        self.gate.issue(_account())
        self.gate.clear()
        self.assertIsNone(self.gate.current_ticket())
        self.assertIsNone(current_ticket())
        with self.assertRaises(AuthError):
            self.gate.require_active()

    def test_clear_active_alias_and_module_function(self):
        self.gate.issue(_account())
        self.gate.clear_active()
        self.assertIsNone(current_ticket())
        self.gate.issue(_account())
        clear_active()
        self.assertIsNone(current_ticket())
        with self.assertRaises(AuthError):
            require_ticket()

    def test_explicit_valid_ticket_without_active_state(self):
        ticket = Ticket.create(_account())
        account = require_ticket(ticket)
        self.assertEqual(account.uuid, UUID)
        self.assertEqual(account.name, "Steve")
        self.assertEqual(account.access_token, "")  # 最小身份信息
        self.assertEqual(account.expires_at, ticket.expires_at)

    def test_explicit_forged_ticket_rejected(self):
        forged = Ticket(UUID, "Steve", now() + 3600, "f" * 64)
        with self.assertRaises(AuthError) as ctx:
            require_ticket(forged)
        self.assertEqual(ctx.exception.code, "bad_ticket")
        self.assertIn("过期或被篡改", ctx.exception.user_message)

    def test_explicit_non_ticket_rejected(self):
        with self.assertRaises(AuthError) as ctx:
            require_ticket("not-a-ticket")  # type: ignore[arg-type]
        self.assertEqual(ctx.exception.code, "bad_ticket")

    def test_expired_ticket_rejected(self):
        expired = Ticket.create(_account(), ttl=-1.0)
        with self.assertRaises(AuthError):
            require_ticket(expired)

    def test_issue_replaces_previous_ticket(self):
        first_account = _account("First")
        second_account = _account("Second")
        self.gate.issue(first_account)
        second = self.gate.issue(second_account)
        self.assertIs(current_ticket(), second)
        self.assertIs(require_active(), second_account)

    def test_issue_invalid_account(self):
        for bad in (None, Account("t", "r", 0.0, "", "name"), Account("t", "r", 0.0, "uuid", "")):
            with self.assertRaises(AuthError) as ctx:
                self.gate.issue(bad)  # type: ignore[arg-type]
            self.assertEqual(ctx.exception.code, "bad_account")

    def test_gate_verify_forged(self):
        self.gate.issue(_account())
        forged = Ticket(UUID, "Steve", now() + 3600, "f" * 64)
        with self.assertRaises(AuthError):
            self.gate.verify(forged)


class IntegrityGuardTest(unittest.TestCase):
    def test_check_passes_in_repo(self):
        problems = IntegrityGuard().check()
        self.assertIsInstance(problems, list)
        self.assertFalse(any("关键模块缺失" in item for item in problems), problems)
        self.assertEqual(IntegrityGuard()._check_modules(), [])

    def test_detects_missing_module(self):
        guard = IntegrityGuard()
        guard.CRITICAL_MODULES = ("cubefetch.this_module_does_not_exist",)  # type: ignore[assignment]
        problems = guard.check()
        self.assertTrue(any("关键模块缺失" in item for item in problems), problems)

    def test_frozen_checks_disabled_when_not_frozen(self):
        if getattr(sys, "frozen", False):
            self.skipTest("当前为打包运行环境")
        self.assertEqual(IntegrityGuard()._check_frozen(), [])

    def test_check_never_raises(self):
        guard = IntegrityGuard()

        def explode():
            raise RuntimeError("boom")

        guard._check_modules = explode  # type: ignore[method-assign]
        problems = guard.check()
        self.assertTrue(any("完整性自检异常" in item for item in problems), problems)


if __name__ == "__main__":
    unittest.main()
