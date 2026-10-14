"""Decode a stopped lightweight incident ETL, then generate candidate reports."""
import argparse
import json
import hashlib
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from ProcessTrace import Session
from IncidentAnalysis import analyze

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path, help='new derived evidence directory; preserves source files')
    args = parser.parse_args()
    folder = args.folder.resolve()
    if args.output:
        destination = args.output.resolve()
        if destination==folder or folder in destination.parents:
            parser.error('output must be a separate new directory')
        destination.mkdir(parents=True,exist_ok=False)
        (destination/'process-trace').mkdir()
        names = ['observation.json','trace-capture.json','event.json',
                 'process-trace/session.json','process-trace/baseline.json',
                 'process-trace/end-baseline.json','process-trace/process.etl']
        copied = []
        for name in names:
            source = folder/name
            if source.exists():
                shutil.copy2(source,destination/name)
                def digest(path):
                    with path.open('rb') as stream:
                        return hashlib.file_digest(stream,'sha256').hexdigest()
                original,copy = digest(source),digest(destination/name)
                if original!=copy:
                    raise RuntimeError('evidence copy hash mismatch')
                copied.append({'file':name,'sha256':copy})
        (destination/'provenance.json').write_text(json.dumps({'source':str(folder),'files':copied},indent=2)+'\n',encoding='utf-8')
        folder = destination
    elif (folder/'process-trace/decode.log').exists():
        parser.error('existing decode attempt retained; use --output with a new directory')
    Session.open_stopped(folder).decode(compact=True)
    result = analyze(folder)
    print(json.dumps({'quality': result['quality'], 'attribution': result['attribution']}, indent=2))
    sys.exit(0 if result['quality']['ranking_usable'] else 2)
