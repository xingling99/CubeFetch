# Third-party Minecraft launcher ecosystem — integration spec for a Windows downloader

Research date: **2026-10-20** (all live probes run from this machine on that date).
Scope: PCL2 config/isolation/version-detection, HMCL config/isolation/version-detection,
Fabric/Quilt/Forge/NeoForge installation, BMCLAPI mirror rules, April Fools version list.

Legend: **[V]** = verified against live HTTP response or quoted source; **[I]** = inferred
(from code structure / partial evidence) — treat as needing a runtime check.

Machine-readable key summary (for the downloader implementation):

```jsonc
{
  "pcl": {
    "global_ini": "<PCL.exe dir>\\PCL\\Setup.ini",     // sectionless "Key:Value" lines
    "root_key": "LaunchFolderSelect",                   // value: absolute path, trailing '\\', '$' = PCL dir
    "extra_roots_key": "LaunchFolders",                 // "Name>Path|Name>Path"
    "isolation_global_key": "LaunchArgumentIndieV2",    // int, default 4
    "isolation_version_key": "VersionArgumentIndieV2",  // bool, per versions\\<id>\\PCL\\Setup.ini
    "isolation_version_legacy_key": "VersionArgumentIndie", // int: -1 undecided, 0 global, 1 on, 2 off
    "root_valid_requires": ["versions\\"],
    "version_valid_requires": ["versions\\<id>\\<id>.json"]
  },
  "hmcl": {
    "local_home": "<cwd>\\.hmcl",                       // config/, state/, cache/, private/
    "user_home": "%APPDATA%\\hmcl",                     // user-settings.json etc.
    "roots_file": "<local_home>\\config\\game-directories.json",
    "settings_file": "<local_home>\\config\\launcher-settings.json",
    "root_ref_key": "selectedGameDirectory",            // UUID of entry in game-directories.json
    "instance_settings": "<root>\\versions\\<id>\\instance-game-settings.json",
    "isolation_key": "runningDirectory",                // "" = inherit; absolute path = isolated
    "isolation_default_key": "defaultIsolationType",    // ALWAYS | MODDED | NEVER (default MODDED)
    "legacy_global": "<local_home>\\hmcl.json",          // keys: configurations{}, selectedProfile
    "legacy_instance": "<root>\\versions\\<id>\\hmclversion.cfg"
  }
}
```

---

## A. PCL2 (Plain Craft Launcher 2, Meloong-Git/PCL, VB.NET)

Source files read (raw.githubusercontent.com/HMCL… no — Meloong-Git/PCL, `HEAD`):

- `Plain Craft Launcher 2/Modules/Minecraft/ModMinecraft.vb`
- `Plain Craft Launcher 2/Modules/Base/ModBase.vb`
- `Plain Craft Launcher 2/Modules/Minecraft/ModDownload.vb`
- `Plain Craft Launcher 2/Modules/Minecraft/ModLaunch.vb`
- `Plain Craft Launcher 2/Pages/PageSetup/Settings.vb`
- `Plain Craft Launcher 2/Modules/ModMain.vb`

### A1. Global config file and Minecraft-root key  [V]

- Global INI lives in the launcher folder: `Paths.Base & "PCL\"`, i.e. **`<PCL.exe dir>\PCL\Setup.ini`**.
  `ModBase.vb:476` (`IniClearCache`), `:487` (`IniGetContent`), `:561` (`IniSetContent`):
  `If Not FileName.Contains(":\") Then FileName = $"{Paths.Base}PCL\{FileName}.ini"`.
- Format is **sectionless** `Key:Value` lines, not real INI sections. `ModBase.vb` `IniGetContent`:
  `Dim Index As Integer = Line.IndexOfF(":")` then `Ini(Line.Substring(0, Index)) = Line.Substring(Index + 1)`
  — split on the **first** colon, no `[section]` support, duplicate keys allowed (last wins).
- **Root folder key = `LaunchFolderSelect`** (`Settings.vb:88`: `New Setting("LaunchFolderSelect", "")`;
  `ModMinecraft.vb:10`: `SetMcFolder(Settings.Get(Of String)("LaunchFolderSelect"))`).
  - Value format: absolute Windows path, **normalized with `PathUtils.AddSlashSuffix(PathUtils.ForCompare(...))`
    so it always ends with `\`** (`ModMinecraft.vb:20`), and a leading `$` token is expanded to `Paths.Base`
    (`ModMinecraft.vb:21`: `Settings.Set("LaunchFolderSelect", Value.Replace(Paths.Base, "$"))`;
    `:18`: `Value.Replace("$", Paths.Base)`). No quotes are written.
  - Example: `LaunchFolderSelect:D:\Games\MC\.minecraft\` (or `LaunchFolderSelect:$\.minecraft\`).
- Extra user-added roots: **`LaunchFolders`**, a single string of `Name>Location` entries joined by `|`
  (`ModMinecraft.vb:89` splits `Settings.Get(Of String)("LaunchFolders")` on `"|"c`, each entry `Name>Path`;
  `:117`: `NewSetup.Add(Folder.Name & ">" & Folder.Location)`; `:120` writes it back). Only non-vanilla
  folder types are stored here; the auto-detected "current folder" / official launcher folder are recomputed.
- Which folder was last launched: `<root>\PCL.ini`, key `Version` (`ModMinecraft.vb:189`:
  `WriteIni(McFolderSelected & "PCL.ini", "Version", If(value?.Name, ""))`).
- Per-instance INI: `<root>\versions\<id>\PCL\Setup.ini` (written/read throughout `ModMinecraft.vb`,
  e.g. `:700-712`, `:1057-1062`; write path in `Settings.vb:271`:
  `WriteIni(Instance.PathVersion & "PCL\Setup.ini", Key, Value)`).
  Keys seen: `State`, `Info`, `CustomInfo`, `Logo`, `LogoCustom`, `IsStar`, `DisplayType`, `ReleaseTime`,
  `VersionFabric`, `VersionOptiFine`, `VersionLiteLoader`, `VersionForge`, `VersionNeoForge`,
  `VersionVanillaName`, `VersionVanilla`.
- Fallback scratch dir when paths are non-ASCII: `%ProgramData%\PCL\` (`ModBase.vb:794-800`, `PathPure`).

### A2. Version isolation (版本隔离)  [V]

Two distinct settings, global default + per-version override:

| Scope | File | Key | Type | Values |
|---|---|---|---|---|
| Global default | `<PCL dir>\PCL\Setup.ini` | `LaunchArgumentIndieV2` | int | `0` off, `1` only moddable, `2` only non-release, `3` non-release **or** moddable, `else` (default **4**) all |
| Per version | `<root>\versions\<id>\PCL\Setup.ini` | `VersionArgumentIndieV2` | bool | `true` = isolated, `false` = not |
| Per version (legacy) | `<root>\versions\<id>\PCL\Setup.ini` | `VersionArgumentIndie` | int | `-1` undecided, `0` follow global, `1` forced on, `2` forced off |

Defaults declared in `Settings.vb`:
`New Setting("LaunchArgumentIndie", 0)`, `New Setting("LaunchArgumentIndieV2", 4)`,
`New Setting("VersionArgumentIndie", -1, Source:=Sources.Instance)`,
`New Setting("VersionArgumentIndieV2", False, Source:=Sources.Instance)`.

Decision logic, `ModMinecraft.vb:242-275` (`PathIndie` getter):

```vb
If Not Settings.HasSaved("VersionArgumentIndieV2", Instance:=Me) Then
    If Settings.HasSaved("VersionArgumentIndie", Instance:=Me) AndAlso
       Settings.Get(Of Integer)("VersionArgumentIndie", Instance:=Me) > 0 Then
        ' legacy migration: 1 => isolated
        Return Settings.Get(Of Integer)("VersionArgumentIndie", Instance:=Me) = 1
    End If
    ' auto-on if <version>\mods\ has files or <version>\saves\ has subdirectories
    If (ModFolder.Exists AndAlso ModFolder.EnumerateFiles.Any) OrElse
       (SaveFolder.Exists AndAlso SaveFolder.EnumerateDirectories.Any) Then Return True
    Select Case Settings.Get(Of Integer)("LaunchArgumentIndieV2")   ' global default
        Case 0 : Return False
        Case 1 : Return Modable
        Case 2 : Return Not IsRelease
        Case 3 : Return Not IsRelease OrElse Modable
        Case Else : Return True
    End Select
End If
Return If(Settings.Get(Of Boolean)("VersionArgumentIndieV2", Instance:=Me), PathVersion, McFolderSelected)
```

So the effective game directory is `PathVersion` (= `<root>versions\<id>\`, trailing `\`) when isolated,
otherwise `McFolderSelected` (= the root, trailing `\`). `ModLaunch.vb:2154` logs
`"版本隔离：" & (McInstanceSelected.PathIndie = McInstanceSelected.PathVersion)`.

**Cheap detection rule for a foreign tool:** a version is isolated iff
`versions\<id>\PCL\Setup.ini` contains `VersionArgumentIndieV2:True`; if that key is absent, PCL
computes it on first launch and *writes it back*, so the key becomes authoritative after one PCL run.
Absent + `mods/` or `saves/` present under `versions\<id>\` ⇒ PCL will treat it as isolated.

### A3. What PCL considers a valid Minecraft root  [V]

`ModMinecraft.vb:66-81` (`McFolderListLoadSub`):

```vb
If DirectoryUtils.Exists(Paths.Base & "versions\") Then ... ' current folder
If DirectoryUtils.Exists(Folder.FullName & "versions\") OrElse Folder.Name = ".minecraft" Then ...
If ... DirectoryUtils.Exists(MojangPath & "versions\") Then ... ' official launcher folder
```

- A candidate root is accepted when **`<root>\versions\` exists** (a folder literally named `.minecraft`
  is also accepted even without `versions\`).
- PCL **creates the missing glue file itself**: `McFolderLauncherProfilesJsonCreate(Folder)` writes
  `<root>\launcher_profiles.json` when absent (`ModMinecraft.vb:146-163`), encoded **GB18030**.
- If no root is found at all, PCL creates `Paths.Base & ".minecraft\versions\"` (`:126`).
- `launcher_profiles.json` is therefore **not** a validity requirement for PCL; `versions\` is.

### A4. What PCL needs inside `versions\<id>\`  [V]

`McInstance.Load` reads `<version dir>\<name>.json` (`ModMinecraft.vb:358`):

```vb
Dim JsonPath As String = PathVersion & Name & ".json"
For Each JsonCandidatePath In DirectoryUtils.EnumerateFiles(PathVersion, searchPattern:="*.json") ...
Throw New Exception($"未找到版本 JSON 文件：{PathVersion}{Name}.json")
```

- Required: **`versions\<id>\<id>.json`** where `<id>` equals the folder name.
- `inheritsFrom` supported and merged: `ModMinecraft.vb:455` removes `inheritsFrom` after merging parent
  `libraries` (`:474-479`); the parent instance is loaded from `<root>versions\<inheritsFrom>\` (`:1619`).
- Client jar `<id>.jar` is **not** required to *list* the version — PCL lists instances from the JSON and
  only complains at launch. **[I]** (jar existence is checked in the launch path, not the list path).
- PCL tolerates a JSON that is not named after the folder (it enumerates `*.json` as candidates,
  `:362`), but the canonical layout `<id>\<id>.json` is what it writes.

---

## B. HMCL (HMCL-dev/HMCL, Java, 2026 HEAD)

Note: HMCL's config layout was **restructured in 2026** ("before HMCL 3.16" used the legacy files).
Support both: read the new `config/*.json`, fall back to legacy `hmcl.json` / `hmclversion.cfg`.

Sources read: `HMCL/src/main/java/org/jackhuang/hmcl/Metadata.java`,
`HMCL/src/main/java/org/jackhuang/hmcl/setting/SettingsManager.java`,
`.../setting/LauncherSettings.java`, `.../setting/GameSettings.java`, `.../setting/GameDirectories.java`,
`.../setting/GameDirectoryManager.java`, `.../setting/DefaultIsolationType.java`,
`.../setting/LegacyConfigMigrator.java`, `.../setting/LegacyGameSettingsMigrator.java`,
`.../ui/instances/Instances.java`, `HMCLCore/.../game/GameInstanceManifest.java`,
`HMCLCore/.../game/GameRepository.java`, and the JSON schemas under `docs/schemas/`.

### B1. Config locations and the Minecraft-root key  [V]

- `Metadata.HMCL_LOCAL_HOME` = `CURRENT_DIRECTORY.resolve(".hmcl")` — i.e. **`<HMCL dir>\.hmcl`**
  (overridable via `-Dhmcl.dir` / `HMCL_LOCAL_HOME`). Sub-dirs (`SettingsManager.java:72-146`):
  `config/`, `state/`, `cache/`, `private/`.
- `Metadata.HMCL_USER_HOME` = `OperatingSystem.getWorkingDirectory("hmcl")` — on Windows that is
  `%APPDATA%\hmcl` (per-user, shared across HMCL installs).
- `Metadata.MINECRAFT_DIRECTORY` = `OperatingSystem.getWorkingDirectory("minecraft")` → `%APPDATA%\.minecraft`.
- **Root(s) live in `config/game-directories.json`** (`SettingsManager.java:122`,
  `LOCAL_CONFIG_FILES_DIRECTORY.resolve("game-directories.json")`; user-level variant
  `config/user-game-directories.json`, `:126`). Shape (from `GameDirectories` + `LegacyConfigMigrator`):

```jsonc
// <HMCL>\.hmcl\config\game-directories.json
{
  "$schema": "https://schemas.glavo.site/hmcl/game-directories/1.0.1",
  "gameDirectories": [
    { "id": "3f2c…uuid…", "path": "D:\\Games\\MC\\.minecraft", "name": "My pack folder" }
    // built-ins: DEFAULT_GAME_DIRECTORY_ID -> ".minecraft" (relative to cwd),
    //            HOME_GAME_DIRECTORY_ID    -> %APPDATA%\.minecraft
  ]
}
```

  Legacy per-profile key was `gameDir` and is migrated to `path` verbatim
  (`LegacyConfigMigrator.java:1068`: `JsonElement legacyGameDir = migrated.remove("gameDir"); … migrated.add("path", legacyGameDir)`).
- **Which root is selected: `config/launcher-settings.json` → `selectedGameDirectory`** (a
  `GameDirectoryID` UUID that references `game-directories.json`), plus `selectedInstance`
  (map `gameDirectoryId → versionId`) and `defaultGameSettingsPreset`
  (`LauncherSettings.java:68,71,74`; schema `launcher-settings/1.0.0`,
  `$schema: https://schemas.glavo.site/hmcl/launcher-settings/1.0.0`):

```jsonc
// <HMCL>\.hmcl\config\launcher-settings.json
{
  "$schema": "https://schemas.glavo.site/hmcl/launcher-settings/1.0.0",
  "selectedGameDirectory": "3f2c…uuid…",
  "selectedInstance": { "3f2c…uuid…": "1.21.4-forge-54.1.22" },
  "defaultGameSettingsPreset": "…uuid…"
}
```

- Legacy global config (HMCL < 3.16): **`<HMCL dir>\.hmcl\hmcl.json`** on Windows and **`.hmcl.json`** on
  Linux (`LegacyConfigMigrator.java:85,88`: `LEGACY_CONFIG_FILENAME = "hmcl.json"`,
  `LEGACY_CONFIG_FILENAME_LINUX = ".hmcl.json"`). Structure: top-level `configurations` object
  (profile name → `{ "gameDir": "...", "selectedMinecraftVersion": "...", "global": {...} }`) and
  `selectedProfile` (`:1041-1045`, `:1209-1210`, `:1232`). Built-in profile names are literally
  `"Default"` and `"Home"` (`:79,82`). User-level legacy settings: `%APPDATA%\hmcl\config.json`
  (`:91`), accounts `%APPDATA%\hmcl\accounts.json` (`:98`).

### B2. HMCL version isolation  [V]

Per-instance settings file: **`<root>\versions\<id>\instance-game-settings.json`**
(`LegacyGameSettingsMigrator.java:61`: `INSTANCE_GAME_SETTINGS_FILENAME = "instance-game-settings.json"`;
legacy: `:45` `LEGACY_INSTANCE_SETTINGS_FILENAME = "hmclversion.cfg"`).

Schema: `https://schemas.glavo.site/hmcl/instance-game-settings/1.0.1`
(`docs/schemas/instance-game-settings/1.0.1.json`, live 200).

```jsonc
// <root>\versions\1.21.4-forge-54.1.22\instance-game-settings.json
{
  "$schema": "https://schemas.glavo.site/hmcl/instance-game-settings/1.0.1",
  "parent": "…gameSettingsPresetId (optional)…",
  "runningDirectory": "",              // "" = inherit root; absolute path = isolated dir
  "defaultIsolationType": "ALWAYS",    // ALWAYS | MODDED | NEVER  (GameSettings.java:224)
  "javaType": "AUTO", "maxMemory": 4096, "icon": "FORGE"
}
```

- The effective "isolate this version" condition is
  `runningDirectory` blank **and** effective `defaultIsolationType == ALWAYS`
  (`LegacyConfigMigrator.java:1346-1347`, `:229-230`): when migrating an old isolated instance HMCL
  sets `defaultIsolationType = ALWAYS` (`:119`) and leaves `runningDirectory` empty so that the
  isolated dir resolves to `versions\<id>\`; when the old instance had a **custom** dir it instead
  writes that path into `runningDirectory` (`:166`, `:318`).
- Global default: `defaultIsolationType` is a normal `GameSettings` property, default
  **`MODDED`** (`GameSettings.java:224-225`:
  `newSettingProperty("defaultIsolationType", DefaultIsolationType.MODDED)`), stored with the
  game-settings preset in `config/game-settings.json`. Enum values: `ALWAYS` = "isolate all instances",
  `MODDED` = "isolate instances with any modloader", `NEVER` = "no isolation"
  (`DefaultIsolationType.java:21-30`).
- Legacy per-version `hmclversion.cfg` is a Java `.properties`-style file; the isolation-relevant
  legacy keys are `gameDirType` (ROOT_FOLDER vs CUSTOM) and `runningDirectory`
  (`LegacyGameSettingsMigrator.java:318`), plus the usual `javaType` / `maxMemory` / `permSize` etc.

### B3. What HMCL requires in `versions\<id>\` to list a version  [V]/[I]

- Instances are enumerated from `<root>\versions\*`; each is a `GameInstance` backed by a
  `GameInstanceManifest` loaded from **`versions\<id>\<id>.json`** (`GameRepository.getInstanceManifest`
  throws `NoSuchGameInstanceException` when not in the index; `GameInstanceManifest` is the version JSON
  model). So `<id>.json` is the hard requirement, same as PCL.
- Client jar `<id>.jar`: **[I]** not required to list (HMCL's launcher lists the version and reports
  missing/`-1` jar size as an incomplete instance rather than hiding it). Verify at runtime before
  relying on it.
- `inheritsFrom` must resolve to another installed `<id>` or to a Mojang version, exactly like PCL.

---

## C. Mod-loader installation (2026 state)

### C1. Fabric  [V, live 2026-10-20]

Endpoints (all `GET`, JSON):

| Purpose | URL |
|---|---|
| Loader versions | `https://meta.fabricmc.net/v2/versions/loader` → `[{separator,build,maven,version,stable}]`, e.g. `{"maven":"net.fabricmc:fabric-loader:0.20.1","version":"0.20.1","stable":false}` |
| Game versions | `https://meta.fabricmc.net/v2/versions/game` → `[{"version":"26.4-snapshot-4","stable":false}, …]` |
| Profile JSON | `https://meta.fabricmc.net/v2/versions/loader/<game>/<loader>/profile/json` |
| Installer-json list | `https://meta.fabricmc.net/v2/versions/loader/<game>/<loader>/profile/json` (same) |
| Yarn mappings | `https://meta.fabricmc.net/v2/versions/yarn/<game>` |
| Mirror (BMCLAPI) | `https://bmclapi2.bangbang93.com/fabric-meta/v2/...` (same paths; verified 200) |

`/v2/versions/loader/1.21.4/0.16.10/profile/json` returned exactly:

```json
{
  "id": "fabric-loader-0.16.10-1.21.4",
  "inheritsFrom": "1.21.4",
  "releaseTime": "…",
  "time": "…",
  "type": "release",
  "mainClass": "net.fabricmc.loader.impl.launch.knot.KnotClient",
  "arguments": { "game": [], "jvm": [ … ] },
  "libraries": [
    { "name": "org.ow2.asm:asm:9.7.1", "url": "https://maven.fabricmc.net/" },
    { "name": "org.ow2.asm:asm-analysis:9.7.1", "url": "https://maven.fabricmc.net/" },
    { "name": "org.ow2.asm:asm-commons:9.7.1",  "url": "https://maven.fabricmc.net/" },
    { "name": "org.ow2.asm:asm-tree:9.7.1",     "url": "https://maven.fabricmc.net/" },
    { "name": "org.ow2.asm:asm-util:9.7.1",     "url": "https://maven.fabricmc.net/" },
    { "name": "net.fabricmc:sponge-mixin:0.15.4+mixin.0.8.7", "url": "https://maven.fabricmc.net/" },
    { "name": "net.fabricmc:intermediary:1.21.4", "url": "https://maven.fabricmc.net/" },
    { "name": "net.fabricmc:fabric-loader:0.16.10", "url": "https://maven.fabricmc.net/" }
  ]
}
```

Installation = write this object to `versions\fabric-loader-<loader>-<game>\fabric-loader-<loader>-<game>.json`,
download the listed libraries into `libraries\`, done. No processors, no jar patching.

### C2. Quilt  [V, live 2026-10-20]

| Purpose | URL |
|---|---|
| Loader versions | `https://meta.quiltmc.org/v3/versions/loader` → `[{"maven":"org.quiltmc:quilt-loader:0.31.0-beta.4","version":"0.31.0-beta.4","build":4,…}]` |
| Game versions | `https://meta.quiltmc.org/v3/versions/game` (top entry was `26.3`) |
| Profile JSON | `https://meta.quiltmc.org/v3/versions/loader/<game>/<loader>/profile/json` |
| Mirror | `https://bmclapi2.bangbang93.com/quilt-meta/v3/...` (verified 200) |

`/v3/versions/loader/1.21.4/0.31.0-beta.4/profile/json`:

```json
{
  "id": "quilt-loader-0.31.0-beta.4-1.21.4",
  "inheritsFrom": "1.21.4",
  "mainClass": "org.quiltmc.loader.impl.launch.knot.KnotClient",
  "libraries": [
    { "name": "net.fabricmc:sponge-mixin:0.17.4+mixin.0.8.7" },
    { "name": "org.quiltmc:quilt-json5:1.0.4+final" },
    { "name": "org.ow2.asm:asm:9.10.1" }, … ,
    { "name": "org.quiltmc:quilt-config:1.3.3" },
    { "name": "org.quiltmc:quilt-loader:0.31.0-beta.4" },
    { "name": "org.quiltmc:hashed:1.21.4" },
    { "name": "net.fabricmc:intermediary:1.21.4" }
  ]
}
```

Same install model as Fabric (profile JSON + libraries, no processors).
Note: non-existent loader versions return HTTP 404 (`…/1.21.4/0.30.0-beta.1/profile/json` → 404), so a
loader list lookup must be validated, not assumed.

### C3. Forge

Installer artifact: `https://maven.minecraftforge.net/net/minecraftforge/forge/<mc>-<forge>/forge-<mc>-<forge>-installer.jar`
— [V] downloaded `forge-1.21.8-58.1.26-installer.jar` (9,397,647 B) and
`forge-1.20.1-47.4.0-installer.jar` (6,056,587 B).

Current promos (live `https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json`):
`1.21.8-latest = 58.1.26`, `1.21.7-latest = 57.0.3`, `1.21.6-latest = 56.0.9`, `1.21.5-recommended = 55.1.0`,
`1.21.4-recommended = 54.1.14`, `1.20.1-recommended = 52.1.0` (1.20.1: latest 52.1.20).

Installer jar contents [V, zip listing, no execution]:

- `META-INF/MANIFEST.MF` → `Main-Class: net.minecraftforge.installer.SimpleInstaller`
- `install_profile.json` — spec 1:

```jsonc
// forge-1.21.8-58.1.26-installer.jar!/install_profile.json
{
  "_comment": ["Please do not automate the download and installation of Forge.", …],
  "hideExtract": true,
  "spec": 1,
  "profile": "forge",
  "version": "1.21.8-forge-58.1.26",
  "path": "net.minecraftforge:forge:1.21.8-58.1.26:shim",   // 1.20.1: null
  "minecraft": "1.21.8",
  "serverJarPath": "{LIBRARY_DIR}/net/minecraft/server/{MINECRAFT_VERSION}/server-{MINECRAFT_VERSION}-bundled.jar",
  "data":   { "MOJMAPS": {"client": "[net.minecraft:client:1.21.8:mappings@tsrg]", …}, … },
  "processors": [ { "sides": ["server"], "jar": "net.minecraftforge:installertools:1.4.3", "classpath": [ … ], "args": ["--task", …] }, … ],
  "libraries": [ … 21 … ],
  "json": "/version.json", "mirrorList": "…", "logo": "…", "welcome": "…"
}
```

- `version.json` (this is what becomes `versions\<id>\<id>.json`, minus processors):

```jsonc
{
  "id": "1.21.8-forge-58.1.26",
  "inheritsFrom": "1.21.8",
  "type": "release",
  "mainClass": "net.minecraftforge.bootstrap.ForgeBootstrap",   // 1.20.1: "cpw.mods.bootstraplauncher.BootstrapLauncher"
  "logging": { … },
  "libraries": [ … 50 entries (1.20.1: 29) … ],
  "arguments": { "game": [ … ], "jvm": [ … ] }
}
```

Processors (counts verified by parsing `install_profile.json`):

| Installer | processors | of which run on client | processor jars |
|---|---|---|---|
| forge 1.21.8-58.1.26 | 7 | 3 | `net.minecraftforge:installertools:1.4.3`, `net.minecraftforge:ForgeAutoRenamingTool:1.0.6`, `net.minecraftforge:binarypatcher:1.2.0` |
| forge 1.20.1-47.4.0 | 10 | 7 | `installertools:1.4.1`, `jarsplitter:1.1.4`, `ForgeAutoRenamingTool:0.1.22:all`, `binarypatcher:1.1.1` |

**Headless client install via the official installer is NOT supported in 2026** [V]. Forge's installer
source (`MinecraftForge/Installer`, `src/main/java/net/minecraftforge/installer/SimpleInstaller.java`,
lines 33-55) declares only:

```java
OptionSpecBuilder serverInstallOption = parser.accepts("installServer", "Install a server to the current directory");
OptionSpecBuilder extractOption       = parser.accepts("extract", "Extract the contained jar file");
OptionSpecBuilder helpOption          = parser.acceptsAll(Arrays.asList("h","help"), "Help with this installer");
OptionSpecBuilder offlineOption       = parser.accepts("offline", "Don't attempt any network calls");
…
else { launchGui(); }   // no options => GUI, not a silent client install
```

There is **no `--installClient` option**: the string `installClient` does appear inside the shipped
`SimpleInstaller.class`/`Actions.class` constants (checked in all three jars by byte scan) but the
current source exposes no such CLI flag, and the no-arg path opens the Swing GUI. Conclusion: a
downloader must implement the client install itself (see C5).

Java requirements per MC version — from each version JSON's `javaVersion.majorVersion` [V, live]:

| MC | Java | MC | Java |
|---|---|---|---|
| 1.16.5 | 8 | 1.20.6 | 21 |
| 1.17.1 | 16 | 1.21.1 | 21 |
| 1.18.2 | 17 | 1.21.4 | 21 |
| 1.20.1 | 17 | 1.21.8 | 21 |
| 1.20.4 | 17 | 1.21.11 | 21 |
| 26.3 / 26.4-snapshot-4 | **25** | | |

Mirror: `https://bmclapi2.bangbang93.com/forge/minecraft` (list of MC versions) and
`…/forge/minecraft/<mcver>` (builds, with `files[]` incl. `category: "installer"`) — [V] 200, 56,543 B for
1.20.1. `…/forge/promotions_slim.json` → **404** (not mirrored).

### C4. NeoForge

- Maven: `https://maven.neoforged.net/releases/net/neoforged/neoforge/`. Version scheme for MC 1.20.2+:
  `<mc-1.minor>.<build>` — e.g. `21.1.264` ↔ MC 1.21.1, `26.3.0.94` ↔ MC 26.3 (metadata live via BMCLAPI
  mirror: 1845 versions, `<latest>26.3.0.94</latest>`).
- MC 1.20.1 uses the **legacy artifact** `net.neoforged:forge` with versions like `1.20.1-47.1.106`
  (metadata: 63 versions, latest `1.20.1-47.1.106`).
- Installer: `…/net/neoforged/neoforge/<ver>/neoforge-<ver>-installer.jar` — [V] downloaded
  `neoforge-21.1.264-installer.jar` (6,992,411 B). Contents: `META-INF/MANIFEST.MF` →
  `Main-Class: net.minecraftforge.installer.SimpleInstaller`; `install_profile.json`
  (`spec:1`, `profile:"NeoForge"`, `version:"neoforge-21.1.264"`, `minecraft:"1.21.1"`, 10 processors,
  70 libraries, data `MAPPINGS` → `net.neoforged:neoform:1.21.1-20240808.144430:mappings@txt`);
  `version.json` (`id:"neoforge-21.1.264"`, `inheritsFrom:"1.21.1"`,
  `mainClass:"cpw.mods.bootstraplauncher.BootstrapLauncher"`, 47 libraries).
- Processors: 10 total, **7 run on client**; jars `net.neoforged.installertools:installertools:2.1.2`,
  `…:jarsplitter:2.1.2`, `net.neoforged:AutoRenamingTool:2.0.3:all`, `…:binarypatcher:2.1.2:fatjar`.
- Same installer codebase ⇒ same CLI limitation: no `--installClient`, no headless client install.
- Direct `maven.neoforged.net` was **unreachable from this machine** (TCP timeout on 443, both
  `maven-metadata.xml` and artifact). BMCLAPI `/maven/net/neoforged/...` worked — [V].

### C5. Is a pure-API install (no Java installer) realistic? [V: mechanism, I: completeness]

- **Fabric / Quilt: yes, trivially.** Profile JSON + libraries; nothing executes Java.
- **Forge / NeoForge: no, not fully** — the client install always includes processors that *patch the
  vanilla client jar* (`binarypatcher --clean`, `ForgeAutoRenamingTool --input`, `jarsplitter`), and those
  are standalone Java programs that must be executed. What you *can* skip is the installer's UI/flow:
  1. download the installer jar (a ZIP; do **not** execute it),
  2. read `install_profile.json` + `version.json` from it,
  3. resolve `data` entries (`[group:artifact:version:classifier@ext]` → maven coords; `'literal'` → literal),
  4. download `libraries` from `https://maven.minecraftforge.net/` / `https://maven.neoforged.net/releases/`,
  5. run each processor whose `sides` is null or contains `client` as
     `java -cp <classpath + processor jar> <Main-Class from that jar's manifest> <args…>`,
  6. write `versions\<id>\<id>.json` from `version.json`.
  This is exactly what HMCL does — `HMCLCore/src/main/java/org/jackhuang/hmcl/download/forge/ForgeNewInstallTask.java`
  builds the command from `JavaRuntime.getDefault().getBinary()`, adds the processor jar to the
  classpath, reads `Attributes.Name.MAIN_CLASS` from the jar manifest, and logs
  `"Executing external processor " + processor.getJar()` ([V] lines 113-151).
- Practical consequence for a "downloader" that refuses to run Java: Forge/NeoForge client installs are
  **out of scope** unless you bundle/execute those processor jars; only Fabric/Quilt can be installed
  purely over HTTP.

---

## D. Mirrors (BMCLAPI) — live status and rewrite rules

Live probes 2026-10-20 [V]:

| Path | Result |
|---|---|
| `https://bmclapi2.bangbang93.com/mc/game/version_manifest_v2.json` | 200, 277,821 B, `Content-Type: binary/octet-stream` |
| `https://bmclapi2.bangbang93.com/mc/game/version_manifest.json` | used by PCL; same schema (v1) |
| `https://bmclapi2.bangbang93.com/fabric-meta/v2/versions/loader/1.21.4/0.16.10/profile/json` | 200 |
| `https://bmclapi2.bangbang93.com/quilt-meta/v3/versions/loader` | 200 (list starts at `0.23.0`) |
| `https://bmclapi2.bangbang93.com/maven/net/neoforged/neoforge/maven-metadata.xml` | 200, 68,446 B |
| `https://bmclapi2.bangbang93.com/maven/net/fabricmc/fabric-loader/…jar` | HEAD → **403** (HEAD blocked; GET works in practice) |
| `https://bmclapi2.bangbang93.com/forge/minecraft` | 200 (array of MC versions) |
| `https://bmclapi2.bangbang93.com/forge/minecraft/1.20.1` | 200, 56,543 B (`files[]` with `category:"installer"`) |
| `https://bmclapi2.bangbang93.com/forge/promotions_slim.json` | **404** |
| `https://bmclapi2.bangbang93.com/neoforge/meta/api/maven/details/releases/net/neoforged/neoforge` | PCL uses this JSON "maven details" API |
| `https://bmclapi2.bangbang93.com/assets/<2hex>/<hash>` | exists, but a fabricated hash returns 404 (expected); use real manifest hashes |
| `https://bmclapi2.bangbang93.com/maven/com/mumfrey/liteloader/versions.json` | PCL liteloader list |
| `https://bmclapi2.bangbang93.com/optifine/versionList` | PCL OptiFine list |
| `https://bmclapi2.bangbang93.com/mirrors/authlib-injector/artifact/latest.json` | PCL authlib-injector mirror |

**PCL's exact host-rewrite rules** (`Plain Craft Launcher 2/Modules/Minecraft/ModDownload.vb:1254-1284`):

```vb
' assets
Replace("https://piston-data.mojang.com", "https://bmclapi2.bangbang93.com/assets")
Replace("https://piston-meta.mojang.com", "https://bmclapi2.bangbang93.com/assets")
Replace("https://resources.download.minecraft.net", "https://bmclapi2.bangbang93.com/assets")
' libraries (two variants: /maven and /libraries)
Replace("https://libraries.minecraft.net", "https://bmclapi2.bangbang93.com/maven")
Replace("https://libraries.minecraft.net", "https://bmclapi2.bangbang93.com/libraries")
```

**PCL's maven rewrite for mod-loader libraries** (`ModMinecraft.vb:1870-1871`):

```vb
Dim BmclapiUrl As String =
    Token.Url.Replace(Token.Url.Substring(0, Token.Url.IndexOfF("maven")), "https://bmclapi2.bangbang93.com/").
              Replace("maven.fabricmc.net", "maven").
              Replace("maven.minecraftforge.net", "maven").
              Replace("maven.neoforged.net/releases", "maven")
```

i.e. `https://maven.fabricmc.net/net/fabricmc/…` → `https://bmclapi2.bangbang93.com/maven/net/fabricmc/…`,
`https://maven.minecraftforge.net/…` → `…/maven/…`, `https://maven.neoforged.net/releases/…` → `…/maven/…`.

**HMCL's provider model** (`HMCLCore/.../download/DownloadProvider.java`) [V]: providers are chosen per
category and expressed as ordered candidate lists (`DownloadCandidates.of(urls…)`), e.g.
`getVersionListCandidates(...)` defaults to
`https://piston-meta.mojang.com/mc/game/version_manifest.json`; assets default to
`https://resources.download.minecraft.net/<location>` (`:318`); Fabric/Quilt/LegacyFabric lists use
`*RemoteVersion.LOADER_META_URL` / `GAME_META_URL` constants (`:187-276`); NeoForge uses
`NeoForgeRemoteVersion.META_URL` / `OLD_URL` (`:285-289`). A BMCLAPI provider is a separate
`DownloadProvider` implementation selected by `org.jackhuang.hmcl.setting.DownloadSource`, and each
candidate URL is tried in order (`getVersionListCandidates`, `:296-313`). Practical rule: keep official
and mirror URLs as an ordered fallback list, per category (version list, assets, maven, loader meta,
OptiFine, authlib-injector).

---

## E. April Fools versions

### E1. Manifest truth (live `https://piston-meta.mojang.com/mc/game/version_manifest_v2.json`) [V]

`count = 919`, `latest.release = 26.3`, `latest.snapshot = 26.4-snapshot-4` (2026-10-20).

April Fools entries that **are** in the manifest (id / type / releaseTime / required Java):

| id | type | releaseTime | java |
|---|---|---|---|
| `15w14a` | snapshot | 2015-04-01 15:08 | 8 |
| `1.RV-Pre1` | snapshot | 2016-04-01 00:18 | 8 |
| `3D Shareware v1.34` | snapshot | 2019-04-01 19:18 | 8 |
| `20w14a` (20w14∞) | snapshot | 2020-04-02 22:28 | 8 |
| `22w13oneblockatatime` | snapshot | 2022-04-01 19:56 | 17 |
| `23w13a_or_b` | snapshot | 2023-04-01 20:52 | 17 |
| `24w14potato` | snapshot | 2024-04-01 19:07 | 17 |
| `25w14craftmine` | snapshot | 2025-04-01 23:50 | 21 |
| `26w14a` | snapshot | 2026-04-01 20:13 | 25 |

Caveats / corrections to the task brief:

- **`15w14a` IS in the manifest** (2015-04-01 15:08, snapshot). The "not in manifest" case is
  **Minecraft 2.0** (2013 joke, client version `2.0`) — also `Minecraft 4k` (2011) — neither has a
  manifest entry, so a launcher cannot download them via piston-meta.
- `20w14a` is listed as a normal `snapshot` with releaseTime 2020-04-02 (after the joke drop on 04-01).
- Beware lookalikes that are **regular** snapshots: `22w14a`, `24w14a`, `19w14a`, `21w14a`, `16w14a`,
  `17w14a`, `18w14a`, `20w13a`, `23w13a`, `22w13a`, `21w13a` are *not* jokes. Match the exact ids above,
  never a `w14a`/`w13a` pattern.
- `26w14a` (2026-04-01 20:13, Java 25) is the newest manifest-visible joke candidate; treat as
  "manifest-verified snapshot, April-Fools-suspected" until the wiki page confirms the joke content.

### E2. Hardcoded list for the downloader

```jsonc
{
  "april_fools_in_manifest": [
    { "id": "15w14a",                 "mc": "1.8.9-era", "java": 8  },
    { "id": "1.RV-Pre1",              "mc": "1.9.x",     "java": 8  },
    { "id": "3D Shareware v1.34",     "mc": "1.13.x",    "java": 8  },
    { "id": "20w14a",                 "mc": "1.15.x",    "java": 8  },
    { "id": "22w13oneblockatatime",   "mc": "1.18.x",    "java": 17 },
    { "id": "23w13a_or_b",            "mc": "1.19.4",    "java": 17 },
    { "id": "24w14potato",            "mc": "1.20.4",    "java": 17 },
    { "id": "25w14craftmine",         "mc": "1.21.4",    "java": 21 },
    { "id": "26w14a",                 "mc": "26.3",      "java": 25 }
  ],
  "april_fools_not_in_manifest": [
    { "id": "2.0",  "note": "Minecraft 2.0 (2013) — no piston-meta version JSON" },
    { "id": "4k",   "note": "Minecraft 4k (2011) — no piston-meta version JSON" }
  ]
}
```

Recommended runtime behaviour: never trust a hardcoded list for *installability* — check
`versions[]` from the live manifest first, then annotate with this table; only use the hardcoded list to
label/"AF badge" versions, and to offer the non-manifest entries as externally-sourced downloads.

---

## F. Implementation checklist (for CubeFetch)

1. **PCL2 import**: read `<root>\PCL\Setup.ini` (`LaunchFolderSelect`, `LaunchFolders`), expand `$` →
   PCL dir, keep trailing `\`; per version read `versions\<id>\PCL\Setup.ini`
   (`VersionArgumentIndieV2` bool + `VersionArgumentIndie` int + `LaunchArgumentIndieV2` int fallback with
   the auto-on rule for `mods/`/`saves/`), and preserve all other per-instance keys verbatim
   (`State`, `Info`, `Logo`, `LogoIsCustom`… actual key is `LogoCustom`, `IsStar`, `DisplayType`,
   `ReleaseTime`, `VersionFabric|Forge|NeoForge|OptiFine|LiteLoader|Vanilla|VanillaName`) — PCL rewrites
   this file on launch, so never drop unknown keys.
2. **HMCL import**: prefer `config/game-directories.json` + `config/launcher-settings.json`
   (`selectedGameDirectory` UUID → `path`); fall back to `hmcl.json` (`configurations[*].gameDir`,
   `selectedProfile`, `selectedMinecraftVersion`). Per-version isolation from
   `versions\<id>\instance-game-settings.json` (`runningDirectory`, `defaultIsolationType`), legacy
   `versions\<id>\hmclversion.cfg` (`gameDirType`, `runningDirectory`).
3. **Version validity**: require `<root>\versions\<id>\<id>.json`; accept `inheritsFrom`; do not require
   `launcher_profiles.json` (PCL creates it) and do not require `<id>.jar` to *list*.
4. **Install paths**: Fabric/Quilt via profile JSON (pure HTTP); Forge/NeoForge only if you are willing
   to execute processor jars with a bundled/selected Java 8/16/17/21/25 runtime.
5. **Mirrors**: implement ordered candidate lists; BMCLAPI is alive as of 2026-10-20 for
   `mc/game/version_manifest*.json`, `fabric-meta`, `quilt-meta`, `maven`, `assets`, `forge/minecraft/*`,
   `optifine`, `liteloader`, `authlib-injector`; it does **not** mirror `forge/promotions_slim.json`.
