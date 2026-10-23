"""manifest.py 单元测试：分类、缓存 TTL、版本 JSON 获取。

使用 tests/fixtures 下的离线真实数据，不依赖外网。
"""
from __future__ import annotations

import copy
import json
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit

from cubefetch.config import cache_dir
from cubefetch.core.errors import CubeFetchError, NetworkError
from cubefetch.core.http import BMCLAPI_BASE, HttpSession, mirror_url
from cubefetch.core.manifest import (
    APRIL_FOOLS_VERSIONS,
    CATEGORY_APRIL,
    CATEGORY_OLD,
    CATEGORY_ORDER,
    CATEGORY_RELEASE,
    CATEGORY_SNAPSHOT,
    MANIFEST_URL,
    ManifestService,
    VersionEntry,
    group_entries_by_category,
    parse_classification,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeHttp:
    """只实现 get_json 的假客户端，记录调用次数。"""

    def __init__(self, payloads: dict[str, dict] | None = None, fail: bool = False) -> None:
        self.payloads = dict(payloads or {})
        self.calls: list[str] = []
        self.fail = fail

    def get_json(self, url: str, *, use_mirror: bool = True) -> dict:
        self.calls.append(url)
        if self.fail:
            raise NetworkError("网络不可用", detail=url)
        if url not in self.payloads:
            raise NetworkError("缺少测试数据", detail=url)
        return copy.deepcopy(self.payloads[url])


class ManifestServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory(prefix="cf-manifest-")
        self.addCleanup(self._tmp.cleanup)
        self.cache_root = Path(self._tmp.name) / "cache"
        self.manifest_payload = load_fixture("version_manifest_v2.json")
        self.version_payload = load_fixture("version_1_20_1.json")
        self.http = FakeHttp({MANIFEST_URL: self.manifest_payload})
        self.service = ManifestService(self.http, cache_root=self.cache_root)

    # -- 清单加载与分类 ---------------------------------------------------

    def test_load_returns_entries(self) -> None:
        entries = self.service.load()
        self.assertEqual(len(entries), len(self.manifest_payload["versions"]))
        self.assertIsInstance(entries[0], VersionEntry)
        ids = [entry.id for entry in entries]
        self.assertIn("1.20.1", ids)
        self.assertIn("1.7.10", ids)
        self.assertIn("26.3", ids)
        for entry in entries:
            self.assertIn(entry.category, CATEGORY_ORDER)
            self.assertTrue(entry.url.startswith("https://piston-meta.mojang.com/"))

    def test_categories_four_keys_and_sorted(self) -> None:
        categories = self.service.categories()
        self.assertEqual(set(categories), {CATEGORY_RELEASE, CATEGORY_SNAPSHOT, CATEGORY_OLD, CATEGORY_APRIL})
        for key, items in categories.items():
            times = [entry.release_time for entry in items]
            self.assertEqual(times, sorted(times, reverse=True), f"{key} 未按时间倒序")
        self.assertIn("1.20.1", {entry.id for entry in categories[CATEGORY_RELEASE]})
        self.assertTrue({"b1.7.3", "a1.0.4"} <= {entry.id for entry in categories[CATEGORY_OLD]})
        april_ids = {entry.id for entry in categories[CATEGORY_APRIL]}
        self.assertEqual(april_ids, set(APRIL_FOOLS_VERSIONS) & set(self.manifest_payload_versions()))
        self.assertTrue(all(entry.category == CATEGORY_APRIL for entry in categories[CATEGORY_APRIL]))

    def manifest_payload_versions(self) -> list[str]:
        return [item["id"] for item in self.manifest_payload["versions"]]

    def test_categories_present_even_if_empty(self) -> None:
        payload = {
            "latest": {"release": "x", "snapshot": "x"},
            "versions": [
                {"id": "1.0", "type": "release", "url": "https://example.com/1.0.json", "releaseTime": "2011-01-01T00:00:00+00:00"}
            ],
        }
        service = ManifestService(FakeHttp({MANIFEST_URL: payload}), cache_root=self.cache_root)
        categories = service.categories()
        self.assertEqual(set(categories), {CATEGORY_RELEASE, CATEGORY_SNAPSHOT, CATEGORY_OLD, CATEGORY_APRIL})
        self.assertEqual([entry.id for entry in categories[CATEGORY_RELEASE]], ["1.0"])
        self.assertEqual(categories[CATEGORY_APRIL], [])

    def test_find_version(self) -> None:
        self.assertEqual(self.service.find_version("1.20.1").id, "1.20.1")
        self.assertIsNone(self.service.find_version("0.0-not-exist"))
        self.assertIsNone(self.service.find_version(""))

    def test_load_malformed_payload_raises(self) -> None:
        service = ManifestService(
            FakeHttp({MANIFEST_URL: {"latest": {}, "versions": "oops"}}),
            cache_root=self.cache_root,
        )
        with self.assertRaises(NetworkError):
            service.load()
        self.assertFalse(self.service.manifest_cache_path().exists())

    # -- 缓存 -------------------------------------------------------------

    def test_cache_written_and_reused(self) -> None:
        self.service.load()
        cache_path = self.service.manifest_cache_path()
        self.assertTrue(cache_path.is_file())
        wrapped = json.loads(cache_path.read_text(encoding="utf-8"))
        self.assertIsInstance(wrapped["fetched_at"], (int, float))
        self.assertEqual(wrapped["manifest"]["versions"], self.manifest_payload["versions"])
        self.service.load()
        self.assertEqual(len(self.http.calls), 1)

    def test_stale_cache_refreshes(self) -> None:
        self.service.load()
        cache_path = self.service.manifest_cache_path()
        wrapped = json.loads(cache_path.read_text(encoding="utf-8"))
        wrapped["fetched_at"] = time.time() - 7200
        cache_path.write_text(json.dumps(wrapped), encoding="utf-8")
        self.service.load()
        self.assertEqual(len(self.http.calls), 2)

    def test_refresh_forces_download(self) -> None:
        self.service.load()
        self.service.refresh()
        self.assertEqual(len(self.http.calls), 2)

    def test_ttl_zero_always_refreshes(self) -> None:
        service = ManifestService(self.http, cache_ttl=0, cache_root=self.cache_root)
        service.load()
        service.load()
        self.assertEqual(len(self.http.calls), 2)

    def test_corrupted_cache_falls_back_to_network(self) -> None:
        self.service.load()
        self.service.manifest_cache_path().write_text("{not json", encoding="utf-8")
        entries = self.service.load()
        self.assertEqual(len(entries), len(self.manifest_payload["versions"]))
        self.assertEqual(len(self.http.calls), 2)

    def test_offline_uses_stale_cache(self) -> None:
        self.service.load()
        cache_path = self.service.manifest_cache_path()
        wrapped = json.loads(cache_path.read_text(encoding="utf-8"))
        wrapped["fetched_at"] = time.time() - 7200
        cache_path.write_text(json.dumps(wrapped), encoding="utf-8")
        self.http.fail = True
        entries = self.service.load()
        self.assertTrue(entries)
        self.assertEqual(len(self.http.calls), 2)

    def test_offline_without_cache_raises(self) -> None:
        service = ManifestService(FakeHttp({}, fail=True), cache_root=self.cache_root)
        with self.assertRaises(NetworkError):
            service.load()

    # -- 版本 JSON --------------------------------------------------------

    def test_fetch_version_json_caches(self) -> None:
        entry = self.service.find_version("1.20.1")
        self.http.payloads[entry.url] = self.version_payload
        data = self.service.fetch_version_json("1.20.1")
        self.assertEqual(data["id"], "1.20.1")
        cache_path = self.service.version_cache_path("1.20.1")
        self.assertTrue(cache_path.is_file())
        self.assertEqual(json.loads(cache_path.read_text(encoding="utf-8")), self.version_payload)
        calls_after_first = len(self.http.calls)
        self.service.fetch_version_json("1.20.1")
        self.assertEqual(len(self.http.calls), calls_after_first)
        self.assertEqual(self.service.load_cached_version_json("1.20.1"), self.version_payload)

    def test_fetch_version_json_unknown_id(self) -> None:
        with self.assertRaises(CubeFetchError):
            self.service.fetch_version_json("0.0-not-exist")

    def test_fetch_version_json_empty_id(self) -> None:
        with self.assertRaises(CubeFetchError):
            self.service.fetch_version_json("")

    def test_load_cached_version_json_missing(self) -> None:
        self.assertIsNone(self.service.load_cached_version_json("1.20.1"))

    # -- 辅助函数与路径 ---------------------------------------------------

    def test_version_cache_path(self) -> None:
        self.assertEqual(
            self.service.version_cache_path("1.20.1"),
            self.cache_root / "versions" / "1.20.1.json",
        )
        self.assertEqual(
            self.service.manifest_cache_path(), self.cache_root / "version_manifest.json"
        )

    def test_default_cache_root_is_config_cache_dir(self) -> None:
        service = ManifestService(FakeHttp())
        self.assertEqual(service.cache_root, Path(cache_dir()))

    def test_group_entries_by_category(self) -> None:
        entries = [
            VersionEntry(
                id="x",
                type="snapshot",
                url="u",
                release_time="2024-01-01T00:00:00+00:00",
                category=CATEGORY_SNAPSHOT,
            ),
            VersionEntry(
                id="24w14potato",
                type="snapshot",
                url="u",
                release_time="2024-04-01T00:00:00+00:00",
                category=CATEGORY_APRIL,
            ),
        ]
        grouped = group_entries_by_category(entries)
        self.assertEqual(len(grouped[CATEGORY_SNAPSHOT]), 1)
        self.assertEqual(len(grouped[CATEGORY_APRIL]), 1)


class ClassificationTest(unittest.TestCase):
    def test_release(self) -> None:
        self.assertEqual(parse_classification("release", "1.20.1"), CATEGORY_RELEASE)

    def test_snapshot(self) -> None:
        self.assertEqual(parse_classification("snapshot", "24w14a"), CATEGORY_SNAPSHOT)

    def test_old_beta_and_alpha(self) -> None:
        self.assertEqual(parse_classification("old_beta", "b1.7.3"), CATEGORY_OLD)
        self.assertEqual(parse_classification("old_alpha", "a1.0.4"), CATEGORY_OLD)

    def test_april_fools_versions(self) -> None:
        for version_id in APRIL_FOOLS_VERSIONS:
            with self.subTest(version=version_id):
                self.assertEqual(parse_classification("snapshot", version_id), CATEGORY_APRIL)

    def test_april_fools_list_matches_contract(self) -> None:
        expected = {
            "15w14a", "1.RV-Pre1", "3D Shareware v1.34", "20w14infinite",
            "22w13oneblockatatime", "23w13a_or_b", "24w14potato", "25w14craftmine", "26w14a",
        }
        self.assertEqual(set(APRIL_FOOLS_VERSIONS), expected)

    def test_unknown_type_defaults_to_snapshot(self) -> None:
        self.assertEqual(parse_classification("unknown-kind", "whatever"), CATEGORY_SNAPSHOT)
        self.assertEqual(parse_classification("", "whatever"), CATEGORY_SNAPSHOT)

    def test_april_id_with_release_type_stays_release(self) -> None:
        self.assertEqual(parse_classification("release", "15w14a"), CATEGORY_RELEASE)


class VersionEntryTest(unittest.TestCase):
    def make_entry(self, release_time: str) -> VersionEntry:
        return VersionEntry(
            id="1.20.1",
            type="release",
            url="https://example.com",
            release_time=release_time,
            category=CATEGORY_RELEASE,
        )

    def test_release_date_parses_iso(self) -> None:
        parsed = self.make_entry("2023-06-12T13:25:51+00:00").release_date()
        self.assertEqual((parsed.year, parsed.month, parsed.day), (2023, 6, 12))
        self.assertIsNotNone(parsed.tzinfo)

    def test_release_date_parses_z_suffix(self) -> None:
        parsed = self.make_entry("2023-06-12T13:25:51Z").release_date()
        self.assertEqual(parsed.year, 2023)

    def test_release_date_invalid_returns_epoch(self) -> None:
        parsed = self.make_entry("not-a-date").release_date()
        self.assertEqual(parsed.year, 1970)


class ManifestMirrorFirstTest(unittest.TestCase):
    """auto 模式（默认）：清单与版本 JSON 的请求优先打到 BMCLAPI 镜像路径。"""

    def setUp(self) -> None:
        from tests.test_http import LocalHttpServer

        self.server = LocalHttpServer().start()
        self.addCleanup(self.server.stop)
        self._tmp = TemporaryDirectory(prefix="cf-manifest-mirror-")
        self.addCleanup(self._tmp.cleanup)
        self.cache_root = Path(self._tmp.name) / "cache"
        self.manifest_payload = load_fixture("version_manifest_v2.json")

    def make_http(self) -> HttpSession:
        http = HttpSession(
            mirror="auto",
            timeout=5,
            retries=1,
            user_agent="CubeFetch-Test/1.0",
            mirror_base=self.server.base,
        )
        self.addCleanup(http.close)
        return http

    def test_default_auto_sources_mirror_first(self) -> None:
        http = HttpSession(mirror="auto")
        self.addCleanup(http.close)
        sources = http._sources(MANIFEST_URL, True)
        self.assertEqual(
            sources,
            [
                BMCLAPI_BASE + "/mc/game/version_manifest_v2.json",
                MANIFEST_URL,
            ],
        )

    def test_load_hits_mirror_path_first(self) -> None:
        self.server.add_bytes(
            "/mc/game/version_manifest_v2.json",
            json.dumps(self.manifest_payload, ensure_ascii=False).encode("utf-8"),
        )
        service = ManifestService(self.make_http(), cache_root=self.cache_root, cache_ttl=0)
        entries = service.load()
        self.assertTrue(entries)
        self.assertEqual(self.server.requests[0]["path"], "/mc/game/version_manifest_v2.json")
        self.assertEqual(self.server.request_count(), 1)

    def test_fetch_version_json_hits_mirror_path_first(self) -> None:
        self.server.add_bytes(
            "/mc/game/version_manifest_v2.json",
            json.dumps(self.manifest_payload, ensure_ascii=False).encode("utf-8"),
        )
        entry = next(
            item for item in self.manifest_payload["versions"] if item["id"] == "1.20.1"
        )
        version_path = urlsplit(mirror_url(entry["url"], self.server.base)).path
        version_payload = load_fixture("version_1_20_1.json")
        self.server.add_bytes(
            version_path,
            json.dumps(version_payload, ensure_ascii=False).encode("utf-8"),
        )
        service = ManifestService(self.make_http(), cache_root=self.cache_root, cache_ttl=0)
        data = service.fetch_version_json("1.20.1")
        self.assertEqual(data["id"], "1.20.1")
        paths = [item["path"] for item in self.server.requests]
        self.assertEqual(paths, ["/mc/game/version_manifest_v2.json", version_path])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
