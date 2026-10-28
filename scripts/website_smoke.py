"""Exercise website HTTP intake, document staging, responses and isolation.

Only synthetic demo data is used. Google Drive stays real: if disconnected,
the expected state is pending, never a fabricated successful upload.
"""
import io
import json
import os
import sys
import uuid
import zipfile
from datetime import datetime, timezone
from urllib.error import HTTPError
from urllib.request import Request

from smoke import ROOT, Session, settings

BASE = f"http://127.0.0.1:{os.getenv('WEB_PORT', '8010')}/website/api"


def call(session, path, payload=None, raw=None, content_type=None):
    headers = {}
    if payload is not None or raw is not None:
        headers = {'Content-Type':content_type or 'application/json',
                   'X-CSRFToken':next(c.value for c in session.cookies if c.name == 'csrftoken')}
    req = Request(BASE+path, data=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with session.opener.open(req, timeout=20) as response:
        return json.load(response)


def multipart(fields, file_field, content):
    boundary = 'website-' + uuid.uuid4().hex
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode() for key,value in fields.items()]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{file_field}"; filename="synthetic-institution-letter.docx"\r\nContent-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document\r\n\r\n'.encode()+content+f'\r\n--{boundary}--\r\n'.encode())
    return b''.join(parts), 'multipart/form-data; boundary='+boundary


def main():
    assert settings.DEMO_MODE, 'Run only with synthetic local demo data.'
    institution, bureau, other = Session('institution'), Session('bureau'), Session('institution2')
    content = io.BytesIO()
    with zipfile.ZipFile(content, 'w', zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('[Content_Types].xml', '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr('_rels/.rels', '<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        archive.writestr('word/document.xml', '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>SYNTHETIC DEMONSTRATION ONLY. Institution document requirements clarification. No real complaint or personal data.</w:t></w:r></w:p></w:body></w:document>')
    if len(sys.argv)>1:
        item=call(institution,'/submissions/'+sys.argv[1])
        raw,kind=multipart({},'document',content.getvalue())
        call(institution,f'/submissions/{item["id"]}/documents',raw=raw,content_type=kind)
    else:
        meta=call(institution,'/metadata')
        fields={'kind':'complaint','category':'document_issue','subject':'Demo: supporting document clarification',
                'description':'Synthetic demonstration: please clarify the supporting documentation format for the linked application.',
                'submission_key':str(uuid.uuid4()),'application_id':meta['applications'][0]['id']}
        raw,kind=multipart(fields,'documents',content.getvalue())
        item=call(institution,'/submissions',raw=raw,content_type=kind)
    path='/submissions/'+item['id']
    before=call(bureau,path)
    assert before['documents'][-1]['name']=='synthetic-institution-letter.docx'
    call(bureau,path,{'expected_version':before['version'],'status':'awaiting_institution',
                     'message':'Demo review: please confirm whether your institution is requesting a format checklist. Your supporting document receipt is on record.'})
    item=call(institution,path)
    call(institution,path,{'expected_version':item['version'],'status':item['status'],
                          'message':'Demo response: yes, please provide the required format checklist for this application.'})
    final=call(bureau,path)
    assert final['status']=='in_review'
    assert final['documents'][-1]['received_at']==before['documents'][-1]['received_at']
    for resource in [path, '/documents/'+final['documents'][-1]['id']+'/download']:
        try: call(other,resource)
        except HTTPError as error: assert error.code==404
        else: raise AssertionError('Other institution was able to access the record.')
    assert call(other,'/submissions?search='+item['reference'])['total']==0
    result={'result':'PASS','reference':final['reference'],'id':final['id'],
            'linked_application':final['application_reference'],'status':final['status'],
            'document_state':final['documents'][-1]['state'],'cross_institution_denied':True,
            'drive_live_verified':False,'verified_at':datetime.now(timezone.utc).isoformat()}
    (ROOT/'.runtime/website-smoke.json').write_text(json.dumps(result,indent=2))
    print(json.dumps(result,indent=2))


if __name__ == '__main__':
    main()
