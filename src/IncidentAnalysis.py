"""Offline temporal candidates; visual observations never prove native causality."""
import collections
import importlib.util
import json
from pathlib import Path

_spec = importlib.util.spec_from_file_location('legacy_process_decoder',
    Path(__file__).resolve().parents[1]/'tools/correlate-process-trace.py')
_decoder = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_decoder)


def process_instances(events, baseline, header, use_baseline=True):
    """Resolve known lifetimes, refusing overlapping or unknown PID identities."""
    instances = collections.defaultdict(list)
    def clock(created):
        return header['raw_start'] + (created-header['StartTime'])*header['PerfFreq']//10_000_000
    if use_baseline:
        for b in baseline:
            if not b.get('CreateTime'):
                continue
            created = _decoder.ticks(b['CreateTime'])
            if any(p['created']==created for p in instances[int(b['ProcessId'])]):
                continue
            instances[int(b['ProcessId'])].append(dict(pid=int(b['ProcessId']), created=created,
                sequence=None, begin=clock(created), end=None,
                image=b.get('ExecutablePath') or b.get('Name'), parent_pid=int(b.get('ParentProcessId',0)),
                source='baseline', key=f"{b['ProcessId']}:c{created}"))
    for e in events:
        if e['kind'] not in ('process_start','process_stop'):
            continue
        created, seq = e.get('create_time'), e.get('sequence')
        if created is None and seq is None:
            continue
        same = [p for p in instances[e['pid']] if
                (created is not None and p['created']==created) or
                (seq is not None and p['sequence']==seq)]
        if len(same)>1:
            e['identity_ambiguous'] = True
            continue
        if same:
            p = same[0]
        else:
            # A stop with no creation clock cannot reconstruct an unseen birth.
            if e['kind']=='process_stop' and created is None:
                continue
            p = dict(pid=e['pid'], created=created, sequence=seq,
                     begin=e['qpc'] if e['kind']=='process_start' else clock(created), end=None,
                     image=e.get('image'), parent_pid=e.get('parent_pid'), source='ETW')
            instances[e['pid']].append(p)
        p.update(created=created if created is not None else p['created'], sequence=seq or p['sequence'])
        p['key'] = f"{p['pid']}:c{p['created']}" if p['created'] is not None else f"{p['pid']}:s{p['sequence']}"
        if e['kind']=='process_start':
            p.update(begin=e['qpc'], image=e.get('image') or p['image'], parent_pid=e.get('parent_pid'), source='ETW')
        else:
            p['end'] = e['qpc']
    def resolve(pid, clock):
        valid = [p for p in instances[pid] if p['begin']<=clock and (p['end'] is None or clock<=p['end'])]
        return valid[0] if len(valid)==1 else None
    for e in events:
        e['instance'] = None if e.get('identity_ambiguous') else resolve(e['pid'],e['qpc'])
    return instances, resolve


def normal_window(samples, end, duration):
    """Only a contiguous run of actually normal visual samples is a control."""
    before = [s for s in samples if s['qpc']<=end]
    if not before or before[-1]['qpc']!=end or before[-1]['state']!='normal':
        return None
    start = end
    for sample in reversed(before):
        if sample['state']!='normal':
            break
        start = sample['qpc']
        if start<=end-duration:
            break
    start = max(start, end-duration)
    return [start,end] if start<end else None


def candidates(events, bounds, resolve, watcher, freq, *, include_start=False, include_end=True):
    counts = collections.defaultdict(lambda: {'child_process_starts':0,'own_process_starts':0,'thread_starts':0})
    processes, unknown = {}, collections.Counter()
    birth_clocks = collections.defaultdict(list)
    totals = collections.Counter()
    for e in events:
        after_start = e['qpc']>=bounds[0] if include_start else e['qpc']>bounds[0]
        before_end = e['qpc']<=bounds[1] if include_end else e['qpc']<bounds[1]
        if not (after_start and before_end) or e['kind'] not in ('process_start','thread_start'):
            continue
        totals[e['kind']] += 1
        p = e['instance']
        if p is None:
            unknown[e['kind']] += 1
            continue
        processes[p['key']] = p
        counts[p['key']]['own_process_starts' if e['kind']=='process_start' else 'thread_starts'] += 1
        if e['kind']=='process_start':
            parent = resolve(p['parent_pid'],p['begin']) if p['parent_pid'] is not None else None
            if parent:
                processes[parent['key']] = parent
                counts[parent['key']]['child_process_starts'] += 1
                birth_clocks[parent['key']].append(e['qpc'])
            else:
                unknown['parent_of_process_start'] += 1
    rows = []
    exposure = (bounds[1]-bounds[0])/freq
    for key, count in counts.items():
        p = processes[key]
        chain, seen, item = [], set(), p
        monitor_related = False
        while item is not None and item['key'] not in seen and len(chain)<16:
            seen.add(item['key'])
            chain.append({k:item[k] for k in ('key','pid','image','created','sequence')})
            monitor_related |= watcher is not None and item['key']==watcher['key']
            item = resolve(item['parent_pid'],item['begin']) if item['parent_pid'] is not None else None
        births = birth_clocks[key]
        left, peak = 0, 0
        for right, clock in enumerate(births):
            while clock-births[left] > freq/100:
                left += 1
            peak = max(peak,right-left+1)
        row = {'identity':key, 'pid':p['pid'], 'image':p['image'], 'parent_chain':chain,
               'parent_chain_complete':item is None and chain[-1]['pid']==0,
               'monitor_related':monitor_related, **count,
               'peak_child_process_starts_in_10ms':peak,
               'child_process_starts_per_second':count['child_process_starts']/exposure,
               'thread_starts_per_second':count['thread_starts']/exposure,
               'assessment':'CANDIDATE; temporal activity only'}
        rows.append(row)
    rows.sort(key=lambda r:(-r['child_process_starts'],-r['thread_starts'],-r['own_process_starts'],r['identity']))
    return rows, dict(totals), dict(unknown)


def phase_summary(events, bounds, control, resolve, watcher, freq, *, include_start=False,
                  include_end=True, errors=(), assessment='CANDIDATE; temporal activity only'):
    """Independent ranking/control per phase; never split an already truncated Top 3."""
    result = {'window_qpc':bounds, 'include_start':include_start, 'include_end':include_end,
              'normal_window_qpc':control, 'ranking_usable':not errors and bounds is not None,
              'errors':list(errors), 'top':[], 'event_counts':{}, 'unresolved_counts':{},
              'normal_event_counts':{}, 'normal_unresolved_counts':{},
              'exposure_seconds':None, 'normal_exposure_seconds':None, 'candidate_instances':0}
    if not result['ranking_usable']:
        return result
    rows, totals, unknown = candidates(events,bounds,resolve,watcher,freq,
                                       include_start=include_start,include_end=include_end)
    normal_rows, normal_totals, normal_unknown = candidates(events,control,resolve,watcher,freq) if control else ([],{}, {})
    normal_by_key = {r['identity']:r for r in normal_rows}
    for row in rows[:3]:
        other = normal_by_key.get(row['identity'])
        if other is None and control:
            start_instance=resolve(row['pid'],control[0]);end_instance=resolve(row['pid'],control[1])
            if start_instance and end_instance and start_instance['key']==end_instance['key']==row['identity']:
                other={'child_process_starts':0,'thread_starts':0,
                       'child_process_starts_per_second':0,'thread_starts_per_second':0}
        # Missing instance history is unknown, never an application-wide zero.
        row['normal_same_instance'] = ({k:other[k] for k in
            ('child_process_starts','thread_starts','child_process_starts_per_second','thread_starts_per_second')}
            if other else None)
        row['assessment'] = assessment
    result.update(top=rows[:3], candidate_instances=len(rows), event_counts=totals, unresolved_counts=unknown,
                  normal_event_counts=normal_totals, normal_unresolved_counts=normal_unknown,
                  exposure_seconds=(bounds[1]-bounds[0])/freq,
                  normal_exposure_seconds=(control[1]-control[0])/freq if control else None)
    return result


def analyze(folder, output=None):
    folder = Path(folder)
    output = Path(output) if output is not None else folder
    trace = folder/'process-trace'
    observation = json.loads((folder/'observation.json').read_text(encoding='utf-8'))
    session = json.loads((trace/'session.json').read_text(encoding='utf-8'))
    baseline = json.loads((trace/'baseline.json').read_text(encoding='utf-8-sig'))
    if isinstance(baseline,dict):
        baseline = [baseline]
    sample_clocks = [s.get('qpc') for s in observation.get('samples',[]) if type(s.get('qpc')) is int]
    marker_clocks = [m.get('qpc') for k,m in observation.get('markers',{}).items()
                     if k in ('last_normal','first_candidate','confirmed') and isinstance(m,dict)
                     and type(m.get('qpc')) is int]
    clocks_to_keep = sample_clocks+marker_clocks
    window = (min(clocks_to_keep),max(clocks_to_keep)) if clocks_to_keep else None
    decoded_file = session.get('decode_file') or 'events.xml'
    if decoded_file not in ('events.xml','events.xml.gz'):
        raise ValueError('unsupported decoded trace path')
    events, header, decode_errors = _decoder.decode(trace/decoded_file,thread_window=window)
    overwritten = 'circular trace may have overwritten earlier buffers' in decode_errors
    errors = [e for e in decode_errors if e!='circular trace may have overwritten earlier buffers']
    freq = header['PerfFreq']
    if freq<=0 or observation.get('qpc_frequency')!=freq or session.get('qpc_frequency')!=freq:
        errors.append('QPC frequency mismatch')
    if session.get('status')!='stopped' or session.get('decode_exit')!=0:
        errors.append('ETW stop/decode not verified')
    coverage_begin = max(session.get('baseline_end_qpc',session.get('started_qpc',0)),
                         (header.get('retained_first_qpc') or min((e['qpc'] for e in events), default=0)) if overwritten else header['raw_start'])
    coverage_end = session.get('stopped_qpc',0)
    end_baseline_path = trace/'end-baseline.json'
    end_baseline = []
    if (end_baseline_path.exists() and session.get('end_baseline_begin_qpc',0)>=coverage_end and
            session.get('end_baseline_end_qpc',0)>=session.get('end_baseline_begin_qpc',0)):
        end_baseline = json.loads(end_baseline_path.read_text(encoding='utf-8-sig'))
        if isinstance(end_baseline,dict):end_baseline=[end_baseline]
    capture_path = folder/'trace-capture.json'
    if capture_path.exists():
        capture = json.loads(capture_path.read_text(encoding='utf-8'))
        if capture.get('status')!='preserved':
            errors.append('incident snapshot not preserved')
        coverage_end = min(coverage_end,capture.get('stop_requested_qpc',coverage_end))
    markers = observation.get('markers',{})
    def marker(name):
        value = markers.get(name)
        return value.get('qpc') if isinstance(value,dict) else None
    first, confirmed, last = marker('first_candidate'),marker('confirmed'),marker('last_normal')
    bounds = [last if last is not None else first, confirmed]
    valid_window = all(type(v) is int and v>0 for v in bounds) and bounds[0]<bounds[1]
    if (not valid_window or type(first) is not int or not bounds[0]<=first<bounds[1] or
            (last is not None and (type(last) is not int or type(first) is not int or last>=first))):
        errors.append('missing or unordered visual observation markers')
    if valid_window and not coverage_begin<=bounds[0]<bounds[1]<=coverage_end:
        errors.append('required observation interval outside retained ETW coverage')
    samples = observation.get('samples',[])
    clocks = [s.get('qpc') for s in samples]
    if any(type(t) is not int for t in clocks) or any(b<=a or b-a>6*freq for a,b in zip(clocks,clocks[1:])):
        errors.append('visual sample clock/order/gap invalid')
    control = normal_window(samples,last,bounds[1]-bounds[0]) if valid_window and last is not None else None
    if control and not coverage_begin<=control[0]<control[1]<=coverage_end:
        control = None
    quality = {'ranking_usable':not errors, 'errors':sorted(set(errors)),
               'events_lost':header['EventsLost'], 'buffers_lost':header['BuffersLost'],
               'circular_overwrite':overwritten, 'baseline_usable':not overwritten,
               'end_baseline_available':bool(end_baseline),
               'coverage_begin_qpc':coverage_begin, 'coverage_end_qpc':coverage_end,
               'missing_pre_candidate_normal':last is None,
               'control_available':control is not None,
               'identity_limit':'Baseline has microsecond precision; only exact identity matches merge. Ambiguous lifetimes stay unresolved.'}
    result = {'schema_version':2, 'attribution':'unproven', 'quality':quality, 'qpc_frequency':freq,
              'continuity':observation.get('continuity',{'status':'not recorded by this watcher version'}),
              'trace_event_counts':{k:header.get(k) for k in ('scanned_events','selected_events')},
              'observation_markers':markers, 'window_qpc':bounds, 'normal_window_qpc':control,
              'ranking':'direct child process starts, then own thread starts, then own process starts',
              'top':[], 'top_scope':'before_first', 'phases':{},
              'event_counts':{}, 'unresolved_counts':{}, 'normal_event_counts':{},
              'event_counts_scope':'whole observation window; phase counts are separate',
              'limits':'Visual receipt interval only, not a native fault interval. Process parent chains are not call stacks. No NtQSI statuses, buffer bytes or causality are measured.'}
    if not errors:
        # A fresh post-stop snapshot proves a surviving instance existed since
        # its creation clock. An old start snapshot cannot bridge lost history.
        identity_baseline = ([] if overwritten else baseline)+end_baseline
        _, resolve = process_instances(events,identity_baseline,header)
        watcher = resolve(observation.get('watcher_pid',-1),confirmed)
        _, totals, unknown = candidates(events,bounds,resolve,watcher,freq)
        _, normal_totals, normal_unknown = candidates(events,control,resolve,watcher,freq) if control else ([],{}, {})
        result.update(event_counts=totals, unresolved_counts=unknown,
                      normal_event_counts=normal_totals, normal_unresolved_counts=normal_unknown,
                      exposure_seconds=(bounds[1]-bounds[0])/freq,
                      normal_exposure_seconds=(control[1]-control[0])/freq if control else None)
    else:
        resolve = watcher = None
    for name, phase_bounds, left, right in (
            ('before_first',[last,first] if last is not None else None,False,False),
            ('after_first',[first,confirmed],last is not None,True)):
        phase_errors = list(errors)
        if phase_bounds is None:
            phase_errors.append('no connected last-normal observation; pre-first activity unknown')
        phase_control = None
        if not phase_errors and last is not None:
            phase_control = normal_window(samples,last,phase_bounds[1]-phase_bounds[0])
            if phase_control and not coverage_begin<=phase_control[0]<phase_control[1]<=coverage_end:
                phase_control = None
        phase = phase_summary(events,phase_bounds,phase_control,resolve,watcher,freq,
                              include_start=left,include_end=right,errors=phase_errors,
                              assessment=('PRE-FIRST OBSERVATION CANDIDATE; not proven cause' if name=='before_first'
                                          else 'POST-FIRST OBSERVATION ACTIVITY; not an onset attribution'))
        result['phases'][name] = phase
    result['top'] = result['phases']['before_first']['top']
    result['quality']['before_first_usable'] = result['phases']['before_first']['ranking_usable']
    result['quality']['after_first_usable'] = result['phases']['after_first']['ranking_usable']
    output.mkdir(parents=True,exist_ok=True)
    (output/'incident-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'incident-analysis.md').write_text(render(result),encoding='utf-8')
    return result


def render(result):
    def cell(value):
        return str(value or 'unknown').replace('|','\\|').replace('\n',' ').replace('\r',' ')
    def phase_text(name, zh):
        p = result['phases'][name]
        before = name=='before_first'
        title = (('首次平线前：转变窗口' if before else '首次平线后：确认期间伴随活动') if zh else
                 ('Before first flat observation: transition window' if before else
                  'After first flat observation: confirmation-period activity'))
        lines = [f'## {title}', '']
        if not p['ranking_usable']:
            lines += [('不可用；不输出具名排行：' if zh else 'Unavailable; named ranking suppressed: ')+
                      cell('; '.join(p['errors'])), '']
            return '\n'.join(lines)+'\n'
        bounds=p['window_qpc']
        interval=f"{'[' if p['include_start'] else '('}{bounds[0]}, {bounds[1]}{']' if p['include_end'] else ')'}"
        exposure=f"{p['exposure_seconds']:.6f}"
        normal=f"{p['normal_exposure_seconds']:.6f}" if p['normal_exposure_seconds'] is not None else 'unknown'
        lines += [(f'时长：{exposure} 秒；正常对照：{normal} 秒；QPC：`{interval}`。' if zh else
                   f'Exposure: {exposure} s; normal control: {normal} s; QPC: `{interval}`.'), '',
                  (f"启动事件：{p['event_counts']}；正常对照：{p['normal_event_counts']}。未解析：{p['unresolved_counts']}；正常对照未解析：{p['normal_unresolved_counts']}。" if zh else
                   f"Start events: {p['event_counts']}; normal: {p['normal_event_counts']}. Unresolved: {p['unresolved_counts']}; normal unresolved: {p['normal_unresolved_counts']}."), '']
        if not p['top']:
            lines += [('此窗口没有可解析身份的活动条目。' if zh else 'No resolved activity rows in this window.'), '']
            return '\n'.join(lines)+'\n'
        lines += [('| 进程 ← 父进程链 | 子进程数（每秒） | 正常子进程数（每秒） | 自身线程数（每秒） | 正常线程数（每秒） | 监测器关联 |' if zh else
                   '| Process <- parent chain | Children (per s) | Normal children (per s) | Own threads (per s) | Normal threads (per s) | Monitor related |'),
                  '|---|---:|---:|---:|---:|---|']
        def metric(row,key):
            return f"{row[key]} ({row[key+'_per_second']:.3f})" if row is not None else 'unknown'
        for row in p['top']:
            chain=cell(' <- '.join(f"{i['image'] or 'unknown'} (PID {i['pid']})" for i in row['parent_chain']))
            other=row['normal_same_instance']
            lines.append(f"| {chain} | {metric(row,'child_process_starts')} | {metric(other,'child_process_starts')} | {metric(row,'thread_starts')} | {metric(other,'thread_starts')} | {row['monitor_related']} |")
        return '\n'.join(lines)+'\n\n'
    q = result['quality']
    errors = '; '.join(q['errors']) or 'none'
    continuity = result.get('continuity',{})
    last_break = (continuity.get('recent_breaks') or [None])[-1]
    reason = last_break.get('reason') if last_break else 'not recorded'
    blocks = []
    for zh in (False, True):
        intro = ('<a id="zh"></a>\n\n[English](#en) · **中文**\n\n以英文为准。\n\n# 首次平线前后活动\n\n'
                 '**仅为时间关联，未证明因果。** 父进程链不是调用栈。平线前仅指最后正常观察到首次平线之间的短暂转变窗口；'
                 '平线后是等待确认期间的伴随活动。边界受采样与界面延迟影响，不等于底层故障时刻。恰在首次平线边界的事件只计入后段。\n\n'
                 f"整体数据可用：{q['ranking_usable']}；错误：{errors}；循环覆盖：{q['circular_overwrite']}；事件／缓冲区丢失：{q['events_lost']}/{q['buffers_lost']}。\n\n"
                 f'最近连续性重置：{reason}；断开前的正常样本不作为对照。\n\n' if zh else
                 '<a id="en"></a>\n\n**English** · [中文](#zh)\n\nEnglish is the controlling version.\n\n# Activity before and after first flat observation\n\n'
                 '**Temporal association only; causality unproven.** Parent chains are not call stacks. Before-first covers only the short transition '
                 'from the last normal observation to the first flat observation; after-first covers companion activity while awaiting confirmation. '
                 'Sampling and display latency mean this boundary is not the native fault time. Events exactly at the first-flat boundary belong only to after-first.\n\n'
                 f"Overall data usable: {q['ranking_usable']}; errors: {errors}; circular overwrite: {q['circular_overwrite']}; lost events/buffers: {q['events_lost']}/{q['buffers_lost']}.\n\n"
                 f'Latest continuity reset: {reason}; disconnected normal samples are never controls.\n\n')
        tail = ('每段独立排行，正常对照尽量等长；时长不足时保留实际时长与每秒速率，缺失身份和对照保持未知。'
                'JSON schema 2 的顶层 top 仅代表平线前，整体 event_counts 仍是两段总计。完整边界、实例身份和对照保存在 incident-analysis.json。'
                '这里未测量 Taskmgr 的查询返回值、缓冲区字节贡献或故障因果链。\n' if zh else
                'Each phase ranks independently with a normal control of matching duration where available; shorter controls retain their actual exposure and rates. '
                'Missing identities and controls remain unknown. JSON schema 2 top contains before-first rows only; overall event_counts still cover both phases. '
                'Full boundaries, instance identities and controls are in incident-analysis.json. No Taskmgr query statuses, buffer-byte contributions or causal chain are measured.\n')
        blocks.append(intro+phase_text('before_first',zh)+phase_text('after_first',zh)+tail)
    return 'The English version governs.\n\n'+ '\n---\n\n'.join(blocks)
