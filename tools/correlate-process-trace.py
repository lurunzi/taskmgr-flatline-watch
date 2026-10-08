"""List temporal candidates, never a verdict, using raw ETW QPC and query receipts."""
import argparse
import collections
import csv
import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

NS = {'e':'http://schemas.microsoft.com/win/2004/08/events/event'}
PROVIDER = '{22fb2cd6-0e7b-422b-a0c7-2fad1fd0e716}'
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


def decode(path):
    events,header,errors = [],None,[]
    iterator = ET.iterparse(path,events=('start','end'))
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
        if provider==PROVIDER:
            event_id = int(node.findtext('e:System/e:EventID',namespaces=NS))
            if not raw or node.find('e:ProcessingErrorData',NS) is not None:
                errors.append('provider event lacked raw clock or could not be decoded')
            elif event_id not in KINDS:
                errors.append(f'unknown provider event {event_id}')
            else:
                events.append({'qpc':int(raw),'kind':KINDS[event_id],'pid':int(data['ProcessID']),
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


def correlate(folder,margin_ms=100):
    trace = folder/'process-trace'
    events,header,errors = decode(trace/'events.xml')
    origin = json.loads((folder/'origin.json').read_text(encoding='utf-8'))
    if origin.get('qpc_frequency')!=header['PerfFreq']:
        raise ValueError('query/ETW clock frequencies do not match')
    session = json.loads((trace/'session.json').read_text(encoding='utf-8'))
    if session.get('status')!='stopped' or session.get('decode_exit')!=0:
        errors.append('ETW stop/decode not verified')
    baseline = json.loads((trace/'baseline.json').read_text(encoding='utf-8-sig'))
    # With loss/overwriting/decode gaps, a stale baseline PID may appear alive
    # after unobserved reuse. Do not emit named candidates from incomplete data.
    if not errors:
        events = identities(events,baseline,header)
    queries_path = folder/'query-trace.jsonl'
    queries = [json.loads(s) for s in queries_path.read_text(encoding='utf-8').splitlines()] if queries_path.exists() else []
    rows,windows = [],[]
    freq = header['PerfFreq']
    margin = int(freq*margin_ms/1000)
    for q in queries:
        if errors:
            break
        begin,end = q.get('entry_received_qpc'),q.get('return_received_qpc')
        if not begin or not end or not q.get('query','').startswith(origin['instance']+':'):
            errors.append('query missing clock or instance identity')
            continue
        if begin<session['baseline_end_qpc'] or end>session['stopped_qpc']:
            errors.append('query outside baseline-to-stop coverage')
            continue
        selected = [e for e in events if begin-margin<=e['qpc']<=end+margin]
        windows.append({'query':q['query'],'kind':q['kind'],'receipt_begin_qpc':begin,'receipt_end_qpc':end,
                        'margin_ms':margin_ms,'events':selected,'calls':q['calls']})
        groups = collections.defaultdict(list)
        for e in selected:
            groups[e['identity']].append(e)
        for identity,group in groups.items():
            p = group[0]['process'] or {}
            counts = collections.Counter(e['kind'] for e in group)
            inside = sum(begin<=e['qpc']<=end for e in group)
            rows.append({'query':q['query'],'query_kind':q['kind'],'identity':identity,
                         'image':p.get('image') or 'unknown','parent_pid':p.get('parent_pid'),
                         'process_start':counts['process_start'],'process_stop':counts['process_stop'],
                         'thread_start':counts['thread_start'],'thread_stop':counts['thread_stop'],
                         'inside_receipt_window':inside,'margin_only':len(group)-inside,
                         'net_threads':counts['thread_start']-counts['thread_stop'],
                         'assessment':'temporal candidate only'})
    result = {'instance':origin['instance'],'etw_events':len(events),'queries':len(windows),
              'abnormal_queries_seen':len(queries),
              'errors':sorted(set(errors)),'trace_header':header,'attribution':'unproven',
              'clock_limit':'Query times are debugger-host receipts; latency has no measured upper bound. Margin is a search heuristic, not an error bound. Coincidence and net growth do not prove kernel buffer contribution.',
              'windows':windows}
    (folder/'process-correlation.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    fields = ['query','query_kind','identity','image','parent_pid','process_start','process_stop',
              'thread_start','thread_stop','inside_receipt_window','margin_only','net_threads','assessment']
    with (folder/'process-candidates.tsv').open('w',encoding='utf-8',newline='') as f:
        writer = csv.DictWriter(f,fields,delimiter='\t')
        writer.writeheader()
        writer.writerows(rows)
    return {k:v for k,v in result.items() if k!='windows'}


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--margin-ms',type=float,default=100)
    args = parser.parse_args()
    if not 0<=args.margin_ms<=1000:
        parser.error('margin-ms must be in [0,1000]')
    print(json.dumps(correlate(args.folder,args.margin_ms),indent=2))
