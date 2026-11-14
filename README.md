# BR 策略竞技场：在本机运行、用比赛包复盘

本版可直接运行两份 Python 策略，按同一个比赛时钟推进 BR，保存逐帧回放和逐件计分。默认 180 秒、每 100 ms 决策一次。请先跑通示例，再改一项策略选择，用同一场景的回放比较差别。规则、动作时间和地图假设以[上级方案](../README.md)与代码为准。

> **不兼容旧版本**：快照改为二进制增量格式（`.rcf`），数据库 schema 升到 `user_version=1`。旧比赛包不能读取、旧数据库会被拒绝加载，需要删掉数据目录重新开始。旧策略源码已归档到 [`examples/archive/`](examples/archive/INDEX.md)。

## 在 Windows 上开始第一局

安装 Python 3.11 或以上版本；从本目录运行，不需要安装第三方运行依赖。

```powershell
python -m robocon_sim demo --output runs/demo-first
python -m robocon_sim replay runs/demo-first
python -m robocon_sim serve --host 127.0.0.1 --port 8765 --trusted-code
```

用浏览器打开 `http://127.0.0.1:8765`。用不同队名分别上传 [stack_builder.py](examples/stack_builder.py) 和 [adaptive_raider.py](examples/adaptive_raider.py)，选择两份版本后创建单局或换边系列。队列显示已完成后，点击该局的比分打开回放。拖动时间轴或点击事件即可查看当时状态；最终得分从内核账本读取。

每个版本只提交一份 UTF-8 `.py` 文件，最大 256 KiB，定义一个直接继承 `brsdk.Strategy` 的类，实现同步的 `on_step(self, obs, api)`。上传会检查语法和入口并生成源码摘要，执行前仍由操作者审查。

要体验跨层建塔，上传 [`l1_reach_builder.py`](examples/l1_reach_builder.py) 和任一基线策略。

## 配置：`platform.json`

比赛时长、tick、教学参数、十局场景表、帧存储选项都在 `<数据目录>/platform.json`（默认 `data/l1-reach/platform.json`，模板见 [`platform.example.json`](platform.example.json)）。

- **热加载**：服务在**创建对战之前**比较文件 mtime，读到新内容就用新内容。已经创建的对局把当时的展开参数冻结在 SQLite 的 `series.matches` 里，改配置不会影响正在运行或排队中的对局。
- **写坏了不会崩**：JSON 非法、字段未知或参数越界时保留上一份可用配置，`/api/config` 的 `warnings` 会列出错误，终端也会打印一行。
- **命令行**：`serve --config <文件>`、`match/series --config <文件>` 使用同一套 schema；不指定时读数据目录下的 `platform.json`，文件不存在则用内置默认值。

```powershell
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
| `manifest.json` | 核对双方源码、地图、参数、种子、故障配置、规则/SDK/协议版本及实现文件摘要。 |
| `commands.jsonl`、`events.jsonl` | 区分策略提交、裁判回执、异常和世界事件。 |
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

112 项自动测试通过。同一组 `stack_builder.py`（红）对 `adaptive_raider.py`（蓝）180 秒：比分 175 / 475，改版前后逐件账本完全一致，1800 帧重演通过。

单局产物体积与耗时（本机实测，同一台机器）：

| | 改版前 | 改版后 |
|---|---|---|
| 单局比赛包 | 79.7 MB（`snapshots.jsonl` 77.2 MB） | **1.3 MB**（`snapshots.rcf` 626 KB） |
| 单局端到端 | 29.1 s | **9.4 s** |
| 重演校验 | ~4 s | ~1.6 s |
| 浏览器打开回放 | 一次下载 77 MB | 首屏约 90 KB，之后每块 50 帧 |

这是单机测量，不是并发容量结论。策略在独立持久子进程运行，启动预算 5 秒；回调预算为 20 ms CPU 和 200 ms 墙钟，父进程同时轮询进程内存，超过 256 MiB 时终止。

`--trusted-code` 仅用于已审查队内代码，服务限本机回环访问。策略与服务同一操作系统用户，可能访问该用户可读文件、环境及网络；限时子进程不构成安全沙箱。不要公开端口、反向代理或接收匿名代码。

地图提供规则尺寸的 2.5D 教学模型，作业位、路网和局部安放位置是平台假设。车辆碰撞、视觉、实车控制与 TR 策略不在本版。动作参数待实车标定，整摞建塔的正式认可与计分待核项见[规则裁决](../05_规则裁决与计分.md)。
