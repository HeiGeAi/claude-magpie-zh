# claude-magpie-zh

macOS 上 **Claude Desktop / Claude Magpie** 第三方接入模式前端汉化工具。零第三方 Python 依赖，支持一键离线汉化、隔离构建验证、Electron 双重完整性校验回写、深度重签名与原版无损回滚。

[快速开始](#快速开始) · [为什么原版没有中文](#为什么原版没有中文) · [实现原理](#实现原理) · [命令与参数](#命令与参数) · [回滚与故障排查](#回滚与故障排查) · [English](#english)

---

## 为什么原版没有中文

官方网页版 `claude.ai` 已经支持简体中文，但当你在 macOS 上通过 **Claude Magpie**（或第三方 API / 网关模式）使用 Claude Desktop 时，客户端加载的是本地内置的 `Contents/Resources/ion-dist.asar` 前端资源包。

这层本地前端只开放了 11 种语言白名单（`en-US`、`de-DE`、`fr-FR`、`ko-KR`、`ja-JP`、`es-419`、`es-ES`、`it-IT`、`hi-IN`、`pt-BR`、`id-ID`），默认不包含 `zh-CN`、`zh-TW`、`zh-HK`，同时打包的 JS 资源中也存在大量未走 i18n 键的硬编码英文界面文案。

`claude-magpie-zh` 做的事情很纯粹：在隔离目录里把前端资源补全为中文，并同步修复 macOS 上 Electron 5.x（Electron 44+）的 ASAR 双重完整性校验和代码签名，最后安全替换应用并支持随时一键恢复官方原版。

---

## 快速开始

### 环境要求

- macOS（Apple Silicon 或 Intel）
- 已安装 `/Applications/Claude.app`
- Python 3.10 及以上（脚本会自动跳过 macOS 自带的 3.9，优先定位 Homebrew / pyenv / 官方安装的 Python 3.10+）
- `git` 与 Xcode Command Line Tools 自带的 `codesign`

### 一键安装汉化

```bash
git clone https://github.com/HeiGeAi/claude-magpie-zh.git
cd claude-magpie-zh
./install.sh
```

脚本会自动执行以下步骤：

1. 拉取上游开源词库 [`javaht/claude-desktop-zh-cn`](https://github.com/javaht/claude-desktop-zh-cn)（MIT 协议）到临时目录。
2. 使用 `ditto` 把 `/Applications/Claude.app` 复制到临时隔离目录，不在原应用上直接施工。
3. 解包 `ion-dist.asar`，解锁 `zh-CN` 语言白名单，替换硬编码界面文案（跳过图标名与组件属性上下文），注入 `Intl.DisplayNames` 语言名称，并合并 `i18n/zh-CN.json`、`statsig`、`dynamic` 与桌面端壳层语言包。
4. 重新打包 `ion-dist.asar`（含 4MB 分块 SHA-256 校验），同步更新 `Info.plist` 中的 `ElectronAsarIntegrity` 哈希以及 `Electron Framework` 二进制内嵌完整性摘要槽位。
5. 剥离团队专有 Entitlements，补上 `com.apple.security.cs.disable-library-validation`，完成全量二进制与嵌套 bundle 的深度 ad-hoc 重签名并验证通过。
6. 将原版应用备份到 `/Applications/Claude.backup-before-zh-CN-<时间戳>.app`，替换新应用，自动更新用户配置（`Claude-Magpie-3p`、`Claude-Magpie`、`Claude-3p`、`Claude`）中的 `locale`，随后重新拉起 `Claude Magpie.app`（或 `Claude.app`）。

### 一键恢复原版

```bash
./uninstall.sh
```

---

## 实现原理

Claude Desktop 5.x（Electron 44+）在 macOS 上对 `ion-dist.asar` 有两层防篡改校验，只改文件或只做 `codesign` 都会直接闪退：

1. **ASAR 自身完整性**：每个文件在 ASAR Header 中带有 4MB 分块 SHA-256，而 `Contents/Info.plist` 的 `ElectronAsarIntegrity["Resources/ion-dist.asar"]["hash"]` 记录了 Header JSON 字符串的 SHA-256。
2. **Electron Framework 编译期摘要槽位**：`Electron Framework` 二进制内部在魔数 `AGbevlPCksUGKNL8TSn7wGmJEuJsXb2A` 之后嵌入了全量 `ElectronAsarIntegrity` 字典摘要（按 key 排序拼接 `key + algorithm + hash` 后的 SHA-256）。修改 ASAR 后必须同步更新 `Info.plist` 和二进制槽位，再对全部 Mach-O 与 `.framework`/`.app` 执行深度签名，才能通过 macOS Gatekeeper 与 Electron 启动校验。

同时，脚本在替换 JS 硬编码字符串时会检测前序上下文（如 `iconName`、`glyph`、`as`、`component`、`role`、`type` 等属性及默认值别名），避免把图标名（例如 `"Copy"`、`"Search"`、`"Settings"`）误替换成中文导致图标消失。

---

## 命令与参数

也可以直接调用主程序 [`claude_magpie_zh.py`](claude_magpie_zh.py)（标准库实现，无任何 pip 依赖）：

```bash
# 查看当前安装状态、语言配置与备份列表
python3 claude_magpie_zh.py status

# 仅在临时目录构建与校验，不替换 /Applications/Claude.app
python3 claude_magpie_zh.py install --stage-only

# 指定繁体中文或本地词库目录
python3 claude_magpie_zh.py install --lang zh-TW --resources /path/to/claude-desktop-zh-cn/resources

# 如果你在 Claude 桌面端内部终端触发，可用延迟后台切换避免进程自我终止
python3 claude_magpie_zh.py install --delayed-swap 6

# 包含斜杠命令菜单补丁（如词库包含 slash-command-menu-patch.json）
python3 claude_magpie_zh.py install --include-slash-menu

# 恢复官方备份
python3 claude_magpie_zh.py restore
```

常用参数说明：

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--lang` | `zh-CN` | 目标语言，可选 `zh-CN`、`zh-TW`、`zh-HK` |
| `--app` | `/Applications/Claude.app` | 目标 Claude.app 路径 |
| `--resources` | 自动拉取 | 指定本地词库 `resources` 目录，支持完全离线运行 |
| `--stage-only` | 关闭 | 只在隔离目录完成构建与签名校验，不替换原应用 |
| `--delayed-swap` | `0` | 延迟 N 秒后在独立会话中关闭并替换应用 |
| `--relaunch` | `auto` | 完成后拉起目标：`auto`（优先拉起 `Claude Magpie.app`）、`magpie`、`claude`、`none` |
| `--backup-dir` | `/Applications` | 原版备份存放目录 |

---

## 回滚与故障排查

- **启动后界面还是英文**：进入应用设置把语言切换为「简体中文」即可。部分配置在切换时会自动把 `locale` 持久化到前端存储。
- **Claude Desktop 自动更新后变回英文**：自动更新会覆盖 `/Applications/Claude.app`，重新执行一次 `./install.sh` 即可。
- **提示 Python 版本过低**：macOS 系统自带的 `/usr/bin/python3` 通常是 3.9，运行 `brew install python` 安装 Python 3.10+ 后重试。
- **需要彻底还原**：运行 `./uninstall.sh`（或手动将 `/Applications/Claude.backup-before-*.app` 改回 `/Applications/Claude.app`）。

---

## 本地测试

```bash
python3 -m unittest discover -s tests -v
```

单元测试使用临时目录与合成 ASAR/Plist 数据运行，不会改动你本地的 `/Applications/Claude.app`。

---

## English

`claude-magpie-zh` localizes the macOS **Claude Desktop / Claude Magpie** third-party gateway frontend (`ion-dist.asar`) into Simplified or Traditional Chinese (`zh-CN`, `zh-TW`, `zh-HK`) with zero external Python dependencies (`Python >= 3.10` stdlib only).

### What It Handles

- Unpacks `Contents/Resources/ion-dist.asar` in an isolated staging copy and unlocks the frontend language whitelist.
- Replaces hardcoded UI strings while guarding structural/icon attributes (`iconName`, `glyph`, `as`, `role`, etc.).
- Injects `Intl.DisplayNames` fallback labels and merges `i18n`, `statsig`, `dynamic`, and desktop shell catalogs.
- Rebuilds ASAR 4MB block SHA-256 hashes, updates `Info.plist` (`ElectronAsarIntegrity`) and `Electron Framework` sentinel digest slots, and deep ad-hoc re-signs all binaries and nested bundles.
- Backs up the original `/Applications/Claude.app`, swaps cleanly, updates `config.json` across Magpie and Claude user-data profiles, and supports one-command rollback.

### Quick Usage

```bash
./install.sh          # Stage, patch, sign, backup, swap, and relaunch
./uninstall.sh        # Restore latest official backup
python3 claude_magpie_zh.py status
```

---

## 致谢

- 词库上游：[`javaht/claude-desktop-zh-cn`](https://github.com/javaht/claude-desktop-zh-cn)（MIT License），本工具在运行时按需拉取或读取其开源词库。
- 兼容启动器：[`yetone/magpie`](https://github.com/yetone/magpie)（Claude Magpie）。

## License

[MIT License](LICENSE) · Copyright (c) 2026 HeiGeAi (Blake Xu)
