import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path('/proxy')
LOCK = threading.RLock()

SERVICES = ('telegram', 'whatsapp')
TELEGRAM_GROUP = 'Telegram代理'
# core.telegram.org/resources/cidr.txt：Telegram 官方服务器地址段。MTProto 转发是按 IP 直连 Telegram 数据中心的，
# 所以分流必须用 IP 段规则，光靠域名规则匹配不到。
TELEGRAM_DOMAINS = ('telegram.org', 't.me', 'telegram.me')
TELEGRAM_CIDRS = ('91.105.192.0/23', '91.108.4.0/22', '91.108.8.0/22', '91.108.12.0/22', '91.108.16.0/22',
    '91.108.20.0/22', '91.108.56.0/22', '149.154.160.0/20', '185.76.151.0/24', '2001:67c:4e8::/48',
    '2001:b28:f23c::/48', '2001:b28:f23d::/48', '2001:b28:f23f::/48', '2a0a:f280::/32')

def telegram_rule_lines():
    rules = ['DOMAIN-SUFFIX,%s,%s' % (d, TELEGRAM_GROUP) for d in TELEGRAM_DOMAINS]
    rules += ['%s,%s,%s,no-resolve' % ('IP-CIDR6' if ':' in c else 'IP-CIDR', c, TELEGRAM_GROUP) for c in TELEGRAM_CIDRS]
    return rules

def add_telegram_group(text):
    """给已有配置加上 Telegram代理 策略组和分流规则；已存在则原样返回。"""
    if re.search(r'^\s*-\s*name:\s*["\']?' + re.escape(TELEGRAM_GROUP), text, re.M):
        return text, False
    if not text.endswith('\n'):
        text += '\n'
    groups = re.search(r'^proxy-groups:[ \t]*\n', text, re.M)
    if not groups or not re.search(r'^rules:[ \t]*\n', text, re.M):
        raise ValueError('当前配置里没有 proxy-groups 或 rules，无法自动添加，请手动加入“' + TELEGRAM_GROUP + '”策略组')
    # 复制 WhatsApp 策略组的写法（同样的可选节点），没有就建一个默认跟随“数据屏代理”的组
    whatsapp = re.search(r'^([ \t]*)- name:\s*["\']?WhatsApp代理["\']?[ \t]*\n(?:\1[ \t]+.*\n)*', text, re.M)
    if whatsapp:
        block = whatsapp.group(0).replace('WhatsApp代理', TELEGRAM_GROUP, 1)
        text = text[:whatsapp.end()] + block + text[whatsapp.end():]
    else:
        group_indent = re.match(r'([ \t]*)-', text[groups.end():])
        pad = group_indent.group(1) if group_indent else '  '
        block = (pad + '- name: ' + TELEGRAM_GROUP + '\n' + pad + '  type: select\n'
                 + pad + '  proxies:\n' + pad + '    - 数据屏代理\n' + pad + '    - DIRECT\n')
        text = text[:groups.end()] + block + text[groups.end():]
    rules = re.search(r'^rules:[ \t]*\n', text, re.M)
    indent = re.match(r'([ \t]*)-', text[rules.end():])
    pad = indent.group(1) if indent else '  '
    lines = ''.join(pad + '- ' + rule + '\n' for rule in telegram_rule_lines())
    return text[:rules.end()] + lines + text[rules.end():], True

def read_access():
    """各服务是否允许外网访问。没有文件=保持原行为（允许）；文件损坏=安全起见一律关闭。"""
    try:
        raw = json.loads((ROOT / 'access.json').read_text())
        if not isinstance(raw, dict):
            return {s: False for s in SERVICES}
        values = {}
        for service in SERVICES:
            item = raw.get(service, {})
            value = item.get('wan', True) if isinstance(item, dict) else None
            values[service] = value if isinstance(value, bool) else False
        return values
    except FileNotFoundError:
        return {s: True for s in SERVICES}
    except Exception:
        return {s: False for s in SERVICES}

def write_access(values):
    with LOCK:
        p = ROOT / 'access.json'
        tmp = p.with_suffix('.json.tmp')
        tmp.write_text(json.dumps({s: {'wan': bool(values[s])} for s in SERVICES}, indent=2))
        os.chmod(tmp, 0o644)  # 不含密钥；转发容器以只读方式读取
        tmp.replace(p)

def valid_host(host):
    return not host or (len(host) <= 253 and re.fullmatch(r'[a-zA-Z0-9.:-]+', host))

def api(path, method='GET', data=None, timeout=12):
    secret = (ROOT / 'controller.secret').read_text().strip()
    req = urllib.request.Request('http://mihomo:9090' + path,
        data=json.dumps(data).encode() if data is not None else None,
        headers={'Authorization': 'Bearer ' + secret, 'Content-Type': 'application/json'}, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as response:
        return json.loads(response.read() or '{}')

def quoted(value):
    return urllib.parse.quote(value, safe='')

def backup(path):
    dest = ROOT / 'backups' / (time.strftime('%Y%m%d-%H%M%S') + '-' + str(time.time_ns()) + '-' + path.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    os.chmod(dest, 0o600)
    return dest

def managed_config(text):
    # Remove the full top-level value, including indented lists/maps, before adding managed values.
    keys = ['mixed-port','port','socks-port','allow-lan','bind-address','external-controller','secret','lan-allowed-ips','profile']
    for key in keys:
        text = re.sub(r'^' + re.escape(key) + r':[^\n]*(?:\n(?:[ \t]+[^\n]*|-[^\n]*|[ \t]*))*(?:\n|$)', '', text, flags=re.M)
    secret = (ROOT / 'controller.secret').read_text().strip()
    return ('mixed-port: 7890\nport: 7892\nsocks-port: 7891\nallow-lan: true\nbind-address: "*"\n'
        'external-controller: 0.0.0.0:9090\nsecret: ' + json.dumps(secret) + '\n'
        'lan-allowed-ips:\n  - 192.168.0.0/16\n  - 172.16.0.0/12\n  - 127.0.0.0/8\n'
        'profile:\n  store-selected: true\n' + text.strip() + '\n')

def save_and_reload(text):
    with LOCK:
        p = ROOT / 'config.yaml'
        saved = backup(p)
        old = p.read_text()
        tmp = p.with_suffix('.yaml.tmp')
        tmp.write_text(managed_config(text)); os.chmod(tmp, 0o600); tmp.replace(p)
        try:
            api('/configs?force=true', 'PUT', {'path':'/root/.config/mihomo/config.yaml'})
        except Exception:
            p.write_text(old)
            api('/configs?force=true', 'PUT', {'path':'/root/.config/mihomo/config.yaml'})
            raise ValueError('配置未通过内核检查，已恢复原配置')
        return str(saved)


def whatsapp_info():
    path = ROOT / 'whatsapp.json'
    info = json.loads(path.read_text()) if path.exists() else {}
    info.setdefault('server', '192.168.120.200')
    info.setdefault('chatPort', 5222)
    info.setdefault('mediaPort', 7777)
    info.setdefault('publicHost', '')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://mtproto:8766/status', timeout=3) as response:
            info['status'] = json.load(response)
        info['running'] = True
    except Exception:
        info['running'] = False
        info['status'] = {}
    return info


def status():
    proxies = api('/proxies')['proxies']
    providers = api('/providers/proxies').get('providers',{})
    for provider in providers.values():
        for node in provider.get('proxies',[]):
            proxies.setdefault(node['name'], node)
    config = api('/configs')
    connections = api('/connections')
    groups = []
    for name, item in proxies.items():
        if 'all' not in item: continue
        nodes = []
        for node in item['all']:
            obj = proxies.get(node, {})
            history = obj.get('history') or []
            nodes.append({'name':node,'type':obj.get('type',''),'delay':history[-1].get('delay') if history else None,
                'alive':obj.get('alive'),'selected':node == item.get('now')})
        groups.append({'name':name,'type':item['type'],'now':item.get('now'),'nodes':nodes})
    tg = {}
    if (ROOT / 'telegram.json').exists(): tg = json.loads((ROOT / 'telegram.json').read_text())
    wa = whatsapp_info()
    host = tg.get('publicHost')
    if host and tg.get('secret'):
        tg['externalLink'] = 'https://t.me/proxy?server=' + urllib.parse.quote(host) + '&port=' + str(tg.get('port', 8443)) + '&secret=' + tg['secret']
    values, counts = read_access(), (wa.get('status') or {}).get('access') or {}
    access = {s: {'wan': values[s], 'stats': {k: v for k, v in (counts.get(s) or {}).items() if k in ('lan', 'wan', 'blocked')}} for s in SERVICES}
    return {'version':api('/version').get('version'), 'mode':config.get('mode'), 'groups':groups,
        'providers':[{'name':k,'updatedAt':v.get('updatedAt'),'count':len(v.get('proxies',[]))} for k,v in providers.items() if v.get('vehicleType')=='HTTP'],
        'upload':connections.get('uploadTotal',0),'download':connections.get('downloadTotal',0),
        'connectionCount':len(connections.get('connections') or []),'memory':connections.get('memory',0),
        'connections':[{'id':c['id'],'host':c.get('metadata',{}).get('host') or c.get('metadata',{}).get('destinationIP'),
            'network':c.get('metadata',{}).get('network'),'chains':c.get('chains',[]),'upload':c.get('upload',0),'download':c.get('download',0)} for c in (connections.get('connections') or [])[:100]],
        'telegram':tg, 'whatsapp':wa, 'access':access}

def action(data):
    kind = data.get('action')
    if kind == 'wan-access':
        service, enabled = data.get('service'), data.get('enabled')
        if service not in SERVICES or not isinstance(enabled, bool):
            raise ValueError('参数无效')
        with LOCK:
            values = read_access()
            values[service] = enabled
            write_access(values)
    elif kind == 'telegram-group':
        with LOCK:
            text, changed = add_telegram_group((ROOT / 'config.yaml').read_text())
            if changed:
                save_and_reload(text)
        return {'ok': True, 'changed': changed}
    elif kind == 'telegram-settings':
        host = str(data.get('publicHost', '')).strip().strip('[]')
        if not valid_host(host):
            raise ValueError('请输入域名或 IP 地址，不要包含协议和端口')
        with LOCK:
            p = ROOT / 'telegram.json'
            if not p.exists():
                raise ValueError('Telegram 尚未部署')
            info = json.loads(p.read_text())
            backup(p)
            info['publicHost'] = host
            tmp = p.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(info, ensure_ascii=False, indent=2)); os.chmod(tmp, 0o600); tmp.replace(p)
    elif kind == 'whatsapp-settings':
        host = str(data.get('publicHost', '')).strip().strip('[]')
        if not valid_host(host):
            raise ValueError('请输入域名或 IP 地址，不要包含协议和端口')
        with LOCK:
            p=ROOT/'whatsapp.json'
            info=json.loads(p.read_text()) if p.exists() else {}
            if p.exists(): backup(p)
            info['publicHost']=host
            tmp=p.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(info,ensure_ascii=False,indent=2));os.chmod(tmp,0o600);tmp.replace(p)
    elif kind == 'select':
        group, node = data.get('group',''), data.get('node','')
        obj = api('/proxies/' + quoted(group))
        if obj.get('type') != 'Selector' or node not in obj.get('all',[]): raise ValueError('请选择手动策略组中的有效节点')
        api('/proxies/' + quoted(group), 'PUT', {'name':node})
    elif kind == 'delay':
        node = data.get('node','')
        if node in api('/proxies')['proxies']:
            path = '/proxies/' + quoted(node) + '/delay'
        else:
            provider = next((name for name,p in api('/providers/proxies').get('providers',{}).items() if any(n['name']==node for n in p.get('proxies',[]))),None)
            if provider is None: raise ValueError('节点不存在')
            path = '/providers/proxies/' + quoted(provider) + '/' + quoted(node) + '/healthcheck'
        return api(path + '?' + urllib.parse.urlencode({'timeout':5000,'url':'https://www.gstatic.com/generate_204'}), timeout=7)
    elif kind == 'test-all':
        obj = api('/proxies/' + quoted(data.get('group','')))
        names = obj.get('all',[])
        if not names: raise ValueError('策略组为空')
        results=api('/group/'+quoted(data.get('group',''))+'/delay?timeout=5000&url=https%3A%2F%2Fwww.gstatic.com%2Fgenerate_204',timeout=20)
        return {'tested':len(names),'available':sum(v>0 for v in results.values())}
    elif kind == 'mode':
        mode = data.get('mode')
        if mode not in ('rule','global','direct'): raise ValueError('模式无效')
        with LOCK:
            p=ROOT/'config.yaml'; text=p.read_text(); backup(p)
            text=re.sub(r'^mode:.*$', 'mode: '+mode, text, flags=re.M) if re.search(r'^mode:',text,re.M) else 'mode: '+mode+'\n'+text
            api('/configs','PATCH',{'mode':mode}); p.write_text(text)
    elif kind == 'provider-update':
        name=data.get('provider','')
        if name not in api('/providers/proxies').get('providers',{}): raise ValueError('订阅不存在')
        api('/providers/proxies/'+quoted(name),'PUT',timeout=45)
    elif kind == 'close':
        ident=data.get('id','')
        if not ident: raise ValueError('缺少连接编号')
        api('/connections/'+quoted(ident),'DELETE')
    elif kind == 'reload':
        api('/configs?force=true','PUT',{'path':'/root/.config/mihomo/config.yaml'})
    else: raise ValueError('不支持的操作')
    return {'ok':True}

def handle(handler, authorized):
    path = urllib.parse.urlparse(handler.path).path
    if not path.startswith('/admin/proxy/'): return False
    def respond(code, body):
        raw=json.dumps(body,ensure_ascii=False).encode()
        handler.send_response(code); handler.send_header('Content-Type','application/json; charset=utf-8')
        handler.send_header('Cache-Control','no-store'); handler.send_header('Content-Length',str(len(raw)))
        if code==401:handler.send_header('WWW-Authenticate','Basic realm="Kindle Dashboard Admin", charset="UTF-8"')
        handler.end_headers();handler.wfile.write(raw)
    if not authorized(handler.headers): respond(401,{'error':'请先登录后台'});return True
    try:
        if handler.command == 'GET' and path == '/admin/proxy/status': respond(200,status())
        elif handler.command == 'POST' and path == '/admin/proxy/action':
            origin=handler.headers.get('Origin','')
            if handler.headers.get('X-Requested-With')!='KindleAdmin' or (origin and urllib.parse.urlparse(origin).netloc != handler.headers.get('Host')):
                respond(403,{'error':'请求来源无效'});return True
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<length<16384:raise ValueError('请求长度无效')
            respond(200,action(json.loads(handler.rfile.read(length))))
        else:respond(404,{'error':'接口不存在'})
    except ValueError as e:respond(400,{'error':str(e)})
    except urllib.error.HTTPError as e:respond(502,{'error':'代理内核操作失败（HTTP '+str(e.code)+'），请检查节点或订阅'})
    except Exception:respond(502,{'error':'代理内核暂时不可用，请稍后刷新'})
    return True

def section():
    return r'''<section class="card" id="proxy-panel"><h2>局域网代理与节点管理</h2>
<style>#proxy-panel .toolbar{display:flex;gap:9px;flex-wrap:wrap;align-items:center;margin:12px 0}#proxy-panel select{padding:10px;border:1px solid #d7d1c3;border-radius:9px;background:white}#proxy-panel button{padding:9px 14px;font-size:13px}#proxy-panel button:disabled{opacity:.5;cursor:wait}.nodegrid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:10px}.node{padding:12px;border:1px solid #d7d1c3;border-radius:10px;background:white}.node.current{border:2px solid #348660;background:#edf7f0}.node b{display:block;overflow-wrap:anywhere}.node small{display:block;color:#6d6a61;margin:8px 0}.node .bad{color:#b93636}.node .good{color:#237847}.proxy-stats{display:flex;flex-wrap:wrap;gap:12px}.proxy-stats span{background:#efede6;padding:10px;border-radius:9px}#proxy-message{white-space:pre-wrap;margin:12px 0;color:#237847}#proxy-panel table{width:100%;font-size:13px;border-collapse:collapse}#proxy-panel td,#proxy-panel th{text-align:left;padding:8px;border-bottom:1px solid #ddd;overflow-wrap:anywhere}#proxy-panel code{overflow-wrap:anywhere}#proxy-panel .group-head{margin:22px 0 10px}#proxy-panel details{margin-top:18px}</style>
<div class="hint">同一局域网设备可使用以下代理。HTTP 支持 HTTPS CONNECT；SOCKS5 支持远程 DNS。节点切换即时生效，内核会保存选择。</div>
<div class="proxy-stats"><span>混合 HTTP / SOCKS5 <code>192.168.120.200:7890</code></span><span>SOCKS5 <code>192.168.120.200:7891</code></span><span>HTTP <code>192.168.120.200:7892</code></span></div>
<div class="toolbar"><label>运行模式 <select id="proxy-mode"><option value="rule">规则模式（按规则分流）</option><option value="global">全局代理（全部走一个出口）</option><option value="direct">全部直连（不走代理）</option></select></label><button type="button" id="proxy-refresh">刷新状态</button><button type="button" id="proxy-reload">重载配置</button><label><input id="proxy-auto" type="checkbox" checked style="width:auto"> 自动刷新</label></div>
<div id="proxy-stats" class="proxy-stats"></div><div id="proxy-message" role="status" aria-live="polite">正在读取代理状态…</div>
<div class="toolbar"><input id="proxy-search" placeholder="搜索节点名称 / 协议" style="max-width:340px"><label><input id="proxy-sort" type="checkbox" style="width:auto"> 按延迟排序</label></div>
<div id="proxy-groups"></div><div id="proxy-providers"></div><div id="proxy-telegram"></div><div id="proxy-whatsapp"></div>
<p class="hint"><a href="https://github.com/Viper-Boss/kindle-dashboard-nas" target="_blank" rel="noopener">项目源码（AGPL-3.0）</a></p><details><summary>活动连接</summary><div style="overflow:auto"><table><thead><tr><th>目标</th><th>协议 / 路径</th><th>上传 / 下载</th><th></th></tr></thead><tbody id="proxy-connections"></tbody></table></div></details></section>
<script>(()=>{'use strict';
const GROUP_INFO={'数据屏代理':['默认出口','没有匹配到其他规则的流量都从这里出去，数据屏的行情/用量请求也走它。'],'Telegram代理':['Telegram 出口','只处理 Telegram 服务器地址的流量（含本 NAS 的 Telegram 代理转发）；默认跟随“默认出口”，想单独指定线路时在这里选。'],'WhatsApp代理':['WhatsApp 出口','只处理 whatsapp.net 的流量；默认跟随“默认出口”，想单独指定线路时在这里选。'],'自动选择':['自动测速','内核定时测速，自动挑选最快的节点，不需要手动切换。'],'GLOBAL':['全局模式出口','仅在运行模式为“全局代理”时才使用；规则模式下这一组不起作用。']};
const TYPE_NAME={Selector:'手动选择',URLTest:'自动测速',Fallback:'故障转移',LoadBalance:'负载均衡',Relay:'链式代理',Direct:'直连',Reject:'拦截'};
const typeName=t=>TYPE_NAME[t]||t;
const groupInfo=g=>{const k=GROUP_INFO[g.name];return k?{title:k[0],desc:k[1]}:{title:g.name,desc:typeName(g.type)+'策略组'}};
window.kindleGroupInfo=groupInfo;window.kindleTypeName=typeName;
let state=null,last=null,busy=0;const $=id=>document.getElementById(id),esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])),bytes=n=>{n=Number(n)||0;const u=['B','KB','MB','GB'];let i=0;while(n>=1024&&i<3){n/=1024;i++}return n.toFixed(i?1:0)+' '+u[i]};
async function call(path,data){const r=await fetch('/admin/proxy/'+path,{credentials:'same-origin',cache:'no-store',...(data?{method:'POST',headers:{'Content-Type':'application/json','X-Requested-With':'KindleAdmin'},body:JSON.stringify(data)}:{})});const j=await r.json();if(!r.ok)throw Error(j.error||'请求失败');return j}
function message(text,bad=false){$('proxy-message').textContent=text;$('proxy-message').style.color=bad?'#b93636':'#237847'}
function render(){if(!state)return;const search=$('proxy-search').value.toLowerCase();$('proxy-groups').innerHTML='';for(const g of state.groups){const wrap=document.createElement('div');const info=groupInfo(g);wrap.dataset.group=g.name;wrap.innerHTML='<div class="group-bar"><div><h3 class="group-head">'+esc(info.title)+' <small>'+esc(g.name)+' · '+esc(typeName(g.type))+'</small></h3><p class="hint">'+esc(info.desc)+'</p><p class="hint">当前使用：<b>'+esc(g.now)+'</b>'+(g.type==='Selector'?' · 点击下方节点即可切换':' · 由内核按测速结果自动选择，无需手动切换')+'</p></div></div>';const test=document.createElement('button');test.type='button';test.textContent='测试本组延迟';test.disabled=busy>0;test.onclick=()=>run({action:'test-all',group:g.name},test);wrap.querySelector('.group-bar').append(test);const grid=document.createElement('div');grid.className='nodegrid';grid.style.marginTop='12px';let nodes=g.nodes.filter(n=>(n.name+' '+n.type).toLowerCase().includes(search));if($('proxy-sort').checked)nodes.sort((a,b)=>(a.delay>0?a.delay:Infinity)-(b.delay>0?b.delay:Infinity));for(const n of nodes){const card=document.createElement('div');card.className='node'+(n.selected?' current':'');const delay=n.delay==null?'未测试':n.delay>0?n.delay+' ms':'超时 / 不可用';const cls=n.delay>0?(n.delay<150?'good':n.delay<300?'mid':'slow'):n.delay===0?'bad':'',bar=n.delay>0?Math.max(8,Math.min(100,100-n.delay/5)):0;card.innerHTML='<div class="node-head"><b>'+esc(n.name)+'</b>'+(n.selected?'<span class="node-check" title="当前使用">✓</span>':'')+'</div><div class="node-meta"><span class="chip">'+esc(n.type)+'</span><span class="lat-text '+cls+'">'+esc(delay)+'</span></div><div class="lat"><i class="'+cls+'" style="width:'+bar+'%"></i></div>';const act=document.createElement('div');act.className='node-actions';if(g.type==='Selector'){const b=document.createElement('button');b.type='button';b.className='btn-use';b.textContent=n.selected?'使用中':'使用此节点';b.disabled=n.selected||busy>0;b.onclick=()=>run({action:'select',group:g.name,node:n.name},b);act.append(b)}const t=document.createElement('button');t.type='button';t.textContent='测延迟';t.disabled=busy>0;t.onclick=()=>run({action:'delay',node:n.name},t);act.append(t);card.append(act);grid.append(card)}wrap.append(grid);$('proxy-groups').append(wrap)}
$('proxy-providers').innerHTML='';for(const p of state.providers){const row=document.createElement('div');row.className='toolbar';row.innerHTML='<span>订阅 '+esc(p.name)+' · '+p.count+' 节点 · 更新于 '+esc(p.updatedAt?new Date(p.updatedAt).toLocaleString():'未知')+'</span>';const b=document.createElement('button');b.type='button';b.textContent='更新订阅';b.disabled=busy>0;b.onclick=()=>run({action:'provider-update',provider:p.name},b);row.append(b);$('proxy-providers').append(row)}
const acc=state.access||{},gnow=n=>{const g=state.groups.find(x=>x.name===n);return g?g.now:null};
const wanRow=k=>{const a=acc[k]||{},st=a.stats||{};return '<div class="wan-row"><div><b>允许外网访问</b><p class="hint">'+(a.wan?'已开启：外网设备也能连接（还需要路由器转发对应端口）。':'已关闭：只接受局域网设备，来自外网的连接会被直接拒绝。')+'</p><p class="hint">已接入：局域网 '+(st.lan||0)+' 次 · 外网 '+(st.wan||0)+' 次 · 已拒绝 '+(st.blocked||0)+' 次（自转发进程启动起）</p></div><label class="switch" title="允许外网访问"><input type="checkbox" data-wan="'+k+'"'+(a.wan?' checked':'')+'><i></i></label></div>'};
const egressRow=(n,label,extra)=>{const now=gnow(n);return '<div class="egress-row"><div><b>出口线路</b><p class="hint">'+(now?label+'当前使用：<b>'+esc(now)+'</b> · <a href="#nodes">去选择</a>':'目前和“默认出口”共用线路。')+'</p></div>'+(now?'':extra)+'</div>'};
const tg=state.telegram;$('proxy-telegram').innerHTML=tg.link?'<h3>Telegram 代理</h3><p>局域网地址：<code>'+esc(tg.server)+':'+esc(tg.port)+'</code></p><p><a class="linkbtn" href="'+esc(tg.link)+'" target="_blank" rel="noopener">添加到 Telegram（局域网）</a>'+(tg.externalLink?' <a class="linkbtn" href="'+esc(tg.externalLink)+'" target="_blank" rel="noopener">添加到 Telegram（外网）</a>':'')+'</p><p class="hint">密钥：<code>'+esc(tg.secret)+'</code> · '+esc(tg.note||'')+'</p>'+egressRow('Telegram代理','Telegram 出口','<button type="button" id="tg-group-add">单独设置出口</button>')+wanRow('telegram')+'<p>外网主机：<code>'+esc(tg.publicHost||'尚未设置')+'</code></p><details><summary>修改外网地址</summary><div class="toolbar"><input id="tg-host" placeholder="公网域名或 IP，不含端口" style="max-width:440px"><button type="button" id="tg-host-save">保存外网地址</button></div></details>':'<h3>Telegram 代理</h3><p class="hint">Telegram MTProto 尚未部署。</p>';
const tgHost=$('tg-host');if(tgHost){tgHost.value=tg.publicHost||'';$('tg-host-save').onclick=()=>run({action:'telegram-settings',publicHost:tgHost.value},$('tg-host-save'))}
const tgAdd=$('tg-group-add');if(tgAdd)tgAdd.onclick=()=>run({action:'telegram-group'},tgAdd);

const wa=state.whatsapp||{},ws=wa.status||{};
$('proxy-whatsapp').innerHTML='<h3>WhatsApp 代理</h3><p><b>'+(wa.running?'运行中':'暂时不可用')+'</b> · 活动连接 '+(ws.active||0)+' · 成功连接 '+(ws.upstreamConnected||0)+'</p><p>局域网主机：<code>'+esc(wa.server)+'</code> · 聊天端口 <code>'+esc(wa.chatPort)+'</code> · 媒体端口 <code>'+esc(wa.mediaPort)+'</code></p><p>外网主机：<code>'+esc(wa.publicHost||'尚未设置')+'</code></p>'+egressRow('WhatsApp代理','WhatsApp 出口','')+wanRow('whatsapp')+'<p class="hint">WhatsApp → 设置 → 存储和数据 → 代理 → 设置代理；填写主机并在高级设置填写聊天和媒体端口，无需用户名、密码。仅代理聊天与媒体，不代理语音/视频通话。外网 IPv4 需要路由器转发两个 TCP 端口，IPv6 需要客户端支持 IPv6 并放行路由器防火墙。</p><details><summary>修改外网地址</summary><div class="toolbar"><input id="wa-host" placeholder="公网域名或 IP，不含端口" style="max-width:440px"><button type="button" id="wa-host-save">保存外网地址</button></div></details>'+(ws.lastError?'<p class="hint">最近连接错误：'+esc(ws.lastError)+'</p>':'');
$('wa-host').value=wa.publicHost||'';$('wa-host-save').onclick=()=>run({action:'whatsapp-settings',publicHost:$('wa-host').value},$('wa-host-save'));
for(const el of document.querySelectorAll('[data-wan]'))el.onchange=()=>run({action:'wan-access',service:el.dataset.wan,enabled:el.checked},el);

$('proxy-connections').innerHTML='';for(const c of state.connections){const tr=document.createElement('tr');tr.innerHTML='<td>'+esc(c.host)+'</td><td>'+esc(c.network)+' / '+esc(c.chains.join(' → '))+'</td><td>'+bytes(c.upload)+' / '+bytes(c.download)+'</td>';const td=document.createElement('td'),b=document.createElement('button');b.type='button';b.textContent='断开';b.onclick=()=>run({action:'close',id:c.id},b);td.append(b);tr.append(td);$('proxy-connections').append(tr)}}
async function refresh(silent=false,force=false){if(silent&&!force&&(busy||document.querySelector('#proxy-telegram details[open],#proxy-whatsapp details[open]')))return;try{state=await call('status');window.dispatchEvent(new CustomEvent('kindle:proxy-state',{detail:state}));$('proxy-mode').value=state.mode;const now=Date.now(),dt=last?(now-last.time)/1000:0,up=dt?Math.max(0,state.upload-last.up)/dt:0,down=dt?Math.max(0,state.download-last.down)/dt:0;last={time:now,up:state.upload,down:state.download};$('proxy-stats').innerHTML='<span>内核 '+esc(state.version)+'</span><span>连接 '+state.connectionCount+'</span><span>上传 '+bytes(state.upload)+' · '+bytes(up)+'/s</span><span>下载 '+bytes(state.download)+' · '+bytes(down)+'/s</span>';render();if(!silent)message('状态已更新 · '+new Date().toLocaleTimeString())}catch(e){message(e.message,true)}}
async function run(data,button){busy++;if(button)button.disabled=true;message('正在执行，请稍候…');try{const result=await call('action',data);await refresh(true,true);message(result.delay!==undefined?'延迟 '+result.delay+' ms':result.tested!==undefined?'已测试 '+result.tested+' 个节点，可用 '+result.available+' 个':'操作成功')}catch(e){message(e.message,true)}finally{busy--;if(button)button.disabled=false;render()}}
$('proxy-mode').onchange=()=>run({action:'mode',mode:$('proxy-mode').value},$('proxy-mode'));$('proxy-refresh').onclick=()=>refresh();$('proxy-reload').onclick=()=>run({action:'reload'},$('proxy-reload'));$('proxy-search').oninput=render;$('proxy-sort').onchange=render;refresh();setInterval(()=>{if($('proxy-auto').checked&&!document.hidden)refresh(true)},5000);
})();</script>'''
