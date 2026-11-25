# CK3-LLM

适配 **Crusader Kings III `1.20.1.2`**（Windows + Python 3.14）的独立联机客户端与只读内存/原生沙箱接口，使大模型能够作为独立玩家 **B** 与本机真实房主 **A** 同房游玩。

- 开发交接与待办清单：[PROJECT_HANDOFF.md](PROJECT_HANDOFF.md)
- 架构与完成状态：[ARCHITECTURE.md](ARCHITECTURE.md)
- 最新实机全量验收：[docs/LIVE_VERIFICATION_REPORT.md](docs/LIVE_VERIFICATION_REPORT.md)
- 协议与偏移研究：[PROTOCOL_FINDINGS.md](PROTOCOL_FINDINGS.md)

---

## 快速启动

先安装 Python 依赖：

```powershell
py -3.14 -m pip install --target .deps -r requirements.txt
```

默认读取 `C:\Games\Crusader Kings III` 下的游戏文件。游戏装在其他目录时，启动房主和客户端时都传入 `--game-dir "游戏安装目录"`。本地运行会生成 `captures/`，协议调查文档中引用的历史抓包是本地研究记录。

### 1. 启动房主游戏 A 与本地联机服务

```powershell
py local_game_bridge.py
```

在游戏内创建本地多人房间并选角。建房后会在 `captures\local-game-<时间戳>\session_profile.json` 自动写入房间配置。

### 2. 接入虚拟机客户端 B

```powershell
py ck3_client.py connect-local `
  --session-profile "captures\local-game-本次目录\session_profile.json" `
  --live-memory-pid 当前ck3进程PID `
  --duration 0 --quiet --stdin --ack-received-turn
```

可选参数：
- `--model-config model_config.json`：接入 Chat Completions 兼容大模型服务，自动在后台执行 `game_query` / `game_action` / `wait_for_update` 循环。
- `--username` / `--identity`：自定义玩家名与本地身份（默认 `AIObserver`）。
- `--pause-on-military-event`：军事事件触发原生暂停（默认关闭）。
- `--auto-accept-player-calls-from 1`：明确授权的常设决定，及时接受指定联机玩家的参战召集，按 machineid 跟随其当前角色与继承人；默认关闭。
- `--auto-accept-ally-calls-from 32396`：同上，但绑定固定角色编号；可重复指定。
- `--auto-disband-at-peace`：明确授权的常设决定，在最后一场战争结束后解散闲置军队；默认关闭，备战期间不会仅因尚未宣战就解散军队。

### 3. 选角、准备与正常退出（stdin 逐行 JSON）

热加入解码完成并输出 `game_world_loaded` 后（假设分配到 `machineid=2`，目标角色为 `65238`）：

```json
{"command":"send_game_command","request_id":"sel-1","type":"set_character_controller","header":{"attribs":14},"fields":{"machineid":2,"playable":65238,"leave_ai_on":false}}
{"command":"send_game_command","request_id":"rdy-1","type":"set_ready_command","header":{"attribs":14},"fields":{"machineid":2,"status":true}}
{"command":"stop"}
```

`--live-memory-pid` 模式会自动确认已接管继承人的继承状态；正常 `stop` 会在断开前复查。历史坏档若出现“因继承而暂停”，见 [继承暂停修复说明](docs/SUCCESSION_PAUSE_FIX.md)，不要把普通解除暂停或重新选角当成继承确认。

### 4. 独立只读查询与通知勾选页

```powershell
# 独立只读查询（无需连房）
py live_objects.py --pid 当前ck3进程PID --stdin

# 通知筛选网页（http://127.0.0.1:8766/，勾选表示关闭，写入 notification_policy.json 热重载）
py notification_settings.py
```

---

## 常用查询与行动示例

所有查询均支持 `fields`、`max_chars`、`offset`、`limit`。完整字段说明通过 `{"command":"schema"}` 获取。

### 查询示例

```json
{"command":"schema"}
{"command":"observe","changes_only":true}
{"command":"query","kind":"players"}
{"command":"query","kind":"character","id":65238}
{"command":"query","kind":"declaration_preview","character":33336,"target":33882,"casus_belli":"claim_cb","titles":[607],"claimant":33336}
{"command":"query","kind":"province","id":2214}
{"command":"query","kind":"war_theater","id":687865856}
{"command":"query","kind":"route","origin":2214,"target":2220,"compute":true}
{"command":"query","kind":"unit_movement","id":1006632968}
{"command":"query","kind":"siege","id":1}
{"command":"query","kind":"holding_buildings","id":2214}
{"command":"query","kind":"construction_quote","id":2214,"building":"pastures_01","index":1}
{"command":"query","kind":"council_tasks"}
{"command":"query","kind":"court_positions"}
{"command":"query","kind":"factions"}
{"command":"query","kind":"schemes"}
{"command":"query","kind":"events"}
{"command":"query","kind":"event","id":771}
{"command":"query","kind":"event_options","id":771,"limit":5}
{"command":"query","kind":"incoming_interactions"}
{"command":"query","kind":"activity_invitations"}
{"command":"query","kind":"decisions","search":"knight","limit":5}
{"command":"query","kind":"decision","id":"invite_knights_decision"}
{"command":"query","kind":"activity_type","id":"activity_feast"}
{"command":"query","kind":"actions","search":"移动"}
{"command":"query","kind":"action","id":"send_interaction"}
```

> `council_tasks`、`court_positions`、`factions`、`schemes`、`secrets`、`task_contracts` 在已选角时默认过滤当前控制角色；如需查全局，传 `"all": true`。

### 行动指令示例

带 `request_id` 的行动会返回 `action_feedback`（`submitted` → `host_echoed` / `delivery_unconfirmed`），可用 `{"command":"query","kind":"action_status","id":"..."}` 查询状态。

```json
{"command":"send_game_command","request_id":"focus-1","type":"set_character_focus","header":{"attribs":14},"fields":{"actor":65238,"target":65238,"focus_type":"stewardship_domain_focus"}}
{"command":"send_game_command","request_id":"council-1","type":"change_council_task","header":{"attribs":14},"fields":{"character":61070,"liege":65238,"active_council_task":4782,"task_type":"task_foreign_affairs"}}
{"command":"send_interaction","request_id":"gift-1","interaction":"gift_interaction","recipient":33592574}
{"command":"send_interaction","request_id":"call-ally-1","interaction":"call_ally_interaction","recipient":32396,"target":{"type":"war","identity":100663342}}
{"command":"send_game_command","request_id":"ev-1","type":"select_event_option","header":{"attribs":14},"fields":{"data":771,"option":0}}
{"command":"send_game_command","request_id":"reply-1","type":"reply_character_interaction","header":{"attribs":14},"fields":{"character_interaction":1660944390,"accept":"accept"}}
{"command":"send_game_command","request_id":"dec-1","type":"execute_decision","header":{"attribs":14},"fields":{"character":65238,"decision":"invite_knights_decision"}}
{"command":"send_game_command","request_id":"join-1","type":"join_activity_command","header":{"attribs":14},"fields":{"activity":654311459,"character":65238,"travel":"yes"}}
{"command":"set_activity_intent_command","request_id":"intent-1","intent":"reduce_stress_intent","character":65238}
{"command":"send_game_command","request_id":"build-1","type":"add_construction","header":{"attribs":14},"fields":{"character":65238,"province":2226,"index":2,"type":605}}
{"command":"send_game_command","request_id":"move-1","type":"move_unit","header":{"attribs":14},"fields":{"valid_for":"player","unit":1006632968,"target":2215,"unit_order_type":"set","type":"land","gathering_army":"reset"}}
```

> `send_interaction` 禁止手动传 `ai_accept`，由客户端 B 在发包前通过 `ShadowEvaluator` 自动在零注入 Unicorn 沙箱中执行 `ck3.exe` 原生 `CalcAIAccept` 求值。

> 召集盟友参战必须传入 `target.type=war`，`target.identity` 使用当前 `wars` / `war` 查询的战争编号。房主回传后，查询 `incoming_interactions` 或 `war` 的参战名单核对送达和接受结果。

> `declaration_preview` 在只读 Unicorn 沙箱中执行原生可发送性检查并返回游戏拒绝原因；`resources.prestige_accumulated` 是累计名望，与可花费的 `prestige` 余额分开读取。

> `connect-local --auto-accept-player-marriages-from 1` 可按联机玩家身份自动接受该玩家发来的婚姻提议，跟随对方继承换角。默认关闭；只回复指定玩家发来的婚姻请求，不会回复其他 AI 或其他类型的互动。与 `--auto-accept-player-calls-from 1`、`--auto-disband-at-peace` 可同时使用。

---

## 模型循环配置（`--model-config`）

```json
{
  "endpoint": "http://127.0.0.1:8000/v1/chat/completions",
  "model": "your-model-name",
  "api_key_env": "CK3_MODEL_API_KEY",
  "instructions": "控制当前选中的角色，与房主共同游玩。按需查询信息并自行决定具体行动。",
  "request_options": {"max_completion_tokens": 4096}
}
```

---

## 回归与原生验证

原生验证需要本机安装受支持版本的 CK3。部分测试会在缺少本地抓包时跳过。

```powershell
# 全量离线单元测试
py -m unittest discover -q

# 本地回环联机演练（使用真实 nakama-cpp.dll）
py ck3_client.py demo

# 97 种原生命令字节级 Unicorn 比对
py verify_jomini_native.py --cases 16
```
