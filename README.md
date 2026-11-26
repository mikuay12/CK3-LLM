# CK3-LLM Desktop

CK3-LLM 的 Rust 桌面版。使用 egui/eframe，当前适配 Windows x64 上的 CK3 `1.20.1.2` 和游戏随附的 Nakama SDK `2.4.1`。

## 启动

解压 **Windows build** 生成的 ZIP，再运行 `ck3-llm.exe`。在「本地房主」或「房间连接」选择游戏安装目录。应用从该目录加载 CK3 随附的 Nakama DLL，原生规则评估使用压缩包内的 `unicorn.dll`。运行桌面端无需安装 Python。

- **本地房主**启动 CK3 和本机联机服务，实机验证使用默认端口 `7350`。在 CK3 的多人菜单创建房间后，程序会保存并填入房间配置。也可以通过 `ck3-llm.exe --start-host` 按已保存的设置启动房主。
- **房间连接**支持导入 `session_profile.json`、登录和入房。勾选游戏内热加入后，可使用快速或串行传输校验快照，随后选角、准备和提交命令。可按请求编号查看房主回传，并用世界状态核对实际效果。
- **世界查询**可连接运行中的 CK3，也可打开二进制存档、热加入快照或旧版 SQLite 快照数据库。在游戏安装目录中可搜索脚本和本地化文字。实时查询覆盖人物、领地、军事、治理、建设和待处理事务。土地边界、陆路路径和战争态势使用实际省份邻接数据。
- **规则评估**在独立虚拟机中读取 CK3 并运行规则，提供人物互动接受度、决议与活动条件和费用、事件选项、互动回复、建设报价、宣战预览，以及军事进度与补给。尚无法读取的值显示为未知或附带求值错误。
- **通知**可按角色跟踪军事变化、事件、互动请求、活动邀请、人物消息和局势提示。支持导入旧版记录和过滤设置。本机游戏窗口中未找到接收者的消息可在世界查询中单独查看。通知记录保存在 `%APPDATA%\CK3-LLM\game_notifications.jsonl`。
- **抓包回放**可打开 `events.jsonl`、搜索命令、校验并恢复快速或串行传输的快照。可以把快照和对应房间的命令记录保存到同一个 SQLite 数据库。
- **模型设置与对话**支持 Chat Completions 兼容 API、连接测试、导入旧版 `model_config.json` 和多轮世界查询。密钥通过环境变量读取或临时输入。设置保存在 `%APPDATA%\CK3-LLM\settings.json`。
- **自动游玩**使用当前房间玩家的角色查询游戏、执行模型选定的行动，并可在通知到来时继续运行。先完成热加入、选角和准备，再连接实时游戏，填写目标后启动模型。运行中可以追加指导或停止。
- **记忆**支持导入旧版 `llm_memory.json`、编辑战役记忆和跨局经验、保存和选择用于对话的条目。

房主捕获文件保存在 `%APPDATA%\CK3-LLM\captures`。停止本地服务会断开房间并恢复原菜单文件，游戏可从 CK3 窗口正常退出。

## 验证范围

- 97 种命令编码已逐项对照旧版。桌面端提供 79 个模型动作。人物互动、活动举办和带旅行草案的活动加入另有 31 组 CK3 原生序列化对照。
- 原装 Nakama DLL 的本地测试覆盖登录、房间传输、两种热加入、快照补传、选角和准备。模型闭环使用本机模拟服务验证世界查询、提交与重试、回执和通知唤醒。
- 本机 CK3 已验证实时人物、头衔、省份、通知轮询、赠礼接受度、决议求值、补给上限、陆路路径和建设报价。复杂动态文本和专用事件窗口仍有覆盖缺口。真实模型长局、长时间断线恢复和各动作的实机结算仍待验收。

## 编译和验证

Windows 完整构建使用 Rust `1.100.0-x86_64-pc-windows-msvc`、MSVC C/C++ 工具、Windows SDK、libclang 和 Frida `17.26.2` 开发包。SQLite 随程序编译。打包脚本校验并收录 Unicorn `2.1.4` 的运行库、许可证和上游源码归档。

```powershell
$devkit = ./scripts/prepare-frida.ps1 -Directory (Join-Path $env:TEMP 'ck3-frida')
$env:CK3_FRIDA_DEVKIT = $devkit
$clangPath = $devkit.Replace('\', '/')
$env:BINDGEN_EXTRA_CLANG_ARGS = "-I`"$clangPath`""
$env:CARGO_ENCODED_RUSTFLAGS = (@('-C', 'target-feature=+crt-static', "-Lnative=$devkit") -join [char]31)
$env:CK3_UNICORN_DIR = ./scripts/prepare-unicorn.ps1 -Directory (Join-Path $env:TEMP 'ck3-unicorn')
$env:CK3_UNICORN_DLL = Join-Path $env:CK3_UNICORN_DIR 'unicorn.dll'
cargo test --locked --features host-bridge --target x86_64-pc-windows-msvc
cargo build --locked --features host-bridge --release --target x86_64-pc-windows-msvc
./scripts/package.ps1 -Target x86_64-pc-windows-msvc
```

指定安装目录后，可在本机执行原装 Nakama DLL 测试。回环测试使用合成房间，无需启动游戏。

```powershell
$env:CK3_GAME_DIR = 'C:\Games\Crusader Kings III'
cargo test --locked --features host-bridge --target x86_64-pc-windows-msvc -- --include-ignored --skip evaluates_rules_in_installed_ck3
```

向 `main` 或 `rust-rewrite` 推送或打开拉取请求会运行 Windows 构建。打 `v*` 版本标签会通过 GitHub Releases 发布 ZIP。
