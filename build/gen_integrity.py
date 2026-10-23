"""生成 CubeFetch 文件完整性清单 ``cubefetch/integrity_manifest.py``。

用法（仓库根目录执行）::

    python -X utf8 build\\gen_integrity.py

实现说明：

- 扫描 ``cubefetch/**/*.py``（排除生成物 ``integrity_manifest.py`` 与 ``__pycache__``），
  产出 ``FILES``（相对仓库根路径 → 源码 sha256）与 ``CODE``（模块名 → 规范化
  code object 的 sha256，供 PyInstaller 冻结模式校验）。
- 只使用标准库；哈希/规范化实现复用 ``cubefetch.core.integrity``，保证构建侧与
  运行时校验口径完全一致。
- 幂等：同一份源码重复运行得到完全相同的清单内容（``BUILD_TIME`` 取源码最新
  mtime，而非当前时间；可用 ``SOURCE_DATE_EPOCH`` 覆盖）。

打包脚本 ``build/build.ps1`` 会在 PyInstaller 之前自动调用本脚本（清单必须与
最终入包的源码一致）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "cubefetch" / "integrity_manifest.py"

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from cubefetch.core.integrity import build_manifest, format_manifest_source  # noqa: E402


def main() -> int:
    try:
        manifest = build_manifest(ROOT)
    except Exception as exc:  # noqa: BLE001 - 构建期脚本：失败即明确报错退出
        print(f"生成文件完整性清单失败：{exc}", file=sys.stderr)
        return 1

    files = manifest["FILES"]
    code = manifest["CODE"]
    if not files or not code:
        print("生成文件完整性清单失败：未扫描到 cubefetch 包内源码", file=sys.stderr)
        return 1

    text = format_manifest_source(manifest)
    try:
        OUTPUT.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT.write_text(text, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"写入 {OUTPUT} 失败：{exc}", file=sys.stderr)
        return 1

    print(
        f"已生成 {OUTPUT}\n"
        f"  FILES={len(files)} 个源码文件，CODE={len(code)} 个模块，"
        f"BUILD_TIME={manifest['BUILD_TIME']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
