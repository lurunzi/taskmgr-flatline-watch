The English version governs.

<a id="en"></a>

# Provoked reproduction by process churn, 1 November 2026 (J17)

**English** · [中文](#zh)

English is the controlling text.

## Result

**The test did not show that process churn reproduces the freeze on demand.**

- Over 80 minutes of alternating blocks, one freeze occurred during a load block and none during quiet blocks.
- Natural freezes on this machine ran at about 0.4 per hour over the preceding day. At that rate, a single freeze in 40 minutes of load is unremarkable: the chance of at least one is about 23%.
- The result is inconclusive. It is neither a reproduction recipe nor evidence against churn as a contributing factor.

## Method

- Tool: `tools/churn-stress.py` (commit `e125fc6`, attribution fixed afterwards).
- Schedule: eight 10-minute blocks, alternating quiet and load, starting quiet, from 16:09:48 to 17:29:49 UTC.
- Load: 4 threads repeatedly running `cmd.exe /d /c exit`. Each `cmd` also starts a `conhost`.
- The watcher ran normally. The tool touched neither Task Manager, the watcher nor any ETW session.
- A freeze is attributed to the block in which its native `0x8007139F` error occurred. The tool first attributed it by recovery time, which put it in the following quiet block; the corrected receipt is `receipt-native-attribution.json`.
- Evidence: `D:\TaskmgrFreezeCaptures\stress-20261101-160948\`, containing `receipt.json`, `receipt-native-attribution.json`, `samples.json` and `progress.json`.

## Blocks

| Block | Kind | UTC | Process starts (`cmd` only) | Per second | Freezes (native time) |
|---:|---|---|---:|---:|---:|
| 0 | quiet | 16:09:48–16:19:49 | 0 | 0 | 0 |
| 1 | load | 16:19:49–16:29:49 | 20,616 | 34.4 | 0 |
| 2 | quiet | 16:29:49–16:39:49 | 0 | 0 | 0 |
| 3 | load | 16:39:49–16:49:49 | 87,942 | 146.6 | 0 |
| 4 | quiet | 16:49:49–16:59:49 | 0 | 0 | 0 |
| 5 | load | 16:59:49–17:09:49 | 74,591 | 124.2 | 1 |
| 6 | quiet | 17:09:49–17:19:49 | 0 | 0 | 0 |
| 7 | load | 17:19:49–17:29:49 | 91,406 | 152.3 | 0 |

Totals: quiet, 0.667 h with 0 freezes; load, 0.667 h with 1 freeze (1.5 per hour) and 274,555 `cmd` starts.

## The one freeze

- Recovery folder: `restart-20261101-171355-621305`.
- The native `0x8007139F` occurred at 17:08:42.785 UTC, about 9 minutes into load block 5.
- The call chain matched every earlier freeze: two logs, then returns at `aggregationdatamonitor.cpp:640` and `:159`, then an exception at `Core.cpp:407`.
- The data-model heartbeat continued throughout. It counts as N1 conforming; the tally stands at 15 of 15.
- In the 2 s before the error there were 564 process starts, against a same-trace control median of 551.5 and a maximum of 678. That is no burst relative to the surrounding load.
- The report shows 0 thread starts in that window although the trace holds 209,190 thread-start events. This is an analysis anomaly under extreme churn and is being checked separately.

## Problems found during the run

1. **The Task Manager window was minimized from 16:20:32 to 17:13:43 UTC.** The visual detector was blind for 53 minutes and the off-page navigation retries could not restore a minimized window.
   - Native monitoring continued: provider events and heartbeats kept arriving.
   - The freeze at 17:08:42 shows that aggregation, and the freeze, continue while the window is minimized. The blind blocks therefore still count.
   - Recovery waited until the window was restored, so it came about 5 minutes after the error.
2. **Load intensity varied between blocks.** Block 1 ran at 34 `cmd` starts per second, against 124–152 in the other load blocks. The cause is unknown.

## Limits

- One machine, one run of 80 minutes, one DLL build (9549).
- The load was only short-lived `cmd.exe` processes. The defect needs the snapshot size to change during a query. Processes with many threads, or thread churn inside long-lived processes, change the size differently and were not tested.
- With 1 event, the plausible load-block rate spans roughly 0.04–8.4 per hour (95%), so the test cannot exclude either no effect or a large one.

## Next steps

- A longer run with larger blocks (for example 4 × 30 minutes) and a thread-churn load. Only after that should a reproduction claim be made or withdrawn.
- The watcher should detect a minimized Task Manager and restore it without taking focus when the user is not active.
- Explain the zero thread-start count in the analysis of `restart-20261101-171355-621305`.

---

<a id="zh"></a>

# 用进程增减压力诱发复现，2026 年 11 月 1 日（J17）

[English](#en) · **中文**

以英文为准。

## 结论

**本次测试没有证明进程增减压力能按需复现冻结。**

- 80 分钟内安静与加压交替：加压段出现 1 次冻结，安静段 0 次。
- 前一天本机自然冻结约每小时 0.4 次。按这个频率，40 分钟加压内出现一次冻结并不稀奇：至少出现一次的概率约 23%。
- 结果不确定：既不能当作复现配方，也不能据此否定进程增减是促成因素。

## 方法

- 工具：`tools/churn-stress.py`（提交 `e125fc6`，归段方式随后修正）。
- 时间安排：8 段、每段 10 分钟，安静与加压交替、安静先行，UTC 16:09:48 至 17:29:49。
- 负载：4 个线程反复运行 `cmd.exe /d /c exit`，每个 `cmd` 还会附带启动一个 `conhost`。
- 监测器照常运行；测试工具不触碰任务管理器、监测器或任何 ETW 会话。
- 冻结按其原生 `0x8007139F` 错误发生时所在的段归属。工具最初按恢复时间归段，把这次冻结算进了随后的安静段；更正后的回执是 `receipt-native-attribution.json`。
- 证据：`D:\TaskmgrFreezeCaptures\stress-20261101-160948\`，包括 `receipt.json`、`receipt-native-attribution.json`、`samples.json` 和 `progress.json`。

## 各段结果

| 段 | 类型 | UTC | 进程启动（仅 `cmd`） | 每秒 | 冻结（按原生错误时间） |
|---:|---|---|---:|---:|---:|
| 0 | 安静 | 16:09:48–16:19:49 | 0 | 0 | 0 |
| 1 | 加压 | 16:19:49–16:29:49 | 20,616 | 34.4 | 0 |
| 2 | 安静 | 16:29:49–16:39:49 | 0 | 0 | 0 |
| 3 | 加压 | 16:39:49–16:49:49 | 87,942 | 146.6 | 0 |
| 4 | 安静 | 16:49:49–16:59:49 | 0 | 0 | 0 |
| 5 | 加压 | 16:59:49–17:09:49 | 74,591 | 124.2 | 1 |
| 6 | 安静 | 17:09:49–17:19:49 | 0 | 0 | 0 |
| 7 | 加压 | 17:19:49–17:29:49 | 91,406 | 152.3 | 0 |

合计：安静 0.667 小时、0 次冻结；加压 0.667 小时、1 次冻结（每小时 1.5 次），`cmd` 启动 274,555 次。

## 这一次冻结

- 恢复目录：`restart-20261101-171355-621305`。
- 原生 `0x8007139F` 出现在 UTC 17:08:42.785，即加压第 5 段开始后约 9 分钟。
- 调用链与以往每次相同：先是两条日志，再从 `aggregationdatamonitor.cpp:640` 和 `:159` 返回，最后在 `Core.cpp:407` 抛出异常。
- 数据模型心跳全程持续。按 N1 计为符合，累计 15 次中 15 次符合。
- 错误前 2 秒有 564 次进程启动，同一 trace 中对照窗口的中位数为 551.5、最大值为 678，相对于周围的负载并没有突增。
- 报告显示该窗口内线程启动为 0，但 trace 里有 209,190 条线程启动事件。这是极高负载下的分析异常，另行核查。

## 测试中发现的问题

1. **任务管理器窗口在 UTC 16:20:32 至 17:13:43 被最小化。** 视觉检测因此失明 53 分钟，离页导航重试也无法还原最小化的窗口。
   - 原生监测没有中断，提供程序事件和心跳持续到达。
   - 17:08:42 的冻结说明，窗口最小化时聚合照常进行，也照样会冻结，所以失明期间的各段仍然有效。
   - 恢复要等到窗口被还原后才发生，比错误晚了约 5 分钟。
2. **各加压段负载强度不同。** 第 1 段每秒只有 34 次 `cmd` 启动，其余加压段为每秒 124–152 次，原因不明。

## 局限

- 只有一台机器、一次 80 分钟的运行、一个 DLL 版本（9549）。
- 负载只有短命的 `cmd.exe` 进程。缺陷需要的是查询期间快照大小发生变化；线程数多的进程，或长寿命进程内部的线程增减，改变大小的方式不同，本次没有测试。
- 只有 1 次事件，加压段频率的 95% 合理区间约为每小时 0.04–8.4 次，因此既不能排除"没有影响"，也不能排除"影响很大"。

## 下一步

- 用更长的段（例如 4 × 30 分钟）和线程增减负载再跑一轮。在那之前，不下"能复现"或"不能复现"的结论。
- 监测器应能识别被最小化的任务管理器，并在用户不活动时以不抢焦点的方式还原。
- 查明 `restart-20261101-171355-621305` 分析中线程启动为 0 的原因。
