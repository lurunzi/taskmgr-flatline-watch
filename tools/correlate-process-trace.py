"""Rank the top three thread creators before an abnormal query receipt (not causality)."""
import argparse
import bisect
import collections
import gzip
import csv
import json
import math
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

NS = {'e':'http://schemas.microsoft.com/win/2004/08/events/event'}
PROVIDER = '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}'
TASKMGR_PROVIDER = '{2e635d8e-1107-4555-9319-32eeb895aaae}'
KINDS = {1:'process_start',2:'process_stop',3:'thread_start',4:'thread_stop'}
EPOCH = 116444736000000000


def ticks(text):
    """ISO UTC/offset timestamp to FILETIME, preserving seven fractional digits."""
    text = text.strip().strip('\u200e\u200f')
    match = re.fullmatch(r'(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:\d\d)',text)
    if not match:
        raise ValueError('unknown creation-time encoding')
    base,frac,offset = match.groups()
    dt = datetime.fromisoformat(base+offset.replace('Z','+00:00'))
    return int(dt.timestamp())*10_000_000+EPOCH+int(((frac or '')+'0000000')[:7])


def decode(path, thread_window=None):
    source = gzip.open(path,'rb') if str(path).endswith('.gz') else open(path,'rb')
    try:
        return _decode_stream(source, thread_window)
    finally:
        source.close()


def _decode_stream(source, thread_window):
    events,header,errors = [],None,[]
    native_events = []
    first_clock = last_clock = None
    total = 0
    iterator = ET.iterparse(source,events=('start','end'))
    _,root = next(iterator)
    for action,node in iterator:
        if action!='end' or node.tag!='{'+NS['e']+'}Event':
            continue
        data = {d.get('Name'):(d.text or '').strip() for d in node.findall('e:EventData/e:Data',NS)}
        provider = node.find('e:System/e:Provider',NS).get('Guid','').lower()
        clock = node.find('e:System/e:TimeCreated',NS)
        raw = clock.get('RawTime')
        if 'PerfFreq' in data and 'StartTime' in data:
            header = {k:int(data[k],0) for k in ('PerfFreq','StartTime','EndTime','EventsLost','BuffersLost','BuffersWritten','BufferSize','MaxFileSize')}
            header['raw_start'] = int(raw) if raw else None
            header['clock_type'] = int(data.get('ReservedFlags','0'),0)
        if provider==TASKMGR_PROVIDER:
            ex = node.find('e:System/e:Execution', NS)
            pid = int(ex.get('ProcessID', 0)) if ex is not None else 0
            tid = int(ex.get('ThreadID', 0)) if ex is not None else 0
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
                native_events.append({
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
        if provider==PROVIDER:
            total += 1
            if total>2_000_000:
                raise ValueError('scanned event count bound exceeded; raw ETL retained')
            event_id = int(node.findtext('e:System/e:EventID',namespaces=NS))
            if not raw or node.find('e:ProcessingErrorData',NS) is not None:
                errors.append('provider event lacked raw clock or could not be decoded')
            elif event_id not in KINDS:
                errors.append(f'unknown provider event {event_id}')
            else:
                stamp = int(raw)
                first_clock = stamp if first_clock is None else min(first_clock,stamp)
                last_clock = stamp if last_clock is None else max(last_clock,stamp)
                # Keep all process lifetimes for PID reuse/parent resolution.
                # Excluded thread events remain in the lossless XML.gz and ETL.
                keep = (event_id in (1,2) or thread_window is None or
                        thread_window[0]<=stamp<=thread_window[1])
                if keep:
                    events.append({'qpc':stamp,'kind':KINDS[event_id],'pid':int(data['ProcessID']),
                               'tid':int(data.get('ThreadID','0')),'parent_pid':int(data.get('ParentProcessID','0')),
                               'create_time':ticks(data['CreateTime']) if data.get('CreateTime') else None,
                               'image':data.get('ImageName'),'sequence':data.get('ProcessSequenceNumber')})
            if len(events)>500_000:
                raise ValueError('event count bound exceeded; raw ETL retained')
        node.clear()
        root.clear()
    if not header or header['raw_start'] is None or header['clock_type']!=1:
        raise ValueError('missing QPC trace header; no correlation is possible')
    if header['EventsLost'] or header['BuffersLost']:
        errors.append('ETW lost events or buffers')
    if header['BuffersWritten']*header['BufferSize']>=header['MaxFileSize']*1024**2:
        errors.append('circular trace may have overwritten earlier buffers')
    events.sort(key=lambda e:e['qpc'])
    header.update(retained_first_qpc=first_clock,retained_last_qpc=last_clock,
                  scanned_events=total,selected_events=len(events),
                  native_events=native_events)
    return events,header,errors


def identities(events,baseline,header):
    """Join by PID and lifetime; never use the event-header PID as thread owner."""
    instances = collections.defaultdict(list)
    def to_qpc(created):
        return header['raw_start']+(created-header['StartTime'])*header['PerfFreq']//10_000_000
    for b in baseline:
        if not b.get('CreateTime'):
            continue
        created = ticks(b['CreateTime'])
        instances[int(b['ProcessId'])].append({'created':created,'begin':to_qpc(created),'end':None,
            'image':b.get('ExecutablePath') or b.get('Name'),'parent_pid':int(b['ParentProcessId']),
            'sequence':None,'source':'baseline (microsecond precision)'})
    for e in events:
        if e['kind'] not in ('process_start','process_stop'):
            continue
        same = [i for i in instances[e['pid']] if i['created']==e['create_time'] or
                (i['source'].startswith('baseline') and i['created']//10==e['create_time']//10)]
        if len(same)==1:
            item = same[0]
            item.update(created=e['create_time'],sequence=e['sequence'])
            if e['kind']=='process_start':
                item.update(begin=e['qpc'],image=e['image'],parent_pid=e['parent_pid'],source='ETW process start')
        else:
            item = {'created':e['create_time'],'begin':e['qpc'] if e['kind']=='process_start' else to_qpc(e['create_time']),'end':None,
                    'image':e['image'],'parent_pid':e['parent_pid'],'sequence':e['sequence'],
                    'source':'ETW '+e['kind']}
            instances[e['pid']].append(item)
        if e['kind']=='process_stop':
            item['end'] = e['qpc']
    for e in events:
        valid = [i for i in instances[e['pid']] if i['begin']<=e['qpc'] and (i['end'] is None or e['qpc']<=i['end'])]
        # A process-start notification can precede its payload creation clock by
        # small implementation differences. Exact payload identity wins there.
        if e['create_time'] and e['kind'] in ('process_start','process_stop'):
            valid = [i for i in instances[e['pid']] if i['created']==e['create_time'] and
                     (i['end'] is None or e['qpc']<=i['end'])]
        item = valid[0] if len(valid)==1 else None
        e['identity'] = f"{e['pid']}:{item['created']}" if item else f"{e['pid']}:unresolved"
        e['process'] = item
    return events


def correlate(folder,lookback_ms=10,output=None):
    if not math.isfinite(lookback_ms) or not 0<lookback_ms<=10:
        raise ValueError('lookback-ms must be in (0,10]')
    folder = Path(folder)
    output = Path(output) if output is not None else folder
    trace = folder/'process-trace'
    events,header,errors = decode(trace/'events.xml')
    origin = json.loads((folder/'origin.json').read_text(encoding='utf-8'))
    if header['PerfFreq']<=0 or origin.get('qpc_frequency')!=header['PerfFreq']:
        raise ValueError('query/ETW clock frequencies do not match')
    session = json.loads((trace/'session.json').read_text(encoding='utf-8'))
    if session.get('status')!='stopped' or session.get('decode_exit')!=0:
        errors.append('ETW stop/decode not verified')
    if session.get('qpc_frequency')!=header['PerfFreq']:
        errors.append('session/ETW clock frequencies do not match')
    baseline = json.loads((trace/'baseline.json').read_text(encoding='utf-8-sig'))
    # With loss/overwriting/decode gaps, a stale baseline PID may appear alive
    # after unobserved reuse. Do not emit named candidates from incomplete data.
    if not errors:
        events = identities(events,baseline,header)
    queries_path = folder/'query-trace.jsonl'
    queries = [json.loads(s) for s in queries_path.read_text(encoding='utf-8').splitlines()] if queries_path.exists() else []
    rows,windows,skipped = [],[],[]
    freq = header['PerfFreq']
    lookback = int(freq*lookback_ms/1000)
    if lookback<1:
        raise ValueError('lookback is below one QPC tick')
    # Thread creation, not net count or every process event, is the ranking key.
    starts = [e for e in events if e['kind']=='thread_start']
    clocks = [e['qpc'] for e in starts]
    for q in queries:
        if errors:
            break
        begin,end = q.get('entry_received_qpc'),q.get('return_received_qpc')
        calls = q.get('calls',[])
        # The final NtQSI return marker is closer to the query than the wrapper
        # return. Do not silently fall back to wall time or claim target timing.
        anchor = calls[-1].get('received_qpc') if calls else None
        reason = None
        if not all(isinstance(t,int) and t>0 for t in (begin,anchor,end)) or not q.get('query','').startswith(origin['instance']+':'):
            reason = 'query missing QPC receipt or instance identity'
        elif not begin<=anchor<=end:
            reason = 'query receipt clocks out of order'
        elif min(begin,anchor-lookback)<session['baseline_end_qpc'] or end>session['stopped_qpc']:
            reason = 'query/lookback outside baseline-to-stop coverage'
        if reason:
            skipped.append({'query':q.get('query'),'reason':reason})
            continue
        lower = anchor-lookback
        selected = starts[bisect.bisect_left(clocks,lower):bisect.bisect_right(clocks,anchor)]
        groups = collections.defaultdict(list)
        for e in selected:
            # Unknown lifetimes must not collapse reused PIDs into one creator.
            if e['process'] is not None:
                groups[(e['identity'],e['process'].get('image') or 'unknown')].append(e)
        ranked = sorted(groups.items(),key=lambda pair:(-len(pair[1]),pair[0]))[:3]
        top = []
        for rank,((identity,image),group) in enumerate(ranked,1):
            p = group[0]['process'] or {}
            row = {'query':q['query'],'query_kind':q['kind'],'rank':rank,'identity':identity,
                   'pid':group[0]['pid'],'create_time':p['created'],'image':image,
                   'parent_pid':p.get('parent_pid'),'thread_start_count':len(group),
                   'thread_starts_per_second':len(group)*freq/lookback,
                   'lookback_ms':lookback*1000/freq,'window_begin_qpc':lower,'anchor_qpc':anchor,
                   'first_start_qpc':group[0]['qpc'],'last_start_qpc':group[-1]['qpc'],
                   'assessment':'temporal ranking only; not a fault threshold'}
            rows.append(row)
            top.append(row)
        windows.append({'query':q['query'],'kind':q['kind'],'receipt_begin_qpc':begin,'receipt_end_qpc':end,
                        'window_begin_qpc':lower,'anchor_qpc':anchor,'lookback_ms':lookback*1000/freq,
                        'thread_start_count':len(selected),
                        'unresolved_thread_starts':sum(e['process'] is None for e in selected),
                        'ranked_processes_available':len(groups),'top':top,'calls':calls})
    result = {'instance':origin['instance'],'etw_events':len(events),'queries':len(windows),
              'abnormal_queries_seen':len(queries),'query_kinds':dict(collections.Counter(q['kind'] for q in queries)),
              'lookback_ms':lookback_ms,'top_limit':3,'ranking_rows':len(rows),'skipped_queries':skipped,
              'errors':sorted(set(errors)),'trace_header':header,'attribution':'unproven',
              'anchor':'final NtQSI return marker received by debugger host',
              'clock_limit':'Query times are debugger-host receipts; latency has no measured upper bound. The inclusive lookback window is a search heuristic, not microsecond target timing. Thread starts do not prove snapshot growth, kernel buffer contribution or a universal churn threshold.',
              'windows':windows}
    output.mkdir(parents=True,exist_ok=True)
    (output/'thread-top-delta.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    fields = ['query','query_kind','rank','identity','pid','create_time','image','parent_pid',
              'thread_start_count','thread_starts_per_second','lookback_ms','window_begin_qpc','anchor_qpc',
              'first_start_qpc','last_start_qpc','assessment']
    with (output/'thread-top-delta.tsv').open('w',encoding='utf-8',newline='') as f:
        writer = csv.DictWriter(f,fields,delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    return {k:v for k,v in result.items() if k!='windows'}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--lookback-ms',type=float,default=10,help='inclusive lookback before final NtQSI receipt, (0,10] ms')
    parser.add_argument('--output',type=Path,help='write new Top Delta reports here, preserving historical evidence')
    args = parser.parse_args()
    if not math.isfinite(args.lookback_ms) or not 0<args.lookback_ms<=10:
        parser.error('lookback-ms must be in (0,10]')
    summary = correlate(args.folder,args.lookback_ms,args.output)
    print(json.dumps(summary,indent=2))
    if summary['errors'] or summary['skipped_queries']:
        raise SystemExit(2)
