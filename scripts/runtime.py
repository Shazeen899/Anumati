"""Local Windows demo lifecycle. All state stays under .runtime; no services or Docker."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

ROOT=Path(__file__).resolve().parent.parent
os.chdir(ROOT)
sys.path.insert(0,str(ROOT))
from anumati import settings
RUNTIME=ROOT/'.runtime'
PYTHON=ROOT/'.venv'/'Scripts'/'python.exe'
FLAGS=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
WEB_PORT=int(os.getenv('WEB_PORT','8010'))

def run(args,**kwargs):
    return subprocess.run([str(x) for x in args],check=True,creationflags=FLAGS,**kwargs)

def available(port):
    with socket.socket() as s:
        return s.connect_ex(('127.0.0.1',int(port)))==0

def wait(port):
    for _ in range(80):
        if available(port):return
        time.sleep(.25)
    raise RuntimeError(f'Port {port} did not become ready. Inspect .runtime/*.log.')

def start(name,args,cwd=ROOT):
    log=(RUNTIME/f'{name}.log').open('ab')
    flags=(subprocess.CREATE_NO_WINDOW | 0x00000008 | 0x00000200) if os.name=='nt' else 0
    p=subprocess.Popen([str(x) for x in args],cwd=cwd,stdout=log,stderr=subprocess.STDOUT,stdin=subprocess.DEVNULL,creationflags=flags)
    ledger_path=RUNTIME/'processes.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    ledger[name]={'pid':p.pid,'args':[str(x) for x in args]}
    ledger_path.write_text(json.dumps(ledger,indent=2))
    time.sleep(.5)
    if p.poll() is not None:raise RuntimeError(f'{name} exited. Inspect .runtime/{name}.log.')
    print(f'{name}: started (PID {p.pid})')

def main():
    import psycopg
    from psycopg import sql
    import redis
    mode=sys.argv[1] if len(sys.argv)>1 else 'start'
    RUNTIME.mkdir(exist_ok=True)
    pg=RUNTIME/'postgres'/'pgsql'/'bin'
    pgdata=RUNTIME/'pgdata'
    db=settings.DATABASES['default']
    redis_port=int(settings.CELERY_BROKER_URL.split(':')[-1].split('/')[0])
    if mode=='credentials':
        path=RUNTIME/'DEMO-CREDENTIALS.txt'
        path.write_text('LOCAL SYNTHETIC DEMO ONLY\n\nURL: http://127.0.0.1:8010\nShared demo password: '+os.environ['DEMO_PASSWORD']+'\n\nAccounts:\ninstitution / institution2: isolated institution workspaces\nreviewer: assigned scrutiny and evidence verification\nevc: evaluation scheduling and reports\ncommittee: SHC/SAC recommendations\nregional: North region oversight\nbureau: cross-region oversight\nexecutive: Executive Committee decisions\ncouncil: Council ratification and appeals\npolicy: demonstration policy configuration\nauditor: read-only case records and reports\noperator: outbox recovery\n\nDo not reuse these credentials outside the local demo.\n',encoding='utf-8')
        print(f'Credentials saved locally: {path}')
        return
    if mode=='status':
        print({name:available(port) for name,port in [('postgresql',db['PORT']),('redis',redis_port),('web',WEB_PORT)]})
        return
    if not (pg/'postgres.exe').exists():raise RuntimeError('Extract the official PostgreSQL Windows binaries into .runtime/postgres first.')
    if not (pgdata/'PG_VERSION').exists():
        pw=RUNTIME/'initdb-password.tmp'
        try:
            pw.write_text(db['PASSWORD'],encoding='utf-8')
            run([pg/'initdb.exe','-D',pgdata,'-U',db['USER'],'--pwfile',pw,'--auth=scram-sha-256','--encoding=UTF8','--locale=C'])
        finally:pw.unlink(missing_ok=True)
        with (pgdata/'postgresql.conf').open('a') as f:
            f.write(f"\nlisten_addresses = '127.0.0.1'\nport = {db['PORT']}\nmax_connections = 60\n")
    if not available(db['PORT']):
        run([pg/'pg_ctl.exe','-D',pgdata,'-l',RUNTIME/'postgres.log','start','-w'])
    wait(db['PORT'])
    with psycopg.connect(dbname='postgres',user=db['USER'],password=db['PASSWORD'],host=db['HOST'],port=db['PORT'],autocommit=True) as conn:
        if not conn.execute('SELECT 1 FROM pg_database WHERE datname=%s',(db['NAME'],)).fetchone():
            conn.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(db['NAME'])))
    binaries=list((RUNTIME/'redis').rglob('redis-server.exe'))
    if not binaries:raise RuntimeError('Extract the Redis Windows demo build into .runtime/redis first.')
    if not available(redis_port):
        start('redis',[binaries[0],'--bind','127.0.0.1','--port',str(redis_port),'--protected-mode','yes','--appendonly','yes','--dir',str(RUNTIME)],cwd=binaries[0].parent)
    wait(redis_port)
    assert redis.Redis.from_url(settings.CELERY_BROKER_URL).ping()
    run([PYTHON,'manage.py','migrate','--noinput'])
    run([PYTHON,'manage.py','seed_demo'])
    if mode=='init':return
    # Prevent duplicate workers/schedulers on repeated starts.
    ledger_path=RUNTIME/'processes.json'
    ledger=json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    def running(name):
        entry=ledger.get(name)
        if not entry:return False
        result=subprocess.run(['tasklist','/FI',f"PID eq {entry['pid']}",'/NH'],capture_output=True,text=True,creationflags=FLAGS)
        return str(entry['pid']) in result.stdout
    if not available(WEB_PORT):start('web',[PYTHON,'manage.py','runserver',f'127.0.0.1:{WEB_PORT}','--noreload'])
    elif not running('web'):raise RuntimeError(f'Port {WEB_PORT} is already occupied by another process. Set WEB_PORT in .env.')
    if not running('worker'):start('worker',[PYTHON,'-m','celery','-A','anumati','worker','--pool=solo','--loglevel=info','--hostname=anumati-local'])
    if not running('beat'):start('beat',[PYTHON,'-m','celery','-A','anumati','beat','--loglevel=info','--schedule',RUNTIME/'celerybeat-schedule'])
    wait(WEB_PORT)
    print(f'Anumati is running at http://127.0.0.1:{WEB_PORT}. Local logs: .runtime/')

if __name__=='__main__':main()
