"""Opt-in, build-pinned address provenance tracing, using QueryTrace's transport.

No field-based provenance: only a watched raw address, completed native copy
operations, and live object generations create edges. All addresses are per run.
The target is paused while a sparse AC event is read; normal query events resume
inside cdb. Only an exhausted query arms the expensive conversion breakpoints.
"""
import ctypes as C
import hashlib
import json
import struct
import threading
import time
import uuid
from pathlib import Path

import QueryTrace as QT
from ProcessTrace import qpc, frequency

MAX_BUFFER = 8 * 1024**2
MAX_OBJECTS = 32
MAX_EVENTS = 4096
ACTIVE_SECONDS = 120
# First complete instruction at each verified RVA. Symbol/disassembly evidence is
# recorded separately; these bytes prevent silently using offsets on another build.
SITES = {
    0x46158: '48895c2408', 0x461ec: '8bc8', 0x46325: '4183fe03',
    0x4638c: '488b4c2440', 0xb4a0: '48895c2410',
    0xa600: '48895c2418', 0x7450: '8b02', 0xac50: '48895c2408',
    0x8090: '48895c2410', 0xdd80: 'c70105000000',
    0x1966c: '4053', 0x34c00: '4c8b4108',
    0x42a9d: '33c0', 0x3e484: '48895c2408', 0x3e5cf: '488d4de0',
}


class Memory:
    def __init__(self, pid):
        self.k = C.WinDLL('kernel32', use_last_error=True)
        self.k.OpenProcess.argtypes = [C.c_uint32, C.c_bool, C.c_uint32]
        self.k.OpenProcess.restype = C.c_void_p
        self.k.ReadProcessMemory.argtypes = [C.c_void_p, C.c_void_p, C.c_void_p,
                                            C.c_size_t, C.POINTER(C.c_size_t)]
        self.k.GetProcessTimes.argtypes = [C.c_void_p] + [C.c_void_p]*4
        self.k.CloseHandle.argtypes = [C.c_void_p]
        self.k.CheckRemoteDebuggerPresent.argtypes = [C.c_void_p, C.POINTER(C.c_int)]
        self.handle = self.k.OpenProcess(0x1410, False, pid)
        if not self.handle:
            raise C.WinError(C.get_last_error())
        values = [C.c_uint64() for _ in range(4)]
        if not self.k.GetProcessTimes(self.handle, *[C.byref(v) for v in values]):
            self.close()
            raise C.WinError(C.get_last_error())
        self.created = values[0].value

    def debugger(self):
        value = C.c_int()
        if not self.k.CheckRemoteDebuggerPresent(self.handle, C.byref(value)):
            raise C.WinError(C.get_last_error())
        return bool(value.value)

    def read(self, address, size):
        if not 0 < size <= MAX_BUFFER:
            raise ValueError('memory read bound')
        data, count = C.create_string_buffer(size), C.c_size_t()
        if not self.k.ReadProcessMemory(self.handle, address, data, size, C.byref(count)) or count.value != size:
            raise C.WinError(C.get_last_error())
        return data.raw

    def u64(self, address):
        return int.from_bytes(self.read(address, 8), 'little')

    def close(self):
        if self.handle:
            self.k.CloseHandle(self.handle)
            self.handle = None


def pe_bytes(image, rva, size):
    pe = struct.unpack_from('<I', image, 0x3c)[0]
    count = struct.unpack_from('<H', image, pe+6)[0]
    optional = struct.unpack_from('<H', image, pe+20)[0]
    for n in range(count):
        at = pe+24+optional+n*40
        virtual_size, start, raw_size, raw = struct.unpack_from('<IIII', image, at+8)
        if start <= rva and rva+size <= start+min(virtual_size, raw_size):
            return image[raw+rva-start:raw+rva-start+size]
    raise ValueError(f'RVA outside backed section: {rva:x}')


def walk_records(data):
    """Return the actual NextEntryOffset walk; refuse truncated or cyclic headers."""
    offset = 0
    records = []
    while len(records) < 8192:
        if offset+0x100 > len(data):
            raise ValueError(f'incomplete header at {offset:x}')
        step = struct.unpack_from('<I', data, offset)[0]
        pid = struct.unpack_from('<Q', data, offset+0x50)[0]
        created = struct.unpack_from('<Q', data, offset+0x20)[0]
        records.append({'offset': offset, 'next': step, 'pid': pid, 'create_time': created})
        if step == 0:
            return records
        if step < 0x100:
            raise ValueError(f'invalid NextEntryOffset {step:x}')
        offset += step
    raise ValueError('record count bound')


class Provenance:
    """Address generations survive copies, but never destruction or overwrite."""
    def __init__(self):
        self.live = {}
        self.generation = 0

    def retire(self, address):
        return self.live.pop(address, None)

    def create(self, address, query, edge):
        self.generation += 1
        node = {'address': hex(address), 'generation': self.generation,
                'query': query, 'edge': edge}
        self.live[address] = node
        return node


class Session(QT.Session):
    def __init__(self, cdb, pid, folder, write_json, finished, memory=None):
        super().__init__(cdb, pid, folder, write_json, finished)
        self.memory = memory or Memory(pid)
        if self.memory.debugger():
            raise RuntimeError('target already has a debugger')
        self.instance = f'{pid}-{self.memory.created:x}-{uuid.uuid4().hex[:12]}'
        self.record.update(classification='address_chain', instance=self.instance,
                           creation_filetime=self.memory.created, acceptance='incomplete',
                           qpc_frequency=frequency(),query_clock='host receipt QPC; debugger latency unbounded')
        self.image = QT.DLL.read_bytes()
        if hashlib.sha256(self.image).hexdigest() != QT.DLL_SHA256:
            raise RuntimeError('DLL hash mismatch')
        for rva, expected in SITES.items():
            if pe_bytes(self.image, rva, len(bytes.fromhex(expected))).hex() != expected:
                raise RuntimeError(f'file instruction mismatch at {rva:x}')
        self.base = None
        self.sequence = self.query_number = 0
        self.provenance = Provenance()
        self.raw = {}
        self.pending_ops = {}
        self.vectors = {}
        self.active_since = None
        self.conflict = threading.Event()
        self.complete = threading.Event()
        self.event_lock = threading.Lock()
        self.markers['H'] = threading.Event()

    def event(self, kind, **fields):
        with self.event_lock:
            self.sequence += 1
            row = {'event': self.sequence, 'time': QT.timestamp(), 'instance': self.instance,
                   'kind': kind, **fields}
            with (self.folder/'address-events.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(row)+'\n')
            return row

    def prepare_scripts(self):
        path = self.folder.resolve().as_posix()
        if any(c in path for c in ' "\'`;'):
            raise ValueError('cdb evidence path contains unsupported characters')
        scripts = self.folder/'cdb'
        scripts.mkdir(exist_ok=True)
        init = scripts/'init.cdb'
        init.write_text('.echo QT ATTACHED\nsxn av\nsxn eh\nsxi out\n'
                        '.printf "AC READY %p\\n", TaskManagerDataLayer\n', encoding='ascii')
        return init

    def bp(self, number, rva, kind, regs, condition=None):
        values = ', '.join(['@$tid'] + regs)
        command = f'.printf "AC {kind} ' + ' '.join(['%I64x']*(len(regs)+1)) + f'\\n", {values}'
        if condition:
            command = f'.if ({condition}) {{ {command} }} .else {{ gc }}'
        # cdb consumes one escape layer inside a quoted breakpoint command. Keep
        # printf in a script so its newline cannot become a space before symbols.
        scripts = self.folder/'cdb'
        scripts.mkdir(exist_ok=True)
        path = scripts/f'event-{number}.cdb'
        path.write_text(command+'\n',encoding='ascii')
        return f'bp{number} {self.base+rva:x} "$$><{path.resolve().as_posix()}"\n'

    def ready(self, base):
        self.base = base
        checked = []
        for rva, expected in SITES.items():
            actual = self.memory.read(base+rva, len(bytes.fromhex(expected))).hex()
            checked.append({'rva': hex(rva), 'expected': expected, 'actual': actual})
            if actual != expected:
                raise RuntimeError(f'in-memory instruction mismatch at {rva:x}')
        self.write_json(self.folder/'verified-sites.json', checked)
        self.event('verified', base=hex(base), sites=checked)
        d = (self.folder/'cdb').resolve().as_posix()
        scripts = dict(QT.SCRIPTS)
        scripts['exit.cdb'] = '.printf "QT X %x %x %p %p\\n", @$tid, @r14d, @rcx, @rbx; ' \
            '.if (@r14d==4) { .printf "AC SEED %I64x %I64x %I64x %I64x %I64x\\n", @$tid, @rcx, @rbx, qwo(@rsp+0x38), @rdi } .else { gc }\n'
        for name, content in scripts.items():
            (self.folder/'cdb'/name).write_text(content, encoding='ascii')
        commands = ''.join(f'bp{i} {base+rva:x} "$$><{d}/{name}.cdb"\n' for i,rva,name in
                           [(0,0x46158,'enter'),(1,0x461ec,'ntqsi'),(2,0x46325,'exit'),(3,0x4638c,'return')])
        commands += self.bp(20, 0x3e5cf, 'CONFLICT', ['@rbx','@rbp','@rdi','@r14'])
        self.record['status'] = 'tracing'
        self.send(commands+'g\n')
        self.save()

    def query_event(self, kind, values):
        tid = values[0]
        received = qpc()
        if kind == 'R' and tid in self.open:
            self.open[tid]['return_received_qpc'] = received
        super().query_event(kind, values)
        if kind == 'E':
            self.query_number += 1
            self.open[tid]['query'] = f'{self.instance}:Q{self.query_number}'
            self.open[tid]['entry_received_qpc'] = received
        elif kind == 'N':
            self.open[tid]['calls'][-1]['received_qpc'] = received

    def finish_invocation(self, invocation):
        super().finish_invocation(invocation)
        if invocation.get('r14') == 4:
            if 'output_vector' in invocation:
                invocation['published_buffer'] = list(struct.unpack('<QQQ',
                    self.memory.read(invocation['output_vector'],24)))
            self.event('query_return', **invocation)

    def handle_line(self, text):
        try:
            self.process_line(text)
        except Exception as exc:
            self.record['chain_error'] = repr(exc)
            try:
                self.event('incomplete', reason=repr(exc))
                self.save()
            except OSError:
                pass  # A full/unavailable evidence drive must not strand cdb.
            finally:
                # Keep the stdout reader alive so detach acknowledgements can
                # still arrive, including when a QT return memory read fails.
                threading.Thread(target=self.detach, daemon=True).start()

    def process_line(self, text):
        clean = QT.PROMPTS.sub('', text)
        # Keep raw query markers too: the legacy logger intentionally discards them.
        if clean.startswith('QT ') and clean.split()[1] in ('E','N','X','T','R'):
            self.log(clean)
        if not clean.startswith('AC '):
            return super().handle_line(text)
        self.log(clean)
        with self.lock:
            self.at_prompt = False
        _, kind, *parts = clean.split()
        values = [int(x.replace('`',''),16) for x in parts]
        if kind == 'READY':
            self.ready(values[0])
            return
        self.dispatch(kind, values)

    def identity(self, address):
        data = self.memory.read(address, 0x28)
        return {'pid': struct.unpack_from('<I',data,0x10)[0],
                'create_time': struct.unpack_from('<Q',data,0x18)[0], 'header': data.hex()}

    def refresh(self):
        live, raw = list(self.provenance.live), list(self.raw)
        if len(live)>MAX_OBJECTS or self.sequence>MAX_EVENTS:
            raise RuntimeError('address/event budget exhausted')
        condition = lambda reg, addresses: ' | '.join(f'({reg}=={a:x})' for a in addresses) or '0'
        commands = self.bp(10,0xb4a0,'CONVERT',['@rcx','@r9','@rsp','qwo(@rsp)'],condition('@rcx',raw))
        for number,rva,kind in [(11,0xa600,'COPY'),(12,0x7450,'MOVE'),(13,0xac50,'ASSIGN')]:
            cond = f'({condition("@rdx",live)}) | ({condition("@rcx",live)})'
            commands += self.bp(number,rva,kind,['@rdx','@rcx','@rsp','qwo(@rsp)'],cond)
        for number,rva,kind in [(14,0x8090,'DESTROY'),(15,0xdd80,'DEFAULT')]:
            commands += self.bp(number,rva,kind,['@rcx','qwo(@rsp)'],condition('@rcx',live))
        begins = [x['begin'] for x in self.raw.values()]
        commands += self.bp(16,0x1966c,'FREE_BUFFER',['@rcx','qwo(@rcx)','qwo(@rsp)'],condition('qwo(@rcx)',begins))
        commands += self.bp(17,0x34c00,'SWAP',['@rcx','@rdx','@rsp','qwo(@rsp)'])
        commands += self.bp(18,0x42a9d,'VECTOR',['@rsi'])
        commands += self.bp(19,0x3e484,'AGGREGATE',['@rcx','@rdx','@rsp'])
        return commands

    def arm_return(self, operation):
        slot = next((n for n in range(40,72) if n not in self.pending_ops), None)
        if slot is None:
            raise RuntimeError('pending operation bound')
        ret = operation['return']
        expected = pe_bytes(self.image, ret-self.base, 1)
        actual = self.memory.read(ret, 1)
        # A return site already used by another tracked call has an int3 byte.
        if actual != expected and not (actual == b'\xcc' and any(p['return']==ret for p in self.pending_ops.values())):
            raise RuntimeError(f'return instruction mismatch: {ret:x}')
        operation['slot'] = slot
        self.pending_ops[slot] = operation
        self.send(self.bp(slot,ret-self.base,'DONE',['@rsp','@rax',f'{slot:x}'],
                          f'(@$tid=={operation["tid"]:x}) & (@rsp=={operation["sp"]+8:x})'))

    def map_snapshot(self, address):
        head = self.memory.u64(address+8)
        count = self.memory.u64(address+16)
        if count > 8192:
            raise ValueError('map size bound')
        node = self.memory.u64(head)
        found = []
        visited = set()
        while node != head:
            if node in visited or len(visited) >= 8192:
                raise ValueError('map walk bound')
            visited.add(node)
            if node+0x20 in self.provenance.live:
                found.append(self.provenance.live[node+0x20])
            node = self.memory.u64(node)
        return {'address':hex(address),'head':hex(head),'size':count,'tracked':found}

    def vector_snapshot(self, address):
        begin,end,capacity = struct.unpack('<QQQ',self.memory.read(address,24))
        if not begin <= end <= capacity or (end-begin)%0x220 or end-begin > MAX_BUFFER:
            raise ValueError('vector bounds')
        found = [self.provenance.live[a] for a in range(begin,end,0x220) if a in self.provenance.live]
        return {'address':hex(address),'begin':hex(begin),'end':hex(end),'capacity':hex(capacity),'tracked':found}

    def dispatch(self, kind, values):
        tid,*v = values
        commands = ''
        if kind == 'SEED':
            begin,end,capacity,out = v
            invocation = self.open.get(tid)
            if not invocation or len(invocation['calls'])!=3 or any(c['status']!=0xc0000004 for c in invocation['calls']):
                raise ValueError('exhausted query without three captured mismatches')
            data = self.memory.read(begin,end-begin)
            records = walk_records(data)
            tail = records[-1]
            invocation['tail'] = dict(tail, records=len(records))
            invocation['output_vector'] = out
            if tail['pid']==0 and tail['create_time']==0:
                if self.active_since is not None:
                    raise RuntimeError('second anomalous query; retain first attempt as incomplete')
                self.active_since = time.monotonic()
                query = invocation['query']
                file = f'query-{self.query_number}-buffer.bin'
                (self.folder/file).write_bytes(data)
                address = begin+tail['offset']
                seed = self.event('raw_record',query=query,tid=tid,begin=begin,end=end,capacity=capacity,
                                  output_vector=out,address=address,tail=tail,records=len(records),
                                  buffer_file=file,sha256=hashlib.sha256(data).hexdigest(),
                                  header=data[tail['offset']:tail['offset']+0x100].hex(),calls=invocation['calls'])
                self.raw[address] = {'begin':begin,'query':query,'edge':seed['event']}
                commands = self.refresh()
        elif kind in ('CONVERT','COPY','MOVE','ASSIGN'):
            source,dest,sp,ret = v
            origin = self.raw.get(source) if kind=='CONVERT' else self.provenance.live.get(source)
            old = self.provenance.retire(dest)
            operation = dict(tid=tid,source=source,destination=dest,sp=sp,return_address=hex(ret),
                             operation=kind,origin=origin,retired=old)
            row = self.event('operation_begin',**operation)
            if origin:
                operation.update(event=row['event'], **{'return':ret})
                self.arm_return(operation)
            commands = self.refresh()
        elif kind == 'DONE':
            sp,result,slot = v
            op = self.pending_ops.pop(slot)
            if tid != op['tid'] or sp != op['sp']+8:
                raise ValueError('return context mismatch')
            commands = f'bc {slot}\n'
            if op['operation']=='SWAP':
                after = [self.map_snapshot(x) for x in op['maps']]
                self.event('cache_swap_end',operation_event=op['event'],before=op['before'],after=after)
            else:
                if (op['operation']=='CONVERT' and result & 0xffffffff) or (op['operation']!='CONVERT' and result!=op['destination']):
                    raise ValueError('native conversion/copy did not complete successfully')
                identity = self.identity(op['destination'])
                if identity['pid'] or identity['create_time']:
                    raise ValueError('tracked destination identity changed')
                row = self.event('operation_end',operation_event=op['event'],operation=op['operation'],
                                 query=op['origin']['query'],source=op['source'],destination=op['destination'],
                                 parent=op['origin']['edge'],tid=tid,result=result,identity=identity)
                node = self.provenance.create(op['destination'],op['origin']['query'],row['event'])
                self.event('object_live',**node)
            commands += self.refresh()
        elif kind in ('DESTROY','DEFAULT'):
            address,ret = v
            self.event('lifetime_end',reason=kind,address=address,tid=tid,caller=hex(ret),
                       retired=self.provenance.retire(address))
            commands = self.refresh()
        elif kind == 'FREE_BUFFER':
            vector,begin,ret = v
            ended = [a for a,item in self.raw.items() if item['begin']==begin]
            self.event('buffer_lifetime_end',vector=vector,begin=begin,records=ended,caller=hex(ret))
            for a in ended:
                del self.raw[a]
            commands = self.refresh()
        elif kind == 'SWAP':
            left,right,sp,ret = v
            before = [self.map_snapshot(x) for x in (left,right)]
            if any(s['tracked'] for s in before):
                row = self.event('cache_swap_begin',tid=tid,caller=hex(ret),before=before)
                self.arm_return({'operation':'SWAP','tid':tid,'sp':sp,'return':ret,
                                 'maps':[left,right],'before':before,'event':row['event']})
        elif kind in ('VECTOR','AGGREGATE'):
            vector = v[0] if kind=='VECTOR' else v[1]
            snapshot = self.vector_snapshot(vector)
            self.event(kind.lower(),tid=tid,vector=snapshot,context=v)
            if kind=='AGGREGATE':
                self.vectors[tid] = snapshot
        elif kind == 'CONFLICT':
            current,rbp,end,manager = v
            idle = self.memory.u64(rbp-0x50)
            current_id,idle_id = self.identity(current),self.identity(idle)
            origin = self.provenance.live.get(current)
            vector = self.vectors.get(tid)
            row = self.event('conflict',tid=tid,current=current,current_identity=current_id,
                             existing=idle,existing_identity=idle_id,manager=manager,vector_end=end,
                             vector=vector,origin=origin,query=origin['query'] if origin else None,
                             rva='0x3e5cf',expected_hresult='0x8007139f')
            self.record.update(conflict_event=row['event'],conflict_query=row['query'],
                               acceptance='incomplete; conflict captured, independent audit and freeze pending')
            self.conflict.set()
            # Exact native mismatch branch, not an unrelated same-code debug string.
            self.send('bc *\nsxi out\nkn 40\n~*kn 16\n'
                      f'.dump /ma {self.folder.resolve().as_posix()}/Taskmgr-origin.dmp\n.echo QT D\n')
            self.save()
            return
        else:
            raise ValueError(f'unknown event {kind}')
        self.send(commands+'g\n')

    def watchdog(self):
        while self.process.poll() is None and not self.stopping:
            time.sleep(.5)
            if self.active_since and time.monotonic()-self.active_since > ACTIVE_SECONDS and not self.conflict.is_set():
                self.record['chain_error'] = 'active tracing time budget exhausted'
                self.event('incomplete',reason=self.record['chain_error'])
                self.detach()
                return
            with self.lock:
                stuck = self.at_prompt and time.monotonic()-self.prompt_since >= QT.STUCK_SECONDS
            if stuck:
                # Pipe prompts survive `g`. A non-breaking probe distinguishes a
                # stopped debugger from a healthy target with no new events.
                self.markers['H'].clear()
                self.send('.echo QT H\n')
                if self.markers['H'].wait(.5):
                    self.record['unexpected_break'] = QT.timestamp()
                    self.detach()
                    return
                with self.lock:
                    self.at_prompt = False
