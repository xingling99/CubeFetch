# 启动器检测 / 版本隔离 / 模组加载器 调研笔记

本文档是 `cubefetch/core/detect.py`、`cubefetch/core/isolation.py`、`cubefetch/core/loader.py`
的实现依据，也是本机「启动器配置格式」的最终权威（Lead 确认 `docs/research/ecosystem.md`
的 A/B/C 节与本文互相印证；若两者冲突，以本文标注的源码/实测证据为准）。

标注约定：

- **VERIFIED**：看过启动器源码或本机真实文件/实测输出，键名与路径确定。
- **INFERRED**：由代码推断或二手资料，未在本机完整验证；实现上按保守策略处理。

调研时间：2026-10。本机环境：Windows 11，PCL2 便携版在 `D:\pcl2`，PCLCE 在 `D:\pcl2CE`，
`%APPDATA%\PCL\config.json` 与 `%APPDATA%\PCLCE\config.v1.json` 存在；本机未安装 HMCL
（HMCL 部分来自 GitHub 源码）。

---

## A. PCL2 / PCLCE（`kind = "pcl"`）

### A1. 配置文件位置与格式

| 文件 | 位置 | 作用 | 证据 |
| --- | --- | --- | --- |
| 启动器全局设置 | `<启动器目录>\PCL\Setup.ini` | `LaunchFolderSelect` / `LaunchFolders` / 隔离默认值 | VERIFIED：`Plain Craft Launcher 2\Modules\Base\ModBase.vb`（`FileName` 无 `:\` 时拼 `Paths.Base & "PCL\" & name & ".ini"`）、`Pages\PageSetup\Settings.vb` |
| 版本级设置 | `<mc_root>\versions\<id>\PCL\Setup.ini` | `VersionArgumentIndieV2` 等版本级键 | VERIFIED：`Settings.vb` 的 `WriteIni(Instance.PathVersion & "PCL\Setup.ini")` |
| PCL2 新配置 | `%APPDATA%\PCL\config.json` | 目前只有 `JavaList`、`JavaConfigVersion` | VERIFIED：本机真实文件（575 B） |
| PCLCE 配置 | `%APPDATA%\PCLCE\config.v1.json` | `LaunchFolders` 等 | VERIFIED：本机真实文件（3210 B） |

INI 格式（PCL 自研，无分节）：每行 `Key:Value`，按**第一个冒号**切分；重复键后者覆盖；
UTF-8（无 BOM 或带 BOM 均可读）；换行 CRLF 为主。实现见 `detect._read_pcl_ini` 与
`isolation._update_ini_file`（后者保留原换行风格和其它行）。

### A2. `.minecraft` 根目录键

- `LaunchFolderSelect`（字符串）：当前选中的 MC 根目录。**VERIFIED**：`Settings.vb` 中
  `New Setting("LaunchFolderSelect", "")`；`ModMinecraft.vb` 中
  `SetMcFolder(Settings.Get(Of String)("LaunchFolderSelect"))`。写入时把启动器目录替换成 `$`：
  `Settings.Set("LaunchFolderSelect", Value.Replace(Paths.Base, "$"))`。所以值可能是
  `$.minecraft\` 形式；`$` = 启动器 exe 所在目录（`Paths.Base`，带尾部 `\`）。
  本机真实值：`D:\pcl2\PCL\Setup.ini` 内容为 `LaunchFolderSelect:$.minecraft\r\n`（33 B）→
  实际根目录 `D:\pcl2\.minecraft\`。
- `LaunchFolders`（字符串）：多根目录列表，格式 `名称>路径|名称>路径`，`ModMinecraft.vb`
  用 `Split("|"c, True)` 解析（VERIFIED）。PCL2 把该键放在 **注册表**
  `HKCU\Software\PCL` 的 `LaunchFolders` 值里（`Settings.vb`: `Source:=Sources.Registry`）；
  PCLCE 则写在 `%APPDATA%\PCLCE\config.v1.json`。本机 PCLCE 真实值：
  `".minecraft>D:\\pcl2CE\\.minecraft\\|pcl2>D:\\pcl2\\.minecraft\\"`。
  PCL2 的注册表设置源 `HKCU\Software\PCL` 下每个值都是一个键（如本机
  `CacheDownloadFolder = D:\pcl2\PCL\MyDownload\`）；`MOD` 探测会从该值推断启动器目录
  （含 `PCL`/`PCLCE` 路径段的父目录），用于定位 `PCL\Setup.ini`。**INFERRED**（本机无该值实测，
  但由 `Settings.vb` 的注册表读写机制支持）。
- 兜底：若以上都没有，PCL 默认根目录为 `<启动器目录>\.minecraft`（**VERIFIED**：PCL 首次启动会
  在启动器目录下创建 `.minecraft\versions\`）。
- 目录有效性：PCL 认为「`<root>\versions\` 存在」或「目录名就是 `.minecraft`」的目录可用
  （**INFERRED**，来自 PCL 选择目录逻辑；实现里作为候选根过滤条件）。

### A3. 版本隔离键

版本级文件 `<root>\versions\<id>\PCL\Setup.ini`（VERIFIED）：

- `VersionArgumentIndieV2: True|False` —— 新键，**保存后以它为准**（True = 隔离）。
- `VersionArgumentIndie: 1|2` —— 旧键：`1` 强制隔离、`2` 强制不隔离；`0` 表示跟随全局。
  （`-1` 只在内存里表示未决定，不落盘。）
- 全局默认在 `<启动器目录>\PCL\Setup.ini` 的 `LaunchArgumentIndieV2`（整数）：`0` 全部不隔离、
  `1` 仅可装 Mod 的版本、`2` 仅非正式版、`3` 两者、其它/`4` 全部隔离（VERIFIED：
  `ModMinecraft.vb:242-277` 的判定逻辑）。
- 自动隔离：版本的 `mods\` 下有文件或 `saves\` 下有子目录时，PCL 启动时会视为需要隔离
  （VERIFIED，同段逻辑）。

本项目 `VersionIsolation.apply(..., enabled=True)` 写入：

```
VersionArgumentIndieV2:True
VersionArgumentIndie:1
```

关闭时写 `False` / `2`。写前备份原文件为 `Setup.ini.bak`（仅第一次），读改写保留其它键，
内容不变时完全不写盘（幂等）。`is_isolated` 的廉价判定：`VersionArgumentIndieV2` 为真即隔离；
没有该键时看 `VersionArgumentIndie == 1`。

### A4. 启动器检测

**VERIFIED**（源码 + 本机）：

- 便携版目录特征：`Plain Craft Launcher 2.exe`（本机 `D:\pcl2\Plain Craft Launcher 2.exe`，
  5750272 B）或 `PCL.exe` / `PCLCE.exe` / `Plain Craft Launcher.exe`，且同目录常有 `PCL\` 数据目录。
- `%APPDATA%\PCL\config.json` 存在 ⇒ 安装过 PCL2；`%APPDATA%\PCLCE\config.v1.json` 存在 ⇒
  PCLCE（同一个 `pcl` 家族）。
- 快捷方式 `%USERPROFILE%\Desktop\Plain Craft Launcher.lnk` 指向 `D:\pcl2\Plain Craft Launcher 2.exe`
  （本机实例，说明「桌面快捷方式」也是可用线索，但实现未解析 .lnk，只做目录扫描）。

实现策略（`detect.LauncherDetector`）：扫描固定盘符根目录（深度 1，限量）+ `%APPDATA%`
配置 + 注册表 `HKCU\Software\PCL` / `HKCU\Software\PCLCE`（Windows 且未禁用注册表时）。
PCL2 与 PCLCE 都归为 `kind="pcl"`，不当作两个启动器；PCLCE 命中时在 `reason` 里注明
「PCL2（社区版）」。

---

## B. HMCL（`kind = "hmcl"`）

### B1. 新版 HMCL（3.16+，2026 HEAD）—— VERIFIED（GitHub HMCL-dev/HMCL）

- `HMCL_LOCAL_HOME = <HMCL 所在目录>\.hmcl`（`Metadata.java`：`CURRENT_DIRECTORY.resolve(".hmcl")`）。
- 配置文件：
  - `.hmcl\config\launcher-settings.json`：`selectedGameDirectory`（UUID）等；
  - `.hmcl\config\game-directories.json`：`{"gameDirectories":[{"id":"<uuid>","path":"D:\\...\\.minecraft","name":"..."}]}`；
  - `.hmcl\config\game-settings.json`：全局游戏设置预设；
  - 旧文件 `.hmcl\settings.json`（`LEGACY_SETTINGS_LOCATION`）。
- 用户级目录 `%APPDATA%\hmcl`（`HMCL_USER_HOME`），默认 MC 目录 `%APPDATA%\.minecraft`。
- 版本级隔离文件 `<root>\versions\<id>\instance-game-settings.json`
  （`INSTANCE_GAME_SETTINGS_FILENAME`，schema `https://schemas.glavo.site/hmcl/instance-game-settings/1.0.1`）：
  - `runningDirectory`：`""` 表示继承根目录；隔离时 HMCL 解析为 `versions/<id>`；
  - `defaultIsolationType`：`ALWAYS` / `MODDED` / `NEVER`（默认 `MODDED`）；
  - 其它键：`parent`（预设 id）、`javaType`、`maxMemory`、`icon` 等。
- 生效条件：`runningDirectory` 为空 **且** 有效 `defaultIsolationType == "ALWAYS"` 才算隔离。
- 旧版 `hmclversion.cfg` 会被 HMCL 迁移：旧的「隔离」实例会迁移成
  `defaultIsolationType=ALWAYS` + `runningDirectory=""`。

### B2. 旧版 HMCL（< 3.16）—— VERIFIED（HMCL 3.5.9.263 源码）

- 配置文件 `hmcl.json`：查找顺序为 HMCL jar 所在目录、当前工作目录、exe 所在目录
  （`ConfigHolder.locateConfig`）；Linux 为 `.hmcl.json`；也有用户级 `%APPDATA%\hmcl\config.json`。
  结构：`configurations`（profile 名 → `{gameDir, selectedMinecraftVersion, ...}`）、
  `selectedProfile`；内置 profile `Default` / `Home`。
- 版本级文件 `versions/<id>/hmclversion.cfg`（`HMCLGameRepository.getVersionSettingPath`），
  内容是 **JSON**（旧代码用 JsonUtils 序列化 VersionSetting）：
  - `gameDirType`：`0` = ROOT_FOLDER（不隔离）、`1` = VERSION_FOLDER（隔离）、`2` = CUSTOM；
  - `gameDir`：CUSTOM 时的自定义目录；默认为空。
  —— 注意：网上有资料说该文件是 `.properties` 风格，**与源码不符**；实现按 JSON 读改写，
  遇到无法解析成 JSON 的旧文件时只提示、不覆盖（保守策略）。

### B3. 本项目 `VersionIsolation.apply` 对 HMCL 的写入

同时写两套（互不影响，HMCL 各自读取自己的文件）：

1. 新版 `versions/<id>/instance-game-settings.json`：
   `{"$schema": ".../instance-game-settings/1.0.1", "runningDirectory": "", "defaultIsolationType": "ALWAYS"}`
   （关闭时 `"NEVER"`）。
2. 旧版 `versions/<id>/hmclversion.cfg`：JSON `{"gameDirType": 1}`（关闭时 `0`）。

保护策略（均已单测）：

- 保留文件里已有的其它键（`parent`、`javaType`、`gameDir` 等）；
- 首次修改前备份 `<文件名>.bak`，之后不再覆盖备份；
- 已存在**非空自定义 `runningDirectory`** 时，启用隔离会跳过（只返回中文说明），避免破坏用户选择；
- 文件内容不是合法 JSON 时跳过并说明，绝不覆盖；
- 内容不变时不写盘（幂等）。

---

## C. 模组加载器端点与安装方式

### C1. Fabric（VERIFIED，官方 meta 实测可访问）

- 列表：`GET https://meta.fabricmc.net/v2/versions/loader/<mc_version>`。**注意真实响应是嵌套结构**
  （2026-10 实测，约 600 KB）：每项为
  `{"loader": {"version", "stable", "maven", "build", "separator"}, "intermediary": {...}, ...}`，
  版本号在 `loader.version`、稳定标记在 `loader.stable`，**不在顶层**。实现同时兼容扁平的
  `{version, stable}`（旧镜像/缓存格式）。BMCLAPI 镜像 `/fabric-meta/v2/...` 也返回同样的嵌套数组。
- Profile：`GET https://meta.fabricmc.net/v2/versions/loader/<mc>/<loader>/profile/json`，
  返回 `id = fabric-loader-<loader>-<mc>`、`inheritsFrom = <mc>`、mainClass
  `net.fabricmc.loader.impl.launch.knot.KnotClient`、libraries 的 `url` 指向
  `https://maven.fabricmc.net/`。
- 安装：把 profile JSON 写到 `versions/<profile_id>/<profile_id>.json`（本项目把 `id` 改写成
  `<mc>-fabric-<loader>` 以便和原版共存），再交给 `Installer.install()` 补齐原版 jar/库/资源。
  **不需要 installer jar，也不需要 Processor。**
- 推荐：列表稳定版优先、版本号倒序后的第一项（实测 1.20.1 推荐 `0.19.5`，0.20.x 是预发布）。

### C2. Quilt（VERIFIED，官方 meta）

- 列表：`GET https://meta.quiltmc.org/v3/versions/loader/<mc>`。真实响应同样是嵌套
  `{"loader": {...}}`（约 892 KB）；**列表项没有 `stable` 字段**，实现用版本号里的
  `alpha/beta/rc/pre/snapshot` 标记推断预发布，稳定版优先（实测 1.20.1 推荐 `0.30.1`）。
- 镜像：BMCLAPI `/quilt-meta/v3/...` 目前返回 404；`http.py` 的镜像规则表也没有 quilt 规则，
  因此 Quilt 走官方直连（`meta.quiltmc.org` 本机可访问）。
- Profile：`GET https://meta.quiltmc.org/v3/versions/loader/<mc>/<loader>/profile/json`；
  不存在的 loader 版本返回 HTTP 404（实现里会捕获成 `NetworkError`，由 UI 提示）。
- 安装同 Fabric，`profile_id = <mc>-quilt-<loader>`。

> 列表缓存：Fabric/Quilt 列表响应很大，`LoaderService` 在内存里缓存 5 分钟
> （`_LIST_CACHE_TTL`），避免 UI「先取列表、再取推荐」触发两次大请求；Forge promotions
> 同样缓存 5 分钟。缓存不参与安装正确性判定。

### C3. Forge（VERIFIED：官方 maven + 本机安装器实测）

- 推荐/最新：`https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json`，
  键为 `<mc>-recommended` / `<mc>-latest`（例：`1.20.1-recommended = 52.1.0`）。
- 全量版本：`https://maven.minecraftforge.net/net/minecraftforge/forge/maven-metadata.xml`，
  版本格式 `<mc>-<loader>`（例 `1.20.1-47.2.0`）。实测 1.20.1 列表 137 项，推荐 `47.4.28`。
- 安装器：`https://maven.minecraftforge.net/net/minecraftforge/forge/<mc>-<loader>/forge-<mc>-<loader>-installer.jar`。
- 安装命令（**VERIFIED，Lead 本机实测**）：
  ```
  java -Djava.awt.headless=true -jar forge-<mc>-<ver>-installer.jar --installClient <mc_root>
  ```
  在只有 `versions/` 与 `launcher_profiles.json` 的临时目录中成功生成
  `versions/1.20.1-forge-47.2.0/1.20.1-forge-47.2.0.json`
  （`id = 1.20.1-forge-47.2.0`，`inheritsFrom = 1.20.1`）。安装器会自行下载 vanilla jar 与
  libraries；官方回退时以安装器实际写出的版本 id 为准。
- **2026-10 起默认不再走这条官方安装器路径**（其 processor 内部硬编码的 launchermeta
  mojmap 下载在本机必挂，见 C6）；仅当 `LoaderService(..., use_official_installer=True)`
  或 `HttpSession(mirror="official")` 时作为回退。
- 注意：ecosystem.md 曾根据 Installer 源码认为 2026 版没有 `--installClient`（GUI-only），
  但本机实测该参数有效，故以实测为准（Lead 指令）。

### C4. NeoForge（VERIFIED：maven + installer 实测）

- Maven：`https://maven.neoforged.net/releases/net/neoforged/neoforge/maven-metadata.xml`
  （本机直连不稳定，`HttpSession` 会按镜像规则重写到
  `https://bmclapi2.bangbang93.com/maven/net/neoforged/...`）。
- 版本映射（MC 1.20.2+）：`<mc-minor>.<mc-patch>.<build>` → MC `1.<mc-minor>.<mc-patch>`，
  例：`21.1.264` ↔ MC `1.21.1`；`20.4.237` ↔ MC `1.20.4`。对无 patch 的 MC：`1.21` → `21.0.`。
  对 MC `26.3`（新版本号方案）：NeoForge 用 `26.3.0.<build>`（`<major>.<minor>.<build>`）。
- MC 1.20.1 是历史例外：artifact 在 `net/neoforged/forge/<mc>-<loader>`
  （例 `1.20.1-47.1.106`），installer 为
  `https://maven.neoforged.net/releases/net/neoforged/forge/<mc>-<loader>/forge-<mc>-<loader>-installer.jar`。
- 现代 installer：
  `https://maven.neoforged.net/releases/net/neoforged/neoforge/<ver>/neoforge-<ver>-installer.jar`，
  安装命令与 Forge 相同（`--installClient <mc_root>`）；实测生成的版本 id 可能是
  `neoforge-21.1.80`（而非预测的 `1.21.1-neoforge-21.1.80`），故实现扫描 `versions/*` 找
  实际生成、`inheritsFrom == <mc>` 的 json 并返回其 id。默认安装路径已改为自己跑
  processors（见 C6），此 id 规则在两条路径上都适用。

### C5. 官方安装器回退路径的执行细节（实现约定）

- 安装器 jar 缓存到 `<mc_root>\.cubefetch\installers\<jar 名>`，经 `HttpSession.download`
  下载（镜像规则自动生效）；重复安装先查已有 profile，命中则直接复用。
- 运行前确保 `<mc_root>\versions\` 存在，并在缺少时创建
  `launcher_profiles.json`（`{"profiles":{},"settings":{},"version":3}`，PCL/HMCL/安装器都认）。
- Java：优先用服务层传入的 `java_exe`，否则 `util.pick_java(17)` 再退回 `pick_java()`；
  找不到抛 `InstallError`。Java 25 可运行 1.20.1 Forge 安装器（Lead 实测）。
- 进程：`subprocess.Popen`，`-Djava.awt.headless=true`，工作目录为临时目录，
  `CREATE_NO_WINDOW`，stdout/stderr 合并逐行读取；关键行转 `Progress(message="正在安装 Forge：…")`；
  取消（`threading.Event`）会杀进程并抛 `CancelledError`；超时 1800 s；非 0 退出抛
  `InstallError`（附最后 10 行输出）。没有走 `util.run_process`，因为需要流式输出与可取消。

> 以上 C5 是**回退路径**（官方安装器）；CubeFetch 默认使用 C6 的「自己执行 processors」路径。

### C6. Forge/NeoForge 自己执行 processors（CubeFetch 实际采用）

**为什么不用官方 `--installClient`（VERIFIED，本机实测）：** 官方安装器的
`--mirror` 参数不会重写 processors 内部**硬编码**的下载地址。client 侧的
`DOWNLOAD_MOJMAPS` processor 会访问
`https://launchermeta.mojang.com/mc/game/version_manifest.json` 取映射表，而本机该连接
被 reset（`SocketException: Connection reset`），NeoForge 21.1.80 安装必挂于此步；
Forge 1.20.1 加 `--mirror` 偶尔成功属于网络碰巧，不可依赖。因此改为由 CubeFetch
自己解包 installer、预置依赖、执行 processors（见 `cubefetch/core/installer_processors.py`）。

**流程概要：**

1. 从 installer jar 读取 `install_profile.json`、嵌入的 `version.json`、各工具 jar 的
   `META-INF/MANIFEST.MF`；`install_profile.json` 的 `libraries` 与嵌入 `version.json` 的
   `libraries` 合并枚举（`name` → maven 路径/url/sha1/size），并把 processor `jar`/
   `classpath` 里**不带方括号的明文坐标**也补进待下载集合。
2. 预置 libraries 到 `<mc_root>/libraries/`：候选 URL 为
   `https://bmclapi2.bangbang93.com/maven/<maven path>`（首选，需 `HttpSession` 自带
   User-Agent；默认 python-requests UA 会 403）与官方 URL（兜底）；目标已存在且 sha1 匹配
   直接跳过；批量下载失败的条目逐文件多候选重试，仍失败的记入 `missing` 并跳过，不中断。
3. `install_profile.json` 的 `data` 值解析 + 占位符替换：`[group:artifact:version[:classifier][@ext]]`
   → `<mc_root>/libraries/<maven path>`；`/data/*.lzma` 等从 installer jar 解到
   `<mc_root>/.cubefetch/installers/<jarstem>.data/`；`{SIDE}`/`{ROOT}`/`{MINECRAFT_JAR}`/
   `{LIBRARY_DIR}`/`{MINECRAFT_VERSION}`/`{INSTALLER}` 等替换。运行前先准备好原版基础：
   写 `versions/<mc>/<mc>.json`、下载 `versions/<mc>/<mc>.jar`。
4. `DOWNLOAD_MOJMAPS` **不执行**：改用原版 version json 的
   `downloads.client_mappings`（含 url/sha1/size）经 `HttpSession` 下载（`auto` 镜像会把
   launchermeta 重写到 bmclapi），写入 `data["MOJMAPS"]` 解析出的目标路径。
5. 其余 `sides` 含 `client` 或为空的 processor 逐个执行：
   `java -cp "<processor.classpath 各 jar + 工具 jar>" <工具 jar MANIFEST 里的 Main-Class> <args…>`
   （`util.run_process`，超时 30 min，失败重试 1 次；`outputs` 里的 sha1 会校验）。
6. 校验最终产物 `data["PATCHED"]`（即 patched client jar）存在且非 0 字节，写
   `versions/<version.json["id"]>/<version.json["id"]>.json`（`profile_id` 取自版本 json 的
   `id`，不自己拼），然后把该版本交回 `Installer.install(InstallRequest(...))` 补齐
   继承的原版库与 assets。

**实测数据（2026-10-21，本机；脚本 `tools/validate_processors_install.py`，日志
`tools/neo_self_validate.log`、`tools/forge_self_validate.log`）：**

| 目标 | 版本 | 安装器 jar | processors | 总耗时 | 关键产物 |
| --- | --- | --- | --- | --- | --- |
| NeoForge | 21.1.80 + MC 1.21.1 | 6,819,067 B（49.3 s） | 140.3 s | 193.2 s | `versions/neoforge-21.1.80/neoforge-21.1.80.json`；`neoforge-21.1.80-client.jar` 5,561,321 B；vanilla jar 26,836,906 B |
| Forge | 47.2.0 + MC 1.20.1 | 5,973,950 B（21.0 s） | 40.2 s | 61.6 s | `versions/1.20.1-forge-47.2.0/1.20.1-forge-47.2.0.json`；patched client jar 4,791,112 B；vanilla jar 23,028,853 B |

两条都用 `C:\Program Files\Java\jdk-21.0.12\bin\java.exe` 执行（MC 1.21.1 需要 Java 21；
1.20.1 需要 17，但 `util.pick_java(17)` 返回 jdk-21，工具链正常；Java 25 也能跑）。

**代码入口与开关语义：**

- `cubefetch/core/installer_processors.py`：核心 API 为 `install_with_processors(...)`
  （端到端），内部用到 `read_installer` / `resolve_variables` / `preseed_libraries` /
  `download_mojmaps` / `run_client_processors` / `build_processor_command` / `read_main_class` /
  `required_java_major` 等。
- `LoaderService._install_with_processors`（`cubefetch/core/loader.py`）：下载/缓存安装器 jar、
  准备 `launcher_profiles.json`、拉原版 version json、调用上面的核心 API、校验版本 json，
  最后调 `Installer.install`。
- 开关：`LoaderService(http, installer, manifest, *, use_official_installer: bool | None = None)`。
  `None`（默认）= 自动：仅当 `http.mirror == "official"` 时走官方 `--installClient`，
  否则走 processors；显式 `True` 强制官方回退，显式 `False` 强制 processors。
  该参数是 keyword-only 且默认 `None`，现有 `LoaderService(self.http, installer, manifest)`
  调用无需修改。

**剩余风险：**

- `--full`（即 processors 之后 `Installer.install` 的完整 assets 下载）尚未做真实端到端验证
  （assets 数百 MB，沿用未改动的 vanilla 安装逻辑）；验证脚本默认只跑到版本 json + 产物校验。
- `_find_existing_profile` 在 processors 之前做幂等跳过：若同一 MC 版本下已存在**其它**
  loader 版本的 profile，按版本字符串匹配可能误判，需要更严格的 `inheritsFrom`+loader 匹配。
- Forge 的 `maven.minecraftforge.net` 没有 BMCLAPI 镜像规则（不在 `http.py` 的
  `MIRROR_RULES` 中），安装器 jar/libraries 走官方直连，本机偶发慢（本次 21 s 成功）；
  NeoForge 依赖库中 2 个因官方源超时重试多花约 90 s。
- 磁盘占用较大（两种 loader 的 libraries + 原版 base 各约数百 MB），重复验证前建议清理临时根。


---

## D. 已知限制 / TODO

1. **首次 Forge/NeoForge 安装仍有下载耗时**：processors 路径（C6）从 BMCLAPI 预置依赖、
   官方/镜像兜底，NeoForge 21.1.80 实测总耗时约 193 s（其中 2 个依赖库因官方源超时重试多花
   约 90 s）；官方安装器回退路径（C5）仍会从官方 CDN 下载 vanilla jar 与 libraries，
   网络差时可能超时（1800 s 上限）。
2. **PCL 注册表 `LaunchFolders`**：本机无该值，格式按源码 `名称>路径|` 解析（INFERRED 部分：
   注册表读写路径已 VERIFIED，具体值形态未在本机验证）。
3. **HMCL 新版未在本机安装**：所有 HMCL 结论来自 GitHub 源码；`hmcl.json` 的旧版 profile
   结构未做迁移写入（只读解析用于检测根目录）。
4. **版本隔离只写 PCL/HMCL**：其它启动器（MultiMC/Prism 等）不在契约范围；
   `VersionIsolation.apply` 对未知 kind 只返回说明、不写文件。
5. **`versions/<id>/<id>.json` 是硬要求**：PCL/HMCL 列版本都要求它存在；本项目安装流程
   全部满足（vanilla 由 Installer 写、fabric/quilt 自己写、forge/neoforge 由 processors 路径写，
   官方回退时由安装器写）。
6. **快照版本**：Forge/NeoForge 对快照基本无支持；NeoForge 的 MC 版本映射对
   `26.x` 之后的新方案按 `<major>.<minor>.0.` 推断（INFERRED），需要上线后按实际 maven 列表校正。
7. **NeoForge 元数据首次拉取慢**：v1.0.0 时 `http.py` 的 `auto` 镜像策略先试官方
   `maven.neoforged.net`（本机直连常超时），失败后才切 BMCLAPI；实测首次拉取约 80 s，
   之后命中 5 分钟缓存。**v1.0.1 起 `auto` 已改为镜像优先**，该场景首次拉取明显变快；
   用户仍可在设置里选 `official` 强制官方源。
8. **Fabric/Quilt 列表很大**：单次响应约 0.6–0.9 MB，首次约 13–16 s（含 TLS 建连）；
   已加 5 分钟内存缓存；UI 线程需在后台线程调用（服务层已按契约放在 worker 线程）。

## E. 实测记录（2026-10，本机）

```
LIST fabric 1.20.1: count=255 top=[('0.19.5', True), ('0.20.1', False), ...] rec=0.19.5
  PREPARE fabric 0.19.5: id=1.20.1-fabric-0.19.5 inheritsFrom=1.20.1 libs=8
LIST quilt  1.20.1: count=307 top=[('0.30.1', True), ...] rec=0.30.1
  PREPARE quilt 0.30.1: id=1.20.1-quilt-0.30.1 inheritsFrom=1.20.1 libs=11
LIST forge  1.20.1: count=137 top=['47.4.28', '47.4.27', ...] rec=47.4.28
LIST neoforge 1.21.1: count=259 top=['21.1.264', ...] rec=21.1.264 (约 81 s，见 D7)
LIST neoforge 1.20.1: count=62 top=['47.1.106', ...] rec=47.1.106
```

## F. 相关测试

- `tests/test_detect.py`：临时目录伪造 PCL/HMCL 配置，覆盖单/双/无启动器、PCLCE、损坏 JSON、
  版本计数、INI/`$`/`LaunchFolders` 解析。
- `tests/test_isolation.py`：PCL/HMCL 写入、幂等、`.bak`、保留原键、自定义目录/损坏文件跳过、
  非法版本号/未安装启动器跳过。
- `tests/test_loader.py`：离线 fixture 验证列表解析/排序/推荐/prepare；安装流程用假
  `HttpSession`/`Installer`/`Manifest` 覆盖 vanilla/fabric/forge/neoforge 的 URL、缓存、
  `launcher_profiles.json`、幂等与取消；官方安装器路径用 mock 的 `_run_java_installer`，
  processors 路径用 mock 的 `installer_processors.install_with_processors`，不联网、不跑 Java。
- `tests/test_installer_processors.py`：用内存 zip 伪造最小 installer jar（含
  `install_profile.json`、`version.json`、`data/client.lzma`、工具 jar 的 MANIFEST），
  覆盖 maven 坐标解析、data/占位符、processor 过滤、`DOWNLOAD_MOJMAPS` 替代下载、
  `java -cp` 命令行拼装、产物 sha1 校验、libraries 预置与 404 容忍、端到端写版本 json 与
  PATCHED 校验。
- `tools/validate_processors_install.py`：真实网络验证脚本（可选用 `--full` 继续跑
  `Installer.install`），实测日志见 `tools/neo_self_validate.log`、`tools/forge_self_validate.log`。
