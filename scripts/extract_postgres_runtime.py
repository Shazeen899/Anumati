"""Extract only PostgreSQL server tools from an official EDB ZIP download.

Can reuse a partial local download and range-fetch missing server sections.
No pgAdmin, installer, system service or PATH change is needed.
"""
import io
import os
import urllib.request
import zipfile
from pathlib import Path

ROOT=Path(__file__).resolve().parent.parent
SOURCE='https://sbp.enterprisedb.com/getfile.jsp?fileid=1260616'
LOCAL=ROOT/'.runtime/postgresql.zip'
TARGET=ROOT/'.runtime/postgres'

class RemoteArchive(io.RawIOBase):
    def __init__(self):
        with urllib.request.urlopen(urllib.request.Request(SOURCE,method='HEAD'),timeout=60) as r:
            self.url=r.url
            self.length=int(r.headers['Content-Length'])
        self.pos=0
        self.blocks=[]
        self.local_size=LOCAL.stat().st_size if LOCAL.exists() else 0
        self.local=LOCAL.open('rb') if LOCAL.exists() else None
    def seekable(self):return True
    def seek(self,offset,whence=0):
        self.pos=offset if whence==0 else self.pos+offset if whence==1 else self.length+offset
        return self.pos
    def tell(self):return self.pos
    def fetch(self,start,end):
        req=urllib.request.Request(self.url,headers={'Range':f'bytes={start}-{end-1}'})
        with urllib.request.urlopen(req,timeout=120) as r:
            if r.status!=206:raise RuntimeError('Server did not honour the bounded range request.')
            data=r.read()
        if len(data)!=end-start:raise RuntimeError('Incomplete archive range.')
        self.blocks.append((start,data))
        return data
    def read(self,n=-1):
        if n<0:n=self.length-self.pos
        n=min(n,self.length-self.pos)
        start=self.pos;self.pos+=n
        if start+n<=self.local_size:
            self.local.seek(start);return self.local.read(n)
        for base,data in self.blocks:
            if base<=start and start+n<=base+len(data):return data[start-base:start-base+n]
        return self.fetch(start,start+n)

def main():
    archive=RemoteArchive()
    with zipfile.ZipFile(archive) as z:
        selected=[x for x in z.infolist() if x.filename.startswith(('pgsql/bin/','pgsql/lib/','pgsql/share/')) and not x.is_dir()]
        missing=[x for x in selected if x.header_offset+x.compress_size+len(x.filename.encode())+4096>archive.local_size]
        if missing:
            begin=min(x.header_offset for x in missing)
            end=min(archive.length,max(x.header_offset+x.compress_size+len(x.filename.encode())+4096 for x in missing))
            print(f'Fetching missing PostgreSQL server assets: {(end-begin)/1024/1024:.1f} MB',flush=True)
            archive.fetch(begin,end)
        for item in selected:
            target=(TARGET/item.filename).resolve()
            if not target.is_relative_to(TARGET.resolve()):raise RuntimeError('Unsafe archive path')
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(z.read(item))  # ZipFile validates each entry CRC.
    print(f'Extracted and CRC-verified {len(selected)} PostgreSQL server files.',flush=True)

if __name__=='__main__':main()
