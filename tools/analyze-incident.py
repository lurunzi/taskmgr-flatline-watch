"""Analyse already decoded lightweight incident evidence; never starts tracing."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from IncidentAnalysis import analyze

if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder',type=Path)
    parser.add_argument('--output',type=Path)
    args = parser.parse_args()
    result = analyze(args.folder,args.output)
    print(json.dumps({'quality':result['quality'],'attribution':result['attribution']},indent=2))
    sys.exit(0 if result['quality']['ranking_usable'] else 2)
