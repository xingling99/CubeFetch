"""开发期探针：验证「预置 libraries + 运行官方 installer」能否离线跳过 NeoForge 官方 maven。

思路（实测依据）：
  - 官方 installer 的 `Downloader.download(File)` 会先校验目标文件 sha1，命中即输出
    "exists. Checksum valid." 并跳过网络请求。
  - 因此把它需要的所有 artifact 预先放到 `<root>/libraries/<maven 路径>`，
    安装器就不再访问 maven.neoforged.net（本机不可达）。
  - BMCLAPI 同时镜像 libraries.minecraft.net 与 maven.neoforged.net：两者都映射到
    https://bmclapi2.bangbang93.com/maven/<path>（已实测 200）。

用法：
    python -X utf8 tools/neo_preseed_probe.py <installer.jar> <mc_root> [--run]

只用于开发验证，不进产品代码。
"""

from __future__ import annotations

import collections
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import zipfile

import requests

USER_AGENT = "CubeFetch/1.0 (+https://github.com/)"
BMCLAPI = "https://bmclapi2.bangbang93.com"
COORD_RE = re.compile(r"^\[([^\]]+)\]$")


def maven_path(name: str) -> str:
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    return "/".join(group.split(".") + [artifact, version, f"{filename}.{ext}"])


def sha1_of(path: str) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(chunk)
    return digest.hexdigest()


def collect(
    profile: dict, version_json: dict | None = None
) -> list[tuple[str, str, str | None, int | None]]:
    """返回 [(相对路径, url, sha1, size)]。"""
    items: list[tuple[str, str, str | None, int | None]] = []
    seen: set[str] = set()

    def add(coord: str, url: str | None, sha1: str | None, size: int | None) -> None:
        rel = maven_path(coord)
        if rel in seen:
            return
        seen.add(rel)
        if not url:
            url = f"{BMCLAPI}/maven/{rel}"
        items.append((rel, url, sha1, size))

    for lib in profile.get("libraries") or []:
        name = lib.get("name") or ""
        art = (lib.get("downloads") or {}).get("artifact") or {}
        if not name:
            continue
        add(name, art.get("url"), art.get("sha1"), art.get("size"))

    # 处理器参数/数据里用 [group:artifact:version[:classifier][@ext]] 引用的输入 artifact
    coords: set[str] = set()
    for proc in profile.get("processors") or []:
        for field in ("jar", "classpath", "args"):
            values = proc.get(field)
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                match = COORD_RE.match(str(value).strip())
                if match:
                    coords.add(match.group(1))
    for value in (profile.get("data") or {}).values():
        client = value.get("client") if isinstance(value, dict) else None
        if isinstance(client, str):
            match = COORD_RE.match(client.strip())
            if match:
                coords.add(match.group(1))
    for coord in sorted(coords):
        add(coord, None, None, None)

    # 嵌入的 version.json 里的运行时 libraries 也会被安装器下载/校验
    for lib in (version_json or {}).get("libraries") or []:
        name = lib.get("name") or ""
        art = (lib.get("downloads") or {}).get("artifact") or {}
        if name:
            add(name, art.get("url"), art.get("sha1"), art.get("size"))

    return items


MIRRORABLE = (
    "https://maven.neoforged.net/releases/",
    "https://libraries.minecraft.net/",
    "https://files.minecraftforge.net/maven/",
    "https://maven.minecraftforge.net/",
)


def candidates(url: str) -> list[str]:
    """官方 URL → 候选列表；本机不可达的官方 host 放到最后。"""
    out: list[str] = []
    for base in MIRRORABLE:
        if url.startswith(base):
            out.append(f"{BMCLAPI}/maven/{url[len(base):]}")
            break
    if url not in out:
        out.append(url)
    return out


def fetch(session: requests.Session, root: str, item: tuple[str, str, str | None, int | None]) -> tuple[str, str]:
    rel, url, sha1, _size = item
    target = os.path.join(root, "libraries", rel.replace("/", os.sep))
    if os.path.exists(target) and sha1 and sha1_of(target) == sha1:
        return rel, "cached"
    os.makedirs(os.path.dirname(target), exist_ok=True)
    last_error = ""
    for candidate in candidates(url):
        try:
            resp = session.get(candidate, timeout=25, headers={"User-Agent": USER_AGENT})
            if resp.status_code == 200 and resp.content:
                actual = hashlib.sha1(resp.content).hexdigest()
                if sha1 and actual != sha1:
                    last_error = f"sha1 mismatch {candidate}"
                    continue
                with open(target, "wb") as handle:
                    handle.write(resp.content)
                return rel, "downloaded"
            last_error = f"http {resp.status_code} {candidate}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
    return rel, f"FAILED {last_error}"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    jar = os.path.abspath(argv[1])
    root = os.path.abspath(argv[2])
    run_installer = "--run" in argv
    os.makedirs(os.path.join(root, "libraries"), exist_ok=True)
    os.makedirs(os.path.join(root, "versions"), exist_ok=True)
    profiles = os.path.join(root, "launcher_profiles.json")
    if not os.path.exists(profiles):
        with open(profiles, "w", encoding="utf-8") as handle:
            json.dump({"profiles": {}, "settings": {}, "version": 3}, handle)

    with zipfile.ZipFile(jar) as zf:
        profile = json.loads(zf.read("install_profile.json"))
        version_json = None
        if "version.json" in zf.namelist():
            version_json = json.loads(zf.read("version.json"))
    items = collect(profile, version_json)
    print(f"artifacts to preseed: {len(items)}")
    by_host = collections.Counter()
    for _rel, url, _sha, _size in items:
        by_host[url.split("/")[2] if "://" in url else "none"] += 1
    print("hosts:", dict(by_host))

    started = time.time()
    results: dict[str, int] = collections.Counter()
    failures: list[tuple[str, str]] = []
    with requests.Session() as session:
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(fetch, session, root, item) for item in items]
            for index, future in enumerate(concurrent.futures.as_completed(futures), 1):
                rel, state = future.result()
                results[state.split(" ")[0]] += 1
                if state.startswith("FAILED"):
                    failures.append((rel, state))
                if index % 20 == 0 or index == len(futures):
                    print(f"  [{index}/{len(futures)}] {dict(results)} {time.time() - started:.1f}s", flush=True)
    print("preseed summary:", dict(results), f"{time.time() - started:.1f}s")
    for rel, state in failures[:10]:
        print("  FAILED:", rel, "|", state)

    if run_installer:
        java = r"C:\Program Files\Java\jdk-21.0.12\bin\java.exe"
        if not os.path.exists(java):
            java = "java"
        log = os.path.join(root, "installer.log")
        print(f"\n== running installer (java={java}) ==")
        started = time.time()
        with open(log, "wb") as handle:
            proc = subprocess.run(
                [java, "-Djava.awt.headless=true", "-jar", jar, "--installClient", root],
                stdout=handle,
                stderr=subprocess.STDOUT,
                timeout=1800,
            )
        print(f"exit={proc.returncode} elapsed={time.time() - started:.1f}s log={log}")
        with open(log, "r", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
        print("--- last 25 lines ---")
        for line in lines[-25:]:
            print(" ", line)

    versions = os.path.join(root, "versions")
    for name in sorted(os.listdir(versions)) if os.path.isdir(versions) else []:
        version_json = os.path.join(versions, name, f"{name}.json")
        print(f"version dir: {name} json={'yes' if os.path.exists(version_json) else 'no'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
