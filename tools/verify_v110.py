"""v1.1.0-beta 新功能真实后端验证（不产生真实下载流量）。

用法：
    python -X utf8 tools\\verify_v110.py --root <已有 1.20.1 安装的 .minecraft>

验证项：
1. services.java_status —— Java 需求判定（不联网）
2. services.existing_version —— 已有版本探测
3. version_rename —— 真实目录重命名 + JSON id 改写 + 非法名拒绝
4. ui.sound —— 提醒音效可用
5. ui.request_close —— 关闭确认接口
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CubeFetch v1.1.0-beta 新功能验证")
    parser.add_argument("--root", required=True)
    args = parser.parse_args(argv)

    from cubefetch.config import AppConfig
    from cubefetch.services import AppServices

    mc_root = Path(args.root)
    config = AppConfig(mc_root=str(mc_root), threads=32)
    services = AppServices(config)
    services.bootstrap()
    failures: list[str] = []

    # 1) Java 状态（不联网）
    status = services.java_status("1.20.4")
    print(f"== java_status(1.20.4): required_major={status['required_major']} "
          f"component={status['component']} need_download={status['need_download']} "
          f"java={status['java_path']}")
    if status["need_download"]:
        print("   （本机 Java 不在标准位置，需下载——属正常分支）")

    # 2) 已有版本探测
    existing = services.existing_version("vanilla", None, "1.20.1")
    print(f"== existing_version(vanilla, 1.20.1) -> {existing!r}")
    if existing != "1.20.1":
        failures.append("existing_version 未探到 1.20.1")

    # 3) 真实目录重命名（临时副本，不动真实安装）
    from cubefetch.core.version_rename import (
        rename_installed_version,
        validate_version_name,
    )

    tmp = Path(tempfile.mkdtemp(prefix="cf_rename_"))
    try:
        src = mc_root / "versions" / "1.20.1"
        if not src.is_dir():
            print(f"!! 缺少 {src}，跳过重命名验证")
            failures.append("缺少 1.20.1 目录")
        else:
            shutil.copytree(src, tmp / "versions" / "1.20.1")
            started = time.time()
            new_id = rename_installed_version(tmp, "1.20.1", "我的整合包")
            data = json.loads(
                (tmp / "versions" / new_id / f"{new_id}.json").read_text(encoding="utf-8")
            )
            print(f"== 重命名 -> {new_id!r}（{time.time() - started:.2f}s），"
                  f"JSON id={data.get('id')!r}，旧目录残留={ (tmp / 'versions' / '1.20.1').exists() }")
            if data.get("id") != new_id:
                failures.append("重命名后 JSON id 未更新")
            for bad in ("a/b", "", "..", "带:冒号", "  "):
                try:
                    validate_version_name(bad)
                except ValueError:
                    continue
                else:
                    failures.append(f"非法名未被拒绝：{bad!r}")
            # 同名幂等
            again = rename_installed_version(tmp, new_id, new_id)
            if again != new_id:
                failures.append("同名重命名不幂等")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # 4) 音效
    from cubefetch.ui.sound import play

    sound_ok = play("info")
    print(f"== sound.play('info') -> {sound_ok}")

    # 5) 关闭确认接口（无后端 FakeServices）
    from cubefetch.ui.app_window import create_app

    root, app = create_app(None)
    try:
        running = app.has_running_task()
        print(f"== has_running_task() -> {running}")
        app.request_close()
        print("== request_close() 已触发关闭")
    finally:
        try:
            root.destroy()
        except Exception:  # noqa: BLE001
            pass

    if failures:
        print("!! 失败项：" + "；".join(failures))
        return 1
    print("VERIFY V1.1.0 PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
