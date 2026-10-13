"""Decode a stopped lightweight incident ETL, then generate candidate reports."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'src'))
from ProcessTrace import Session
from IncidentAnalysis import analyze

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('folder', type=Path)
    args = parser.parse_args()
    Session.open_stopped(args.folder).decode()
    result = analyze(args.folder)
    print(json.dumps({'quality': result['quality'], 'attribution': result['attribution']}, indent=2))
    sys.exit(0 if result['quality']['ranking_usable'] else 2)
