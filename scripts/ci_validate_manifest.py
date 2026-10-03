"""Validate the actual OCI index, ignoring only unknown/unknown attestations."""
from pathlib import Path
import json
import sys


def validate(document, expected):
    if document.get('schemaVersion') != 2 or not isinstance(document.get('manifests'), list):
        raise ValueError('Expected an OCI/Docker multi-platform index')
    actual=[]
    attestations=0
    for descriptor in document['manifests']:
        platform=descriptor.get('platform', {})
        os_name, arch=platform.get('os'), platform.get('architecture')
        if (os_name,arch)==('unknown','unknown'):
            attestations+=1
            continue
        variant=platform.get('variant')
        suffix='/'+variant if arch=='arm' and variant else ''
        actual.append(f'{os_name}/{arch}{suffix}')
    if len(actual)!=len(expected) or set(actual)!=set(expected):
        raise ValueError(f'Unexpected platforms: {actual}; expected: {expected}')
    return attestations


def self_test():
    expected=['linux/amd64','linux/arm64','linux/arm/v7']
    good={'schemaVersion':2,'manifests':[{'platform':{'os':'linux','architecture':'amd64'}},
           {'platform':{'os':'linux','architecture':'arm64','variant':'v8'}},
           {'platform':{'os':'linux','architecture':'arm','variant':'v7'}},
           {'platform':{'os':'unknown','architecture':'unknown'}}]}
    assert validate(good,expected)==1
    checks=1
    for bad in [{'schemaVersion':2,'manifests':good['manifests'][:2]},
                {'schemaVersion':2,'manifests':good['manifests'][:3]+[good['manifests'][0]]},
                {'schemaVersion':2,'manifests':[{'platform':{'os':'linux','architecture':'arm'}}]},
                {'schemaVersion':2,'manifests':[{'platform':{'os':'windows','architecture':'amd64'}}]},
                {'schemaVersion':2,'config':{}}]:
        try:
            validate(bad,expected)
        except ValueError:
            checks+=1
        else:
            raise AssertionError('Invalid index was accepted')
    print(f'MANIFEST_VALIDATOR: checks={checks} passed={checks} failed=0')


if __name__=='__main__':
    if sys.argv[1:]==['--self-test']:
        self_test()
    else:
        if len(sys.argv)<3:raise SystemExit('Usage: ci_validate_manifest.py FILE EXPECTED_PLATFORM...')
        expected=sys.argv[2:]
        count=validate(json.loads(Path(sys.argv[1]).read_text(encoding='utf-8')),expected)
        print('MANIFEST: platforms='+','.join(expected)+f' expected=PASS attestations={count}')
