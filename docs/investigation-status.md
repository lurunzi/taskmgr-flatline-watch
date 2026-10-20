The English version governs.

<a id="en"></a>

# Query retry defect: evidence and current limits

**English** · [中文](#zh)

English is the controlling text.

Reviewed 20 October 2026. This is an independent technical assessment for the existing [Feedback Hub report](https://aka.ms/AA13rk15), not a Microsoft acknowledgement or a claim that every visually flat CPU chart has the same cause.

## Defect that can be assessed independently

The historically tested `TaskManagerDataLayer.dll` has a query retry/status defect. There are at most three NtQuerySystemInformation calls, including the initial zero-length size probe. At the loop boundary the old counter is compared with 3 after the live counter has incremented. Three length mismatches leave the counter at 4, but the exit rejects only counter 3. Subject to successful allocation and ordinary control flow, an exhausted query publishes its buffer and returns S_OK; a third-call success is instead rejected as E_OUTOFMEMORY.

Retained controlled execution of the original native function verifies both outcomes, plus second-call success and access-denied controls. The child executed the query function, not Taskmgr's downstream aggregation or UI. Accept a snapshot only after actual query success; reject exhausted queries without publishing their contents. Changing the exit comparison to `counter >= 3` alone would still reject a successful third call.

Allocation rounds the required length up to a 4 KiB boundary; additional headroom is 0–4095 bytes. It is not a fixed 4 KiB reserve. Snapshot growth depends on processes and their threads; no universal per-process byte increment or process/thread-rate threshold has been established. Failed query contents are not guaranteed valid snapshots by the [API documentation](https://learn.microsoft.com/en-us/windows/win32/api/winternl/nf-winternl-ntquerysysteminformation).

## Binary identity and new static comparison

| Property | Historical native test | System DLL read on 20 October |
|---|---|---|
| Matching component-store version | 10.0.26100.9278 | 10.0.26100.9549 |
| SHA-256 | `829263f58610a9ff013b41f32ecad6a21e9de1631074039de6c20548d3515dce` | `0cc75ace69fef8f512c6efa2e6ac9b4257f8c450302e45b76de441a608fb3867` |
| PE function start RVA | `0x46158` | `0x4BCA8` |
| Counter loop / exit check RVA | `0x461B6` / `0x46325` | `0x4BD06` / `0x4BE75` |
| Publication / success-zero RVA | `0x46346` / `0x46384` | `0x4BE96` / `0x4BED4` |
| Native execution evidence | Retained controlled and real-kernel tests | Not rerun |

The new read-only comparison maps the function through the PE exception directory and compares the old CDB instruction boundaries against both files. Of 154 retained instructions, 137 are byte-identical; 17 differ only in external relative call/import/data operands. The counter, local branch and return sequences match. The listed instructions cover 595 of 600 function bytes; the remaining five bytes also match but are not decoded by this comparison. External destinations were not re-resolved.

This independently supports persistence of the local retry/exit defect pattern in the newer file. It is not a complete new-build native reproduction, verification of a running process's loaded module, or proof of the cause of later visual incidents. Component-store versions are distinguished from OS build numbers. Production debugger hash/byte guards remain pinned to the historically verified binary.

## Impact evidence and counterexamples

| Evidence | Supported conclusion | Limit |
|---|---|---|
| Original-function real-kernel experiment | 11 exhausted queries returned S_OK; 7 had zero-identity tails | Four did not; observer overhead and generated load prevent natural-rate claims |
| Live Taskmgr query Q456 | Three mismatches, counter 4 and S_OK; tail PID 5172 with nonzero creation time | No conflict was captured; no raw buffer was retained |
| Eleven aggregation-origin dumps | Input contains real Idle and a final PID 0 / CreateTime 0 entry; representative conflict is verified | These are error-site dumps, not eleven process crashes; upstream buffer lineage is missing |
| Frozen-state dump pair | Core async action is Error; summary stays 14.058286822726798 while independent CPU data changes and the sampling ring advances | Failed async operation is not evidence that its worker thread died |
| Later visual recoveries | The watcher detected its visual predicate and restarted Taskmgr | Neither defect identity nor a uniquely responsible application follows |

The process source distinguishes `(PID, CreateTime)`, while the observed aggregation conflict involves PID 0 with a different creation time. This is a strongly supported mechanism connecting invalid process data to stale summaries. The historical conflict branch is RVA `0x3E5CF`; the function entry is `0x3E484`. Error propagation must distinguish inner exception handling/HRESULT return from Core's subsequent throw, rather than claiming one exception passes uncaught through every layer.

No same-incident continuous address lineage from the anomalous query buffer through conversion/cache publication to aggregation failure and frozen UI has been captured. Matching identity fields and close timestamps are insufficient. Q456 is a counterexample to *inevitable zero-identity output*, not proof that its nonzero tail was a complete valid snapshot or that it could never cause a later problem.

The two small benchmark batches reporting 3/20 and 2/20 E_OUTOFMEMORY returns were separate runs, not identical-run conflicting results. Their HRESULT-only output does not identify each invocation's internal path, measure retry exhaustion, or establish unique Git/conhost causation. Repeated normal controls remain necessary for application-level attribution.

## Operational decisions

- Keep automatic recovery as a mitigation. The 30-second visual confirmation is not a native defect diagnosis.
- Preserve optional lightweight event recording and all retained evidence. Its budget failure does not prevent recovery; absent events cannot be recovered retrospectively.
- Expose storage usage and the limiting resource. The default remains 2 GiB, with an explicit saved 2–64 GiB budget; do not exclude historic incident traces merely to evade the limit.
- Prepare corrected submission material before considering further live tracing. No new debugger, stress workload or external submission was performed for this review.
- Leave navigation-speed changes and new-build native execution for separately scoped work. A static fix alone would not prove all visual failures resolved or justify automatic retirement of the watcher.

---

<a id="zh"></a>

# 查询重试缺陷：证据与当前边界

[English](#en) · **中文**

以英文为准。复核日期为 2026 年 10 月 20 日。本文用于补充已有反馈中心报告，是独立技术判断，不代表微软确认，也不宣称所有 CPU 平线现象都由同一原因造成。

## 可独立评估的缺陷

历史验证版本存在查询重试／状态判断缺陷。最多三次 NtQuerySystemInformation 调用包含首次零长度探测。循环边界先递增现有计数，再用旧值与 3 比较；三次长度不匹配后计数变为 4，出口却只拒绝计数 3。在分配成功及正常控制流完成的条件下，耗尽查询发布缓冲区并返回成功；第三次查询真正成功却被误报为内存不足。

保留的原函数受控执行验证了上述两种结果，也验证了第二次成功和拒绝访问的对照。诊断子进程执行的是查询函数，没有执行 Taskmgr 下游聚合或界面。修复应依据实际查询成功状态接受快照，耗尽时拒绝发布。只把出口改成“大于等于 3”仍会错误拒绝第三次成功。

分配是按 4 KiB 向上取整，额外余量为 0～4095 字节，并非固定预留 4 KiB。快照增长取决于进程和线程，没有建立通用的单进程增量或进程／线程速率阈值。查询失败后的缓冲区不是接口保证有效的快照。

## 版本与本轮静态核对

完整哈希和地址表统一列于英文区。历史原函数验证对应组件库版本 10.0.26100.9278；本轮读取的系统 DLL 与组件库版本 10.0.26100.9549 字节相同。组件版本与操作系统构建号分别记录。

本轮通过 PE 异常目录确定函数范围，再以保存的 CDB 指令边界逐条比对新旧文件。154 条指令中，137 条字节相同，17 条仅外部相对调用、导入或数据操作数不同；计数、局部分支与返回序列相同。指令覆盖函数 600 字节中的 595 字节，其余 5 字节也相同，但本次未对其解码；未重新解析外部目标。

这独立支持新版文件仍保留该局部重试／退出缺陷模式，不等于新版原函数运行复现、核对活体进程实际加载模块，或证明后来视觉事件的原因。产品调试器仍保留原版本哈希和指令保护。

## 影响证据与反例

原函数真实内核实验记录了 11 次耗尽仍成功，其中 7 次为零身份尾、4 次不是；观察开销与生成负载使其不能代表自然发生率。活体 Q456 同样三次不匹配、计数 4、返回成功，但尾记录是 PID 5172、创建时间非零；没有捕获冲突，也未保留原始缓冲区。

十一份聚合错误现场转储均有真实 Idle 与末尾零身份条目；代表现场已核对冲突。这些不是十一次进程崩溃，也没有保留假记录到上游查询缓冲区的完整来源。冻结后双转储显示 Core 异步操作失败，汇总值固定，而独立 CPU 数据与采样环继续变化；异步操作失败不等于承载线程死亡。之后的视觉恢复只说明检测条件满足并执行恢复，不能据此认定同一缺陷或唯一责任应用。

进程源按 PID 与创建时间区分身份，已观察的聚合冲突涉及创建时间不同的 PID 0。这强烈支持无效进程数据导致汇总陈旧的机制。历史冲突分支地址是 0x3E5CF，函数入口是 0x3E484。异常说明须区分内层处理并返回错误码与 Core 后续重新抛错，不能描述为同一个异常全程未被捕获。

尚未捕获同一次现场从异常查询缓冲区经转换、缓存发布到聚合失败与图表冻结的连续地址来源。字段相同和时间相近都不够。Q456 反驳的是“必然产生零身份尾”，并不能证明其非零尾就是完整有效快照，或以后绝无异常。

报告 3/20 与 2/20 内存不足返回的两个小样本属于不同轮次，并非同次数据矛盾。只记录返回码不能辨识每次内部路径，不能测定重试耗尽率，也不能证明 Git／conhost 为唯一诱因。应用归因仍需要反复的正常对照。

## 运行决策

- 保留自动恢复作为缓解；30 秒视觉确认不构成原生缺陷诊断。
- 保留可选的轻量事件记录与已有证据；预算阻断不影响恢复，漏采事件无法事后补回。
- 显示实际用量及限制原因。默认仍为 2 GiB，显式配置可保存 2～64 GiB 整数预算；不能靠排除历史事件来绕过限额。
- 先整理修正后的提交材料，再考虑进一步活体追踪。本轮没有新增调试器、压测或外部提交。
- 切页提速与新版原函数运行验证单独安排。静态修复本身也不证明所有视觉故障消失，不能据此自动停用守护。
