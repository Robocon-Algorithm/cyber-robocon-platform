# BR 策略竞技场：在本机运行、用比赛包复盘

本版可直接运行两份 Python 策略，按同一个比赛时钟推进 BR，保存逐帧回放和逐件计分。默认 180 秒、每 100 ms 决策一次。请先跑通示例，再改一项策略选择，用同一场景的回放比较差别。规则、动作时间和地图假设以[上级方案](../README.md)与代码为准。

> **不兼容旧版本**：快照为二进制增量格式（`.rcf`），数据库 schema 为 `user_version=3`。旧数据库会被拒绝加载，需要删掉数据目录重新开始。旧策略源码已归档到 [`examples/archive/`](examples/archive/INDEX.md)。

## 在 Windows 上开始第一局

安装 Python 3.11 或以上版本；从本目录运行，不需要安装第三方运行依赖。

```powershell
python -m robocon_sim serve --host 127.0.0.1 --port 8765 --trusted-code
```

首次启动会创建管理员并把密码打印一次在终端（也可以用 `--admin-password` 指定）。用浏览器打开 `http://127.0.0.1:8765`，以管理员登录后：

1. **建队伍** —— 「管理员」面板里创建队伍，例如 `逐星算法组`。
2. **建账号** —— 给每支队伍建一个或多个队员账号，初始密码至少 8 位。
3. **队员登录后上传策略** —— 只能看到并下载本队自己的版本，草稿与正式分开标记。
4. **打训练局** —— 手动对手单局/换边双局、自动跑一轮、对所有人，这些都不计分。
5. **设为正式策略** —— 系统自动对当时所有其他队伍已提交的正式策略各打一组换边双局，结束后结算 Elo 并写入批次报告。
   一个队伍可以有多份正式策略：每次提交都是给那一版盖章，**盖章不可撤销**；之后新提交的成为「当前出战」，旧的正式版保留标记与历史战绩，界面用「当前出战」标出哪份正在被挑战。

之后新增的正式提交会继续和你的正式策略交手，不需要你重新发起。

单局仍然是 180 秒、每 100 ms 决策一次。上传的版本冻结在磁盘上，队列里的批次取的是冻结版本，之后重新上传不会影响已排队的对局。

每个版本只提交一份 UTF-8 `.py` 文件，最大 256 KiB，定义一个直接继承 `brsdk.Strategy` 的类，实现同步的 `on_step(self, obs, api)`。上传会检查语法和入口并生成源码摘要，执行前仍由操作者审查。

要体验跨层建塔，上传 [`l1_reach_builder.py`](examples/l1_reach_builder.py) 和任一基线策略。

## 配置：`platform.json` + `profiles/`

数据目录默认 `data/arena/`，结构如下：

```
data/arena/
  platform.json      # 时长、tick、十局场景表、帧存储选项、默认参数档
  profiles/          # 每方一套物理参数，一个文件一档
    baseline.json    # 赛级基线（5 m/s）
    training.json    # 教学慢速（3 m/s）
  strategies/  runs/  platform.sqlite3
```

`platform.json` 的 `profiles: {"red": "baseline", "blue": "baseline"}` 决定默认选哪两档；网页创建对战时可以给红蓝分别选档，命令行用 `--red-profile / --blue-profile`。

- **每方独立参数**：`profiles/<名字>.json` 里只写与 `config.py` 教学初值不同的字段，载入时整体覆盖。红蓝两方可以指向不同档——这正是后续「参数随机扰动测试」需要的形态：给某一方换一份 profile 文件即可。双方档位不同时不进入排位（不更新 Elo），网页会提示「仅训练记录」。
- **热加载**：服务在**创建对战之前**按内容哈希比较 `platform.json` 与 `profiles/*.json`，读到新内容就用新内容，改完保存即可，不必重启。已经创建的对局把当时展开的参数冻结在 SQLite 的 `series.matches` 里，改配置不影响正在运行或排队中的对局。
- **写坏了不会崩**：JSON 非法、字段未知、参数越界时保留上一份可用配置，`/api/config` 的 `warnings` 列出错误，终端也会打印一行。缺 `platform.json` 或 `profiles/` 时 `serve` 会自动生成一份可编辑的默认值。

### 赛级速度档（`baseline`）

| 项目 | 教学初值（`training`） | 赛级基线（`baseline`） |
|---|---|---|
| 空载 / 载货直线 | 3000 mm/s | **5000 mm/s** |
| 带球 | 3000 mm/s | 4500 mm/s |
| 加/减速 | 1000 mm/s² | 3500 mm/s² |
| 90° 转向 | 1500 ms | **400 ms** |
| 上/下坡道 | 6000 / 5000 ms | 2000 / 1500 ms |
| 上/下楼梯、L2 升降 | 6000 / 5000 / 5000 / 4000 ms | 2000 / 1500 / 1800 / 1200 ms |
| 取一箱 / 取两箱 | 1500 / 2500 ms | 700 / 1200 ms |
| 整摞取 / 建 / 还 | 2000 / 1500 / 1000 ms | 900 / 700 / 500 ms |
| 翻顶 | 6000 ms | 1500 ms |
| 接球 / 放球 | 2000 / 2000 ms | 900 / 900 ms |

5 m/s 是 127–203 mm 麦克纳姆轮配 400–600 rpm 减速电机的上限区间；转向与上下楼按「与行走并行完成的机构动作」压缩，不再串行占用整段时间。`config.py` 里的教学默认值保持不变。

### 随机参数干扰

`platform.json` 里的 `perturbation` 决定哪些批次给参数加抖动：

```json
"perturbation": {"enabled": true, "applies_to": ["official"], "speed_pct": 3, "accel_pct": 3, "time_pct": 2}
```

- 一组换边双局用**同一组**抖动，且双方参数相同——否则就变成「一方机器人更慢」的不公平对决，而不是鲁棒性测试。
- 抖动种子由 `队伍 | 对手 | 批次类型 | nonce` 派生，倍率和最终参数写进 `series.matches`，批次配置里也留了记录，所以照样能复现。
- `applies_to` 可选 `official / manual / auto_round / all_play`；默认只对正式赛开启，训练对照保持干净参数。

```powershell
python -m robocon_sim demo --output runs/demo-first
python -m robocon_sim replay runs/demo-first
python -m robocon_sim match --red examples/stack_builder.py --blue examples/adaptive_raider.py --output runs/my-match --duration-ms 22000
python -m robocon_sim series --red examples/stack_builder.py --blue examples/adaptive_raider.py --count 2 --output runs/my-pair
python -m unittest discover -s tests -v
```

每次单局或演示的 `--output` 请用新目录或空目录。命令行、网页单局以及自定义配置的网页系列均为训练，不更新 Elo。

只有网页默认换边双局和十局场景包计榜，创建时需要选双方当前激活版本。新上传不会改变已创建任务的冻结版本。换边交换策略，场地和供料继续绑定红蓝；十局由四组常规双局和一组故障双局组成，场景表在 `platform.json` 的 `scenarios["10"]`。完整双局按胜负均值结算一次，Elo 初值 1000、K=24。平台故障重试一次，无法完整完成则作废；旧尝试不能覆盖新尝试。策略回调硬超限或连续三帧异常属于技术结果。

## 比赛包能回答什么

| 文件 | 用途 |
|---|---|
| `snapshots.rcf` | 逐帧状态：静态块一次、每 100 帧一个关键帧、其余帧只存与上一帧的差异，附帧索引。 |
| `manifest.json` | 核对双方源码、地图、两方参数档、种子、故障配置、规则/SDK/协议版本及实现文件摘要。 |
| `commands.jsonl.gz` | 逐帧只含 `seq / sim_time_ms / decisions / failures`，即重演所需的最小集合。 |
| `receipts.jsonl.gz` | 只在有命令判定的帧写入裁判回执，便于追查「为什么没动」。 |
| `events.jsonl`、`diagnostics.jsonl` | 世界事件；启动结果与策略异常。 |
| `integrity.json` | 每个文件的字节数与 crc32，以及终局状态摘要。 |
| `score_ledger.json`、`result.json` | 核对逐件得分、破平、资格、放球、失败和故障状态。 |
| `series_report.json` | 按 A/B 汇总胜局、均分、资格与放球，常规和故障场景分别列出。 |

重演使用当时记录的决定和技术失败，复核世界状态和事件，不再次运行策略。`python -m robocon_sim replay <目录>` 会解码每一帧、重跑内核并**逐帧与记录状态全等比较**，所以篡改会被发现；rfc 里的 crc32 只用于发现意外损坏，不是密码学签名。查看某一帧不需要写脚本：

```powershell
python -m robocon_sim dump runs/demo-first --meta          # 帧索引：时间、是否关键帧、变更字段数
python -m robocon_sim dump runs/demo-first --frame 900     # 该帧完整状态（含地图与参数）
```

实现摘要有差异时不能当作新版重演通过。资格与放球时间来自事件，未达成保留空值。

## 当前验证和使用边界

132 项自动测试通过。同一组 `stack_builder.py`（红）对 `adaptive_raider.py`（蓝）180 秒、赛级基线下比分 500 / 670，1800 帧重演通过。切换到 `training` 档可以复现 3 m/s 的旧节奏。

单局产物体积与耗时（本机实测，同一台机器）：

| | 第一版（3 m/s，JSON 全量） | 现在（5 m/s，二进制增量 + 精简 IPC） |
|---|---|---|
| 单局比赛包 | 79.7 MB | **833 KB** |
| 其中逐帧状态 | 77.2 MB（`snapshots.jsonl`） | 624 KB（`snapshots.rcf`） |
| 其中命令日志 | 643 KB（`commands.jsonl`） | 15.6 KB（`commands.jsonl.gz`） |
| 单局端到端 | 29.1 s | **5.8 s**（其中约 2.5 s 是 1800 次进程往返的固定开销） |
| 浏览器打开回放 | 一次下载 77 MB | 首屏约 90 KB，之后每块 50 帧 |

单局时间已经接近这套架构的下限：每帧必须等两个子进程各返回一次决定，实测一次往返 1.1–1.5 ms，1800 帧仅往返就要 2 秒以上。再要一个数量级只能把策略放进主进程（放弃子进程隔离与预算），或者降低决策频率（改规则），两者都不属于性能优化。

这是单机测量，不是并发容量结论。策略在独立持久子进程运行，启动预算 5 秒；回调预算为 20 ms CPU 和 200 ms 墙钟，父进程同时轮询进程内存，超过 256 MiB 时终止。

`--trusted-code` 仅用于已审查队内代码，服务限本机回环访问。策略与服务同一操作系统用户，可能访问该用户可读文件、环境及网络；限时子进程不构成安全沙箱。不要公开端口、反向代理或接收匿名代码。

地图提供规则尺寸的 2.5D 教学模型，作业位、路网和局部安放位置是平台假设。车辆碰撞、视觉、实车控制与 TR 策略不在本版。动作参数待实车标定，整摞建塔的正式认可与计分待核项见[规则裁决](../05_规则裁决与计分.md)。
