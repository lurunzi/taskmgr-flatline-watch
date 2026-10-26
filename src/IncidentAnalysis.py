"""Offline temporal candidates; visual observations never prove native causality."""
import collections
import gzip
import importlib.util
import json
import statistics
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
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


TASKMGR_PROVIDER = '{2e635d8e-1107-4555-9319-32eeb895aaae}'
NS = {'e': 'http://schemas.microsoft.com/win/2004/08/events/event'}


def parse_native_events_from_xml(path):
    source = gzip.open(path, 'rb') if str(path).endswith('.gz') else open(path, 'rb')
    native = []
    try:
        iterator = ET.iterparse(source, events=('start', 'end'))
        _, root = next(iterator)
        for action, node in iterator:
            if action != 'end' or node.tag != '{' + NS['e'] + '}Event':
                continue
            prov = node.find('e:System/e:Provider', NS)
            guid = (prov.get('Guid') or '').lower() if prov is not None else ''
            if guid == TASKMGR_PROVIDER:
                ex = node.find('e:System/e:Execution', NS)
                clock = node.find('e:System/e:TimeCreated', NS)
                pid = int(ex.get('ProcessID', 0)) if ex is not None else 0
                tid = int(ex.get('ThreadID', 0)) if ex is not None else 0
                raw = clock.get('RawTime') if clock is not None else None
                system_time = clock.get('SystemTime') if clock is not None else None
                task = node.findtext('e:RenderingInfo/e:Task', namespaces=NS) or node.findtext('e:System/e:Task', namespaces=NS) or ''
                wil_data = {}
                for cd in node.findall('e:EventData/e:ComplexData', NS):
                    if cd.get('Name') == 'wilResult':
                        wil_data = {d.get('Name'): (d.text or '').strip() for d in cd.findall('e:Data', NS)}
                        break
                if wil_data:
                    try:
                        hr = int(wil_data.get('hresult', '0'))
                    except ValueError:
                        hr = 0
                    try:
                        line = int(wil_data.get('lineNumber', '0') or 0)
                    except ValueError:
                        line = 0
                    try:
                        ft = int(wil_data.get('failureType', '0') or 0)
                    except ValueError:
                        ft = 0
                    native.append({
                        'raw': raw,
                        'system_time': system_time,
                        'task': task,
                        'pid': pid,
                        'tid': tid,
                        'hresult': hr,
                        'module': wil_data.get('module', ''),
                        'file': wil_data.get('fileName', ''),
                        'line': line,
                        'failure_type': ft,
                        'message': wil_data.get('message', '')
                    })
            node.clear()
            root.clear()
    finally:
        source.close()
    return native


def extract_native_signature(native_events, events, header, observation, resolve, freq,
                             coverage_begin, coverage_end, quality=None, folder=None):
    if not native_events and folder is not None:
        folder_p = Path(folder)
        candidates_dirs = [folder_p, folder_p/'process-trace']
        wil_probe_dir = folder_p.parent/'wil-probe-20261025'
        if wil_probe_dir.exists():
            candidates_dirs.extend([wil_probe_dir, wil_probe_dir/'decode-1543-to-2030', wil_probe_dir/'decode-upto-20261026-1543'])
        for c in candidates_dirs:
            if not c.exists():
                continue
            for pattern in ('*wil*.xml*', '*native*.xml*', 'events.xml*'):
                for p in c.glob(pattern):
                    if p.name in ('events.xml', 'events.xml.gz') and c in (folder_p, folder_p/'process-trace'):
                        continue  # already parsed by correlate
                    try:
                        parsed = parse_native_events_from_xml(p)
                        if parsed:
                            native_events = parsed
                            break
                    except Exception:
                        pass
                if native_events:
                    break

    markers = observation.get('markers', {})
    def marker_qpc(name):
        v = markers.get(name)
        return v.get('qpc') if isinstance(v, dict) else None

    first = marker_qpc('first_candidate')
    confirmed = marker_qpc('confirmed') or coverage_end
    last = marker_qpc('last_normal')

    anchor_qpc = last or first
    anchor_utc = None
    if anchor_qpc:
        m = markers.get('last_normal' if last else 'first_candidate')
        if m and m.get('utc'):
            try:
                anchor_utc = datetime.fromisoformat(m['utc'])
            except Exception:
                pass

    matching = []
    for ev in native_events:
        mod = ev.get('module', '')
        hr = ev.get('hresult', 0)
        if not (mod.lower().startswith('taskmanagerdatalayer') and hr in (0x8007139F, 2147947423, -2147019873)):
            continue
        qpc_val = None
        if ev.get('raw'):
            try:
                qpc_val = int(ev['raw'])
            except ValueError:
                pass
        if qpc_val is None and ev.get('system_time') and anchor_utc and anchor_qpc:
            try:
                dt = datetime.fromisoformat(ev['system_time'])
                diff_sec = (dt - anchor_utc).total_seconds()
                qpc_val = anchor_qpc + int(diff_sec * freq)
            except Exception:
                pass
        elif qpc_val is None and ev.get('system_time') and header.get('raw_start') and header.get('StartTime'):
            try:
                dt = datetime.fromisoformat(ev['system_time'])
                ticks_val = int(dt.timestamp()) * 10_000_000 + 116444736000000000 + dt.microsecond * 10
                qpc_val = header['raw_start'] + (ticks_val - header['StartTime']) * freq // 10_000_000
            except Exception:
                pass
        if qpc_val is None:
            continue
        ev_copy = dict(ev)
        ev_copy['qpc'] = qpc_val
        if ev.get('system_time'):
            ev_copy['utc'] = ev['system_time']
        elif anchor_utc and anchor_qpc:
            ev_copy['utc'] = (anchor_utc + timedelta(seconds=(qpc_val - anchor_qpc)/freq)).isoformat()
        else:
            ev_copy['utc'] = None
        matching.append(ev_copy)

    matching = [m for m in matching if (confirmed is None or m['qpc'] <= confirmed)]
    matching.sort(key=lambda m: m['qpc'])

    if matching:
        target = matching[0]
        target_qpc = target['qpc']
        target_utc = target['utc']
        target_tid = target['tid']
        target_pid = target['pid']

        call_chain = []
        for ev in native_events:
            if ev.get('tid') != target_tid:
                continue
            ev_qpc = None
            if ev.get('raw'):
                try:
                    ev_qpc = int(ev['raw'])
                except ValueError:
                    pass
            if ev_qpc is None and ev.get('system_time') and anchor_utc and anchor_qpc:
                try:
                    dt = datetime.fromisoformat(ev['system_time'])
                    ev_qpc = anchor_qpc + int((dt - anchor_utc).total_seconds() * freq)
                except Exception:
                    pass
            if ev_qpc is None or abs(ev_qpc - target_qpc) > int(0.050 * freq):
                continue
            ft = ev.get('failure_type', 0)
            ft_name = {0: 'Exception', 1: 'Return', 2: 'Log', 3: 'FailFast'}.get(ft, f'Type{ft}')
            fn = ev.get('file', '')
            call_chain.append({
                'qpc': ev_qpc,
                'system_time': ev.get('system_time'),
                'module': ev.get('module', ''),
                'file': fn,
                'file_basename': Path(fn).name if fn else '',
                'line': ev.get('line', 0),
                'failure_type': ft,
                'failure_type_name': ft_name,
                'message': ev.get('message', ''),
                'hresult': hex(ev.get('hresult', 0))
            })
        call_chain.sort(key=lambda c: c['qpc'])

        lead_seconds = round((first - target_qpc) / freq, 3) if first else None

        two_sec_before = target_qpc - 2 * freq
        p_starts = [e for e in events if e['kind'] == 'process_start' and two_sec_before <= e['qpc'] < target_qpc]
        t_starts = [e for e in events if e['kind'] == 'thread_start' and two_sec_before <= e['qpc'] < target_qpc]

        processes_started = []
        for e in p_starts:
            p = resolve(e['pid'], e['qpc']) if resolve else None
            parent = resolve(e['parent_pid'], e['qpc']) if resolve and e.get('parent_pid') else None
            parent_img = parent['image'] if parent else str(e.get('parent_pid', 0))
            child_img = p['image'] if p else (e.get('image') or str(e.get('pid', 0)))
            processes_started.append({
                'parent': parent_img,
                'parent_pid': e.get('parent_pid', 0),
                'child': child_img,
                'child_pid': e.get('pid', 0)
            })

        t_counter = collections.Counter()
        for e in t_starts:
            p = resolve(e['pid'], e['qpc']) if resolve else None
            img = p['image'] if p else str(e.get('pid', 0))
            t_counter[img] += 1
        top_thread_creators = [{'image': img, 'count': cnt} for img, cnt in t_counter.most_common(5)]

        ctrl_start = target_qpc - 120 * freq
        ctrl_end = target_qpc - 20 * freq
        window_ticks = 2 * freq
        ctrl_p, ctrl_t = [], []
        for w_start in range(ctrl_start, ctrl_end, window_ticks):
            w_end = w_start + window_ticks
            ctrl_p.append(sum(1 for e in events if e['kind'] == 'process_start' and w_start <= e['qpc'] < w_end))
            ctrl_t.append(sum(1 for e in events if e['kind'] == 'thread_start' and w_start <= e['qpc'] < w_end))

        proc_med = float(statistics.median(ctrl_p)) if ctrl_p else 0.0
        proc_max = max(ctrl_p) if ctrl_p else 0
        thread_med = float(statistics.median(ctrl_t)) if ctrl_t else 0.0
        thread_max = max(ctrl_t) if ctrl_t else 0

        is_conforming = (first is not None and target_qpc <= first)
        verdict = 'conforming' if is_conforming else 'non_conforming'

        return {
            'recorded': True,
            'verdict': verdict,
            'reason': None,
            'hresult': hex(target['hresult']),
            'hresult_decimal': target['hresult'],
            'module': target['module'],
            'qpc': target_qpc,
            'utc': target_utc,
            'taskmgr_pid': target_pid,
            'thread_id': target_tid,
            'lead_seconds_before_first_candidate': lead_seconds,
            'call_chain': call_chain,
            'call_chain_event_count': len(call_chain),
            'activity_before': {
                'window_seconds': 2.0,
                'process_starts': len(p_starts),
                'thread_starts': len(t_starts),
                'control_windows_count': len(ctrl_p),
                'control_process_starts_median': proc_med,
                'control_process_starts_max': proc_max,
                'control_thread_starts_median': thread_med,
                'control_thread_starts_max': thread_max,
                'processes_started': processes_started,
                'top_thread_creators': top_thread_creators
            },
            'limits': 'Temporal association only; not proven causation. Call chain is WIL event log, not full stack trace.',
            'trigger_kind': observation.get('trigger_kind', 'visual'),
            'visual_corroboration_seconds': observation.get('visual_corroboration_seconds')
        }

    preceding_blind = quality.get('preceding_blind_interval') if quality else None
    circular = quality.get('circular_overwrite') if quality else False

    # Check whether the provider actually delivered events in the pre-onset window (e.g. 10s before first candidate).
    has_recent_provider_events = False
    if first is not None:
        provider_window_start = first - 10 * freq
        for ev in native_events:
            ev_qpc = None
            if ev.get('raw'):
                try:
                    ev_qpc = int(ev['raw'])
                except ValueError:
                    pass
            if ev_qpc is None and ev.get('system_time') and anchor_utc and anchor_qpc:
                try:
                    dt = datetime.fromisoformat(ev['system_time'])
                    diff_sec = (dt - anchor_utc).total_seconds()
                    ev_qpc = anchor_qpc + int(diff_sec * freq)
                except Exception:
                    pass
            elif ev_qpc is None and ev.get('system_time') and header.get('raw_start') and header.get('StartTime'):
                try:
                    dt = datetime.fromisoformat(ev['system_time'])
                    ticks_val = int(dt.timestamp()) * 10_000_000 + 116444736000000000 + dt.microsecond * 10
                    ev_qpc = header['raw_start'] + (ticks_val - header['StartTime']) * freq // 10_000_000
                except Exception:
                    pass
            if ev_qpc is not None and provider_window_start <= ev_qpc <= first:
                has_recent_provider_events = True
                break

    if preceding_blind:
        reason = 'blind: monitoring inactive or lost before incident; onset indeterminate'
        verdict = 'indeterminate'
    elif last is None:
        reason = 'missing_normal: no connected last normal observation before first candidate; onset indeterminate'
        verdict = 'indeterminate'
    elif first and (first - 120 * freq > 0 and coverage_begin > first - 120 * freq or circular):
        reason = 'uncovered: trace history does not cover pre-candidate window'
        verdict = 'indeterminate'
    elif not has_recent_provider_events:
        reason = 'provider_uncovered: no provider events delivered in pre-onset window'
        verdict = 'indeterminate'
    else:
        reason = 'not_present: provider error did not occur in retained window'
        verdict = 'non_conforming'

    return {
        'recorded': False,
        'verdict': verdict,
        'reason': reason,
        'hresult': None,
        'qpc': None,
        'utc': None,
        'call_chain': [],
        'call_chain_event_count': 0,
        'activity_before': None,
        'limits': 'Temporal association only; not proven causation.'
    }


def update_n1_summary(summary_dir, incident_name, native_sig, timestamp_utc=None):
    summary_path = Path(summary_dir)/'n1-summary.json'
    summary = {}
    if summary_path.exists():
        try:
            summary = json.loads(summary_path.read_text(encoding='utf-8'))
        except Exception:
            pass

    summary.setdefault('qualifying_count', 0)
    summary.setdefault('conforming_count', 0)
    summary.setdefault('non_conforming_count', 0)
    summary.setdefault('indeterminate_count', 0)
    summary.setdefault('rule', 'Pass if in first 10 qualifying flatlines >=9 conform (0x8007139F from TaskManagerDataLayer.dll <= first_candidate) and outside flatlines <=1/day; fail if >=3 missing')
    summary.setdefault('status', 'evaluating')
    summary.setdefault('history', [])

    existing_idx = None
    for idx, h in enumerate(summary['history']):
        if h.get('incident') == incident_name:
            existing_idx = idx
            break

    verdict = native_sig.get('verdict', 'indeterminate')
    entry = {
        'incident': incident_name,
        'timestamp_utc': timestamp_utc or datetime.now(timezone.utc).isoformat(),
        'verdict': verdict,
        'recorded': native_sig.get('recorded', False),
        'reason': native_sig.get('reason'),
        'hresult': native_sig.get('hresult'),
        'qpc': native_sig.get('qpc'),
        'lead_seconds': native_sig.get('lead_seconds_before_first_candidate')
    }

    if existing_idx is not None:
        summary['history'][existing_idx] = entry
    else:
        summary['history'].append(entry)

    summary['conforming_count'] = sum(1 for h in summary['history'] if h.get('verdict') == 'conforming')
    summary['non_conforming_count'] = sum(1 for h in summary['history'] if h.get('verdict') == 'non_conforming')
    summary['indeterminate_count'] = sum(1 for h in summary['history'] if h.get('verdict') == 'indeterminate')
    summary['qualifying_count'] = summary['conforming_count'] + summary['non_conforming_count']

    if summary['conforming_count'] >= 9:
        summary['status'] = 'passed'
    elif summary['non_conforming_count'] >= 3:
        summary['status'] = 'failed'
    else:
        summary['status'] = 'evaluating'

    try:
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    except Exception:
        pass
    return summary


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
    min_clock = (min(clocks_to_keep) - 130 * session.get('qpc_frequency', 10_000_000)) if clocks_to_keep else None
    window = (min_clock, max(clocks_to_keep)) if clocks_to_keep else None
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
    blind_intervals = observation.get('blind_intervals', [])
    preceding_blind = None
    if blind_intervals:
        first_qpc = marker('first_candidate')
        for b in reversed(blind_intervals):
            end_qpc = b.get('end_qpc')
            if end_qpc is None or (first_qpc and end_qpc >= first_qpc - 86400 * freq):
                preceding_blind = b
                break
    if preceding_blind:
        control = None  # Blind intervals cannot serve as normal controls.
    quality = {'ranking_usable':not errors, 'errors':sorted(set(errors)),
               'events_lost':header['EventsLost'], 'buffers_lost':header['BuffersLost'],
               'circular_overwrite':overwritten, 'baseline_usable':not overwritten,
               'end_baseline_available':bool(end_baseline),
               'coverage_begin_qpc':coverage_begin, 'coverage_end_qpc':coverage_end,
               'missing_pre_candidate_normal':last is None,
               'preceding_blind_interval':preceding_blind,
               'freeze_onset_indeterminate':bool(preceding_blind or last is None),
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

    native_sig = extract_native_signature(
        header.get('native_events', []),
        events, header, observation, resolve, freq,
        coverage_begin, coverage_end, quality=quality, folder=folder
    )
    result['native_signature'] = native_sig
    result['trigger_kind'] = observation.get('trigger_kind', 'visual')
    if observation.get('visual_corroboration_seconds') is not None:
        result['visual_corroboration_seconds'] = observation['visual_corroboration_seconds']

    hb_path = folder/'heartbeat.json'
    result['heartbeat'] = analyze_heartbeat(hb_path, observation, markers, native_sig, freq) if hb_path.exists() else None

    output.mkdir(parents=True,exist_ok=True)
    (output/'incident-analysis.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    (output/'incident-analysis.md').write_text(render(result),encoding='utf-8')

    try:
        update_n1_summary(output.parent, folder.name, native_sig, observation.get('timestamp'))
    except Exception:
        pass

    return result


def native_signature_text(sig, zh):
    if not sig:
        return ''
    title = '原生卡死特征：Taskmgr 内部错误' if zh else 'Native freeze signature: Taskmgr internal error'
    lines = [f'## {title}', '']
    if not sig.get('recorded'):
        reason_map = {
            'blind: monitoring inactive or lost before incident; onset indeterminate': '监测失明：事件前监测失效或断开；起点不可判定',
            'missing_normal: no connected last normal observation before first candidate; onset indeterminate': '缺失正常基线：首次候选前无相连正常观察；起点不可判定',
            'uncovered: trace history does not cover pre-candidate window': '未覆盖：追踪历史未完整覆盖候选前窗口',
            'provider_uncovered: no provider events delivered in pre-onset window': '提供程序未覆盖：起点前窗口内未收到该提供程序事件',
            'not_present: provider error did not occur in retained window': '确实没有：保留窗口内未出现该提供程序错误'
        }
        reason = sig.get('reason', 'unspecified')
        reason_text = reason_map.get(reason, reason) if zh else reason
        lines += [(f'未记录到原生特征（原因：{reason_text}）。' if zh else f'Not recorded (reason: {reason_text}).'), '']
        return '\n'.join(lines) + '\n'

    trigger = sig.get('trigger_kind')
    corroboration = sig.get('visual_corroboration_seconds')
    trigger_lines = []
    if trigger:
        corrob_str = f" ({corroboration} s visual corroboration)" if corroboration is not None else ""
        corrob_zh = f"（视觉印证 {corroboration} 秒）" if corroboration is not None else ""
        trigger_lines.append(f"触发方式：`{trigger}`{corrob_zh}。" if zh else f"Trigger path: `{trigger}`{corrob_str}.")

    verdict_label = ('符合' if sig['verdict'] == 'conforming' else '不符合') if zh else sig['verdict'].capitalize()
    lines += trigger_lines + [
        (f"状态：{verdict_label}（来自 `{sig['module']}` 的 `{sig['hresult']}`）。" if zh else
         f"Status: {verdict_label} (`{sig['hresult']}` from `{sig['module']}`)."),
        (f"时间：`{sig['utc']}`（QPC: `{sig['qpc']}`）；线程：`{sig['thread_id']}`（PID: `{sig.get('taskmgr_pid', 'unknown')}`）。" if zh else
         f"Time: `{sig['utc']}` (QPC: `{sig['qpc']}`); Thread: `{sig['thread_id']}` (PID: `{sig.get('taskmgr_pid', 'unknown')}`)."),
        (f"相对首次平线候选提前量：约 {sig.get('lead_seconds_before_first_candidate', 'unknown')} 秒。" if zh else
         f"Lead time before first flat observation: ~{sig.get('lead_seconds_before_first_candidate', 'unknown')} s."),
        ''
    ]

    cc = sig.get('call_chain', [])
    lines += [(f'### 调用链（同线程 50 ms 内 {len(cc)} 条事件）：' if zh else
               f'### Call chain ({len(cc)} events in thread within 50 ms):'), '']
    for i, item in enumerate(cc, 1):
        fn = item.get('file_basename') or item.get('file') or 'unknown'
        ln = item.get('line', 0)
        loc = f'{fn}:{ln}' if ln else fn
        msg = f": {item['message']}" if item.get('message') else ''
        lines.append(f"{i}. `[{item.get('failure_type_name', 'Event')}]` `{loc}` ({item.get('hresult', '')}){msg}")
    lines.append('')

    act = sig.get('activity_before')
    if act:
        lines += [(f"### 原生错误前 [-2, 0) 秒伴随活动：" if zh else
                   f"### Activity in [-2, 0) s before native error:"), '']
        lines += [
            (f"- 进程启动：{act['process_starts']} 次（[-120, -20) 秒内 2 秒对照窗口中位数：{act['control_process_starts_median']}，最大值：{act['control_process_starts_max']}）。" if zh else
             f"- Process starts: {act['process_starts']} (control median: {act['control_process_starts_median']}, max: {act['control_process_starts_max']} in 2 s windows [-120, -20) s)."),
            (f"- 线程启动：{act['thread_starts']} 次（[-120, -20) 秒内 2 秒对照窗口中位数：{act['control_thread_starts_median']}，最大值：{act['control_thread_starts_max']}）。" if zh else
             f"- Thread starts: {act['thread_starts']} (control median: {act['control_thread_starts_median']}, max: {act['control_thread_starts_max']} in 2 s windows [-120, -20) s).")
        ]
        procs = act.get('processes_started', [])
        if procs:
            procs_str = '、'.join(f"{p['parent']} -> {p['child']}" for p in procs) if zh else ', '.join(f"{p['parent']} -> {p['child']}" for p in procs)
            lines.append((f"- 启动的进程（父→子）：{procs_str}" if zh else f"- Started processes (parent -> child): {procs_str}"))
        top_t = act.get('top_thread_creators', [])
        if top_t:
            top_str = '、'.join(f"{t['image']} ({t['count']})" for t in top_t) if zh else ', '.join(f"{t['image']} ({t['count']})" for t in top_t)
            lines.append((f"- 线程启动最多进程：{top_str}" if zh else f"- Top thread creators: {top_str}"))
        lines.append('')

    return '\n'.join(lines) + '\n'




def analyze_heartbeat(heartbeat_path, observation, markers, native_sig, freq):
    """Analyze Task Manager CurrentUpdateCycle heartbeat events (N4)."""
    if not heartbeat_path or not Path(heartbeat_path).exists():
        return {'recorded': False, 'reason': 'no heartbeat file found in incident folder'}
    try:
        data = json.loads(Path(heartbeat_path).read_text(encoding='utf-8'))
    except Exception as exc:
        return {'recorded': False, 'reason': f'heartbeat file corrupted: {exc}'}

    events = data.get('events', [])
    if not events:
        return {'recorded': False, 'reason': 'heartbeat event list is empty'}

    first = markers.get('first_candidate', {}).get('qpc') if isinstance(markers.get('first_candidate'), dict) else None
    confirmed = markers.get('confirmed', {}).get('qpc') if isinstance(markers.get('confirmed'), dict) else None
    native_qpc = native_sig.get('qpc') if native_sig and native_sig.get('recorded') else None

    started = [e for e in events if e.get('operation') == 'UpdateStarted']
    finalized = [e for e in events if e.get('operation') == 'UpdateFinalized']

    intervals = []
    for a, b in zip(started, started[1:]):
        if type(a.get('qpc')) is int and type(b.get('qpc')) is int and b['qpc'] > a['qpc']:
            intervals.append((b['qpc'] - a['qpc']) / freq)

    med_interval = float(statistics.median(intervals)) if intervals else 1.0
    min_interval = float(min(intervals)) if intervals else 1.0
    max_interval = float(max(intervals)) if intervals else 1.0

    onset_qpc = first or confirmed
    continued_during_freeze = False
    updates_before_onset = []
    updates_after_onset = []

    if onset_qpc:
        updates_before_onset = [e for e in started if e.get('qpc', 0) < onset_qpc]
        updates_after_onset = [e for e in started if e.get('qpc', 0) >= onset_qpc]
        continued_during_freeze = len(updates_after_onset) > 0

    last_update = updates_before_onset[-1] if updates_before_onset else (started[-1] if started else None)
    first_missing_qpc = None
    lead_seconds_before_first_candidate = None
    timing_relative_to_native_error_seconds = None

    if last_update and not continued_during_freeze and onset_qpc:
        first_missing_qpc = last_update['qpc'] + int(med_interval * freq)
        if first:
            lead_seconds_before_first_candidate = round((first - first_missing_qpc) / freq, 3)
        if native_qpc:
            timing_relative_to_native_error_seconds = round((first_missing_qpc - native_qpc) / freq, 3)

    return {
        'recorded': True,
        'total_events': len(events),
        'update_started_count': len(started),
        'update_finalized_count': len(finalized),
        'continued_during_freeze': continued_during_freeze,
        'interval_seconds': {
            'median': round(med_interval, 3),
            'min': round(min_interval, 3),
            'max': round(max_interval, 3),
            'samples': len(intervals)
        },
        'last_update_before_onset': last_update,
        'first_missing_qpc': first_missing_qpc,
        'lead_seconds_before_first_candidate': lead_seconds_before_first_candidate,
        'timing_relative_to_native_error_seconds': timing_relative_to_native_error_seconds,
        'limits': 'Heartbeat is recorded for offline diagnostic evaluation only; it is not a recovery criterion.'
    }


def heartbeat_text(hb, zh):
    if not hb:
        return ''
    title = '数据层心跳：CurrentUpdateCycle' if zh else 'Data layer heartbeat: CurrentUpdateCycle'
    lines = [f'## {title}', '']
    if not hb.get('recorded'):
        reason = hb.get('reason', 'not recorded')
        reason_zh = '未记录到心跳事件' if ('empty' in reason or 'not found' in reason) else reason
        lines += [(f'未记录到数据层心跳（原因：{reason_zh}）。' if zh else f'Heartbeat not recorded (reason: {reason}).'),
                  ('注：数据层心跳仅作离线诊断评估记录，不作为恢复判据。' if zh else
                   'Note: Heartbeat is recorded for offline diagnostic evaluation only; not a recovery criterion.'),
                  '']
        return '\n'.join(lines) + '\n'

    continued = hb.get('continued_during_freeze')
    status_en = 'Continued during freeze' if continued else 'Stopped before freeze onset'
    status_zh = '冻结期间持续循环' if continued else '在冻结起点前停止循环'
    intervals = hb.get('interval_seconds', {})

    lines += [
        (f"状态：{status_zh}（更新开始：{hb.get('update_started_count')} 次，更新完成：{hb.get('update_finalized_count')} 次）。" if zh else
         f"Status: {status_en} ({hb.get('update_started_count')} started, {hb.get('update_finalized_count')} finalized)."),
        (f"循环间隔：中位数 {intervals.get('median')} 秒（范围：{intervals.get('min')}–{intervals.get('max')} 秒，样本数：{intervals.get('samples')}）。" if zh else
         f"Cycle interval: median {intervals.get('median')} s (range: {intervals.get('min')}–{intervals.get('max')} s, samples: {intervals.get('samples')}).")
    ]

    if not continued and hb.get('lead_seconds_before_first_candidate') is not None:
        lines.append(
            f"首次缺失周期提前量：相对首次平线候选约 {hb.get('lead_seconds_before_first_candidate')} 秒。" if zh else
            f"First missing cycle lead time: ~{hb.get('lead_seconds_before_first_candidate')} s before first flat observation."
        )
    if not continued and hb.get('timing_relative_to_native_error_seconds') is not None:
        diff = hb.get('timing_relative_to_native_error_seconds')
        order_zh = f"原生错误后 {diff} 秒" if diff >= 0 else f"原生错误前 {abs(diff)} 秒"
        order_en = f"{diff} s after native error" if diff >= 0 else f"{abs(diff)} s before native error"
        lines.append(
            f"与原生错误先后：{order_zh}。" if zh else
            f"Timing relative to native error: {order_en}."
        )

    lines += [
        ('注：数据层心跳仅作离线诊断评估记录，不作为恢复判据。' if zh else
         'Note: Data layer heartbeat is recorded for diagnostic evaluation only; it is not a recovery criterion.'),
        ''
    ]
    return '\n'.join(lines) + '\n'


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
    blind = q.get('preceding_blind_interval')
    blind_notice_zh = (f"前序监测失明记录：持续 {blind.get('duration_seconds', 'unknown')} 秒（原因：{blind.get('reason', 'unknown')}）。冻结发生起点不可判定，失明区间不能作为正常对照。\n\n"
                       if blind else '')
    blind_notice_en = (f"Preceding blind interval recorded: {blind.get('duration_seconds', 'unknown')} s (reason: {blind.get('reason', 'unknown')}). Freeze onset is indeterminate; blind intervals cannot serve as controls.\n\n"
                       if blind else '')
    blocks = []
    for zh in (False, True):
        intro = ('<a id="zh"></a>\n\n[English](#en) · **中文**\n\n以英文为准。\n\n# 首次平线前后活动\n\n'
                 '**仅为时间关联，未证明因果。** 父进程链不是调用栈。平线前仅指最后正常观察到首次平线之间的短暂转变窗口；'
                 '平线后是等待确认期间的伴随活动。边界受采样与界面延迟影响，不等于底层故障时刻。恰在首次平线边界的事件只计入后段。\n\n'
                 f"整体数据可用：{q['ranking_usable']}；错误：{errors}；循环覆盖：{q['circular_overwrite']}；事件／缓冲区丢失：{q['events_lost']}/{q['buffers_lost']}。\n\n"
                 f'最近连续性重置：{reason}；断开前的正常样本不作为对照。\n\n'
                 + blind_notice_zh if zh else
                 '<a id="en"></a>\n\n**English** · [中文](#zh)\n\nEnglish is the controlling version.\n\n# Activity before and after first flat observation\n\n'
                 '**Temporal association only; causality unproven.** Parent chains are not call stacks. Before-first covers only the short transition '
                 'from the last normal observation to the first flat observation; after-first covers companion activity while awaiting confirmation. '
                 'Sampling and display latency mean this boundary is not the native fault time. Events exactly at the first-flat boundary belong only to after-first.\n\n'
                 f"Overall data usable: {q['ranking_usable']}; errors: {errors}; circular overwrite: {q['circular_overwrite']}; lost events/buffers: {q['events_lost']}/{q['buffers_lost']}.\n\n"
                 f'Latest continuity reset: {reason}; disconnected normal samples are never controls.\n\n'
                 + blind_notice_en)
        tail = ('每段独立排行，正常对照尽量等长；时长不足时保留实际时长与每秒速率，缺失身份和对照保持未知。'
                'JSON schema 2 的顶层 top 仅代表平线前，整体 event_counts 仍是两段总计。完整边界、实例身份和对照保存在 incident-analysis.json。'
                '这里未测量 Taskmgr 的查询返回值、缓冲区字节贡献或故障因果链。\n' if zh else
                'Each phase ranks independently with a normal control of matching duration where available; shorter controls retain their actual exposure and rates. '
                'Missing identities and controls remain unknown. JSON schema 2 top contains before-first rows only; overall event_counts still cover both phases. '
                'Full boundaries, instance identities and controls are in incident-analysis.json. No Taskmgr query statuses, buffer-byte contributions or causal chain are measured.\n')
        native_sec = native_signature_text(result.get('native_signature'), zh)
        hb_sec = heartbeat_text(result.get('heartbeat'), zh)
        blocks.append(intro+native_sec+hb_sec+phase_text('before_first',zh)+phase_text('after_first',zh)+tail)
    return 'The English version governs.\n\n'+ '\n---\n\n'.join(blocks)
