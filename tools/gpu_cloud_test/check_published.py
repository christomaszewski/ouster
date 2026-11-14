#!/usr/bin/env python3
"""Match offline SDK/ROS oracle to actual published frame bytes, excluding padding."""
import argparse
import hashlib
import json
from pathlib import Path

p=argparse.ArgumentParser()
p.add_argument('capture',type=Path)
p.add_argument('fixtures',type=Path)
p.add_argument('output',type=Path)
a=p.parse_args()
capture=json.loads((a.capture/'capture.json').read_text())
published={c['stamp_sec']*10**9+c['stamp_nanosec']:c for c in capture['clouds']}
if len(published)!=len(capture['clouds']):
    raise SystemExit('Duplicate published timestamps; anchor is ambiguous')
expected_fields=[
    {'name':name,'offset':offset,'datatype':datatype,'count':1}
    for name,offset,datatype in [('x',0,7),('y',4,7),('z',8,7),
        ('intensity',16,7),('t',20,6),('reflectivity',24,4),
        ('ring',26,4),('ambient',28,4),('range',32,6)]
]
# Every registered field plus PCL homogeneous coordinate; unspecified padding
# at bytes30..31 and36..47 is deliberately excluded for this external anchor.
lanes=list(range(30))+list(range(32,36))
def registered(data):
    return b''.join(data[offset::48] for offset in lanes)
results=[]
for d in sorted(a.fixtures.glob('frame*')):
    m=dict(line.split('=',1) for line in (d/'manifest.txt').read_text().splitlines() if '=' in line)
    stamp=int(m['scan_ts']);c=published.get(stamp)
    result={'frame':d.name,'scan_ts':stamp,'published_match_found':c is not None}
    if c:
        expected=(d/'reference.cloud48').read_bytes();actual=(a.capture/c['file']).read_bytes()
        width,height=int(m['width']),int(m['height'])
        schema_ok=(c.get('width')==width and c.get('height')==height and
                   c.get('point_step')==48 and c.get('row_step')==width*48 and
                   c.get('frame_id')==m.get('frame','os_sensor') and
                   c.get('fields')==expected_fields)
        result.update(published_file=c['file'],schema_matches_fixed_contract=schema_ok,
                      length_equal=len(expected)==len(actual)==width*height*48,
                      expected_registered_sha256=hashlib.sha256(registered(expected)).hexdigest(),
                      published_registered_sha256=hashlib.sha256(registered(actual)).hexdigest(),
                      whole_buffer_equal=expected==actual)
        result['registered_and_homogeneous_equal']=result['length_equal'] and result['expected_registered_sha256']==result['published_registered_sha256']
        if not result['registered_and_homogeneous_equal']:
            for i in range(min(len(actual),len(expected))//48):
                bad=[j for j in lanes if actual[48*i+j]!=expected[48*i+j]]
                if bad:result['first_mismatch']={'point':i,'byte_offsets':bad};break
    results.append(result)
matched=[r for r in results if r['published_match_found']]
out={'capture':str(a.capture),'fixtures':str(a.fixtures),'excluded_padding_offsets':[30,31,*range(36,48)],
     'frames':results,'matched_frames':len(matched),
     'all_fixtures_matched':bool(results) and len(matched)==len(results),
     'all_schema_checks_pass':bool(matched) and all(r['schema_matches_fixed_contract'] for r in matched),
     'all_matched_equal':bool(matched) and all(r['registered_and_homogeneous_equal'] for r in matched)}
out['passed']=out['all_fixtures_matched'] and out['all_schema_checks_pass'] and out['all_matched_equal']
a.output.write_text(json.dumps(out,indent=2)+'\n')
print(json.dumps({k:out[k] for k in ['matched_frames','all_fixtures_matched','all_schema_checks_pass','all_matched_equal','passed']}))
if not out['passed']:raise SystemExit(1)
