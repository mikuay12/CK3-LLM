# CK3-LLM Desktop

CK3-LLM 的 Rust 桌面版。使用 egui/eframe，当前适配 Windows x64 上的 CK3 `1.20.1.2` 和游戏随附的 Nakama SDK `2.4.1`。

## 启动

从 GitHub Actions 的 **Windows build** 下载 Windows ZIP，解压后双击 `ck3-llm.exe`。房主运行时随程序构建，日常运行无需安装 Python。

- **本地房主**启动 CK3 和本机联机服务，实机验证使用默认端口 `7350`。在 CK3 的多人菜单创建房间后，程序会保存并填入房间配置。也可以通过 `ck3-llm.exe --start-host` 按已保存的设置启动房主。
- **房间连接**检测 SDK，登录并加入 Nakama 房间，查看收包与 PDX 帧统计。也可导入已有 `session_profile.json`。勾选游戏内热加入后，客户端协商会话、接收并验证快照，并提供选角、准备和按原字段提交游戏命令。
- **世界查询**可打开同版本的 CK3 二进制存档或热加入快照，也可连接运行中的本机 CK3。进入战役后查询实时日期、玩家、人物、头衔和省份。实时查询核对 `ck3.exe` 哈希，并在只读进程句柄上执行。
- **通知**查看房间和快照状态，导入旧版通知记录和筛选设置，按类别和类型管理显示内容。
- **模型设置**使用 Chat Completions 兼容 API，支持连接测试和导入旧版 `model_config.json` 中的地址、模型、密钥环境变量、超时和指令。
- **模型对话**支持与配置的模型进行多轮文本对话。勾选查询选项后，模型可以查询「世界查询」中选中的数据源。密钥可通过环境变量读取或临时输入。设置可保存到 `%APPDATA%\CK3-LLM\settings.json`。
- **记忆**支持导入旧版 `llm_memory.json`、编辑战役记忆和跨局经验、保存和选择用于对话的条目。

房主捕获文件保存在 `%APPDATA%\CK3-LLM\captures`。停止本地服务会断开房间并恢复原菜单文件，游戏可从 CK3 窗口正常退出。

已在实机验证房主启动、本地认证和实时传输连接。热加入、快照补传、选角和准备已通过原装 DLL 的合成房间测试。已迁移的 94 种指令含玩家、常用游戏操作和传输控制，封包逐项对照旧版。实机热加入和行动效果仍待验收。世界查询的原生规则求值、复杂人物互动和活动计划、模型自动游玩、实时游戏通知采集及回放待迁移。

## 编译和验证

Windows 完整构建使用 Rust `1.100.0-x86_64-pc-windows-msvc`、可用的 libclang 和 Frida `17.26.2` 开发包。

```powershell
$devkit = ./scripts/prepare-frida.ps1 -Directory (Join-Path $env:TEMP 'ck3-frida')
$env:CK3_FRIDA_DEVKIT = $devkit
$clangPath = $devkit.Replace('\', '/')
$env:BINDGEN_EXTRA_CLANG_ARGS = "-I`"$clangPath`""
$env:CARGO_ENCODED_RUSTFLAGS = (@('-C', 'target-feature=+crt-static', "-Lnative=$devkit") -join [char]31)
cargo test --locked --features host-bridge --target x86_64-pc-windows-msvc
cargo build --locked --features host-bridge --release --target x86_64-pc-windows-msvc
./scripts/package.ps1 -Target x86_64-pc-windows-msvc
```

本地可以用安装目录里的原装 DLL 测试真实 ABI。回环测试使用合成 Nakama 房间，无需启动游戏。

```powershell
$env:CK3_GAME_DIR = 'C:\Games\Crusader Kings III'
cargo test --locked --test nakama_dll --test local_backend -- --ignored
cargo test --locked --lib -- --ignored
```

推送 `rust-rewrite` 分支或打开拉取请求会运行 Windows 构建。打 `v*` 版本标签会通过 GitHub Releases 发布 ZIP。应用在运行时从用户选择的游戏安装目录加载 Nakama DLL。
