"""HTML sign-in with revocable, opaque sessions; no browser authentication challenge."""
from contextlib import contextmanager
import hashlib
import hmac
from http.cookies import SimpleCookie, CookieError
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import urllib.parse

DB_PATH = Path(os.getenv('AUTH_DB', '/data/auth.sqlite3'))
COOKIE = 'kindle_session'
PASSWORD_ROUNDS = 600_000


def account_record(db=None):
    if db is None:
        with database() as connection:
            return account_record(connection)
    row = db.execute('SELECT username,salt,digest,rounds,revision FROM account WHERE id=1').fetchone()
    return dict(zip(('username','salt','digest','rounds','revision'),row)) if row else None


def credentials_configured():
    return bool(account_record()) or os.getenv('ADMIN_PASSWORD','') not in ('','change-me')


def credentials_valid(username, password, db=None):
    if not isinstance(username,str) or not isinstance(password,str):
        return False
    saved = account_record(db)
    if saved:
        digest = hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(saved['salt']),saved['rounds']).hex()
        return (hmac.compare_digest(username.encode(),saved['username'].encode())
                & hmac.compare_digest(digest,saved['digest']))
    expected = os.getenv('ADMIN_PASSWORD', '')
    if expected in ('', 'change-me') or not isinstance(username, str) or not isinstance(password, str):
        return False
    return (hmac.compare_digest(username.encode(), os.getenv('ADMIN_USERNAME', 'admin').encode())
            & hmac.compare_digest(password.encode(), expected.encode()))


def fingerprint(db=None):
    saved = account_record(db)
    if saved:
        return saved['revision']
    return hashlib.sha256((os.getenv('ADMIN_USERNAME', 'admin')+'\0'+os.getenv('ADMIN_PASSWORD', '')).encode()).hexdigest()


@contextmanager
def database():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=10)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, username TEXT, fingerprint TEXT, expires INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS attempts(peer TEXT PRIMARY KEY, started INTEGER, failures INTEGER)')
        db.execute('CREATE TABLE IF NOT EXISTS account(id INTEGER PRIMARY KEY CHECK(id=1),username TEXT,salt TEXT,digest TEXT,rounds INTEGER,revision TEXT)')
        os.chmod(DB_PATH, 0o600)
        with db:
            yield db
    finally:
        db.close()


def token_from(headers):
    try:
        jar = SimpleCookie(); jar.load(headers.get('Cookie', ''))
        value = jar[COOKIE].value if COOKIE in jar else ''
        return value if len(value) == 43 and all(c.isalnum() or c in '-_' for c in value) else ''
    except CookieError:
        return ''


def session_user(headers):
    token = token_from(headers)
    if not token:
        return None
    with database() as db:
        row = db.execute('SELECT username,expires,fingerprint FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
    if row and row[1] > time.time() and hmac.compare_digest(row[2], fingerprint()):
        return {'username':row[0], 'expiresAt':row[1]}
    return None


def login(username, password, peer, remember=False):
    now = int(time.time())
    with database() as db:
        db.execute('DELETE FROM attempts WHERE started<?', (now-900,))
        attempt = db.execute('SELECT failures FROM attempts WHERE peer=?', (peer,)).fetchone()
        if attempt and attempt[0] >= 10:
            return None, 429
        if not credentials_valid(username, password, db):
            db.execute('INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(peer) DO UPDATE SET failures=failures+1', (peer,now))
            return None, 401
        db.execute('DELETE FROM attempts WHERE peer=?', (peer,))
        db.execute('DELETE FROM sessions WHERE expires<=?', (now,))
        token = secrets.token_urlsafe(32); age = 7*86400 if remember else 12*3600
        db.execute('INSERT INTO sessions VALUES (?,?,?,?)', (hashlib.sha256(token.encode()).hexdigest(),username,fingerprint(db),now+age))
        # Retain a bounded number of devices for this single administrator.
        db.execute('DELETE FROM sessions WHERE token NOT IN (SELECT token FROM sessions ORDER BY expires DESC,rowid DESC LIMIT 32)')
    return (token, age), 200


def change_account(username, current_password, new_password, confirmation, peer):
    if (not isinstance(username,str) or not 1<=len(username)<=64
            or any(c.isspace() or ord(c)<32 or ord(c)==127 or c==':' for c in username)):
        raise ValueError('用户名为 1–64 个字符，不能包含空格或冒号。')
    if not all(isinstance(x,str) for x in (current_password,new_password,confirmation)):
        raise ValueError('请输入有效的密码。')
    if new_password != confirmation:
        raise ValueError('两次输入的新密码不一致。')
    if new_password and not 12<=len(new_password)<=128:
        raise ValueError('新密码需要 12–128 个字符。')
    now=int(time.time())
    with database() as db:
        db.execute('DELETE FROM attempts WHERE started<?',(now-900,))
        failed=db.execute('SELECT failures FROM attempts WHERE peer=?',(peer,)).fetchone()
        if failed and failed[0]>=10:return 429
        if not credentials_valid((account_record(db) or {}).get('username',os.getenv('ADMIN_USERNAME','admin')),current_password,db):
            db.execute('INSERT INTO attempts VALUES (?,?,1) ON CONFLICT(peer) DO UPDATE SET failures=failures+1',(peer,now))
            return 403
        salt=secrets.token_bytes(16)
        digest=hashlib.pbkdf2_hmac('sha256',(new_password or current_password).encode(),salt,PASSWORD_ROUNDS).hex()
        db.execute('INSERT OR REPLACE INTO account VALUES (1,?,?,?,?,?)',(username,salt.hex(),digest,PASSWORD_ROUNDS,secrets.token_hex(32)))
        db.execute('DELETE FROM sessions');db.execute('DELETE FROM attempts')
    return 200


def export_account(destination):
    # Keep the account hash in private migrations, while requiring a fresh login.
    with database() as source, sqlite3.connect(destination) as target:
        source.backup(target)
        target.execute('DELETE FROM sessions');target.execute('DELETE FROM attempts')
        target.commit();target.execute('VACUUM')


def reset_to_environment():
    with database() as db:
        db.execute('DELETE FROM account');db.execute('DELETE FROM sessions');db.execute('DELETE FROM attempts')


def logout(headers):
    token = token_from(headers)
    if token:
        with database() as db:
            db.execute('DELETE FROM sessions WHERE token=?', (hashlib.sha256(token.encode()).hexdigest(),))


def cookie_header(token='', age=0):
    secure = '; Secure' if os.getenv('COOKIE_SECURE') == '1' else ''
    return f'{COOKIE}={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Strict'+secure


def safe_next(value):
    return value if isinstance(value, str) and (value == '/admin' or value.startswith('/admin#')) else '/admin'


def redirect(handler, target):
    handler.send_response(303); handler.send_header('Location', target)
    handler.send_header('Cache-Control', 'no-store'); handler.send_header('Content-Length', '0'); handler.end_headers()


def handle(handler):
    path = urllib.parse.urlparse(handler.path).path
    if path not in ('/login','/auth/login','/auth/logout','/auth/session','/auth/account'):
        return False
    def respond(code, value, cookie=None):
        raw = json.dumps(value, ensure_ascii=False).encode()
        handler.send_response(code); handler.send_header('Content-Type','application/json; charset=utf-8')
        handler.send_header('Cache-Control','no-store'); handler.send_header('Content-Length',str(len(raw)))
        if cookie: handler.send_header('Set-Cookie',cookie)
        handler.end_headers(); handler.wfile.write(raw)
    try:
        if handler.command == 'GET' and path == '/login':
            if session_user(handler.headers):
                redirect(handler,'/admin'); return True
            raw = LOGIN_PAGE.encode()
            handler.send_response(200); handler.send_header('Content-Type','text/html; charset=utf-8')
            handler.send_header('Cache-Control','no-store'); handler.send_header('Content-Length',str(len(raw)))
            handler.send_header('X-Frame-Options','DENY'); handler.send_header('Referrer-Policy','same-origin')
            handler.end_headers(); handler.wfile.write(raw); return True
        if handler.command == 'GET' and path == '/auth/session':
            user = session_user(handler.headers); respond(200 if user else 401,user or {'error':'请先登录'}); return True
        if handler.command != 'POST' or path not in ('/auth/login','/auth/logout','/auth/account'):
            respond(404,{'error':'接口不存在'}); return True
        origin = handler.headers.get('Origin','')
        if (handler.headers.get('X-Requested-With') != 'KindleAdmin'
                or (origin and urllib.parse.urlparse(origin).netloc != handler.headers.get('Host',''))):
            respond(403,{'error':'请求来源无效'}); return True
        if path == '/auth/logout':
            logout(handler.headers); respond(200,{'ok':True},cookie_header()); return True
        length = int(handler.headers.get('Content-Length','0'))
        if not 0 < length <= 4096: raise ValueError('请求过大')
        data = json.loads(handler.rfile.read(length))
        if not isinstance(data,dict) or type(data.get('remember',False)) is not bool: raise ValueError('请求格式无效')
        if path == '/auth/account':
            if not session_user(handler.headers):
                respond(401,{'error':'请先登录'});return True
            code=change_account(data.get('username'),data.get('currentPassword'),data.get('newPassword',''),data.get('confirmation',''),handler.client_address[0])
            if code!=200:
                respond(code,{'error':'当前密码不正确。' if code==403 else '尝试次数过多，请在 15 分钟后重试。'})
            else:
                respond(200,{'ok':True,'reauthenticate':True},cookie_header())
            return True
        session, code = login(data.get('username'),data.get('password'),handler.client_address[0],data.get('remember',False))
        if not session:
            respond(code,{'error':'尝试次数过多，请在 15 分钟后重试。' if code == 429 else '用户名或密码不正确，请重新输入。'})
        else:
            respond(200,{'ok':True,'next':safe_next(data.get('next'))},cookie_header(*session))
    except (ValueError,TypeError,KeyError) as error:
        respond(400,{'error':str(error) if path=='/auth/account' else '请输入有效的用户名和密码。'})
    except Exception:
        respond(503,{'error':'登录服务暂不可用，请稍后重试。'})
    return True


LOGIN_PAGE = r'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录 · Kindle 工作台</title><style>
*{box-sizing:border-box}body{margin:0;color:#172b42;background:#f1f5f9;font-family:system-ui,-apple-system,"Microsoft YaHei",sans-serif}main{min-height:100svh;display:grid;grid-template-columns:1.05fr 1fr;max-width:1320px;margin:auto;padding:64px;gap:90px;align-items:center}.brand{display:flex;align-items:center;gap:12px;font-size:20px;font-weight:650;letter-spacing:-.4px}.brand svg{width:38px;height:38px;padding:8px;border-radius:11px;background:#0f3341;color:#58d1cb}.intro h1{font-size:clamp(36px,4.5vw,56px);font-weight:550;line-height:1.18;letter-spacing:-2px;margin:65px 0 24px}.intro p{color:#61758d;font-size:16px;line-height:1.9;max-width:380px}.features{display:flex;gap:22px;margin-top:45px;font-size:12px;color:#61758d}.features span::before{content:'';display:inline-block;width:5px;height:5px;border-radius:50%;background:#149c9a;margin:0 7px 2px 0}.panel{width:100%;max-width:410px;background:white;border:1px solid #dce5ef;border-radius:20px;padding:36px;box-shadow:0 12px 45px #17384e07}.eyebrow{font-size:11px;letter-spacing:2px;color:#0a8b8a;font-weight:650}h2{font-size:26px;font-weight:550;letter-spacing:-.7px;margin:12px 0 8px}.sub{font-size:13px;color:#718197;margin:0 0 30px;line-height:1.7}label.field{display:block;font-size:13px;font-weight:550;margin:19px 0 8px}input:not([type=checkbox]){font:inherit;font-size:15px;width:100%;height:46px;border:1px solid #cbd8e7;border-radius:9px;padding:0 13px;color:#172b42;background:#fff;outline:none}input:focus{border-color:#159b9c;box-shadow:0 0 0 3px #159b9c1a}.password{position:relative}.password input{padding-right:62px}.reveal{position:absolute;right:5px;top:5px;height:36px;padding:0 9px;border:0;background:transparent;color:#61758d;font-size:12px;cursor:pointer}.remember{display:flex;align-items:center;gap:8px;color:#61758d;font-size:13px;margin:20px 0 24px}.remember input{accent-color:#0b9595;width:15px;height:15px}.submit{width:100%;height:46px;background:#108f90;color:#fff;border:1px solid #108f90;border-radius:9px;font-size:15px;font-weight:550;cursor:pointer}.submit:hover{background:#087e80}.submit:disabled{opacity:.65;cursor:wait}.error{font-size:13px;line-height:1.7;padding:10px 12px;background:#fff1f2;color:#b64552;border-radius:8px;margin:18px 0 0}.error[hidden]{display:none}.note{font-size:11px;color:#8b99aa;line-height:1.8;text-align:center;margin:24px 0 0}a{color:#5f748c;text-decoration:none}.footer{font-size:11px;color:#8b99aa;margin-top:60px}button:focus-visible,a:focus-visible{outline:3px solid #71c8c8;outline-offset:3px}@media(max-width:850px){main{grid-template-columns:1fr;gap:34px;padding:36px 22px;max-width:450px}.intro h1,.intro p,.features,.footer{display:none}.panel{padding:28px;max-width:none}.brand{font-size:18px}.intro{padding:0 5px}}@media(prefers-reduced-motion:no-preference){.submit{transition:background .15s}}
</style></head><body><main><section class="intro"><div class="brand"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="5" y="2" width="14" height="20" rx="2"/><path d="M9 7h6M9 11h6M11 18h2"/></svg>Kindle 工作台</div><h1>让网络有序，<br>让连接清晰。</h1><p>管理你的家庭代理与数据屏。线路、设备和流量，集中在一个安静的工作空间。</p><div class="features"><span>线路管理</span><span>流量历史</span><span>组件更新</span></div><div class="footer">PRIVATE WORKSPACE · YOUR NAS</div></section><section class="panel" aria-labelledby="login-title"><span class="eyebrow">WELCOME BACK</span><h2 id="login-title">登录工作台</h2><p class="sub">使用此 NAS 的后台账号继续。</p><form id="login-form"><label class="field" for="username">用户名</label><input id="username" name="username" autocomplete="username" required maxlength="200" autofocus><label class="field" for="password">密码</label><div class="password"><input id="password" name="password" type="password" autocomplete="current-password" required maxlength="1000"><button class="reveal" id="reveal" type="button" aria-label="显示密码" aria-pressed="false">显示</button></div><label class="remember"><input type="checkbox" id="remember">7 天内保持登录</label><button class="submit" id="submit" type="submit">登录</button><p class="error" id="error" hidden role="alert"></p></form><p class="note">登录信息仅用于此 NAS 的管理后台。<br><a href="/">返回数据屏首页 ↗</a></p></section></main><script>
const form=document.getElementById('login-form'),error=document.getElementById('error'),button=document.getElementById('submit'),password=document.getElementById('password'),reveal=document.getElementById('reveal');
reveal.onclick=()=>{const show=password.type==='password';password.type=show?'text':'password';reveal.textContent=show?'隐藏':'显示';reveal.setAttribute('aria-label',show?'隐藏密码':'显示密码');reveal.setAttribute('aria-pressed',String(show))};
form.onsubmit=async event=>{event.preventDefault();error.hidden=true;button.disabled=true;button.textContent='正在登录…';try{const next=new URLSearchParams(location.search).get('next')||('/admin'+location.hash);const response=await fetch('/auth/login',{method:'POST',credentials:'same-origin',headers:{'Content-Type':'application/json','X-Requested-With':'KindleAdmin'},body:JSON.stringify({username:form.username.value,password:password.value,remember:document.getElementById('remember').checked,next})});const data=await response.json();if(!response.ok)throw Error(data.error||'登录未完成');location.replace(data.next)}catch(e){error.textContent=e.message||'网络连接异常，请重试。';error.hidden=false;password.focus()}finally{button.disabled=false;button.textContent='登录'}};
</script></body></html>'''
