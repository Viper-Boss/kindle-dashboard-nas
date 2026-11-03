#!/usr/bin/env python3
"""NAS-side updater: fixed repository/main, successful CI, validated builds and rollback.

The dashboard can request only upgrade/rollback; it cannot supply commands, URLs or paths.
"""
import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shlex
import stat
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request

CONTAINERS = {'kindle-dashboard':'kindle-dashboard','mtproto':'nas-mtproto','mihomo':'kindle-dashboard-proxy'}
BLOCKED = {'.env','mtproto/config.py','proxy/config.yaml'}


def allowed_source(name):
    p=PurePosixPath(name)
    return (not p.is_absolute() and all(x not in ('..','.') for x in p.parts)
            and bool(p.parts) and p.parts[0] not in ('.git','data','backups','security-staging')
            and name not in BLOCKED and not name.startswith('mtproto/vendor/')
            and not (name.startswith('proxy/') and name != 'proxy/config.example.yaml'))


def source_directory(root, relative, create=False):
    """Return an anchored directory fd; writable proxy files cannot redirect host writes."""
    parts=PurePosixPath(relative).parts
    if not parts or any(p in ('.','..') for p in parts) or PurePosixPath(relative).is_absolute():raise ValueError('无效源码路径')
    flags=os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW
    current=os.open(root,flags)
    try:
        for name in parts[:-1]:
            if create:
                try:os.mkdir(name,0o755,dir_fd=current)
                except FileExistsError:pass
            following=os.open(name,flags,dir_fd=current);os.close(current);current=following
        return current,parts[-1]
    except Exception:
        os.close(current);raise


def read_source(root, relative):
    try:directory,name=source_directory(root,relative)
    except FileNotFoundError:return None
    try:
        try:fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
        except FileNotFoundError:return None
        with os.fdopen(fd,'rb') as stream:
            info=os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):raise ValueError('源码不是普通文件')
            data=stream.read(8_000_001)
            if len(data)>8_000_000:raise ValueError('源码过大')
            return data,stat.S_IMODE(info.st_mode)
    finally:os.close(directory)


def write_source(root, relative, data, mode):
    directory,name=source_directory(root,relative,True);temporary='kindle-tmp-'+secrets_token()
    try:
        fd=os.open(temporary,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,mode,dir_fd=directory)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno());os.fchmod(stream.fileno(),mode)
        os.replace(temporary,name,src_dir_fd=directory,dst_dir_fd=directory)
    finally:
        try:os.unlink(temporary,dir_fd=directory)
        except FileNotFoundError:pass
        os.close(directory)


class Worker:
    def __init__(self, project, state, maintenance):
        self.project,self.state,self.m=Path(project).resolve(),Path(state).resolve(),maintenance
        self.state.mkdir(parents=True,exist_ok=True);self.state.chmod(0o700)
        self.shared=maintenance.SharedStore(self.project/'data')
        self.info={'busy':False,'stage':'空闲','heartbeat':time.time(),'rollbackAvailable':bool(self.private('rollback.json'))}
        self.lock=threading.Lock();self.job=None
        self.proxy='http://'+self.env_settings().get('NAS_BIND_IP','192.168.120.200')+':7890'
        os.environ.update(HTTP_PROXY=self.proxy,HTTPS_PROXY=self.proxy,NO_PROXY='localhost,127.0.0.1')
        self.secret_values=[v.strip('"\'') for k,v in self.env_settings().items()
                            if any(x in k for x in ('PASSWORD','KEY','TOKEN','SECRET')) and len(v)>6]
        self.stop=threading.Event()

    def env_settings(self):
        return dict(line.split('=',1) for line in (self.project/'.env').read_text().splitlines()
                    if '=' in line and not line.lstrip().startswith('#'))

    def private(self, name, default=None):
        p=self.state/name
        return json.loads(p.read_text()) if p.exists() else default

    def save_private(self, name, data):
        p=self.state/name;p.parent.mkdir(parents=True,exist_ok=True)
        tmp=p.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2));tmp.chmod(0o600);tmp.replace(p)

    def heartbeat(self):
        while not self.stop.is_set():
            try:
                with self.lock:
                    self.info['heartbeat']=time.time();self.shared.write('status.json',self.info)
            except Exception:pass
            self.stop.wait(10)

    def stage(self, text):
        with self.lock:self.info['stage']=text
        if self.job:
            self.job.update(stage=text,state='running');self.shared.write(self.job['id']+'.json',self.job,'jobs')
        self.log(text+'\n')

    def log(self, text):
        for value in self.secret_values:text=text.replace(value,'[已隐藏]')
        text=re.sub(r'(https?://)[^\s/@]+:[^\s/@]+@',r'\1[已隐藏]@',text)
        if self.job:self.shared.append_log(self.job['id'],text)

    def run(self, args, cwd=None, env=None):
        self.log('$ '+shlex.join([str(x) for x in args])+'\n')
        process=subprocess.Popen([str(x) for x in args],cwd=cwd or self.project,env=env,
                                 stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,errors='replace')
        for line in process.stdout:self.log(line)
        code=process.wait()
        if code:raise RuntimeError('命令失败（退出码 '+str(code)+'）：'+str(args[0]))

    def run_network(self, args, env=None):
        # Package registries may work directly even when the selected proxy
        # node interrupts TLS. Retry through that proxy only if direct fails.
        direct_env=dict(os.environ if env is None else env)
        for key in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy','ALL_PROXY','all_proxy'):
            direct_env[key]=''
        direct_env['NO_PROXY']=direct_env['no_proxy']='*'
        direct_args=[str(x).split('=',1)[0]+'=' if str(x).startswith(('HTTP_PROXY=','HTTPS_PROXY=','http_proxy=','https_proxy=')) else x for x in args]
        try:self.run(direct_args,env=direct_env)
        except RuntimeError:
            self.log('直连构建或扫描失败，使用现有代理重试。\n')
            self.run(args,env=env)

    def compose(self, *args, extra=None):
        command=['docker','compose','--project-directory',str(self.project),'-f',str(self.project/'docker-compose.yml')]
        if (self.state/'logging-override.json').exists():command+=['-f',str(self.state/'logging-override.json')]
        if extra:command+=['-f',str(extra)]
        self.run(command+list(args))

    def fetch_source(self, release, workspace):
        entries=self.m.api('/git/trees/'+release['tree']+'?recursive=1')
        if entries.get('truncated'):raise ValueError('GitHub 文件清单不完整')
        manifest={x['path']:x for x in entries['tree'] if x['type']=='blob'}
        if any(not allowed_source(n) or x['mode'] not in ('100644','100755') for n,x in manifest.items()):
            raise ValueError('仓库含受保护运行文件或不支持的链接')
        request=urllib.request.Request('https://codeload.github.com/'+self.m.REPOSITORY+'/tar.gz/'+release['commit'],headers={'User-Agent':'Kindle-NAS-Updater'})
        with self.m.open_url(request,timeout=90) as response:payload=response.read(32_000_001)
        if len(payload)>32_000_000:raise ValueError('源码归档过大')
        source=workspace/'source';source.mkdir(parents=True)
        found=set()
        with tarfile.open(fileobj=io.BytesIO(payload)) as archive:
            for member in archive:
                if not member.isfile():continue
                name=PurePosixPath(*PurePosixPath(member.name).parts[1:]).as_posix()
                if name not in manifest:continue
                if member.size>8_000_000:raise ValueError('源码文件过大')
                data=archive.extractfile(member).read()
                digest=hashlib.sha1(b'blob '+str(len(data)).encode()+b'\0'+data).hexdigest()
                if digest!=manifest[name]['sha']:raise ValueError('源码校验失败：'+name)
                p=source/name
                if not p.resolve().is_relative_to(source.resolve()):raise ValueError('无效源码路径')
                p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(data);p.chmod(0o755 if manifest[name]['mode']=='100755' else 0o644)
                found.add(name)
        if found!=set(manifest):raise ValueError('源码文件不完整')
        return source,manifest

    def build(self, source, release):
        env=dict(os.environ,DOCKER_BUILDKIT='0')
        # Pull immutable bases explicitly; legacy builder avoids FnOS mirror's BuildKit HEAD failures.
        bases=set()
        for name in ('Dockerfile','mtproto/Dockerfile','mihomo/Dockerfile'):
            for line in (source/name).read_text().splitlines():
                m=re.match(r'FROM\s+(\S+)',line,re.I)
                if m and m[1]!='scratch':bases.add(m[1])
        for base in sorted(bases):
            if not re.fullmatch(r'[a-zA-Z0-9_./:@-]+',base):raise ValueError('基础镜像引用无效')
            self.run(['docker','pull',base])
        images={}
        for service,context in [('kindle-dashboard','.'),('mtproto','mtproto'),('mihomo','mihomo')]:
            self.stage('构建 '+service)
            tag='kindle-upgrade-'+service+':'+release['commit'][:12]
            command=['docker','build','--network','host','--memory=2g',
                     '--build-arg','HTTP_PROXY='+self.proxy,'--build-arg','HTTPS_PROXY='+self.proxy,
                     '--build-arg','http_proxy='+self.proxy,'--build-arg','https_proxy='+self.proxy]
            if service=='mihomo':
                cache=self.private('build-cache.json',{}).get('image')
                if cache and re.fullmatch(r'[a-zA-Z0-9_./:@-]+',cache):
                    exists=subprocess.run(['docker','image','inspect',cache],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode==0
                    if exists:
                        text=(source/'mihomo/Dockerfile').read_text()
                        marker='RUN sh /tmp/patch-dependencies.sh'
                        if marker in text:
                            text='FROM '+cache+' AS kindle_build_cache\n'+text
                            text=text.replace(marker,'COPY --from=kindle_build_cache /go/pkg/mod /go/pkg/mod\nCOPY --from=kindle_build_cache /root/.cache/go-build /root/.cache/go-build\n'+marker,1)
                            (source/'mihomo/Dockerfile.cache').write_text(text)
                            command+=['-f',str(source/'mihomo/Dockerfile.cache')]
            self.run_network(command+['-t',tag,str(source/context)],env=env)
            images[service]=tag
            if service=='mihomo':
                builder='kindle-upgrade-builder:'+release['commit'][:12]
                self.run_network(command+['--target','build','-t',builder,str(source/context)],env=env)
                self.save_private('build-cache.json',{'image':builder})
        return images

    def scanner(self):
        target=self.state/'scanner/trivy'
        if target.exists():return target
        self.stage('准备镜像安全检查')
        arch={'aarch64':'ARM64','arm64':'ARM64','x86_64':'64bit','amd64':'64bit'}.get(platform.machine().lower())
        if not arch:raise ValueError('此 NAS 架构暂不支持自动镜像扫描')
        version='0.76.1';name=f'trivy_{version}_Linux-{arch}.tar.gz'
        base='https://github.com/aquasecurity/trivy/releases/download/v'+version+'/'
        with self.m.open_url(base+name,timeout=180) as response:data=response.read(100_000_000)
        with self.m.open_url(base+f'trivy_{version}_checksums.txt',timeout=30) as response:checks=response.read().decode()
        wanted=next((line.split()[0] for line in checks.splitlines() if line.split()[-1]==name),None)
        if hashlib.sha256(data).hexdigest()!=wanted:raise ValueError('安全扫描器校验失败')
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            member=archive.getmember('trivy')
            if not member.isfile():raise ValueError('扫描器归档无效')
            target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(archive.extractfile(member).read());target.chmod(0o755)
        return target

    def validate(self, source, images, workspace):
        self.stage('检查程序启动和现有代理配置')
        self.run(['docker','run','--rm','--network','none',images['kindle-dashboard'],'python','-c',
                  'import app,traffic_stats,maintenance,management;from PIL import Image;import io;i=Image.open(io.BytesIO(app.render_dashboard({"quotas":{},"kindle":{}})));assert i.mode=="L" and i.size==(600,800)'])
        self.run(['docker','run','--rm','--network','none','-v',str(source/'mtproto')+':/app:ro',images['mtproto'],'python','-c',
                  'import Crypto,socks,whatsapp_proxy,traffic_accounting;from Crypto.Cipher import AES;AES.new(bytes(32),AES.MODE_CTR,nonce=bytes(8))'])
        self.run(['docker','run','--rm','--network','none','-v',str(self.project/'proxy')+':/root/.config/mihomo:ro',images['mihomo'],'-t','-f','/root/.config/mihomo/config.yaml'])
        self.stage('扫描三个候选镜像')
        scanner=self.scanner();env=dict(os.environ,HTTP_PROXY=self.proxy,HTTPS_PROXY=self.proxy,NO_PROXY='localhost,127.0.0.1',GOMEMLIMIT='700MiB')
        for service,image in images.items():
            report=workspace/(service+'-security.json')
            self.run_network([str(scanner),'image','--scanners','vuln','--cache-dir',str(self.state/'trivy-cache'),'--timeout','20m','--format','json','--output',str(report),image],env=env)
            data=json.loads(report.read_text())
            fixable=[v for result in data.get('Results',[]) for v in result.get('Vulnerabilities',[]) if v.get('FixedVersion')]
            if fixable:raise RuntimeError(service+' 镜像仍有 '+str(len(fixable))+' 条可修补漏洞')

    def backup(self, manifest):
        self.stage('保存回退版本与配置')
        backup=self.state/'backups'/self.job['id'];backup.mkdir(parents=True)
        old=self.private('current.json',{})
        for name in manifest:
            previous=read_source(self.project,name)
            if previous is not None:
                dst=backup/'source'/name;dst.parent.mkdir(parents=True,exist_ok=True);dst.write_bytes(previous[0]);dst.chmod(previous[1])
        shutil.copytree(self.project/'proxy',backup/'proxy',symlinks=True,ignore=shutil.ignore_patterns('backups'))
        shutil.copy2(self.project/'.env',backup/'runtime.env')
        try:self.run(['docker','exec','kindle-dashboard','python','-c',
                      'import importlib.util; m=importlib.util.find_spec("traffic_stats"); exec("import traffic_stats;traffic_stats.collect_once();traffic_stats.TrafficStore().backup(\"/data/maintenance-traffic-backup.sqlite3\")") if m else None'])
        except Exception:self.log('旧版本尚未提供流量备份接口，继续保留现有数据目录。\n')
        db=self.project/'data/maintenance-traffic-backup.sqlite3'
        if db.exists():
            directory,name=source_directory(self.project,'data/maintenance-traffic-backup.sqlite3')
            try:
                fd=os.open(name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory)
                with os.fdopen(fd,'rb') as stream,open(backup/'traffic.sqlite3','wb') as target:
                    if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):raise ValueError('统计备份不是普通文件')
                    shutil.copyfileobj(stream,target)
            finally:os.close(directory)
            directory,name=source_directory(self.project,'data/maintenance-traffic-backup.sqlite3')
            try:os.unlink(name,dir_fd=directory)
            finally:os.close(directory)
        objects=json.loads(subprocess.check_output(['docker','inspect',*CONTAINERS.values()],text=True))
        override={'services':{}}
        for service,container in CONTAINERS.items():
            obj=next(x for x in objects if x['Name']=='/'+container)
            tag='kindle-rollback-'+service+':'+self.job['id']
            self.run(['docker','tag',obj['Image'],tag]);override['services'][service]={'image':tag}
        (backup/'images.json').write_text(json.dumps(override))
        (backup/'release.json').write_text(json.dumps(old))
        self.save_private('rollback.json',{'directory':str(backup),'commit':old.get('commit')})
        return backup

    def selectors(self, restore=None):
        script='import proxy_admin,json\np=proxy_admin.api("/proxies")["proxies"]\n'
        if restore is None:
            script+='print(json.dumps({k:v.get("now") for k,v in p.items() if v.get("type")=="Selector"}))'
        else:
            script+='saved='+repr(restore)+'\nfor group,node in saved.items():\n if group in p and node in p[group].get("all",[]):proxy_admin.action({"action":"select","group":group,"node":node})\n'
        result=subprocess.run(['docker','exec','-i','kindle-dashboard','python','-'],input=script,capture_output=True,text=True,timeout=45)
        if result.returncode:raise RuntimeError('节点选择读取/恢复失败')
        return json.loads(result.stdout) if restore is None else None

    def install_source(self, source, manifest):
        for name,entry in manifest.items():
            write_source(self.project,name,(source/name).read_bytes(),0o755 if entry['mode']=='100755' else 0o644)
        for p in [self.project/'proxy',self.project/'mtproto',self.project/'mtproto/pyaes']:
            if p.exists():p.chmod(0o755)

    def health(self):
        for _ in range(36):
            try:
                script='import urllib.request; o=urllib.request.build_opener(urllib.request.ProxyHandler({})); assert o.open("http://127.0.0.1:8787/healthz",timeout=3).status==200'
                result=subprocess.run(['docker','exec','kindle-dashboard','python','-c',script],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=8)
                info=json.loads(subprocess.check_output(['docker','inspect','kindle-dashboard-proxy','nas-mtproto'],text=True))
                if result.returncode==0 and all(x['State'].get('Health',{}).get('Status')=='healthy' for x in info):return
            except Exception:pass
            time.sleep(2)
        raise RuntimeError('部署后健康检查未通过')

    def rollback(self):
        item=self.private('rollback.json')
        if not item:raise ValueError('没有可用的回退版本')
        backup=Path(item['directory']).resolve()
        if not backup.is_relative_to((self.state/'backups').resolve()):raise ValueError('回退路径无效')
        self.stage('恢复上次版本（保留流量历史）')
        for p in (backup/'source').rglob('*'):
            if p.is_file():
                name=p.relative_to(backup/'source').as_posix()
                write_source(self.project,name,p.read_bytes(),stat.S_IMODE(p.stat().st_mode))
        self.compose('up','-d','--no-build','--force-recreate',extra=backup/'images.json')
        self.health()
        current=json.loads((backup/'release.json').read_text());self.save_private('current.json',current)
        write_source(self.project,'data/deployment.json',json.dumps(current).encode(),0o600)
        # A manual rollback pauses auto sync so it cannot immediately undo that choice.
        self.shared.write('settings.json',{'autoSync':False})
        self.save_private('apply.json',{'pending':False})

    def upgrade(self):
        self.stage('核对 GitHub 最新版本和安全检查')
        release=self.m.latest_release();self.shared.write('latest.json',release)
        if not release['ready']:raise ValueError('GitHub 安全检查尚未通过，未修改正式服务')
        if release['commit']==self.private('current.json',{}).get('commit'):
            self.log('当前已经是 GitHub 最新版本。\n');return
        self.job['target']=release['commit']
        workspace=self.project/'security-staging/maintenance'/self.job['id'];workspace.mkdir(parents=True,mode=0o700)
        self.stage('下载并逐文件校验源码')
        source,manifest=self.fetch_source(release,workspace)
        images=self.build(source,release);self.validate(source,images,workspace)
        selected=self.selectors();backup=self.backup(manifest)
        self.save_private('apply.json',{'pending':True,'backup':str(backup)})
        try:
            self.stage('应用新版本，重建服务')
            self.install_source(source,manifest)
            config=json.loads(subprocess.check_output(['docker','compose','--project-directory',str(self.project),'config','--format','json'],cwd=self.project,text=True))
            for service,image in images.items():self.run(['docker','tag',image,config['services'][service]['image']])
            self.compose('config','--quiet');self.compose('up','-d','--no-build','--force-recreate')
            self.health();self.selectors(selected)
            record={'commit':release['commit'],'installedAt':self.m.utc_now(),'message':release['message'],'managedFiles':sorted(manifest)}
            self.save_private('current.json',record)
            write_source(self.project,'data/deployment.json',json.dumps(record,ensure_ascii=False,indent=2).encode(),0o600)
            self.save_private('apply.json',{'pending':False})
        except Exception:
            self.log('应用新版本失败，执行自动回退。\n');self.rollback();raise
        with self.lock:self.info['rollbackAvailable']=True

    def valid_request(self, job):
        if not isinstance(job,dict):return False
        allowed={'id','kind','state','requestedAt'}|({'logSettings'} if job.get('kind')=='logs' else set())
        valid=(not set(job)-allowed and bool(self.m.JOB_ID.fullmatch(str(job.get('id',''))))
               and job.get('kind') in ('upgrade','rollback','logs') and job.get('state')=='queued'
               and isinstance(job.get('requestedAt'),str) and len(job['requestedAt'])<=60)
        if valid and job['kind']=='logs':
            try:self.m.validate_log_settings(job.get('logSettings'))
            except ValueError:return False
        return valid

    def apply_logs(self):
        policy=self.m.validate_log_settings(self.job['logSettings'])
        before=self.private('logging-override.json')
        old=self.m.log_settings(self.project/'data')
        per_file=policy['maxMB']*1024*1024//8
        logging={'driver':'json-file','options':{'max-size':str(per_file),'max-file':'2'}} if policy['enabled'] else {'driver':'none'}
        override={'services':{name:{'logging':logging} for name in CONTAINERS}}
        self.save_private('logging-pending.json',{'pending':True,'policy':policy})
        selected=self.selectors();completed=False
        try:
            self.stage('应用日志容量与记录开关')
            self.run(['docker','exec','kindle-dashboard','python','-c','import traffic_stats;traffic_stats.collect_once()'])
            self.save_private('logging-override.json',override)
            self.compose('config','--quiet');self.compose('up','-d','--no-build','--force-recreate')
            self.health();self.selectors(selected)
            self.shared.write('log-settings.json',policy)
            self.shared.prune_logs(policy['maxMB']*1024*1024//4)
            completed=True
        except Exception:
            if before is None:(self.state/'logging-override.json').unlink(missing_ok=True)
            else:self.save_private('logging-override.json',before)
            self.shared.write('log-settings.json',old)
            self.compose('up','-d','--no-build','--force-recreate');self.health();self.selectors(selected)
            completed=True
            raise
        finally:
            if completed:self.save_private('logging-pending.json',{'pending':False})

    def process(self, job):
        if not self.valid_request(job):raise ValueError('维护请求格式无效')
        self.job=job
        with self.lock:self.info.update(busy=True,job=job['id'],lastError=None)
        try:
            if job['kind']=='upgrade':self.upgrade()
            elif job['kind']=='logs':self.apply_logs()
            else:self.rollback()
            job.update(state='success',stage='已完成',finishedAt=self.m.utc_now())
        except Exception as error:
            self.log(type(error).__name__+': '+str(error)+'\n')
            job.update(state='failed',stage='操作未完成',error=str(error)[:300],finishedAt=self.m.utc_now())
        finally:
            if job.get('state')=='failed' and job.get('target'):
                self.save_private('failure.json',{'commit':job['target'],'failedAt':time.time()})
            self.shared.write(job['id']+'.json',job,'jobs')
            with self.lock:self.info.update(busy=False,stage='空闲',lastJob=job['id'],rollbackAvailable=bool(self.private('rollback.json')))
            self.job=None

    def recover_interrupted(self):
        for name in self.shared.names('jobs'):
            job=self.shared.read(name,{},'jobs')
            if isinstance(job,dict) and job.get('state')=='running':
                job.update(state='failed',stage='上次维护进程已中断',finishedAt=self.m.utc_now())
                self.shared.write(name,job,'jobs')
        if self.private('apply.json',{}).get('pending'):
            self.process({'id':secrets_token(),'kind':'rollback','state':'queued','requestedAt':self.m.utc_now()})
            if self.private('apply.json',{}).get('pending'):
                raise RuntimeError('中断后的自动回退未完成，请检查 NAS 维护日志')

        pending=self.private('logging-pending.json',{})
        if pending.get('pending'):
            self.process({'id':secrets_token(),'kind':'logs','state':'queued','requestedAt':self.m.utc_now(),'logSettings':pending['policy']})

    def loop(self, once=False):
        self.recover_interrupted()
        threading.Thread(target=self.heartbeat,daemon=True).start()
        next_check=0
        while not self.stop.is_set():
            try:
                pending=[]
                for name in self.shared.names('jobs'):
                    job=self.shared.read(name,{},'jobs')
                    if isinstance(job,dict) and job.get('state')=='queued':
                        if job.get('id')!=name[:-5] or not self.valid_request(job):
                            self.shared.write(name,{'id':name[:-5],'state':'failed','stage':'维护请求格式无效','finishedAt':self.m.utc_now()},'jobs')
                        else:pending.append(job)
                if pending:
                    self.process(sorted(pending,key=lambda j:str(j.get('requestedAt','')))[0])
                    if once:return
                    # Reload worker code after a completed deployment without expanding the queue API.
                    self.stop.set();os.execv(sys.executable,[sys.executable,str(self.project/'scripts/maintenance_worker.py'),'--project',str(self.project),'--state',str(self.state)])
                elif time.time()>=next_check:
                    next_check=time.time()+3600
                    if self.m.settings()['autoSync']:
                        latest=self.m.latest_release();self.shared.write('latest.json',latest)
                        failure=self.private('failure.json',{})
                        cooling=(failure.get('commit')==latest['commit'] and time.time()-failure.get('failedAt',0)<3600)
                        if latest['ready'] and not cooling and latest['commit']!=self.private('current.json',{}).get('commit'):
                            self.process({'id':secrets_token(),'kind':'upgrade','state':'queued','requestedAt':self.m.utc_now()})
                            if once:return
                            self.stop.set();os.execv(sys.executable,[sys.executable,str(self.project/'scripts/maintenance_worker.py'),'--project',str(self.project),'--state',str(self.state)])
                if once:return
            except Exception as error:
                with self.lock:self.info['lastError']=type(error).__name__+': '+str(error)[:200]
                if once:raise
            self.stop.wait(2)


def secrets_token():
    import secrets
    return secrets.token_hex(16)


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--project',required=True);parser.add_argument('--state');parser.add_argument('--once',action='store_true')
    args=parser.parse_args();project=Path(args.project).resolve()
    if os.geteuid()!=0:raise SystemExit('NAS maintenance worker must be installed by root')
    os.environ['KINDLE_DATA_ROOT']=str(project/'data');sys.path.insert(0,str(project))
    import maintenance
    state=Path(args.state) if args.state else project/'security-staging/maintenance-private';state.mkdir(parents=True,exist_ok=True);state.chmod(0o700)
    with open(state/'worker.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        Worker(project,state,maintenance).loop(args.once)


if __name__=='__main__':main()
