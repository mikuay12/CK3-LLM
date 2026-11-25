# CK3-LLM Desktop

CK3-LLM 的 Rust 重写版。使用 egui/eframe 提供 Windows x64 桌面界面，目前适配 CK3 `1.20.1.2` 和游戏随附的 Nakama SDK `2.4.1`。

## 启动

从 GitHub Actions 的 **Windows build** 下载 Windows ZIP，解压后双击 `ck3-llm.exe`。源码构建可以运行 `cargo run --locked`。

- **房间连接**选择 CK3 安装目录和原版 Python 房主桥接生成的 `session_profile.json`。可检测 SDK，登录并加入 Nakama 房间，查看收包与 PDX 帧统计。
- **模型设置**使用 Chat Completions 兼容 API，支持连接测试和导入旧版 `model_config.json` 中的地址、模型、密钥环境变量、超时和指令。
- **模型对话**支持与配置的模型进行多轮文本对话。密钥可通过环境变量读取，也可在界面临时输入。设置可保存到 `%APPDATA%\CK3-LLM\settings.json`。

这一阶段跑通桌面管理和房间传输。CK3 游戏内握手、热加入、角色控制、世界查询、模型行动、通知和持久记忆待迁移。启动房主和导出房间配置目前继续使用 Python 版。

## 编译和验证

在 Windows 安装 Rust x64 工具链后执行以下命令。GitHub Actions 使用 `1.100.0-x86_64-pc-windows-msvc`。

```powershell
cargo fmt --all --check
cargo clippy --locked --all-targets -- -D warnings
cargo test --locked
cargo build --locked --release --target x86_64-pc-windows-msvc
./scripts/package.ps1 -Target x86_64-pc-windows-msvc
```

本地可以用安装目录里的原装 DLL 测试真实 ABI。回环测试使用合成 Nakama 房间，无需启动游戏。

```powershell
$env:CK3_GAME_DIR = 'C:\Games\Crusader Kings III'
cargo test --locked --test nakama_dll -- --ignored
```

推送 `rust-rewrite` 分支或打开拉取请求会运行 Windows 构建。打 `v*` 版本标签会通过 GitHub Releases 发布 ZIP。应用在运行时从用户选择的游戏安装目录加载 Nakama DLL。
