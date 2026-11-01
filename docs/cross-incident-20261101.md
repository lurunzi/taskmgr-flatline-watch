The English version governs.

<a id="en"></a>

# Cross-incident table: activity before the native freeze signature (J9)

**English** · [中文](#zh)

English is the controlling text.

Compiled on 1 November 2026 from the 11 qualifying flatlines that conform to N1, as roadmap item J9 requires once 10 conforming freezes exist. It is a temporal association only and does not establish causation.

## Sources

- Per incident: `native_signature` and `activity_before` in `incident-analysis.json`, taken from the `-reanalysis` folder where one exists, and `trigger_kind` in `event.json`, all under `D:\TaskmgrFreezeCaptures\restart-*`.
- Call chains: the incident trace for the first six freezes. For the last five, originally cross-checked against the independent probe `wil-20261031-1550-to-20261101-1500.etl`; the direct decoder field loss from J15 is fixed in `c78b2db` via TDH TraceLogging decoding, verified field by field against tracerpt, and backfilled into `-reanalysis2` folders.
- Onset: the first `0x8007139F` `FallbackError` from `TaskManagerDataLayer.dll`.
- Onset window: [-2, 0) s before that error. Controls: 50 two-second windows from -120 to -20 s in the same trace.

## Table

| Freeze (UTC) | Recovery trigger | Lead before first flat candidate (s) | Process starts: onset / control median / control max | Thread starts: onset / control median / control max | Heartbeat during freeze |
|---|---|---:|---|---|---|
| 26 Oct 16:44 | visual | 13.0 | 14 / 0 / 4 | 340 / 36 / 169 | not recorded |
| 28 Oct 01:50 | native fast | 14.1 | 100 / 4 / 23 | 1,859 / 100 / 851 | not recorded |
| 28 Oct 01:51 | visual | 29.3 | 344 / 0 / 0 (controls empty) | 2,216 / 0 / 0 (controls empty) | not recorded |
| 28 Oct 02:05 | native fast | 14.9 | 55 / 66.5 / 79 | 1,854 / 1,985.5 / 2,354 | not recorded |
| 28 Oct 19:47 | native fast | 9.4 | 8 / 0 / 7 | 249 / 51 / 154 | not recorded |
| 30 Oct 14:06 | native fast | 14.9 | 106 / 4 / 51 | 590 / 93 / 310 | continued |
| 31 Oct 19:39 | native fast | 14.3 | 408 / 14 / 47 | 4,072 / 132 / 722 | continued |
| 31 Oct 22:49 | native fast | 13.2 | 127 / 8 / 53 | 910 / 117 / 551 | continued |
| 31 Oct 22:54 | native fast | 15.4 | 71 / 11 / 213 | 624 / 116 / 1,107 | continued |
| 31 Oct 23:49 | native fast | 14.4 | 52 / 2 / 104 | 409 / 83.5 / 533 | continued |
| 1 Nov 11:50 | native fast | 13.6 | 1 / 0 / 35 | 222 / 35 / 602 | continued |

## Findings

1. **Signature.** All 11 freezes carried the same five `0x8007139F` events on one thread within about 1 ms: two logs, then returns at `aggregationdatamonitor.cpp:640` and `:159`, then an exception at `Core.cpp:407`.
   - The independent probe recorded `0x8007139F` only at freezes: 4 groups in about 57 hours to 30 October and 5 groups in about 23 hours to 1 November, none outside a freeze.
2. **Lead time.** The native error came 9.4–29.3 s before the first flat candidate, with a median of 14.3 s.
3. **Recovery.** 9 of the 11 were recovered by the N3 native fast path.
4. **Process churn.** 10 freezes have usable controls; on 28 October at 01:51 every control window was empty.
   - In 6 of the 10, process starts in the onset window exceeded all 50 control windows.
   - In 2 more, they exceeded the control median but not its maximum.
   - In 2 they did not stand out: on 28 October at 02:05, during sustained heavy creation (median 66.5 per 2 s), and on 1 November at 11:50, with 1 process start.
   - Bursts as large as the onset windows also occur in control windows without a freeze. Churn therefore raises the likelihood but is neither necessary nor sufficient on this evidence. This fits the query-retry defect, which needs a size change during a query rather than a burst as such.
5. **Data model.** In all 6 freezes with heartbeat coverage, `CurrentUpdateCycle` continued about once a second until recovery. The freeze is confined to the aggregated summary.

## Limits

- Process and thread starts are counted per two-second window; process exits and thread exits, which also change the query size, are not counted.
- Control windows come from the same trace and may themselves contain a near-miss.
- Parent chains and process names are not causes.
- One machine, one DLL build (9549).

---

<a id="zh"></a>

# 跨事件对照表：原生卡死特征出现前的活动（J9）

[English](#en) · **中文**

以英文为准。

2026 年 11 月 1 日整理，依据符合 N1 的 11 次合格平线。路线图 J9 要求在符合的冻结达到 10 次后整理此表。本表只反映时间关联，不证明因果。

## 来源

- 各事件数据：`D:\TaskmgrFreezeCaptures\restart-*` 下 `incident-analysis.json` 中的 `native_signature` 与 `activity_before`（有 `-reanalysis` 目录时取该目录），以及 `event.json` 中的 `trigger_kind`。
- 调用链：前六次取自事件 trace；后五次最初对照独立探测会话 `wil-20261031-1550-to-20261101-1500.etl`；J15 直接解码器字段丢失问题已在提交 `c78b2db` 中通过 TDH TraceLogging 解码修复，与 tracerpt 解码逐字段核验完全一致，并已全量回填至 `-reanalysis2` 目录。
- 起点：来自 `TaskManagerDataLayer.dll` 的第一条 `0x8007139F` `FallbackError`。
- 起点窗口：该错误之前的 [-2, 0) 秒。对照：同一 trace 中 -120 至 -20 秒的 50 个 2 秒窗口。

## 对照表

| 冻结时间（UTC） | 恢复触发 | 比首次平线候选早（秒） | 进程启动：起点 / 对照中位 / 对照最大 | 线程启动：起点 / 对照中位 / 对照最大 | 冻结期间心跳 |
|---|---|---:|---|---|---|
| 10-26 16:44 | 视觉 | 13.0 | 14 / 0 / 4 | 340 / 36 / 169 | 未记录 |
| 10-28 01:50 | 原生快速 | 14.1 | 100 / 4 / 23 | 1,859 / 100 / 851 | 未记录 |
| 10-28 01:51 | 视觉 | 29.3 | 344 / 0 / 0（对照为空） | 2,216 / 0 / 0（对照为空） | 未记录 |
| 10-28 02:05 | 原生快速 | 14.9 | 55 / 66.5 / 79 | 1,854 / 1,985.5 / 2,354 | 未记录 |
| 10-28 19:47 | 原生快速 | 9.4 | 8 / 0 / 7 | 249 / 51 / 154 | 未记录 |
| 10-30 14:06 | 原生快速 | 14.9 | 106 / 4 / 51 | 590 / 93 / 310 | 持续 |
| 10-31 19:39 | 原生快速 | 14.3 | 408 / 14 / 47 | 4,072 / 132 / 722 | 持续 |
| 10-31 22:49 | 原生快速 | 13.2 | 127 / 8 / 53 | 910 / 117 / 551 | 持续 |
| 10-31 22:54 | 原生快速 | 15.4 | 71 / 11 / 213 | 624 / 116 / 1,107 | 持续 |
| 10-31 23:49 | 原生快速 | 14.4 | 52 / 2 / 104 | 409 / 83.5 / 533 | 持续 |
| 11-01 11:50 | 原生快速 | 13.6 | 1 / 0 / 35 | 222 / 35 / 602 | 持续 |

## 发现

1. **特征。** 11 次冻结都在同一线程、约 1 毫秒内出现同样的五条 `0x8007139F` 事件：先是两条日志，再从 `aggregationdatamonitor.cpp:640` 和 `:159` 返回，最后在 `Core.cpp:407` 抛出异常。
   - 独立探测会话只在冻结时记录到 `0x8007139F`：截至 10 月 30 日约 57 小时内 4 组，截至 11 月 1 日约 23 小时内 5 组，冻结之外 0 次。
2. **提前量。** 原生错误比首次平线候选早 9.4–29.3 秒，中位数 14.3 秒。
3. **恢复。** 11 次中有 9 次由 N3 原生快速路径恢复。
4. **进程增减。** 10 次冻结有可用对照；10 月 28 日 01:51 那次所有对照窗口都是空的。
   - 10 次中有 6 次，起点窗口的进程启动数超过全部 50 个对照窗口。
   - 另有 2 次超过对照中位数，但没有超过对照最大值。
   - 有 2 次并不突出：10 月 28 日 02:05 处在持续大量创建期间（每 2 秒中位 66.5 次）；11 月 1 日 11:50 只有 1 次进程启动。
   - 与起点窗口一样大的突增，在没有冻结的对照窗口里也会出现。所以按现有证据，进程增减会提高冻结的可能，但既不是必要条件，也不是充分条件。这与查询重试缺陷的机制相符：它需要的是查询期间快照大小发生变化，而不是突增本身。
5. **数据模型。** 在有心跳覆盖的 6 次冻结中，`CurrentUpdateCycle` 都约每秒一次地持续到恢复为止，冻结局限在聚合摘要层。

## 局限

- 只统计每 2 秒窗口内的进程和线程启动；进程退出和线程退出同样会改变查询大小，但未计入。
- 对照窗口取自同一 trace，其中本身可能包含险些冻结的情况。
- 父进程链和进程名都不代表原因。
- 只来自一台机器、一个 DLL 版本（9549）。
