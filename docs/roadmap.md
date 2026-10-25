The English version governs.

<a id="en"></a>

# Task Manager CPU flatline: roadmap

**English** · [中文](#zh)

English is the controlling text.

Reviewed 25 October 2026. This page states what "done" means, where each workstream stands, what a critical review of the work so far found, and the next actions in order. Evidence stays in the [consolidated evidence review](investigation-status.md) and the [21 October incident review](flatline-incident-20261021.md); this page links to them rather than repeating them.

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
| Evidence: light ETW, offline analysis, retry | Running; retry added 25 October | Every incident has a report or a recorded reason |
| Defect identity | Done: 9278 native execution; 9549 static comparison plus real-kernel and controlled query runs | — |
| Escalation | Feedback Hub report [AA13rk15](https://aka.ms/AA13rk15) exists; the corrected submission material is not yet sent | Corrected material posted by the owner |
| Causal chain within one incident | Open and parked; live address-chain collection retired on 13 October | One incident with continuous lineage from query buffer to frozen summary |
| Association with process churn | Open; per-incident reports only, no cross-incident result | A pre-stated comparison across at least 20 incidents |
| Fix watch | Manual; nothing records DLL changes | A hash change is detected and assessed |

## Critical review of the work so far

1. **The analysed onset window is too late.** The detector needs the rightmost 14 of 71 chart pixels to be flat. The chart moves about 1.25 px/s, so when the first candidate is flagged the total line has already been flat for 11–14 s. This was measured on the 28 incidents since 20 October that had connected markers; the exceptions were already flat when first observed. The "before-first" window covers only the last 2–3.8 s before that flag, so it lies inside the freeze. The "normal control" that ends at `last_normal` is also partly inside it. Every before-first ranking since 20 October therefore describes activity after the summary had stopped changing. The reports say the boundary is not the native fault time, but the window systematically misses the visible onset. A one-pixel quantisation can lengthen a flat run at low load, so the estimated onset is an upper bound on how long the line has been flat.
2. **Per-incident rankings cannot answer the open question.** Each report lists at most three companion processes and is labelled "attribution unproven". No result compares incidents with each other or with controls, so 120 incidents have not changed the state of the churn hypothesis.
3. **The recording cannot see the defect itself.** Light ETW records process and thread starts and exits. It does not record NtQuerySystemInformation status codes, retry counts or buffers, so it can support an association but never the causal chain.
4. **The flat line is not checked against an independent counter.** The detector already records `system_cpu_percent`, but neither confirmation nor the reports compare it with the height of the frozen line. On 24 October at 16:06 the line stood at about 67% while the counter read 100%; that disagreement is the strongest evidence that the incident was a freeze and not a true constant load, and it is not reported.
5. **Escalation lags the evidence.** The evidence review says corrected submission material should be prepared first. It has not been sent, and a fix from Microsoft is the only route to the definition of done.
6. **Operational gaps.**
   - Shutdown can wait for a running analysis attempt for up to 300 s (it was 120 s before the retry change). `Start.cmd` exits silently while the old process still holds the single-instance mutex, so stopping and immediately starting can leave no watcher running.
   - An analysis timeout ends only `process-incident.py`. Its `tracerpt.exe` child can keep writing into the incident folder.
   - About 4.4 GiB of the 40 GiB ETW budget is used, and incident folders since 19 October average about 54 MB. At five incidents a day, recording will block in roughly four months unless a retention decision is made; evidence is never deleted automatically.

## Next actions, in order

| # | Action | Done when | Cost and risk |
|---|---|---|---|
| A1 | Add an onset phase to the offline analysis: estimate when the line went flat from the flat-run length in the first-candidate image and the measured chart speed, and analyse a window around that time with a control earlier in the same trace | Unit tests pass; reports state the estimate and its uncertainty | Repository code only; no new collection |
| A2 | Reanalyse the retained incidents since 20 October into new `--output` folders and build one cross-incident table: process and thread start rates in the onset window against several normal windows from the same trace | The table exists; the decision rule below was written down before the run | A few CPU-hours; originals untouched |
| A3 | Report the frozen-line height against `system_cpu_percent` in each observation and report | The discrepancy appears in new reports | Small |
| A4 | Record the `TaskManagerDataLayer.dll` SHA-256 in `status.json` at watcher start; when it changes, run the static comparison | A hash change is visible without manual checks | Small |
| A5 | Fix the operational gaps: `Stop.ps1` waits for the process to exit, `Start.cmd` reports an instance that is already running, and an analysis timeout ends the whole process tree | Tests cover each; one real stop and start cycle checked | Small |
| A6 | Finalise the corrected Feedback Hub material and add the 9549 result and the incident rate | The owner posts it | Owner only; external publication |
| A7 | Decide whether to resume live query tracing on 9549 for the causal chain | A recorded decision | Parked unless A2 shows a clear association and the owner authorises it |

Decision rule for A2: if, across at least 20 incidents, the median ratio of onset-window to normal-window process and thread start rates is no higher than 1.2, process churn is not a visible trigger. Further companion-process rankings would then stop. If the ratio is clearly higher, report the effect size and take it to A6 and A7.

---

<a id="zh"></a>

# 任务管理器 CPU 平线：路线图

[English](#en) · **中文**

以英文为准。复核日期为 2026 年 10 月 25 日。本页说明怎样算完成、各条工作线的现状、对已有工作的批判性审查，以及按顺序排列的下一步行动。证据仍以[统一证据摘要](investigation-status.md#zh)和[10 月 21 日事件复核](flatline-incident-20261021.md#zh)为准，本页只链接，不重复。

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
| 证据：轻量 ETW、离线分析、重试 | 运行中；10 月 25 日加入重试 | 每次事件都有报告或记录在案的原因 |
| 缺陷认定 | 已完成：9278 原函数执行；9549 静态比对加真实内核与受控查询 | — |
| 上报 | 反馈中心报告 [AA13rk15](https://aka.ms/AA13rk15) 已存在；修订后的提交材料尚未发出 | 负责人发布修订材料 |
| 同一事件内的因果链 | 未闭合，已搁置；10 月 13 日停止现场地址链采集 | 一次事件内从查询缓冲区到冻结摘要的连续来源链 |
| 与进程增减的关联 | 未闭合；只有单次报告，没有跨事件结论 | 至少 20 次事件、事先写定规则的比较 |
| 修复监视 | 手动；没有记录 DLL 变化 | 能发现哈希变化并完成评估 |

## 对已有工作的批判性审查

1. **分析的起点窗口太晚。** 检测器要求图表最右侧 71 像素中的 14 像素持平。图表约每秒移动 1.25 像素，所以首次判为候选时，总图已经平了 11–14 秒。这是在 10 月 20 日以来 28 次有连续标记的事件上量出来的；例外的几次在首次观察时已经是平的。"平线前"窗口只覆盖候选前最后 2–3.8 秒，因此落在冻结期内。以 `last_normal` 结束的"正常对照"也有一部分落在冻结期内。10 月 20 日以来所有平线前排行描述的都是摘要停止变化之后的活动。报告虽注明边界不是底层故障时刻，但这个窗口系统性地错过了可见的起点。低负载时一像素的量化会拉长平线，因此估出的起点是"已平多久"的上限。
2. **单次排行回答不了未决问题。** 每份报告最多列三个伴随进程，并注明"未证明归因"。没有跨事件或与对照的比较，所以 120 次事件没有推进进程增减假设的判断。
3. **记录看不到缺陷本身。** 轻量 ETW 只记录进程和线程的启动与退出，不记录 NtQuerySystemInformation 的返回码、重试次数或缓冲区，因此最多支持关联，永远无法证明因果链。
4. **平线没有与独立计数器核对。** 检测器已经记录 `system_cpu_percent`，但确认和报告都没有拿它与冻结线的高度比较。10 月 24 日 16:06 那次平线停在约 67%，计数器是 100%；这个差异是"冻结而非真实恒定负载"的最强证据，却没有出现在报告里。
5. **上报落后于证据。** 证据摘要要求先准备修订后的提交材料，至今没有发出，而微软修复是达到完成标准的唯一途径。
6. **运行缺口。**
   - 退出时可能要等正在进行的分析尝试最多 300 秒（加入重试前为 120 秒）。旧进程仍占用单实例互斥量时，`Start.cmd` 会静默退出，所以停止后立刻启动可能导致没有监测器在运行。
   - 分析超时只结束 `process-incident.py`，它的 `tracerpt.exe` 子进程可能继续写事件目录。
   - 40 GiB 的 ETW 预算已用约 4.4 GiB，10 月 19 日以来每个事件目录平均约 54 MB。按每天五次估算，约四个月后记录会被阻止，需要事先决定保留策略；证据不会被自动删除。

## 下一步行动（按顺序）

| # | 行动 | 完成标志 | 成本与风险 |
|---|---|---|---|
| A1 | 在离线分析中加入起点阶段：用首次候选截图里的平线长度和量得的图表速度估计开始变平的时间，分析该时间附近的窗口，并在同一 trace 更早处取对照 | 单元测试通过；报告写明估计值及其不确定度 | 只改仓库代码，不新增采集 |
| A2 | 把 10 月 20 日以来保留的事件重新分析到新的 `--output` 目录，汇总成一张跨事件表：起点窗口与同一 trace 中若干正常窗口的进程和线程启动速率 | 表格完成；下面的判定规则在运行前已写定 | 数个 CPU 小时；原始证据不动 |
| A3 | 在每份观察和报告中给出冻结线高度与 `system_cpu_percent` 的差异 | 新报告出现该差异 | 小 |
| A4 | 监测器启动时把 `TaskManagerDataLayer.dll` 的 SHA-256 写入 `status.json`；变化时做静态比对 | 不用手动检查就能看到哈希变化 | 小 |
| A5 | 补运行缺口：`Stop.ps1` 等进程退出，`Start.cmd` 提示已有实例在运行，分析超时时结束整个进程树 | 各有测试覆盖；实际停止和启动一次核对 | 小 |
| A6 | 定稿修订后的反馈中心材料，补充 9549 结果和事件频率 | 负责人发布 | 仅负责人；属对外发布 |
| A7 | 决定是否为因果链恢复 9549 上的现场查询追踪 | 有记录在案的决定 | 搁置，除非 A2 显示明确关联且负责人授权 |

A2 的判定规则：如果至少 20 次事件中，起点窗口与正常窗口的进程和线程启动速率之比的中位数不高于 1.2，则进程增减不是可见的触发条件，停止继续做伴随进程排行。如果明显更高，报告效应大小，并转入 A6 和 A7。
