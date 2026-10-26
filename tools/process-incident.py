"""Decode a stopped lightweight incident ETL, then generate candidate reports."""
import argparse
import hashlib
import json
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from ProcessTrace import Session
from IncidentAnalysis import analyze

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    parser.add_argument('--output', type=Path, help='new derived evidence directory; preserves source files')
    parser.add_argument('--decode-timeout', type=int, default=90, help='seconds before decoder bound is exceeded')
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
                 'process-trace/end-baseline.json','process-trace/process.etl',
                 'process-trace/providers.guid']
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
    Session.open_stopped(folder).decode(compact=True, timeout=args.decode_timeout)
    analyze_t0 = time.monotonic()
    result = analyze(folder)
    analyze_dur = round(time.monotonic() - analyze_t0, 3)

    session_file = folder/'process-trace/session.json'
    decode_dur = compress_dur = 0
    if session_file.exists():
        try:
            s_data = json.loads(session_file.read_text(encoding='utf-8'))
            decode_dur = s_data.get('decode_duration_seconds', 0)
            compress_dur = s_data.get('compress_duration_seconds', 0)
        except Exception:
            pass

    stage_summary = {
        'stage': 'complete' if result['quality']['ranking_usable'] else 'analyze',
        'reason': 'ranking_usable' if result['quality']['ranking_usable'] else 'ranking_unusable',
        'stage_durations': {
            'decode': decode_dur,
            'compress': compress_dur,
            'analyze': analyze_dur
        }
    }
    (folder/'process-trace/stage-summary.json').write_text(
        json.dumps(stage_summary, indent=2)+'\n', encoding='utf-8')
    print(json.dumps({'quality': result['quality'], 'attribution': result['attribution'], 'stage_summary': stage_summary}, indent=2))
    sys.exit(0 if result['quality']['ranking_usable'] else 2)
