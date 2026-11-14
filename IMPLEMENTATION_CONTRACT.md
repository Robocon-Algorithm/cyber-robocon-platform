# 首版代码的模块合同

此文件用于并行开发时固定边界，规则仍以父目录 01—08 为准。代码写在本目录，Python 3.11+，运行时只用标准库，测试用 `unittest`。目录名是工作目录，不创建 `platform` Python 包；可执行包为 `robocon_sim`，SDK 包为 `brsdk`。

## 1. 三部分的所有权

- SDK 与规划：`brsdk/`、`robocon_sim/config.py`、`robocon_sim/map.py`、`robocon_sim/planning.py`、`examples/`、`tests/test_sdk.py`、`tests/test_planning.py`。配置、地图、SDK 类型、纯估时器和基线策略归此模块。
- 内核与裁判：`robocon_sim/engine.py`、`robocon_sim/world.py`、`robocon_sim/rules.py`、`robocon_sim/supply.py`、`robocon_sim/faults.py`、内核测试。只此模块能改世界。
- 运行与产品：`robocon_sim/runner.py`、`worker.py`、`storage.py`、`server.py`、`replay.py`、`cli.py`、`__main__.py`、`web/`、运行测试、打包配置、运行说明。负责隔离进程、比赛包、上传、换边评级与网页。

`robocon_sim/__init__.py` 由 SDK 模块创建，保持轻量，不提前导入其他模块。各模块不得覆盖他人文件，发现合同需改先协商。

## 2. 公共数据使用可 JSON 序列化的字典

内核不依赖 SDK 类。快照是深拷贝；SDK 再递归转换为只读字段视图，使 `obs.self.pose.x_mm`、`obs.towers[id]`、`obs.stacks.get(id)` 可用。字典键访问与属性访问并存，嵌套列表不可修改。SDK 不接受修改本地视图来改服务器。

`map` 与 `parameters` 在引擎内只读，发布时以**共享引用**进入快照，不逐帧深拷贝；其余字段仍每帧新建。任何就地修改 `world["map"]` / `world["parameters"]` 的行为都会在 `Engine.result()` 被断言拦下。

物理参数按方独立：`MatchConfig.parameters` 是基线，`MatchConfig.sides[team]` 只写与该方不同之处，`merged_parameters(team)` 合并。内核只通过 `Engine.param(team, name)` 取参数，机器人子进程拿到的 `obs["parameters"]` 是**本方**的扁平参数集，因此报价与制动预览用的物理和内核一致。记录用的公共快照里 `parameters` 是 `{red, blue}` 两份，双方各自的道具不进入对方的公开上下文。

机器人编号 `red_br`、`blue_br`，队伍键 `red`、`blue`。坐标单位 mm，角度 mrad，时钟整数 ms。首版字段如下：

- 顶层：`seq, sim_time_ms, world_revision, remaining_ms, self, opponent, robots, towers, items, stacks, supply, qualified, central_ball, score, events, receipts, map, parameters`。
- 机器人：`id, team, pose, motion_state, cargo, carried_stack_id, control_state, action`。`cargo` 是物品字典列表。`control_state` 为 `INIT / READY / PAUSED / RETRYING / FINISHED`。`motion_state` 含速度与当前分段进度，**不重复携带分段表**；运动分段只在 `action.segments` 上发布，取用方（如 `braking_preview`）以它为准。
- 姿态：`x_mm, y_mm, z_mm, heading_mrad, layer, workpoint_id`；途中作业位为 null。
- 物品：`id, kind, owner_color, holder_id, location, disabled_for_use, version, support_id, orientation`。`kind` 为 `box / cap / ball`；箱体没有预设塔基中段类型。`location` 用稳定字符串，如 `stock`、`transit:red`、`buffer:red:BOX_LOWER`、`held:red_br`、`tower:L1_PUBLIC_1:base`、`disabled:L1:red`、`out`。`support_id` 可为机器人 ID、下层物品 ID 或 `green:塔号`。
- 摞关系：`id, version, bottom_item_id, top_item_id, intact, holder_id, source_bay_id`；不是额外物品。两个箱体各自库存、持有、计分。
- 塔：`id, layer, public, owner, center, approach_point_id, base, middle, cap, version, operation, top_color`。三层字段为物品 ID 或 null。`owner` 是专属区域队伍或 null，收益按物品色和顶面色；`center` 有 `x_mm, y_mm`。
- 供料：`slots` 映射 `BOX_LOWER / BOX_UPPER / CAP_BAY` 到 ID/null，另有 `slot_versions, box_bay_version, stack_id, batch, inventory, control_state, recovery_phase, recovery_until_ms, ball_task`。库存键 `boxes_remaining, caps_remaining`；批次包含 `id, item_ids, status, expected_arrival_ms`。
- 动作：`id, kind, target, phase, started_at_ms, expected_end_ms, cancel_state` 以及计算当前位置所需的公开运动分段。初始取消状态字符串 `NONE`。
- 分数：`score[team]` 是带 `total`、分项与计分原因的字典。事件有 `id, type, occurred_at_ms` 和相关对象，回执有 `command_id, action_id, code, reason, occurred_at_ms`。

隐藏故障配置、未来脚本和种子不能出现在快照和 `on_start` 的公开配置中。已知在途 ETA 和当前执行目标公开。

## 3. 配置与地图由一个模块发布

`config.MatchConfig` 为 dataclass，字段至少 `duration_ms=180000, tick_ms=100, seed, parameters, supply_delays_ms, faults, auto_entry=True`，提供 `to_dict()`、`public_dict()`、`from_dict(data)`。私有清单可以完整保存，公开版本过滤 seed、faults。参数使用父目录 03 表中的教学初值；另明确入场、坡道、阶梯、回区与清理时间，均标初值。

`map.default_map()` 返回 JSON 字典，固定版本、尺寸、10 塔位、作业位、连接和合法静态路网。优先查本地 `D:\RoboCon\tmp\pdfs\rule-33.png`、`plan-upright.png` 和 V3 原文；不得把展示图颜色估算坐标说成测量。未明确的机器人作业点为平台作业位，记录来源和几何假设。塔 ID 为 `L1_RED_1/2, L1_BLUE_1/2, L1_PUBLIC_1/2, L2_PUBLIC_1/2/3/4`；塔默认作业位为 `塔号_APPROACH`。

本队交接点为 `RED_TRANSFER / BLUE_TRANSFER`，地面起点为 `RED_START / BLUE_START`。L1/L2 高度 600/900，跨层入口须显式 `traverse`；中央点 `CENTRAL_BALL_APPROACH`。首版不模拟车辆碰撞，仍验证专属区域、围栏和层级。普通移动只沿同层合法路网，不默认跨层。

L2 塔除 L2 的 `approach_point_id` 外，也有 `l1_approach_point_id`。对应 L1 点只允许所属队伍放置指定塔的塔基、中段和完整箱摞，不能加顶、翻顶或从塔上取箱。详细坐标和可达性由地图提供；计分按塔所在层核算。

## 4. 估时与 SDK 的线协议

`planning.quote_action(observation: dict, action: dict, after_stop: bool=False) -> dict` 是无副作用函数，供 SDK 与内核共同复算。动作字典为 `{"kind": str, "params": dict}`；返回至少 `ok, error, action, based_on_seq, estimate_ms, expected_end_ms, segments, preview_only`。失败有可读原因。运动时间含启停和必要转向；整摞抓 2000、放 1500、退 1000 ms，不重复叠加单件时间。忙碌时普通报价 BUSY；行驶中 `after_stop=True` 只给停车后新移动预览，不可执行。

SDK 导出 `Strategy, Action, Quote, Decision, Observation, StrategyAPI`。`Action` 提供文档 02 全部静态构造器，包括 `pick_stack, build_stack, put_down_stack`。`Action.to_dict()` 导出上述动作字典。

`StrategyAPI(obs)` 的 `quote` 调共享纯函数；`execute(quote, request_ball=False)`、`cancel(action_id, request_ball=False)`、`request_ball()` 返回 Decision，不立即执行。Decision JSON 统一为：

```json
{
  "based_on_seq": 0,
  "primary": {
    "kind": "execute",
    "action": {"kind": "move_to", "params": {"point_id": "RED_TRANSFER"}},
    "quote_seq": 0,
    "preview_only": false
  },
  "request_ball": false
}
```

取消时 `primary={"kind":"cancel","action_id":"..."}`；仅服务时 `primary=null, request_ball=true`。无决定为 null。一帧至多一个主通道命令及一个球请求，两通道分别校验；服务器忽略伪造耗时，拒绝旧帧、预览与不合法动作。SDK 提供 `to_dict / from_dict` 或等价转换供 worker 使用。

## 5. 内核对运行器的公共接口

`engine.Engine(config: MatchConfig | dict | None=None, map_data: dict | None=None)`。

- 属性 `done, sim_time_ms, seq, events`；`events` 是完整已发生事件列表。
- `observations() -> dict`：返回 `red, blue` 两份独立快照。公共世界来自同一边界，事件窗口相同，回执各自私有。
- `public_state() -> dict`：不含 self/opponent 私有信封的公共世界，用于日志和观战。
- `step(decisions: dict[str, dict | None]) -> None`：在当前边界同时校验决定，然后推进到下一边界；精确时间事件也处理，末边界发终场状态，不再接受命令。
- `fail_policies(failures: dict[str,str]) -> None`：同一边界的双方技术失败一次处理；单方失败另一方胜，双方失败为技术平局。
- `result() -> dict`：`status, winner`（red/blue/null）、`scores`（队伍到整数）、`score_ledger, sim_time_ms, qualified` 和破平、故障统计。

同一毫秒事件分批原子裁判，资格在整批后判断。两方同毫秒接触同空塔皆争用失败；同毫秒中央有效球按文档双方掉球清理，不能遍历红优先。命令只排入未来正时长阶段，不能零时长事件无限递归。

## 6. 必须保留的规则边界

正常两层事务提交，不等于故障全部回滚；真实独立有效下箱可保留。摞仍为两件，BR 不可携顶，完整摞只放空点。翻顶抬起期间不转移归属但不算耸立全塔，掉落恢复原塔。资格至少同时两全塔且一公共，取得后永久保留。五色石不结束比赛且唯一 250 分，球任务从申请到持有／清理都占本队令牌。

三槽只有一个两层箱体位与一个顶位。有限 20 箱6顶，实际交付后下一趟才出发，槽满等待；已承诺普通批次先于球。箱失误按 5.1.3，顶／球等强制重试回区后15秒另加回区清理。一摞掉两箱仍一次故障，绑定物料不因取消重抽。180000ms完成不生效，179999ms有效；终场持有箱不计5分，顶历史搬运分按05暂定口径。

## 7. 运行器、服务和测试边界

`runner.run_match(red_path, blue_path, config=None, output_dir=None, frame=None) -> dict`：开两个持久子进程，先 on_start，再每帧给相同快照各调用 on_step。收齐或判错后才 step。硬超时终止实例技术判负；已返回的异常可连续三帧判负。启动预算与回调预算分开；标准输出由 worker 处理为结构化协议，作者打印走限额诊断。`frame` 是帧存储选项（关键帧间隔等），来自平台配置。

快照写入 `snapshots.rcf`（`frame_codec.py`，`rcf/1`）：静态块只存一次（`map` 与 `parameters`），逐帧只存与上一帧的差异，每 100 帧一个自包含关键帧（字符串驻留表在关键帧处重置），文件尾是帧索引。编解码必须保持生产者的 dict 键顺序——计分账本按 `towers` 的迭代顺序生成，排序会让重演结果与记录不一致。每条记录带 crc32，只用于发现意外损坏。

旁路日志按职责拆开：`commands.jsonl.gz` 只保存重演需要的 `seq / sim_time_ms / decisions / failures`；`receipts.jsonl.gz` 只在有回执的帧写入；`events.jsonl` 与 `diagnostics.jsonl` 保持明文便于排查。三者都是 `{log_seq, payload}` 行，读取时校验 `log_seq` 单调。

`python -m robocon_sim demo` 跑两份基线；`match --red ... --blue ...` 单局；`series` 换边双局和10局场景；`replay` 重演核验；`dump` 打印某帧或帧索引；`serve --host 127.0.0.1 --port 8765 --trusted-code` 本机队内模式。`--config` 指向平台配置文件（`platform_config.py`），`profiles/` 目录里每方一档参数，文件变更在**创建对战前**生效，已创建的对局在 `series.matches` 冻结；红蓝档位不同则不得进入排位。子进程限时不等于安全沙箱：未具备容器／VM 隔离时不开放匿名上传或外网绑定，文档明确真实限制。

Web 提供上传策略版本、选择两方、创建对战／换边系列、任务状态、2.5D 地图回放与时间轴、事件和回执、逐件分项、排行榜。回放只读内核快照，不能另写计分。`/api/replay` 只返回元数据与帧时间轴，`/api/replay/frames` 按 50 帧分块提供状态。使用 SQLite 保存队伍、版本、系列占用和唯一结算，双局完整一次事务更新 Elo、重复完成不重复加分，平台失败重跑／整组作废不扣分。schema 由 `PRAGMA user_version=2` 守卫，版本不符直接拒绝加载，不做迁移。

测试至少覆盖运动手算、只读快照、整摞取放、旧报价、接触争用、资格、翻顶、球、供料背压、故障和重试、精确终场、进程超时、命令重演、帧编解码往返与损坏检测、配置热加载与回退、双局一次结算、HTTP上传与回放。具体失败案例优先于照抄实现的测试。实现未覆盖事项须在运行说明列明，不声称已实现或通过。
