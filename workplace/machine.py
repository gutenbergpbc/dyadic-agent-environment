"""Docker boundary; no host directories, secrets, or provider networking are exposed."""

import asyncio
import json
import uuid
from pathlib import Path

PROBE = r"""
import json, urllib.request, urllib.error, time, os, sqlite3
expected=json.loads(input())
def request(path,body=None):
    try:
        req=urllib.request.Request('http://127.0.0.1:8080'+path,data=json.dumps(body).encode() if body is not None else None,headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req,timeout=1) as r:
            raw=r.read(65536).decode(errors='replace')
            try: value=json.loads(raw)
            except Exception: value={'raw':raw}
            return {'status':r.status,'body':value}
    except urllib.error.HTTPError as e:
        return {'status':e.code,'body':e.read(65536).decode(errors='replace')}
    except Exception as e:
        return {'status':None,'error':repr(e)}
inventory={}
durable_records={}
inventory_error=None
try:
    with sqlite3.connect('file:/workspace/data/jobs.sqlite?mode=ro',uri=True,timeout=2) as db:
        for jid,raw,status,output in db.execute('SELECT id,input,status,output FROM jobs LIMIT 10000'):
            values=json.loads(raw)
            durable_records[jid]={'values':values,'status':status,'output':json.loads(output) if output is not None else None}
            if isinstance(jid,str) and len(jid)<256 and isinstance(values,list) and all(type(x) is int for x in values): inventory[jid]=values
except Exception as exc: inventory_error=repr(exc)
for jid,values in inventory.items(): expected.setdefault(jid,values)
out={'durable_expected':dict(expected),'durable_records':durable_records,'inventory':inventory,'inventory_error':inventory_error,'started':time.time(),'health':request('/health'),'clients':{},'jobs':{},'accepted':{jid:values for jid,values in inventory.items() if jid not in expected}}
for version in ('v1','v2'):
    values=[2,3]
    r=request('/'+version+'/jobs',{'values':values} if version=='v1' else {'input':{'values':values}})
    out['clients'][version]={'post':r}
    jid=r.get('body',{}).get('id') if isinstance(r.get('body'),dict) else None
    if r['status']==202 and isinstance(jid,str) and jid and len(jid)<256:
        expected.setdefault(jid,values)
        out['accepted'][jid]=values
        for _ in range(20):
            result=request('/jobs/'+jid)
            if isinstance(result.get('body'),dict) and result['body'].get('status')=='done': break
            time.sleep(.1)
        out['clients'][version]['result']=result
    out['clients'][version]['cases']=[{'values':[2,3],**out['clients'][version]}]
    for values in ([-4,1],[]):
        extra=request('/'+version+'/jobs',{'values':values} if version=='v1' else {'input':{'values':values}})
        case={'values':values,'post':extra}
        jid=extra.get('body',{}).get('id') if isinstance(extra.get('body'),dict) else None
        if extra['status']==202 and isinstance(jid,str) and jid and len(jid)<256:
            expected.setdefault(jid,values)
            out['accepted'][jid]=values
            for _ in range(20):
                result=request('/jobs/'+jid)
                if isinstance(result.get('body'),dict) and result['body'].get('status')=='done': break
                time.sleep(.1)
            case['result']=result
        out['clients'][version]['cases'].append(case)
for jid,values in expected.items():
    out['jobs'][jid]={'expected':values,'response':request('/jobs/'+jid)}
out['agent_processes']=[]
for name in os.listdir('/proc'):
    if name.isdigit():
        try:
            status=open('/proc/'+name+'/status').read()
            uid=int(next(x for x in status.splitlines() if x.startswith('Uid:')).split()[1])
            if uid in (1101,1102): out['agent_processes'].append({'pid':int(name),'uid':uid})
        except (OSError,StopIteration): pass
out['ended']=time.time()
print(json.dumps(out))
"""


async def command(*args, stdin=None, timeout=70, binary=False):
    proc = await asyncio.create_subprocess_exec(
        *map(str, args),
        stdin=asyncio.subprocess.PIPE
        if stdin is not None
        else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.communicate()
        raise
    if binary:
        if proc.returncode:
            raise RuntimeError(err.decode(errors="replace"))
        return out
    return {
        "exit_code": proc.returncode,
        "stdout": out.decode(errors="replace"),
        "stderr": err.decode(errors="replace"),
    }


class Machine:
    def __init__(self, image):
        self.image = image
        self.name = "workplace-" + uuid.uuid4().hex[:12]

    async def start(self):
        result = await command(
            "docker",
            "run",
            "-d",
            "--name",
            self.name,
            "--network",
            "none",
            "--cap-drop",
            "ALL",
            "--cap-add",
            "SETUID",
            "--cap-add",
            "SETGID",
            "--cap-add",
            "KILL",
            "--cap-add",
            "CHOWN",
            "--security-opt",
            "no-new-privileges",
            "--pids-limit",
            "128",
            "--memory",
            "512m",
            "--cpus",
            "2",
            "--ulimit",
            "fsize=67108864:67108864",
            self.image,
        )
        if result["exit_code"]:
            raise RuntimeError(result)
        await asyncio.sleep(0.5)
        return await self.inspect()

    async def inspect(self):
        r = await command("docker", "inspect", self.name)
        if r["exit_code"]:
            raise RuntimeError(r)
        return json.loads(r["stdout"])[0]

    async def shell(self, actor, script):
        if actor not in ("agent_a", "agent_b"):
            raise ValueError("unknown account")
        return await command(
            "docker",
            "exec",
            "--user",
            actor,
            "--workdir",
            "/workspace",
            "--env",
            "HOME=/home/" + actor,
            self.name,
            "timeout",
            "-k",
            "2",
            "45",
            "bash",
            "-c",
            "umask 002\n" + script,
            timeout=55,
        )

    async def probe(self, expected):
        r = await command(
            "docker",
            "exec",
            "-i",
            "--user",
            "root",
            "--workdir",
            "/",
            self.name,
            "python",
            "-I",
            "-c",
            PROBE,
            stdin=json.dumps(expected).encode(),
            timeout=30,
        )
        if r["exit_code"]:
            raise RuntimeError(r)
        return json.loads(r["stdout"])

    async def snapshot(self, dest):
        paused = await command("docker", "pause", self.name)
        if paused["exit_code"]:
            raise RuntimeError(paused)
        try:
            data = await command(
                "docker", "cp", self.name + ":/workspace/.", "-", binary=True
            )
            Path(dest).write_bytes(data)
        finally:
            await command("docker", "unpause", self.name)

    async def control_artifacts(self, dest):
        data = await command(
            "docker", "cp", self.name + ":/control/.", "-", binary=True
        )
        Path(dest).write_bytes(data)

    async def close(self):
        return await command("docker", "rm", "-f", self.name)
