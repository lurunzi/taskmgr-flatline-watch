The English version governs.

<a id="en"></a>

# Task Manager CPU flatline: roadmap

**English** · [中文](#zh)

English is the controlling text.

Reviewed 25 October 2026. This page states what "done" means, where each workstream stands, what a critical review of the work so far found, and the next actions in order. Evidence stays in the [consolidated evidence review](investigation-status.md) and the [21 October incident review](flatline-incident-20261021.md); this page links to them rather than repeating them. Updated the same day with the native freeze signature.

## Goal and definition of done

The problem is the Task Manager total CPU chart freezing as a flat line while the logical-processor charts keep moving. It is resolved when all of the following hold:

1. `TaskManagerDataLayer.dll` changes, and static comparison shows that an exhausted `QueryProcessInformation` query is no longer published as success.
2. With the watcher running in its normal mode, 14 consecutive days of ordinary use produce no confirmed flatline.
3. The decision to disable the watcher's autostart is taken manually. The tools and evidence are kept.

If flatlines continue on a fixed DLL, the visual failure has another cause and the causal workstream reopens. Baseline: 120 recovery folders from 4 to 25 October 2026, 0–12 per day. The DLL in use is still the 9549 file (SHA-256 `0cc75ace…3867`, written 13 October 2026).

## Status by workstream

| Workstream | Status | Exit criterion |
|---|---|---|
| Mitigation: visual detection, restart, navigation | Running | Retired only under the definition of done |
| Native freeze signature: Taskmgr's own ETW error events | Probing since 25 October; repository code unchanged | The N1 decision rule below is met or failed |
| Evidence: light ETW, offline analysis, retry | Running; retry added 25 October | Every incident has a report or a recorded reason |
| Defect identity | Done: 9278 native execution; 9549 static comparison plus real-kernel and controlled query runs | — |
| Escalation | Feedback Hub report [AA13rk15](https://aka.ms/AA13rk15) exists; the corrected submission material is not yet sent | Corrected material posted by the owner |
| Causal chain within one incident | Open and parked; live address-chain collection retired on 13 October | One incident with continuous lineage from query buffer to frozen summary |
| Association with process churn | Open; per-incident reports only, no cross-incident result | A pre-stated comparison across at least 20 incidents |
| Fix watch | Manual; nothing records DLL changes | A hash change is detected and assessed |

## Critical review of the work so far

1. **Detection and analysis still read pixels, although Task Manager reports its own failures.** The aggregation error `0x8007139F` was previously seen only with a debugger attached, through `OutputDebugStringW`; a passive listener without a debugger received none from Taskmgr in 90 s. But `TaskManagerDataLayer.dll` and `Taskmgr.exe` both embed the TraceLogging provider `Microsoft.Windows.Taskmgr` (`{2e635d8e-1107-4555-9319-32eeb895aaae}`, read from their metadata). An ordinary ETW session receives it without a debugger. In a 10-second sample it produced `FallbackError` events at error level, each carrying the HRESULT, module, thread, source file and line, and a once-per-second `CurrentUpdateCycle` pair (`UpdateStarted`/`UpdateFinalized`) from the data model. No `0x8007139F` appeared in that sample, which contained no flatline. If the error appears at each freeze, it gives an exact native onset instead of a pixel estimate, and items 2 and 3 below lose most of their weight.
2. **The analysed onset window is too late.** The detector needs the rightmost 14 of 71 chart pixels to be flat. The chart moves about 1.25 px/s, so when the first candidate is flagged the total line has already been flat for 11–14 s. This was measured on the 28 incidents since 20 October that had connected markers; the exceptions were already flat when first observed. The "before-first" window covers only the last 2–3.8 s before that flag, so it lies inside the freeze. The "normal control" that ends at `last_normal` is also partly inside it. Every before-first ranking since 20 October therefore describes activity after the summary had stopped changing. The reports say the boundary is not the native fault time, but the window systematically misses the visible onset. A one-pixel quantisation can lengthen a flat run at low load, so the estimated onset is an upper bound on how long the line has been flat.
3. **Per-incident rankings cannot answer the open question.** Each report lists at most three companion processes and is labelled "attribution unproven". No result compares incidents with each other or with controls, so 120 incidents have not changed the state of the churn hypothesis.
4. **The recording cannot see the defect itself.** Light ETW records process and thread starts and exits. It does not record NtQuerySystemInformation status codes, retry counts or buffers, so it can support an association but never the causal chain.
5. **The flat line is not checked against an independent counter.** The detector already records `system_cpu_percent`, but neither confirmation nor the reports compare it with the height of the frozen line. On 24 October at 16:06 the line stood at about 67% while the counter read 100%; that disagreement is the strongest evidence that the incident was a freeze and not a true constant load, and it is not reported.
6. **Escalation lags the evidence.** The evidence review says corrected submission material should be prepared first. It has not been sent, and a fix from Microsoft is the only route to the definition of done.
7. **Operational gaps.**
   - Shutdown can wait for a running analysis attempt for up to 300 s (it was 120 s before the retry change). `Start.cmd` exits silently while the old process still holds the single-instance mutex, so stopping and immediately starting can leave no watcher running.
   - An analysis timeout ends only `process-incident.py`. Its `tracerpt.exe` child can keep writing into the incident folder.
   - About 4.4 GiB of the 40 GiB ETW budget is used, and incident folders since 19 October average about 54 MB. At five incidents a day, recording will block in roughly four months unless a retention decision is made; evidence is never deleted automatically.

## Next actions, in order

| # | Action | Done when | Cost and risk |
|---|---|---|---|
| N1 | Validate the native signature. The probe session `TaskmgrWilProbe` records this provider at error level into `D:\TaskmgrFreezeCaptures\wil-probe-20261025\wil.etl` (circular, 128 MB, QPC clock, lost on reboot). After each flatline, compare `FallbackError` events from `TaskManagerDataLayer.dll` with the visual markers | The decision rule below is met or failed | No repository change; under 1 MB/h at error level |
| N2 | If N1 passes, add the provider at error level to the light ETW session and anchor the offline analysis on the first native error, with process and thread activity in a window before it | Tests pass; new reports name the native onset | Repository code; replaces A1 and A2 |
| N3 | If N1 passes, trigger recovery on the native error, using a real-time ETW consumer, and keep the visual detector as a fallback and cross-check | Each recovery records which signal fired first | Repository code; recovery behaviour changes, so it needs a stop and start check |
| N4 | Record `CurrentUpdateCycle` with an event-name filter, so that the high-volume `NetworkUtilizationRefresh` at the same level is excluded, and test whether the data model keeps cycling during a freeze | Answered for at least three incidents | Needs `EnableTraceEx2` with a name filter; `logman` cannot do it |
| A1 | Fallback, only if N1 fails: add an onset phase to the offline analysis: estimate when the line went flat from the flat-run length in the first-candidate image and the measured chart speed, and analyse a window around that time with a control earlier in the same trace | Unit tests pass; reports state the estimate and its uncertainty | Repository code only; no new collection |
| A2 | Fallback, only if N1 fails: reanalyse the retained incidents since 20 October into new `--output` folders and build one cross-incident table: process and thread start rates in the onset window against several normal windows from the same trace | The table exists; the decision rule below was written down before the run | A few CPU-hours; originals untouched |
| A3 | Report the frozen-line height against `system_cpu_percent` in each observation and report | The discrepancy appears in new reports | Small |
| A4 | Record the `TaskManagerDataLayer.dll` SHA-256 in `status.json` at watcher start; when it changes, run the static comparison | A hash change is visible without manual checks | Small |
| A5 | Fix the operational gaps: `Stop.ps1` waits for the process to exit, `Start.cmd` reports an instance that is already running, and an analysis timeout ends the whole process tree | Tests cover each; one real stop and start cycle checked | Small |
| A6 | Finalise the corrected Feedback Hub material and add the 9549 result and the incident rate | The owner posts it | Owner only; external publication |
| A7 | Decide whether to resume live query tracing on 9549 for the causal chain | A recorded decision | Parked unless N1 or A2 shows a clear association and the owner authorises it |

Decision rule for N1: the signature passes if, in the first 10 flatlines recorded by the probe, `0x8007139F` from `TaskManagerDataLayer.dll` occurs in at least 9 no later than the first flat candidate, and if outside flatlines it occurs at most once a day. It fails if it is missing from 3 or more of those 10; the analysis then falls back to A1 and A2.

Decision rule for A2: if, across at least 20 incidents, the median ratio of onset-window to normal-window process and thread start rates is no higher than 1.2, process churn is not a visible trigger. Further companion-process rankings would then stop. If the ratio is clearly higher, report the effect size and take it to A6 and A7.

---

<a id="zh"></a>

# 任务管理器 CPU 平线：路线图

[English](#en) · **中文**

以英文为准。复核日期为 2026 年 10 月 25 日。本页说明怎样算完成、各条工作线的现状、对已有工作的批判性审查，以及按顺序排列的下一步行动。证据仍以[统一证据摘要](investigation-status.md#zh)和[10 月 21 日事件复核](flatline-incident-20261021.md#zh)为准，本页只链接，不重复。同日补充原生卡死特征。

## 目标与完成标准

问题是任务管理器的 CPU 总图冻结成一条水平线，而各逻辑处理器图仍在变化。以下三条同时满足才算解决：

1. `TaskManagerDataLayer.dll` 发生变化，且静态比对显示查询重试耗尽时不再按成功发布结果。
2. 监测器按正常模式运行，日常使用连续 14 天没有确认的平线。
3. 关闭监测器自启动由人手动决定，工具和证据保留。

如果换了已修复的 DLL 仍出现平线，说明视觉故障另有原因，因果工作线重新打开。基线：2026 年 10 月 4 日至 25 日共 120 个恢复目录，每天 0–12 次。当前使用的仍是 9549 版本文件（SHA-256 `0cc75ace…3867`，写入时间 2026 年 10 月 13 日）。

## 各工作线现状

| 工作线 | 现状 | 退出条件 |
|---|---|---|
| 缓解：视觉检测、重启、导航 | 运行中 | 只按完成标准退役 |
| 原生卡死特征：Taskmgr 自身的 ETW 错误事件 | 10 月 25 日起探测中；仓库代码未改 | 下文 N1 判定规则得出通过或不通过 |
| 证据：轻量 ETW、离线分析、重试 | 运行中；10 月 25 日加入重试 | 每次事件都有报告或记录在案的原因 |
| 缺陷认定 | 已完成：9278 原函数执行；9549 静态比对加真实内核与受控查询 | — |
| 上报 | 反馈中心报告 [AA13rk15](https://aka.ms/AA13rk15) 已存在；修订后的提交材料尚未发出 | 负责人发布修订材料 |
| 同一事件内的因果链 | 未闭合，已搁置；10 月 13 日停止现场地址链采集 | 一次事件内从查询缓冲区到冻结摘要的连续来源链 |
| 与进程增减的关联 | 未闭合；只有单次报告，没有跨事件结论 | 至少 20 次事件、事先写定规则的比较 |
| 修复监视 | 手动；没有记录 DLL 变化 | 能发现哈希变化并完成评估 |

## 对已有工作的批判性审查

1. **检测和分析仍在读像素，而任务管理器自己会报告故障。** 聚合错误 `0x8007139F` 以前只能在附加调试器时通过 `OutputDebugStringW` 看到；不附加调试器的被动监听 90 秒内没有收到 Taskmgr 的任何一条。但 `TaskManagerDataLayer.dll` 和 `Taskmgr.exe` 都内置 TraceLogging 提供程序 `Microsoft.Windows.Taskmgr`（`{2e635d8e-1107-4555-9319-32eeb895aaae}`，从两者的元数据读出），普通 ETW 会话无需调试器即可接收。约 10 秒的样本中，它发出错误级的 `FallbackError`，每条带 HRESULT、模块、线程、源文件和行号；数据模型每秒还发出一对 `CurrentUpdateCycle`（`UpdateStarted`/`UpdateFinalized`）。该样本中没有 `0x8007139F`，当时也没有平线。如果每次冻结都出现这个错误，它就给出精确的原生起点，取代像素估算，下面第 2、3 条的分量随之大减。
2. **分析的起点窗口太晚。** 检测器要求图表最右侧 71 像素中的 14 像素持平。图表约每秒移动 1.25 像素，所以首次判为候选时，总图已经平了 11–14 秒。这是在 10 月 20 日以来 28 次有连续标记的事件上量出来的；例外的几次在首次观察时已经是平的。"平线前"窗口只覆盖候选前最后 2–3.8 秒，因此落在冻结期内。以 `last_normal` 结束的"正常对照"也有一部分落在冻结期内。10 月 20 日以来所有平线前排行描述的都是摘要停止变化之后的活动。报告虽注明边界不是底层故障时刻，但这个窗口系统性地错过了可见的起点。低负载时一像素的量化会拉长平线，因此估出的起点是"已平多久"的上限。
3. **单次排行回答不了未决问题。** 每份报告最多列三个伴随进程，并注明"未证明归因"。没有跨事件或与对照的比较，所以 120 次事件没有推进进程增减假设的判断。
4. **记录看不到缺陷本身。** 轻量 ETW 只记录进程和线程的启动与退出，不记录 NtQuerySystemInformation 的返回码、重试次数或缓冲区，因此最多支持关联，永远无法证明因果链。
5. **平线没有与独立计数器核对。** 检测器已经记录 `system_cpu_percent`，但确认和报告都没有拿它与冻结线的高度比较。10 月 24 日 16:06 那次平线停在约 67%，计数器是 100%；这个差异是"冻结而非真实恒定负载"的最强证据，却没有出现在报告里。
6. **上报落后于证据。** 证据摘要要求先准备修订后的提交材料，至今没有发出，而微软修复是达到完成标准的唯一途径。
7. **运行缺口。**
   - 退出时可能要等正在进行的分析尝试最多 300 秒（加入重试前为 120 秒）。旧进程仍占用单实例互斥量时，`Start.cmd` 会静默退出，所以停止后立刻启动可能导致没有监测器在运行。
   - 分析超时只结束 `process-incident.py`，它的 `tracerpt.exe` 子进程可能继续写事件目录。
   - 40 GiB 的 ETW 预算已用约 4.4 GiB，10 月 19 日以来每个事件目录平均约 54 MB。按每天五次估算，约四个月后记录会被阻止，需要事先决定保留策略；证据不会被自动删除。

## 下一步行动（按顺序）

| # | 行动 | 完成标志 | 成本与风险 |
|---|---|---|---|
| N1 | 验证原生特征。探测会话 `TaskmgrWilProbe` 以错误级记录该提供程序，写入 `D:\TaskmgrFreezeCaptures\wil-probe-20261025\wil.etl`（循环 128 MB，QPC 时钟，重启后消失）。每次平线后，把来自 `TaskManagerDataLayer.dll` 的 `FallbackError` 与视觉标记对照 | 下面的判定规则得出通过或不通过 | 不改仓库；错误级每小时不到 1 MB |
| N2 | N1 通过后，把该提供程序以错误级并入轻量 ETW 会话，离线分析以第一条原生错误为起点，分析它之前窗口内的进程和线程活动 | 测试通过；新报告给出原生起点 | 改仓库代码；取代 A1 和 A2 |
| N3 | N1 通过后，用实时 ETW 消费者在原生错误出现时触发恢复，视觉检测保留作兜底和交叉核对 | 每次恢复记录哪个信号先触发 | 改仓库代码；恢复行为变化，需要实际停止和启动核对 |
| N4 | 用事件名过滤记录 `CurrentUpdateCycle`，排除同级别、量很大的 `NetworkUtilizationRefresh`，检验冻结期间数据模型是否仍在循环 | 至少三次事件得到答案 | 需要带名称过滤的 `EnableTraceEx2`；`logman` 做不到 |
| A1 | 兜底，仅在 N1 不通过时做：在离线分析中加入起点阶段：用首次候选截图里的平线长度和量得的图表速度估计开始变平的时间，分析该时间附近的窗口，并在同一 trace 更早处取对照 | 单元测试通过；报告写明估计值及其不确定度 | 只改仓库代码，不新增采集 |
| A2 | 兜底，仅在 N1 不通过时做：把 10 月 20 日以来保留的事件重新分析到新的 `--output` 目录，汇总成一张跨事件表：起点窗口与同一 trace 中若干正常窗口的进程和线程启动速率 | 表格完成；下面的判定规则在运行前已写定 | 数个 CPU 小时；原始证据不动 |
| A3 | 在每份观察和报告中给出冻结线高度与 `system_cpu_percent` 的差异 | 新报告出现该差异 | 小 |
| A4 | 监测器启动时把 `TaskManagerDataLayer.dll` 的 SHA-256 写入 `status.json`；变化时做静态比对 | 不用手动检查就能看到哈希变化 | 小 |
| A5 | 补运行缺口：`Stop.ps1` 等进程退出，`Start.cmd` 提示已有实例在运行，分析超时时结束整个进程树 | 各有测试覆盖；实际停止和启动一次核对 | 小 |
| A6 | 定稿修订后的反馈中心材料，补充 9549 结果和事件频率 | 负责人发布 | 仅负责人；属对外发布 |
| A7 | 决定是否为因果链恢复 9549 上的现场查询追踪 | 有记录在案的决定 | 搁置，除非 N1 或 A2 显示明确关联且负责人授权 |

N1 的判定规则：探测记录到的前 10 次平线中，至少 9 次在首次平线候选之前或同时出现来自 `TaskManagerDataLayer.dll` 的 `0x8007139F`，且平线之外每天最多出现一次，则通过。10 次中有 3 次或以上缺失则不通过，分析退回 A1 和 A2。

A2 的判定规则：如果至少 20 次事件中，起点窗口与正常窗口的进程和线程启动速率之比的中位数不高于 1.2，则进程增减不是可见的触发条件，停止继续做伴随进程排行。如果明显更高，报告效应大小，并转入 A6 和 A7。
