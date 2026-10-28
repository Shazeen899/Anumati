"""Exercise the running HTTP application and real Celery worker with synthetic data."""
import http.cookiejar
import json
import os
import sys
import time
import urllib.request
import urllib.error
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
sys.path.insert(0,str(ROOT))
from anumati import settings
BASE=f"http://127.0.0.1:{os.getenv('WEB_PORT','8010')}/api"

class Session:
    def __init__(self,username):
        self.cookies=http.cookiejar.CookieJar()
        self.opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.cookies))
        self.call('/session')
        self.call('/login',{'username':username,'password':os.environ['DEMO_PASSWORD']})
    def call(self,path,payload=None,key=None,raw=None,content_type=None):
        headers={}
        if payload is not None or raw is not None:
            headers['Content-Type']=content_type or 'application/json'
            headers['X-CSRFToken']=next(c.value for c in self.cookies if c.name=='csrftoken')
        if key:headers['Idempotency-Key']=key
        req=urllib.request.Request(BASE+path,data=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None,headers=headers)
        with self.opener.open(req,timeout=20) as response:
            content=response.read()
            return json.loads(content) if 'json' in response.headers.get('Content-Type','') else content

def main():
    assert settings.DEMO_MODE,'Smoke script is only for synthetic demonstration data.'
    users={name:Session(name) for name in ['institution','reviewer','executive','council','bureau','institution2']}
    token=uuid.uuid4().hex[:10]
    c=users['institution'].call('/applications',{'category':'name_change','external_id':'HTTP-SMOKE-'+token,'academic_year':'2026-2027'})
    pk=c['id']
    def act(role,action,**fields):
        current=users[role].call('/applications/'+pk)
        return users[role].call('/applications/'+pk+'/commands',{'action':action,'expected_version':current['version'],'reason':'Synthetic HTTP smoke verification '+action,**fields},key=str(uuid.uuid4()))
    act('institution','submit')
    act('reviewer','return',requirement='SYNTHETIC-REQUIREMENT-01')
    act('institution','resubmit',reason='Corrected the original synthetic finding for the end-to-end test.')
    d=users['reviewer'].call('/applications/'+pk)['deficiencies'][0]
    act('reviewer','resolve_deficiency',deficiency_id=d['id'])
    act('reviewer','complete_stage')
    act('executive','approve')
    act('council','ratify')
    act('bureau','add_obligation',title='HTTP smoke undertaking',kind='undertaking',source='Synthetic verification fixture',due_at=(datetime.now(timezone.utc)+timedelta(days=2)).isoformat())
    current=users['institution'].call('/applications/'+pk)
    obligation=current['obligations'][0]['id']
    boundary='anumati-'+uuid.uuid4().hex
    parts=[]
    for key,value in {'expected_version':current['version'],'obligation_id':obligation}.items():
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="synthetic-smoke-evidence.txt"\r\nContent-Type: text/plain\r\n\r\nSynthetic evidence for the live end-to-end verification.\r\n--{boundary}--\r\n'.encode())
    receipt=users['institution'].call('/applications/'+pk+'/evidence',raw=b''.join(parts),content_type='multipart/form-data; boundary='+boundary)
    evidence_id=receipt['evidence']['id']
    print(f'Created {c["reference"]}; approval, deficiency cycles and evidence receipt succeeded.',flush=True)
    print('Waiting for the actual Celery worker to validate synthetic evidence…',flush=True)
    for _ in range(30):
        current=users['reviewer'].call('/applications/'+pk)
        evidence=next(e for e in current['evidence'] if e['id']==evidence_id)
        if evidence['safety']=='demo_validated':break
        time.sleep(2)
    else:raise AssertionError('Celery did not validate evidence within 60 seconds.')
    users['reviewer'].call('/evidence/'+evidence_id+'/review',{'outcome':'accepted','expected_version':current['version'],'reason':'Accepted exact synthetic version during live HTTP verification.'})
    final=users['institution'].call('/applications/'+pk)
    assert final['stage']=='APPROVED'
    assert final['obligations'][0]['status']=='fulfilled'
    assert final['evidence'][0]['received_at']==receipt['evidence']['received_at']
    assert users['institution'].call('/evidence/'+evidence_id+'/download').startswith(b'Synthetic evidence')
    try:users['institution2'].call('/applications/'+pk)
    except urllib.error.HTTPError as e:assert e.code==404
    else:raise AssertionError('Cross-institution access was not denied.')
    assert c['reference'].encode() in users['institution'].call('/reports')
    result={'result':'PASS','case_reference':c['reference'],'case_id':pk,'stage':final['stage'],'obligation':final['obligations'][0]['status'],'evidence_safety':final['evidence'][0]['safety'],'receipt_preserved':True,'cross_institution_denied':True,'audit_events':len(final['audit']),'verified_at':datetime.now(timezone.utc).isoformat()}
    (ROOT/'.runtime/http-smoke.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))

if __name__=='__main__':main()
