The English version governs.

<a id="en"></a>

**English** · [中文](#zh)

English is the controlling version.

# Activity before and after first flat observation

**Temporal association only; causality unproven.** Parent chains are not call stacks. Before-first covers only the short transition from the last normal observation to the first flat observation; after-first covers companion activity while awaiting confirmation. Sampling and display latency mean this boundary is not the native fault time. Events exactly at the first-flat boundary belong only to after-first.

Overall data usable: True; errors: none; circular overwrite: False; lost events/buffers: 0/0.

Latest continuity reset: detector_state; disconnected normal samples are never controls.

## Native freeze signature: Taskmgr internal error

Status: Conforming (`0x8007139f` from `TaskManagerDataLayer.dll`).
Time: `2026-10-26T16:44:09.109593000+00:00` (QPC: `1731226186497`); Thread: `64580` (PID: `9436`).
Lead time before first flat observation: ~13.0 s.

### Call chain (5 events in thread within 50 ms):

1. `[Log]` `unknown` (0x8007139f)
2. `[Log]` `unknown` (0x8007139f)
3. `[Return]` `aggregationdatamonitor.cpp:640` (0x8007139f): winrt::hresult_error: The group or resource is not in the correct state to perform the requested operation.
4. `[Return]` `aggregationdatamonitor.cpp:159` (0x8007139f)
5. `[Exception]` `Core.cpp:407` (0x8007139f)

### Activity in [-2, 0) s before native error:

- Process starts: 14 (control median: 0.0, max: 4 in 2 s windows [-120, -20) s).
- Thread starts: 340 (control median: 36.0, max: 169 in 2 s windows [-120, -20) s).
- Started processes (parent -> child): C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\UUS\amd64\MoUsoCoreWorker.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Program Files\Common Files\microsoft shared\ClickToRun\OfficeC2RClient.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\taskhostw.exe, services.exe -> \Device\HarddiskVolume3\Windows\System32\svchost.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\backgroundTaskHost.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\RuntimeBroker.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\Defrag.exe, \Device\HarddiskVolume3\Windows\System32\Defrag.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\taskhostw.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe, C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe, \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe, \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe, services.exe -> \Device\HarddiskVolume3\Windows\System32\svchost.exe
- Top thread creators: C:\WINDOWS\system32\SearchIndexer.exe (113), C:\WINDOWS\system32\svchost.exe (34), \Device\HarddiskVolume3\Windows\System32\conhost.exe (24), 46944 (21), C:\Program Files (x86)\Interhaptics\HapticService\HapticService.exe (20)

## Before first flat observation: transition window

Exposure: 1.987391 s; normal control: 1.987391 s; QPC: `(1731336311137, 1731356185047)`.

Start events: {'thread_start': 127, 'process_start': 2}; normal: {'thread_start': 31}. Unresolved: {'thread_start': 7}; normal unresolved: {}.

| Process <- parent chain | Children (per s) | Normal children (per s) | Own threads (per s) | Normal threads (per s) | Monitor related |
|---|---:|---:|---:|---:|---|
| \Device\HarddiskVolume3\Windows\System32\OpenSSH\ssh.exe (PID 24376) <- C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.503) | unknown | 75 (37.738) | unknown | False |
| C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.503) | 0 (0.000) | 0 (0.000) | 0 (0.000) | False |
| C:\Program Files (x86)\Interhaptics\HapticService\HapticService.exe (PID 5252) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 0 (0.000) | 0 (0.000) | 20 (10.063) | 18 (9.057) | False |

## After first flat observation: confirmation-period activity

Exposure: 30.003829 s; normal control: 4.005961 s; QPC: `[1731356185047, 1731656223332]`.

Start events: {'thread_start': 832, 'process_start': 12}; normal: {'thread_start': 79, 'process_start': 1}. Unresolved: {'thread_start': 25, 'process_start': 1}; normal unresolved: {'thread_start': 15, 'process_start': 1}.

| Process <- parent chain | Children (per s) | Normal children (per s) | Own threads (per s) | Normal threads (per s) | Monitor related |
|---|---:|---:|---:|---:|---|
| C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 3 (0.100) | 0 (0.000) | 1 (0.033) | 0 (0.000) | False |
| \Device\HarddiskVolume3\Windows\System32\cmd.exe (PID 2180) <- C:\Program Files\TencentDocs\TencentDocs.exe (PID 33740) <- C:\WINDOWS\Explorer.EXE (PID 13736) | 2 (0.067) | unknown | 4 (0.133) | unknown | False |
| \Device\HarddiskVolume3\Windows\System32\OpenSSH\ssh.exe (PID 34628) <- C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.033) | unknown | 75 (2.500) | unknown | False |

Each phase ranks independently with a normal control of matching duration where available; shorter controls retain their actual exposure and rates. Missing identities and controls remain unknown. JSON schema 2 top contains before-first rows only; overall event_counts still cover both phases. Full boundaries, instance identities and controls are in incident-analysis.json. No Taskmgr query statuses, buffer-byte contributions or causal chain are measured.

---

<a id="zh"></a>

[English](#en) · **中文**

以英文为准。

# 首次平线前后活动

**仅为时间关联，未证明因果。** 父进程链不是调用栈。平线前仅指最后正常观察到首次平线之间的短暂转变窗口；平线后是等待确认期间的伴随活动。边界受采样与界面延迟影响，不等于底层故障时刻。恰在首次平线边界的事件只计入后段。

整体数据可用：True；错误：none；循环覆盖：False；事件／缓冲区丢失：0/0。

最近连续性重置：detector_state；断开前的正常样本不作为对照。

## 原生卡死特征：Taskmgr 内部错误

状态：符合（来自 `TaskManagerDataLayer.dll` 的 `0x8007139f`）。
时间：`2026-10-26T16:44:09.109593000+00:00`（QPC: `1731226186497`）；线程：`64580`（PID: `9436`）。
相对首次平线候选提前量：约 13.0 秒。

### 调用链（同线程 50 ms 内 5 条事件）：

1. `[Log]` `unknown` (0x8007139f)
2. `[Log]` `unknown` (0x8007139f)
3. `[Return]` `aggregationdatamonitor.cpp:640` (0x8007139f): winrt::hresult_error: The group or resource is not in the correct state to perform the requested operation.
4. `[Return]` `aggregationdatamonitor.cpp:159` (0x8007139f)
5. `[Exception]` `Core.cpp:407` (0x8007139f)

### 原生错误前 [-2, 0) 秒伴随活动：

- 进程启动：14 次（[-120, -20) 秒内 2 秒对照窗口中位数：0.0，最大值：4）。
- 线程启动：340 次（[-120, -20) 秒内 2 秒对照窗口中位数：36.0，最大值：169）。
- 启动的进程（父→子）：C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\UUS\amd64\MoUsoCoreWorker.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Program Files\Common Files\microsoft shared\ClickToRun\OfficeC2RClient.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\taskhostw.exe、services.exe -> \Device\HarddiskVolume3\Windows\System32\svchost.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\backgroundTaskHost.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\RuntimeBroker.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\Defrag.exe、\Device\HarddiskVolume3\Windows\System32\Defrag.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\Windows\System32\taskhostw.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe、C:\WINDOWS\system32\svchost.exe -> \Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe、\Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe、\Device\HarddiskVolume3\ProgramData\Microsoft\Windows Defender\Platform\4.18.26090.9-0\MpCmdRun.exe -> \Device\HarddiskVolume3\Windows\System32\conhost.exe、services.exe -> \Device\HarddiskVolume3\Windows\System32\svchost.exe
- 线程启动最多进程：C:\WINDOWS\system32\SearchIndexer.exe (113)、C:\WINDOWS\system32\svchost.exe (34)、\Device\HarddiskVolume3\Windows\System32\conhost.exe (24)、46944 (21)、C:\Program Files (x86)\Interhaptics\HapticService\HapticService.exe (20)

## 首次平线前：转变窗口

时长：1.987391 秒；正常对照：1.987391 秒；QPC：`(1731336311137, 1731356185047)`。

启动事件：{'thread_start': 127, 'process_start': 2}；正常对照：{'thread_start': 31}。未解析：{'thread_start': 7}；正常对照未解析：{}。

| 进程 ← 父进程链 | 子进程数（每秒） | 正常子进程数（每秒） | 自身线程数（每秒） | 正常线程数（每秒） | 监测器关联 |
|---|---:|---:|---:|---:|---|
| \Device\HarddiskVolume3\Windows\System32\OpenSSH\ssh.exe (PID 24376) <- C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.503) | unknown | 75 (37.738) | unknown | False |
| C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.503) | 0 (0.000) | 0 (0.000) | 0 (0.000) | False |
| C:\Program Files (x86)\Interhaptics\HapticService\HapticService.exe (PID 5252) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 0 (0.000) | 0 (0.000) | 20 (10.063) | 18 (9.057) | False |

## 首次平线后：确认期间伴随活动

时长：30.003829 秒；正常对照：4.005961 秒；QPC：`[1731356185047, 1731656223332]`。

启动事件：{'thread_start': 832, 'process_start': 12}；正常对照：{'thread_start': 79, 'process_start': 1}。未解析：{'thread_start': 25, 'process_start': 1}；正常对照未解析：{'thread_start': 15, 'process_start': 1}。

| 进程 ← 父进程链 | 子进程数（每秒） | 正常子进程数（每秒） | 自身线程数（每秒） | 正常线程数（每秒） | 监测器关联 |
|---|---:|---:|---:|---:|---|
| C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 3 (0.100) | 0 (0.000) | 1 (0.033) | 0 (0.000) | False |
| \Device\HarddiskVolume3\Windows\System32\cmd.exe (PID 2180) <- C:\Program Files\TencentDocs\TencentDocs.exe (PID 33740) <- C:\WINDOWS\Explorer.EXE (PID 13736) | 2 (0.067) | unknown | 4 (0.133) | unknown | False |
| \Device\HarddiskVolume3\Windows\System32\OpenSSH\ssh.exe (PID 34628) <- C:\Program Files\WindowsApps\OpenAI.Codex_26.1021.3705.0_x64__2p2nqsd0c76g0\app\ChatGPT.exe (PID 56348) <- C:\WINDOWS\system32\sihost.exe (PID 10992) <- C:\WINDOWS\system32\svchost.exe (PID 3496) <- services.exe (PID 2244) <- wininit.exe (PID 2160) | 1 (0.033) | unknown | 75 (2.500) | unknown | False |

每段独立排行，正常对照尽量等长；时长不足时保留实际时长与每秒速率，缺失身份和对照保持未知。JSON schema 2 的顶层 top 仅代表平线前，整体 event_counts 仍是两段总计。完整边界、实例身份和对照保存在 incident-analysis.json。这里未测量 Taskmgr 的查询返回值、缓冲区字节贡献或故障因果链。
