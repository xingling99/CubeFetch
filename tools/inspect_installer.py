"""开发期工具：检查 Forge/NeoForge 安装器 jar 的 install_profile.json。

用法：
    python -X utf8 tools/inspect_installer.py <path-to-installer.jar>

只读：不修改任何文件，打印结构统计并探测 BMCLAPI 对各类下载主机的映射可用性。
"""

from __future__ import annotations

import collections
import json
import sys
import urllib.request
import zipfile

USER_AGENT = "CubeFetch/1.0 (+https://github.com/)"
BMCLAPI = "https://bmclapi2.bangbang93.com"


def probe(url: str, timeout: float = 20.0) -> str:
    """GET Range 0-0，返回 '200'/'206'/'302'/'404'/'ERR:<exc>'。"""
    req = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Range": "bytes=0-0"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return str(resp.status)
    except Exception as exc:  # noqa: BLE001 - 探测工具需要吞掉所有异常
        return f"ERR:{type(exc).__name__}"


def mirror_candidates(url: str) -> list[str]:
    """给定安装器里的官方 URL，给出 BMCLAPI 候选 URL。"""
    out: list[str] = []
    for base in (
        "https://maven.neoforged.net/releases/",
        "https://maven.minecraftforge.net/",
        "https://libraries.minecraft.net/",
        "https://files.minecraftforge.net/maven/",
    ):
        if url.startswith(base):
            rest = url[len(base):]
            out.append(f"{BMCLAPI}/maven/{rest}")
            out.append(f"{BMCLAPI}/libraries/{rest}")
            break
    return out


def maven_path(name: str) -> str:
    """把 Maven 坐标转成相对路径：group:artifact:version[:classifier][@ext]。"""
    ext = "jar"
    if "@" in name:
        name, ext = name.split("@", 1)
    parts = name.split(":")
    group, artifact, version = parts[0], parts[1], parts[2]
    classifier = parts[3] if len(parts) > 3 else None
    filename = f"{artifact}-{version}"
    if classifier:
        filename += f"-{classifier}"
    filename += f".{ext}"
    return "/".join(group.split(".") + [artifact, version, filename])


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    jar = argv[1]
    with zipfile.ZipFile(jar) as zf:
        names = zf.namelist()
        print(f"jar entries: {len(names)}")
        for key in ("install_profile.json", "version.json", "META-INF/MANIFEST.MF"):
            print(f"  {key}: {'yes' if key in names else 'no'}")
        profile = json.loads(zf.read("install_profile.json"))
        embedded = json.loads(zf.read("version.json")) if "version.json" in names else None

    print("\n== install_profile keys ==")
    print(" ", list(profile.keys()))
    print("  spec:", profile.get("spec"), "| minecraft:", profile.get("minecraft"))
    print("  profile:", profile.get("profile"))
    print("  hideExtract:", profile.get("hideExtract"), "| serverJarPath:", profile.get("serverJarPath"))

    libs = profile.get("libraries") or []
    hosts = collections.Counter()
    missing_url: list[str] = []
    for lib in libs:
        art = (lib.get("downloads") or {}).get("artifact") or {}
        url = art.get("url") or ""
        if "://" in url:
            hosts[url.split("/")[2]] += 1
        else:
            missing_url.append(lib.get("name", "?"))
    print(f"\n== libraries ({len(libs)}) ==")
    print("  hosts:", dict(hosts))
    print("  libs without artifact url:", len(missing_url), missing_url[:5])
    print("  clientreq flags:", dict(collections.Counter(lib.get("clientreq") for lib in libs)))
    print("  first 5:", [lib.get("name") for lib in libs[:5]])

    data = profile.get("data") or {}
    print(f"\n== data ({len(data)}) ==")
    for key, value in data.items():
        print(f"  {key}: {value}")

    procs = profile.get("processors") or []
    print(f"\n== processors ({len(procs)}) ==")
    for idx, proc in enumerate(procs):
        print(
            f"  [{idx}] sides={proc.get('sides')} jar={proc.get('jar')} "
            f"classpath={len(proc.get('classpath') or [])} "
            f"args={proc.get('args')} "
            f"outputs={list((proc.get('outputs') or {}).keys())} "
            f"jvm={proc.get('jvm')}"
        )

    if embedded:
        print("\n== embedded version.json ==")
        print("  id:", embedded.get("id"), "| inheritsFrom:", embedded.get("inheritsFrom"))
        print("  mainClass:", embedded.get("mainClass"), "| type:", embedded.get("type"))
        print("  libraries:", len(embedded.get("libraries") or []))

    # 探测 BMCLAPI 映射
    print("\n== BMCLAPI mapping probes ==")
    seen: set[str] = set()
    probes: list[tuple[str, list[str]]] = []
    for lib in libs:
        art = (lib.get("downloads") or {}).get("artifact") or {}
        url = art.get("url") or ""
        if url and url not in seen:
            seen.add(url)
        # 只探测每个 host 的首个 URL
    by_host: dict[str, str] = {}
    for url in seen:
        host = url.split("/")[2]
        by_host.setdefault(host, url)
    for key, value in data.items():
        coord = value[0] if isinstance(value, list) and value else None
        if isinstance(coord, str) and ":" in coord:
            host = "maven.neoforged.net(releases)"
            by_host.setdefault(host, f"https://maven.neoforged.net/releases/{maven_path(coord)}")
    for host, url in by_host.items():
        print(f"  host {host}: {url}")
        for candidate in mirror_candidates(url):
            print(f"    -> {candidate} : {probe(candidate)}")

    # 数据条目（客户端侧）单独列出，确认是否都是 maven 坐标
    print("\n== data artifacts (client side) ==")
    for key, value in data.items():
        coord = value[0] if isinstance(value, list) and value else None
        if isinstance(coord, str) and ":" in coord:
            print(f"  {key}: {maven_path(coord)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
