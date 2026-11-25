# 继承后退出导致永久暂停：调查与修复

日期：2026-11-24。版本：CK3 1.20.1.2。

## 根因与证据

角色继承与玩家确认继承是两件事。`DirectObservationQueue` 已能从原生玩家列表跟随继承人，但旧客户端没有提交 `close_succession`。此外，每个房主包会用 `SessionState` 中未更新的旧角色重新覆盖观测角色，形成内存角色与协议缓存交替覆盖的问题。

该局的本地协议记录为 `captures/connect-local-20261124-194533-f00996c1/events.jsonl`。穆尔哈德 `33336` 于游戏 `1094.2.13` 去世，之后布里安 `38035` 继续被控制；该局没有出现 `close_succession`。结束时对话工具使用强制终止后台任务，日志没有正常断开完成记录。

最新退出存档 `autosave_exit.ck3` 中明确保留：

```text
played_character={
    name="Antigravity"
    character=38035
    player=-1
    has_open_succession=yes
    succession_data={
        previous_played=33336
        player_heir=38035
        ...
    }
}
```

这条离线玩家记录仍待确认。房主更换自己的角色或普通解除暂停不会清理它。此前“继承人能继续下令、没有 OOS”只验证了接管与同步，不足以证明继承确认与退出流程完整。

## 原生命令与只读状态

`CCloseSuccessionCommand` 的 token 为 `0x3c00`，vtable RVA `0x4770590`，Write/Read RVA 为 `0x28a1cf0/0x28a1d90`。载荷 `character`（U32，token `0x6ec`）位于对象 `+0x20`，`machineid`（I32，token `0x16`）位于 `+0x24`。

在早上另一局房主继承时保留了真实封包：`captures/connect-local-20261124-101533-200fb88f/events.jsonl` 的 UTC `02:44:44.435871`，内容为 `close_succession(character=38295, machineid=1)`。新增编码与这份封包逐字节一致，并经 `NativeSerializerOracle` 与编译后的原生写入函数比对。

原生执行 helper `0x28a1ba0` 按**角色与 machineid 同时匹配**查找 `CPlayerInGame`，清除 `+0x260` 的 `has_open_succession`，销毁并清除 `+0x268/+0x2c8` 的可选继承数据。`CPlayerInGame::Write`（`0x23e1500`）对这两项的保存位置提供独立验证。

`memory_world.py` 新增 `player_successions` 只读查询。记录数组位于世界数据 `+0x22340`，记录的角色、玩家身份、magic 分别为 `+0xb0/+0xd8/+0xdc`（`PlIn`）。查询包含离线历史玩家，`machineid=-1` 不等于当前客户端。

```json
{"command":"query","kind":"player_successions"}
```

## 客户端修复

使用 `--live-memory-pid` 时，`succession_guard.py` 默认作为协议维护流程运行，无需模型另选角色。仅在原生记录属于本客户端、角色等于当前已接管角色且存活时确认继承；不会操作房主或其他玩家，不会选择随机新天命。每半秒检查，最多三次有间隔重试，读取标记清除后停止。

正常 `{"command":"stop"}` 与已有异常清理路径会在断开握手前再次检查，等待原生确认。`succession_close_requested` 只证明已提交；`succession_confirmation_completed` 才表示重新读取到标记清除。清理失败或超时会记录专用错误，不把回传当成效果验证。强制杀进程无法执行退出清理，应使用 `stop`。

内存读取模式不再用协议缓存的旧 `playable` 覆盖实时继承人。普通玩家暂停状态不在本次继承确认流程中修改。

## 已有坏档恢复

已经创建修复副本，原件保持不变：

`%USERPROFILE%\Documents\Paradox Interactive\Crusader Kings III\save games\autosave_exit_succession_fixed_20261124.ck3`

工具 `repair_succession_save.py` 仅接受明文 SAV 存档，要求按名字和角色唯一匹配、玩家已离线、待确认继承人一致。与原生命令效果对应，仅删除该记录的 `has_open_succession` 和 `succession_data`，拒绝原地覆盖或覆盖已有输出。旁边的 `.ck3.repair.json` 保存两份 SHA-256 和精确删除字节范围。本次只删除 989 字节，独立校验确认其余字节与原件完全相同。

```powershell
py -m ck3_llm.tools.repair_succession_save "原存档.ck3" --output "新的修复副本.ck3" --player-name Antigravity --character 38035
```

## 验证边界

专项测试覆盖真实抓包字节、原生序列化、Unicorn 执行原生确认 helper、角色与玩家隔离、主控角色缓存回退、重试、正常退出顺序、坏档范围与原件保护。原生执行测试仅桩掉私有测试堆的继承数据析构函数；状态清除和身份匹配执行真实 CK3 指令，未向游戏进程写入或注入代码。

最终 `py -m unittest discover -q`：408 项，407 项通过、1 项跳过，耗时 81.647 秒。完整回归中同步更新了导航测试的原生查询桩，以及一个已过时的非法作用域错误提示断言。

游戏已关闭；尚未实机复测“自然死亡 → 确认 → 退出 → 房主继续推进”和修复副本读档。下次开局应先验收这两条，不把离线验证标记成实机完成。

研究材料与最小证据保存在 `captures/succession-bug-20261124/`。
