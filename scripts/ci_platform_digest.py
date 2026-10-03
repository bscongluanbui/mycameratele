"""Select a single runnable platform manifest, not the shared index digest.

Classic Docker image stores cannot replace one index digest with a different
architecture in a sequential smoke loop. Child manifest digests avoid this.
"""
import json
from pathlib import Path
import re
import sys


def select(document, expected):
    matches = []
    for item in document.get('manifests', []):
        p = item.get('platform', {})
        name = str(p.get('os'))+'/'+str(p.get('architecture'))
        if p.get('architecture') == 'arm' and p.get('variant'):
            name += '/'+p['variant']
        if name == expected:
            matches.append(item.get('digest', ''))
    if len(matches) != 1 or not re.fullmatch(r'sha256:[a-f0-9]{64}', matches[0]):
        raise ValueError('Expected exactly one valid runnable digest for '+expected)
    return matches[0]


if __name__ == '__main__':
    if len(sys.argv) != 3:
        raise SystemExit('Usage: ci_platform_digest.py INDEX.json linux/ARCH[/VARIANT]')
    print(select(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')), sys.argv[2]))
