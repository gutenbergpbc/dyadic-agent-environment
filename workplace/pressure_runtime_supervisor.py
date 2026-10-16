"""Root-owned supervisor programs injected into the isolated study container.

No agent imports this module. Receipts live outside the shared workspace. PID
identities include Linux start ticks; ancestry observations are sampled evidence.
"""

LAUNCH = r"""
import json, os, pwd, subprocess, sys, threading, time
cfg=json.load(sys.stdin)
root='/pressure-supervisor'
os.makedirs(root,mode=0o700,exist_ok=True)
os.chmod(root,0o700)
directory=root+'/'+cfg['id']
os.mkdir(directory,0o700)
def write(name,value):
    path=directory+'/'+name
    with open(path+'.tmp','w') as f:
        json.dump(value,f); f.flush(); os.fsync(f.fileno())
    os.replace(path+'.tmp',path)
write('admission.json',cfg)
pid=os.fork()
if pid:
    print(json.dumps({'supervisor_pid':pid,'id':cfg['id']}),flush=True)
    sys.exit(0)
os.setsid()
fd=os.open('/dev/null',os.O_RDWR)
for n in (0,1,2): os.dup2(fd,n)
try:
    account=pwd.getpwnam(cfg['actor'])
    env={'PATH':'/usr/local/bin:/usr/bin:/bin','HOME':account.pw_dir,'USER':cfg['actor'],'LANG':'C.UTF-8'}
    # Host and container share the kernel's monotonic clock. Check again after
    # Docker scheduling and account lookup, immediately before starting bash.
    now=time.monotonic()
    if now>=cfg['expires_mono']:
        write('denied.json',{'mono':now,'reason':'actor_deadline_before_launch'})
        os._exit(0)
    proc=subprocess.Popen(['bash','-c','umask 002\n'+cfg['script']],cwd='/workspace',env=env,
        user=account.pw_uid,group=account.pw_gid,extra_groups=os.getgrouplist(account.pw_name,account.pw_gid),
        start_new_session=True,stdin=subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    def identity(pid):
        try: return {'pid':pid,'start_ticks':int(open('/proc/%s/stat'%pid).read().rsplit(')',1)[1].split()[19])}
        except OSError: return {'pid':pid,'start_ticks':None}
    write('launch.json',{'mono':time.monotonic(),'shell':identity(proc.pid),'supervisor':identity(os.getpid())})
    def drain(stream,name):
        count=0
        with open(directory+'/'+name,'wb') as f:
            while True:
                data=os.read(stream.fileno(),65536)
                if not data: break
                count+=len(data)
                if f.tell()<8*1024*1024:
                    f.write(data[:8*1024*1024-f.tell()]); f.flush()
        write(name+'-complete.json',{'mono':time.monotonic(),'bytes':count,'truncated':count>8*1024*1024})
    threads=[threading.Thread(target=drain,args=(proc.stdout,'stdout')),threading.Thread(target=drain,args=(proc.stderr,'stderr'))]
    for t in threads: t.start()
    code=proc.wait()
    write('termination.json',{'mono':time.monotonic(),'exit_code':code,'meaning':'direct shell reaped; descendants may remain'})
    for t in threads: t.join()
    write('output-complete.json',{'mono':time.monotonic(),'meaning':'both inherited output pipes reached EOF; detached descendants may remain'})
except BaseException as exc:
    write('error.json',{'mono':time.monotonic(),'error':repr(exc)})
os._exit(0)
"""

STATUS = r"""
import json,os,sys,time
root='/pressure-supervisor/'+sys.argv[1]
out={'sample_mono':time.monotonic()}
for name in ('launch','termination','output-complete','denied','error'):
    try:
        with open(root+'/'+name+'.json') as f: out[name]=json.load(f)
    except FileNotFoundError: pass
for name in ('stdout','stderr'):
    try:
        with open(root+'/'+name,'rb') as f: out[name]=f.read(24000).decode(errors='replace')
    except FileNotFoundError: out[name]=''
print(json.dumps(out))
"""

PROCESSES = r"""
import json,os,time
out={'started_mono':time.monotonic(),'processes':[],'read_failures':0,'scope':'sampled identities and ancestry; absence is not exact termination'}
for name in os.listdir('/proc'):
    if not name.isdigit(): continue
    try:
        status=open('/proc/'+name+'/status').read()
        uid=int(next(x for x in status.splitlines() if x.startswith('Uid:')).split()[1])
        stat=open('/proc/'+name+'/stat').read().rsplit(')',1)[1].split()
        out['processes'].append({'pid':int(name),'uid':uid,'start_ticks':int(stat[19]),'ppid':int(stat[1]),'pgrp':int(stat[2]),'session':int(stat[3]),'state':stat[0], 'cmdline':open('/proc/'+name+'/cmdline','rb').read(4096).decode(errors='replace')})
    except (OSError,StopIteration,ValueError): out['read_failures']+=1
out['ended_mono']=time.monotonic()
print(json.dumps(out))
"""
