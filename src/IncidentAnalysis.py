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


def candidates(events, bounds, resolve, watcher, freq):
    counts = collections.defaultdict(lambda: {'child_process_starts':0,'own_process_starts':0,'thread_starts':0})
    processes, unknown = {}, collections.Counter()
    birth_clocks = collections.defaultdict(list)
    totals = collections.Counter()
    for e in events:
        if not bounds[0]<e['qpc']<=bounds[1] or e['kind'] not in ('process_start','thread_start'):
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
    if not valid_window or type(first) is not int or not bounds[0]<=first<=bounds[1]:
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
    result = {'schema_version':1, 'attribution':'unproven', 'quality':quality, 'qpc_frequency':freq,
              'continuity':observation.get('continuity',{'status':'not recorded by this watcher version'}),
              'trace_event_counts':{k:header.get(k) for k in ('scanned_events','selected_events')},
              'observation_markers':markers, 'window_qpc':bounds, 'normal_window_qpc':control,
              'ranking':'direct child process starts, then own thread starts, then own process starts',
              'top':[], 'event_counts':{}, 'unresolved_counts':{}, 'normal_event_counts':{},
              'limits':'Visual receipt interval only, not a native fault interval. Process parent chains are not call stacks. No NtQSI statuses, buffer bytes or causality are measured.'}
    if not errors:
        # A fresh post-stop snapshot proves a surviving instance existed since
        # its creation clock. An old start snapshot cannot bridge lost history.
        identity_baseline = ([] if overwritten else baseline)+end_baseline
        _, resolve = process_instances(events,identity_baseline,header)
        watcher = resolve(observation.get('watcher_pid',-1),confirmed)
        rows, totals, unknown = candidates(events,bounds,resolve,watcher,freq)
        normal_rows, normal_totals, normal_unknown = candidates(events,control,resolve,watcher,freq) if control else ([],{}, {})
        normal_by_key = {r['identity']:r for r in normal_rows}
        for row in rows[:3]:
            other = normal_by_key.get(row['identity'])
            if other is None and control:
                start_instance=resolve(row['pid'],control[0]);end_instance=resolve(row['pid'],control[1])
                if start_instance and end_instance and start_instance['key']==end_instance['key']==row['identity']:
                    other={'child_process_starts':0,'thread_starts':0,
                           'child_process_starts_per_second':0,'thread_starts_per_second':0}
            # An absent instance during control is not evidence of zero activity
            # by the same application: retain instance-level counts only.
            row['normal_same_instance'] = ({k:other[k] for k in
                ('child_process_starts','thread_starts','child_process_starts_per_second','thread_starts_per_second')}
                if other else None)
        result.update(top=rows[:3], candidate_instances=len(rows), event_counts=totals, unresolved_counts=unknown,
                      normal_event_counts=normal_totals, normal_unresolved_counts=normal_unknown,
                      exposure_seconds=(bounds[1]-bounds[0])/freq,
                      normal_exposure_seconds=(control[1]-control[0])/freq if control else None)
    output.mkdir(parents=True,exist_ok=True)
    (output/'incident-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'incident-analysis.md').write_text(render(result),encoding='utf-8')
    return result


def render(result):
    def cell(value):
        return str(value or 'unknown').replace('|','\\|').replace('\n',' ').replace('\r',' ')
    table = '\n'.join('| '+cell(' <- '.join(f"{p['image'] or 'unknown'} (PID {p['pid']})" for p in r['parent_chain']))+
                     f" | {r['child_process_starts']} | {r['normal_same_instance']['child_process_starts'] if r.get('normal_same_instance') is not None else 'unknown'} | {r['thread_starts']} | {r['monitor_related']} |" for r in result['top'])
    q = result['quality']
    errors = '; '.join(q['errors']) or 'none'
    continuity = result.get('continuity',{})
    last_break = (continuity.get('recent_breaks') or [None])[-1]
    reason = last_break.get('reason') if last_break else 'not recorded'
    return ("The English version governs.\n\n# Incident candidates\n\n"
            "**CANDIDATES ONLY — causality unproven.** Parent chains are process relationships, not call stacks.\n\n"
            f"Ranking usable: {q['ranking_usable']}. Errors: {errors}. Circular overwrite: {q['circular_overwrite']}. "
            f"Lost events/buffers: {q['events_lost']}/{q['buffers_lost']}.\n\n"
            f"Observation exposure: {result.get('exposure_seconds')} s; normal control: {result.get('normal_exposure_seconds')} s. "
            f"Unresolved events/parents: {result['unresolved_counts']}.\n\n"
            f"Latest continuity reset: {reason}. Disconnected normal samples are never used as controls.\n\n"
            "| Process <- parent chain | Child process starts | Normal child starts | Own thread starts | Monitor related |\n|---|---:|---:|---:|---|\n"+
            table+"\n\nRaw times, instance identities, rates and control counts are in incident-analysis.json. "
            "Missing identities and controls remain unknown. Compare repeated incidents and matched normal exposures; "
            "controlled application stop/start tests are separate work. This report does not measure Taskmgr query failures.\n\n"
            "---\n\n# 异常活动候选\n\n**仅为候选，未证明因果。** 父进程链表示进程关系，不是调用栈。\n\n"
            f"排行可用：{q['ranking_usable']}；诊断详情见上方及 JSON。循环覆盖：{q['circular_overwrite']}；"
            f"事件／缓冲区丢失：{q['events_lost']}/{q['buffers_lost']}。\n\n"
            f"异常观察时长：{result.get('exposure_seconds')} 秒；正常对照时长：{result.get('normal_exposure_seconds')} 秒。"
            f"身份或父关系未解析计数：{result['unresolved_counts']}。\n\n"
            f"最近一次连续性重置：{reason}。断开前的正常样本只作背景，不作为正常对照。\n\n"
            "| 进程 ← 父进程链 | 子进程启动数 | 正常对照子进程数 | 自身线程启动数 | 监测器关联 |\n|---|---:|---:|---:|---|\n"+
            table+"\n\n原始时刻、进程实例身份、速率和对照计数保存在 incident-analysis.json。缺失的身份和对照保持未知。"
            "应比较多次异常与相近负载的正常观察时长，应用停启对照需另行实施。这里记录的是视觉观察时段，"
            "不代表底层故障时刻，也未测量 Taskmgr 查询的返回值或缓冲区字节贡献。\n")
