"""Rebuild provenance from operations and lifetimes, without trusting collector links.

Outputs audit.json and address-chain.tsv. This is deliberately conservative:
address closure, a visual freeze, and update-task failure are separate findings.
"""
import argparse
import hashlib
import json
import struct
from pathlib import Path


class DumpMemory:
    def __init__(self, path):
        self.file = path.open('rb')
        header = self.file.read(32)
        magic,_,count,directory,_,_,_ = struct.unpack('<4sIIIIIQ',header)
        if magic != b'MDMP' or not 0 < count < 1024:
            raise ValueError('bad dump directory')
        self.file.seek(directory)
        entries = [struct.unpack('<III',self.file.read(12)) for _ in range(count)]
        memory = next((offset for kind,_,offset in entries if kind==9),None)
        if memory is None:
            raise ValueError('no Memory64List')
        self.file.seek(memory)
        count,offset = struct.unpack('<QQ',self.file.read(16))
        if count>1000000:
            raise ValueError('memory range bound')
        self.ranges = []
        for _ in range(count):
            address,size = struct.unpack('<QQ',self.file.read(16))
            self.ranges.append((address,size,offset))
            offset += size

    def read(self,address,size):
        for start,length,offset in self.ranges:
            if start <= address and address+size <= start+length:
                self.file.seek(offset+address-start)
                data = self.file.read(size)
                if len(data)!=size:
                    raise ValueError('truncated dump memory')
                return data
        raise ValueError(f'address absent from dump: {address:x}')

    def identity(self,address):
        data = self.read(address,0x20)
        return struct.unpack_from('<I',data,0x10)[0],struct.unpack_from('<Q',data,0x18)[0]


def audit(folder):
    events = [json.loads(x) for x in (folder/'address-events.jsonl').read_text(encoding='utf-8').splitlines()]
    if not events:
        raise ValueError('empty event stream')
    instance = events[0]['instance']
    live,raw,pending,queries,edges = {},{},{},{},{}
    errors,conflicts,rows = [],[],[]
    for expected,e in enumerate(events,1):
        if e['event']!=expected or e['instance']!=instance:
            errors.append('event sequence or instance mismatch')
        kind = e['kind']
        if kind=='incomplete':
            errors.append(e['reason'])
        elif kind=='raw_record':
            data = (folder/e['buffer_file']).read_bytes()
            offset = e['address']-e['begin']
            if hashlib.sha256(data).hexdigest()!=e['sha256'] or len(data)!=e['end']-e['begin']:
                errors.append('raw buffer hash/length mismatch')
            walked,count = 0,0
            while walked+256<=len(data) and count<8192:
                step = struct.unpack_from('<I',data,walked)[0]
                count += 1
                if not step:
                    break
                if step<256:
                    errors.append('invalid raw walk')
                    break
                walked += step
            if walked!=offset or offset+256>len(data):
                errors.append('raw record is not the bounded tail')
            else:
                pid,created = struct.unpack_from('<Q',data,offset+0x50)[0],struct.unpack_from('<Q',data,offset+0x20)[0]
                if pid or created or struct.unpack_from('<I',data,offset)[0]:
                    errors.append('raw record fields mismatch')
            raw[e['address']] = {'query':e['query'],'edge':e['event'],'begin':e['begin']}
            edges[e['event']] = e
        elif kind=='query_return':
            queries[e['query']] = e
        elif kind=='operation_begin':
            origin = (raw if e['operation']=='CONVERT' else live).get(e['source'])
            pending[e['event']] = (e,dict(origin) if origin else None)
            live.pop(e['destination'],None)
        elif kind=='operation_end':
            begin,origin = pending.pop(e['operation_event'],(None,None))
            valid = begin and origin and begin['source']==e['source'] and begin['destination']==e['destination'] \
                and origin['query']==e['query'] and origin['edge']==e['parent'] and begin['tid']==e['tid']
            if not valid:
                errors.append(f'operation {e["event"]} lacks a live source and matching entry')
                continue
            if (e['operation']=='CONVERT' and e['result'] & 0xffffffff) or \
               (e['operation']!='CONVERT' and e['result']!=e['destination']):
                errors.append('unsuccessful native operation')
                continue
            live[e['destination']] = {'query':origin['query'],'edge':e['event']}
            edges[e['event']] = e
        elif kind=='lifetime_end':
            live.pop(e['address'],None)
        elif kind=='buffer_lifetime_end':
            raw = {a:n for a,n in raw.items() if n['begin']!=e['begin']}
        elif kind=='cache_swap_end':
            before,after = e['before'],e['after']
            if before[0]['head']!=after[1]['head'] or before[1]['head']!=after[0]['head']:
                errors.append('cache swap heads did not exchange')
        elif kind=='conflict':
            origin = live.get(e['current'])
            if not origin:
                errors.append('conflict current object has no live recorded ancestry')
                continue
            q = queries.get(origin['query'])
            if not q or q['hr']!=0 or q['r14']!=4 or len(q['calls'])!=3 or any(c['status']!=0xc0000004 for c in q['calls']):
                errors.append('query lacks three failures followed by S_OK')
            vector = e.get('vector')
            if not vector:
                errors.append('missing aggregation vector entry')
                continue
            start,end = int(vector['begin'],16),int(vector['end'],16)
            if not start<=e['current']<end or (e['current']-start)%0x220 or end!=e['vector_end']:
                errors.append('conflict current object is not an aggregation vector element')
            if e['current_identity']['pid'] or e['current_identity']['create_time'] or \
               e['existing_identity']['pid'] or not e['existing_identity']['create_time']:
                errors.append('not the Idle identity conflict')
            chain = []
            edge = origin['edge']
            while edge in edges and len(chain)<128:
                item = edges[edge]
                chain.append(item)
                if item['kind']=='raw_record':
                    break
                edge = item['parent']
            if not chain or chain[-1]['kind']!='raw_record':
                errors.append('chain does not reach raw record')
            else:
                seed = chain[-1]
                if q and q.get('published_buffer') != [seed['begin'],seed['end'],seed['capacity']]:
                    errors.append('published query vector was not verified against the seed buffer')
                for item in reversed(chain):
                    rows.append({'query':origin['query'],'event':item['event'],
                                 'operation':item.get('operation','raw_record'),
                                 'source':hex(item.get('source',item.get('begin',0))),
                                 'destination':hex(item.get('destination',item.get('address',0)))})
                rows.append({'query':origin['query'],'event':e['event'],'operation':'conflict',
                             'source':hex(e['current']),'destination':hex(e['existing'])})
            try:
                dump = DumpMemory(folder/'Taskmgr-origin.dmp')
                try:
                    if dump.identity(e['current'])!=(0,0) or dump.identity(e['existing'])!=(0,e['existing_identity']['create_time']):
                        errors.append('dump object identities disagree')
                    dumped = struct.unpack('<QQQ',dump.read(int(vector['address'],16),24))
                    if dumped!=(start,end,int(vector['capacity'],16)):
                        errors.append('dump aggregation vector disagrees')
                finally:
                    dump.file.close()
            except Exception as exc:
                errors.append(f'endpoint dump check failed: {exc}')
            conflicts.append(e['event'])
    result = {'instance':instance,'events':len(events),'conflicts':conflicts,'errors':errors,
              'address_chain_closed':bool(conflicts) and not errors,
              'post_detach_visual_freeze':False,'update_task_failure_independently_verified':False,
              'acceptance':'incomplete'}
    freeze = folder/'freeze.json'
    if freeze.exists():
        f = json.loads(freeze.read_text(encoding='utf-8'))
        result['post_detach_visual_freeze'] = f.get('instance')==instance and f.get('after_debugger_detach') is True \
            and f['frames'][-1]['measurement']['state']=='suspect' and f['frames'][-1]['measurement']['seconds']>=30
    # An external, evidence-backed stack/update-action audit is still required.
    (folder/'audit.json').write_text(json.dumps(result,indent=2)+'\n',encoding='utf-8')
    keys = ['query','event','operation','source','destination']
    (folder/'address-chain.tsv').write_text('\t'.join(keys)+'\n'+''.join('\t'.join(str(r[k]) for k in keys)+'\n' for r in rows),encoding='utf-8')
    return result


if __name__=='__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('folder',type=Path)
    a = p.parse_args()
    print(json.dumps(audit(a.folder),indent=2))
