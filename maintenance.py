"""Version checks and a narrow file queue; the web container never receives Docker access."""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import re
import secrets
import stat
import threading
import time
import urllib.request
import urllib.error

REPOSITORY = 'Viper-Boss/kindle-dashboard-nas'
DATA_ROOT = Path(os.getenv('KINDLE_DATA_ROOT', '/data'))
JOB_ID = re.compile(r'^[0-9a-f]{32}$')
_cache = None
_cache_at = 0
_cache_lock = threading.Lock()
_log_lock = threading.Lock()
MEBIBYTE = 1024*1024


class SharedStore:
    """Directory-fd operations reject symlinks, including during host-worker queue races."""
    def __init__(self, root=None):
        self.root = Path(DATA_ROOT if root is None else root)

    @contextmanager
    def directory(self, section=None):
        self.root.mkdir(parents=True, exist_ok=True)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        parent = os.open(self.root, flags)
        opened = [parent]
        try:
            for name in ['maintenance'] + ([section] if section else []):
                if name not in ('maintenance','jobs','logs'):
                    raise ValueError('无效维护目录')
                try:
                    os.mkdir(name, 0o700, dir_fd=parent)
                except FileExistsError:
                    pass
                parent = os.open(name, flags, dir_fd=parent)
                opened.append(parent)
            yield parent
        finally:
            for fd in reversed(opened):
                os.close(fd)

    @staticmethod
    def _name(name):
        if not re.fullmatch(r'[a-zA-Z0-9_.-]{1,100}',name) or name.startswith('.'):
            raise ValueError('无效维护文件名')

    def read(self, name, default=None, section=None):
        self._name(name)
        try:
            with self.directory(section) as directory:
                fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
                with os.fdopen(fd, 'rb') as stream:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                        raise ValueError('维护文件不是普通文件')
                    raw = stream.read(2_000_001)
                    if len(raw)>2_000_000:
                        raise ValueError('维护文件过大')
                    return json.loads(raw)
        except FileNotFoundError:
            return default

    def write(self, name, value, section=None):
        self._name(name)
        raw = json.dumps(value,ensure_ascii=False,indent=2).encode()
        with self.directory(section) as directory:
            temporary = 'tmp-'+secrets.token_hex(16)
            fd = os.open(temporary, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW, 0o600, dir_fd=directory)
            try:
                with os.fdopen(fd,'wb') as stream:
                    stream.write(raw);stream.flush();os.fsync(stream.fileno())
                os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
            finally:
                try:os.unlink(temporary,dir_fd=directory)
                except FileNotFoundError:pass

    def names(self, section):
        with self.directory(section) as directory:
            return [n for n in os.listdir(directory) if re.fullmatch(r'[0-9a-f]{32}\.json',n)]

    def prune_logs(self, limit, keep=None):
        with self.directory('logs') as directory:
            files=[]
            for name in os.listdir(directory):
                if not re.fullmatch(r'[0-9a-f]{32}\.log',name):continue
                info=os.stat(name,dir_fd=directory,follow_symlinks=False)
                if stat.S_ISREG(info.st_mode):files.append((info.st_mtime_ns,name,info.st_size))
            total=sum(x[2] for x in files)
            for _,name,size in sorted(files,key=lambda x:(x[1]==keep,x[0],x[1])):
                if total<=limit:break
                os.unlink(name,dir_fd=directory);total-=size

    def append_log(self, job, text):
        if not JOB_ID.fullmatch(job):raise ValueError('无效任务编号')
        policy=log_settings(self.root)
        if not policy['enabled']:return
        limit=policy['maxMB']*MEBIBYTE//4
        raw=text.encode('utf-8')[-limit:]
        with _log_lock:
            with self.directory('logs') as directory:
                fd=os.open(job+'.log',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=directory)
                if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise ValueError('维护日志不是普通文件')
                with os.fdopen(fd,'r+b') as stream:
                    stream.seek(0,2);size=stream.tell()
                    if size+len(raw)>limit:
                        stream.seek(max(0,size-(limit-len(raw))));tail=stream.read(limit-len(raw))
                        stream.seek(0);stream.truncate();stream.write(tail)
                    stream.write(raw)
            self.prune_logs(limit,job+'.log')

    def log(self, job):
        if not JOB_ID.fullmatch(job):raise ValueError('无效任务编号')
        try:
            with self.directory('logs') as directory:
                fd=os.open(job+'.log',os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                if not stat.S_ISREG(os.fstat(fd).st_mode):os.close(fd);raise ValueError('维护日志不是普通文件')
                with os.fdopen(fd,'rb') as stream:
                    stream.seek(0,2);stream.seek(max(0,stream.tell()-24000))
                    return stream.read().decode('utf-8','replace')
        except FileNotFoundError:return ''


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def open_url(request, timeout=20):
    # GitHub is often reachable directly while a healthy latency-test node may
    # reject GitHub TLS. Both routes keep normal certificate verification.
    direct=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        return direct.open(request,timeout=timeout)
    except urllib.error.HTTPError as error:
        if error.code < 500:
            raise
    except (urllib.error.URLError,TimeoutError,OSError):
        pass
    return urllib.request.urlopen(request,timeout=timeout)


def api(path):
    request=urllib.request.Request('https://api.github.com/repos/'+REPOSITORY+path,
                                  headers={'Accept':'application/vnd.github+json','User-Agent':'Kindle-NAS-Updater'})
    with open_url(request,timeout=20) as response:return json.load(response)


def deployment():
    for name in ['deployment.json','security-deployment.json']:
        try:
            data=json.loads((DATA_ROOT/name).read_text())
            if re.fullmatch(r'[0-9a-f]{40}',data.get('commit','')):return data
        except (OSError,ValueError,TypeError):pass
    return {'commit':None}


def latest_release():
    commit=api('/commits/main')
    sha=commit.get('sha','')
    if not re.fullmatch(r'[0-9a-f]{40}',sha):raise ValueError('GitHub 提交编号无效')
    runs=api('/actions/runs?head_sha='+sha).get('workflow_runs',[])
    security=[r for r in runs if r.get('path')=='.github/workflows/security.yml' and r.get('head_sha')==sha and r.get('head_branch')=='main']
    ready=bool(security) and security[0].get('conclusion')=='success'
    for run in runs:
        if 'codeql' in run.get('path','').lower() and run.get('head_sha')==sha:
            ready=ready and run.get('conclusion')=='success'
    return {'commit':sha,'tree':commit['commit']['tree']['sha'],
            'message':commit['commit']['message'].split('\n')[0][:240],
            'date':commit['commit']['committer']['date'],'ready':ready,
            'url':'https://github.com/'+REPOSITORY+'/commit/'+sha}


def check_updates(force=False):
    global _cache,_cache_at
    with _cache_lock:
        if not force and _cache and time.time()-_cache_at<60:return _cache
        _cache=latest_release();_cache_at=time.time()
        SharedStore().write('latest.json',_cache)
        return _cache


def settings():
    data=SharedStore().read('settings.json',{'autoSync':True})
    if not isinstance(data,dict) or type(data.get('autoSync')) is not bool:
        raise ValueError('自动同步设置无效')
    return {'autoSync':data['autoSync'],'interval':3600}


def set_auto_sync(enabled):
    if type(enabled) is not bool:raise ValueError('自动同步开关必须为布尔值')
    SharedStore().write('settings.json',{'autoSync':enabled})
    return settings()


def enqueue(kind, policy=None):
    if kind not in ('upgrade','rollback','logs'):raise ValueError('不支持的维护操作')
    store=SharedStore();state=store.read('status.json',{})
    if state.get('busy'):raise ValueError('已有升级任务正在运行')
    if time.time()-float(state.get('heartbeat',0))>90:
        raise ValueError('NAS 升级服务未连接')
    for name in store.names('jobs'):
        item=store.read(name,{},'jobs')
        if item.get('state') in ('queued','running'):raise ValueError('已有维护任务在队列中')
    job=secrets.token_hex(16)
    payload={'id':job,'kind':kind,'state':'queued','requestedAt':utc_now()}
    if kind=='logs':payload['logSettings']=validate_log_settings(policy)
    store.write(job+'.json',payload,'jobs')
    return payload


def validate_log_settings(value):
    if (not isinstance(value,dict) or set(value)!={'enabled','maxMB'}
            or type(value['enabled']) is not bool or type(value['maxMB']) is not int
            or not 20<=value['maxMB']<=500):
        raise ValueError('日志容量必须为 20–500 MB，开关必须为布尔值')
    return dict(value)


def log_settings(root=None):
    return validate_log_settings(SharedStore(root).read('log-settings.json',{'enabled':True,'maxMB':100}))


def status():
    store=SharedStore();worker=store.read('status.json',{})
    worker['connected']=time.time()-float(worker.get('heartbeat',0))<90
    jobs=[]
    for name in store.names('jobs'):
        item=store.read(name,{},'jobs')
        if isinstance(item,dict):jobs.append(item)
    jobs=sorted(jobs,key=lambda x:x.get('requestedAt',''),reverse=True)[:10]
    return {'worker':worker,'settings':settings(),'jobs':jobs,'current':deployment(),
            'latest':store.read('latest.json'), 'components':components(),'logSettings':log_settings()}


def components():
    import platform
    versions={'Python':platform.python_version(),'Pillow':importlib.metadata.version('Pillow'),
              'Mihomo':None,'Telegram / WhatsApp 转发':None,'转发 Python':None,'PyCryptodome':None,'PySocks':None}
    try:
        import proxy_admin
        versions['Mihomo']=proxy_admin.api('/version',timeout=3)['version']
    except Exception:pass
    try:
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://mtproto:8766/status',timeout=3) as response:
            versions.update(json.load(response).get('componentVersions',{}))
    except Exception:pass
    return versions
