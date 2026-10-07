import json
import importlib.util
import hashlib
import struct
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
import AddressChain as A
import QueryTrace as Q

spec = importlib.util.spec_from_file_location('audit_chain',Path(__file__).resolve().parents[1]/'tools/audit-address-chain.py')
audit_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit_module)


class Memory:
    created = 12345
    def __init__(self):
        self.blocks = {}
    def debugger(self):
        return False
    def read(self,address,size):
        for start,data in self.blocks.items():
            if start <= address and address+size <= start+len(data):
                return data[address-start:address-start+size]
        raise ValueError(f'unmapped {address:x}')
    def u64(self,address):
        return int.from_bytes(self.read(address,8),'little')


class AddressChainTests(unittest.TestCase):
    def test_walk_rejects_truncated_headers_and_bad_offsets(self):
        for data in (bytes(255),struct.pack('<I',8)+bytes(508),struct.pack('<I',512)+bytes(508)):
            with self.assertRaises(ValueError):
                A.walk_records(data)
        data = bytearray(512)
        struct.pack_into('<I',data,0,256)
        struct.pack_into('<Q',data,0x20,123)
        self.assertEqual(A.walk_records(data)[-1]['offset'],256)

    def test_retired_address_is_not_reused_as_provenance(self):
        p = A.Provenance()
        old = p.create(0x100,'Q1',1)
        self.assertEqual(p.retire(0x100),old)
        self.assertNotIn(0x100,p.live)
        new = p.create(0x100,'Q2',8)
        self.assertNotEqual(old['generation'],new['generation'])
        self.assertEqual(new['query'],'Q2')

    def test_actual_address_copy_completion_and_destructor(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            mem = Memory()
            session = A.Session('unused',4321,folder,lambda *a:None,lambda s:None,memory=mem)
            session.base = 0x10000000
            session.send = Mock(return_value=True)
            session.refresh = Mock(return_value='')
            data = bytearray(512)
            struct.pack_into('<I',data,0,256)
            struct.pack_into('<Q',data,0x20,123)
            mem.blocks[0x1000] = data
            mem.blocks[0x2000] = bytes(0x220)
            mem.blocks[0x3000] = bytes(0x220)
            mem.blocks[0x9000] = struct.pack('<QQQ',0x1000,0x1200,0x1200)
            ret = session.base+0x45678
            mem.blocks[ret] = A.pe_bytes(session.image,0x45678,1)
            session.query_event('E',[1])
            for i in range(3):
                session.query_event('N',[1,0xc0000004,i*256,(i+1)*256])
            session.query_event('X',[1,4,0x1000,0x1200])
            session.dispatch('SEED',[1,0x1000,0x1200,0x1200,0x9000])
            session.query_event('R',[1,0])
            session.dispatch('CONVERT',[1,0x1100,0x2000,0x8000,ret])
            self.assertNotIn(0x2000,session.provenance.live)  # entry alone proves no completed write
            session.dispatch('DONE',[1,0x8008,0,40])
            first = session.provenance.live[0x2000]
            session.dispatch('COPY',[1,0x2000,0x3000,0x8000,ret])
            session.dispatch('DONE',[1,0x8008,0x3000,40])
            self.assertEqual(session.provenance.live[0x3000]['query'],first['query'])
            session.dispatch('DESTROY',[1,0x2000,ret])
            session.dispatch('ASSIGN',[1,0x2000,0x3000,0x8000,ret])
            self.assertFalse(session.provenance.live)  # same zero fields cannot revive destroyed source
            events = [json.loads(x) for x in (folder/'address-events.jsonl').read_text().splitlines()]
            copies = [x for x in events if x['kind']=='operation_end']
            self.assertEqual(len(copies),2)
            self.assertEqual(copies[1]['parent'],copies[0]['event'])

    def test_incomplete_queries_cannot_seed_provenance(self):
        with tempfile.TemporaryDirectory() as directory:
            s = A.Session('unused',1,Path(directory),lambda *a:None,lambda s:None,memory=Memory())
            s.query_event('E',[1])
            s.query_event('N',[1,0xc0000004,0,256])
            with self.assertRaises(ValueError):
                s.dispatch('SEED',[1,0x1000,0x1200,0x1200,0x9000])
            self.assertFalse(s.raw)

    def test_all_pinned_instruction_bytes_match_allowed_dll(self):
        image = Q.DLL.read_bytes()
        for rva,expected in A.SITES.items():
            self.assertEqual(A.pe_bytes(image,rva,len(bytes.fromhex(expected))).hex(),expected,hex(rva))

    def test_printf_newline_is_kept_out_of_nested_breakpoint_quotes(self):
        with tempfile.TemporaryDirectory() as directory:
            s = A.Session('unused',1,Path(directory),lambda *a:None,lambda s:None,memory=Memory())
            s.base=0x10000000
            command=s.bp(10,0xb4a0,'CONVERT',['@rcx'])
            self.assertIn('$$><',command)
            self.assertNotIn('.printf',command)
            self.assertIn('\\n', (Path(directory)/'cdb/event-10.cdb').read_text())

    def test_return_read_error_detaches_without_losing_stdout_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            s = A.Session('unused',1,Path(directory),lambda *a:None,lambda s:None,memory=Memory())
            s.open[1]={'started':'test','tid':1,'calls':[],'r14':4,'output_vector':0x9000}
            with patch.object(A.threading,'Thread') as thread:
                s.handle_line('QT R 1 0')
                self.assertIn('unmapped',s.record['chain_error'])
                self.assertEqual(thread.call_args.kwargs['target'],s.detach)
            s.handle_line('QT G')
            self.assertTrue(s.markers['G'].is_set())

    def test_independent_audit_rejects_field_match_without_source_address(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            data=bytearray(512)
            struct.pack_into('<I',data,0,256)
            struct.pack_into('<Q',data,0x20,123)
            (folder/'raw.bin').write_bytes(data)
            events=[
                {'kind':'raw_record','begin':0x1000,'end':0x1200,'capacity':0x1200,'address':0x1100,'query':'Q',
                 'buffer_file':'raw.bin','sha256':hashlib.sha256(data).hexdigest()},
                {'kind':'query_return','query':'Q','hr':0,'r14':4,'calls':[{'status':0xc0000004}]*3,
                 'published_buffer':[0x1000,0x1200,0x1200]},
                {'kind':'operation_begin','source':0x1100,'destination':0x2000,'operation':'CONVERT','tid':1},
                {'kind':'operation_end','operation_event':3,'source':0x1100,'destination':0x2000,
                 'operation':'CONVERT','tid':1,'query':'Q','parent':1,'result':0},
                {'kind':'conflict','current':0x2000,'existing':0x3000,'vector_end':0x2220,
                 'vector':{'address':'0x5000','begin':'0x2000','end':'0x2220','capacity':'0x2220'},
                 'current_identity':{'pid':0,'create_time':0},'existing_identity':{'pid':0,'create_time':123}},
            ]
            for i,e in enumerate(events,1):e.update(event=i,instance='one-instance')
            def save():
                (folder/'address-events.jsonl').write_text(''.join(json.dumps(e)+'\n' for e in events))
            dump=Mock()
            dump.identity.side_effect=lambda address:(0,0 if address==0x2000 else 123)
            dump.read.return_value=struct.pack('<QQQ',0x2000,0x2220,0x2220)
            with patch.object(audit_module,'DumpMemory',return_value=dump):
                save()
                self.assertTrue(audit_module.audit(folder)['address_chain_closed'])
                events[2]['source']=0x1110  # Same zero-filled memory, different raw address.
                events[3]['source']=0x1110
                save()
                self.assertFalse(audit_module.audit(folder)['address_chain_closed'])


if __name__=='__main__':
    unittest.main()
