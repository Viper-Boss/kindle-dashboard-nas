import http.client
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
import web_auth
import maintenance


class WebLoginTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'auth.sqlite3'
        self.env=patch.dict(os.environ,{'ADMIN_USERNAME':'管理员','ADMIN_PASSWORD':'test-password-秘密'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.db=patch.object(web_auth,'DB_PATH',self.path); self.db.start(); self.addCleanup(self.db.stop)
        self.maintenance=patch.object(maintenance,'DATA_ROOT',Path(self.temp.name)/'maintenance-data')
        self.maintenance.start();self.addCleanup(self.maintenance.stop)

    def test_cookie_session_survives_reopen_but_is_revoked_at_logout(self):
        session,code=web_auth.login('管理员','test-password-秘密','192.168.1.5',True)
        self.assertEqual(code,200); token,age=session
        self.assertEqual(age,7*86400)
        headers={'Cookie':web_auth.COOKIE+'='+token}
        self.assertEqual(web_auth.session_user(headers)['username'],'管理员')
        with sqlite3.connect(self.path) as db:
            saved=db.execute('SELECT token FROM sessions').fetchone()[0]
        self.assertNotEqual(saved,token);self.assertEqual(len(saved),64)
        web_auth.logout(headers); self.assertIsNone(web_auth.session_user(headers))
        cookie=web_auth.cookie_header(token,age)
        self.assertIn('HttpOnly',cookie); self.assertIn('SameSite=Strict',cookie)

    def test_expiry_and_password_change_invalidate_sessions(self):
        session,_=web_auth.login('管理员','test-password-秘密','192.168.1.5')
        headers={'Cookie':web_auth.COOKIE+'='+session[0]}
        with patch.dict(os.environ,{'ADMIN_PASSWORD':'a-new-password'}):
            self.assertIsNone(web_auth.session_user(headers))
        with patch.object(web_auth.time,'time',return_value=time.time()+13*3600):
            self.assertIsNone(web_auth.session_user(headers))

    def test_failed_logins_are_rate_limited(self):
        for _ in range(10):
            self.assertEqual(web_auth.login('管理员','wrong','192.168.1.5')[1],401)
        self.assertEqual(web_auth.login('管理员','test-password-秘密','192.168.1.5')[1],429)
        self.assertEqual(web_auth.login('管理员','test-password-秘密','192.168.1.6')[1],200)
        with patch.object(web_auth.time,'time',return_value=time.time()+901):
            self.assertEqual(web_auth.login('管理员','test-password-秘密','192.168.1.5')[1],200)

    def test_account_change_rejects_wrong_current_password_and_revokes_old_sessions(self):
        session,_=web_auth.login('管理员','test-password-秘密','192.168.1.5')
        headers={'Cookie':web_auth.COOKIE+'='+session[0]}
        self.assertEqual(web_auth.change_account('新管理员','wrong','new-password-12345','new-password-12345','192.168.1.5'),403)
        self.assertIsNotNone(web_auth.session_user(headers))
        self.assertEqual(web_auth.change_account('新管理员','test-password-秘密','new-password-12345','new-password-12345','192.168.1.5'),200)
        self.assertIsNone(web_auth.session_user(headers))
        self.assertFalse(web_auth.credentials_valid('管理员','test-password-秘密'))
        self.assertTrue(web_auth.credentials_valid('新管理员','new-password-12345'))
        with sqlite3.connect(self.path) as db:
            saved=db.execute('SELECT digest,salt FROM account').fetchone()
        self.assertNotIn('new-password',repr(saved))

    def test_username_only_change_preserves_password_and_private_export_discards_sessions(self):
        self.assertEqual(web_auth.change_account('new-admin','test-password-秘密','','','local'),200)
        self.assertTrue(web_auth.credentials_valid('new-admin','test-password-秘密'))
        web_auth.login('new-admin','test-password-秘密','local')
        destination=Path(self.temp.name)/'migration.sqlite3';web_auth.export_account(destination)
        with sqlite3.connect(destination) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM sessions').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT username FROM account').fetchone()[0],'new-admin')
        web_auth.reset_to_environment()
        self.assertTrue(web_auth.credentials_valid('管理员','test-password-秘密'))
        self.assertFalse(web_auth.credentials_valid('new-admin','test-password-秘密'))

    def test_account_validation_does_not_change_credentials(self):
        for username,password,confirmation in [('bad name','long-password-123','long-password-123'),('admin','short','short'),('admin','long-password-123','different')]:
            with self.assertRaises(ValueError):web_auth.change_account(username,'test-password-秘密',password,confirmation,'local')
        self.assertTrue(web_auth.credentials_valid('管理员','test-password-秘密'))

    def test_redirect_targets_cannot_leave_admin(self):
        for target in ['https://evil.example','//evil.example','/admin/../other','/admin?next=//evil']:
            self.assertEqual(web_auth.safe_next(target),'/admin')
        self.assertEqual(web_auth.safe_next('/admin#traffic'),'/admin#traffic')

    def test_latest_login_is_not_evicted_when_sessions_share_timestamp(self):
        for _ in range(34):
            session,code=web_auth.login('管理员','test-password-秘密','192.168.1.5')
            self.assertEqual(code,200)
        self.assertIsNotNone(web_auth.session_user({'Cookie':web_auth.COOKIE+'='+session[0]}))

    def test_html_login_api_and_logout_without_browser_authentication_dialog(self):
        class QuietHandler(app.Handler):
            def log_message(self,*args):pass
        server=app.ThreadingHTTPServer(('127.0.0.1',0),QuietHandler)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        host='127.0.0.1:'+str(server.server_port)
        def request(method,path,data=None,headers=None):
            c=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=5)
            raw=json.dumps(data).encode() if data is not None else None
            c.request(method,path,raw,headers or {});r=c.getresponse();body=r.read();result=(r.status,dict(r.headers),body);c.close();return result
        status,headers,_=request('GET','/admin')
        self.assertEqual(status,303);self.assertTrue(headers['Location'].startswith('/login'))
        self.assertNotIn('WWW-Authenticate',headers)
        status,headers,body=request('GET','/login')
        self.assertEqual(status,200);self.assertIn(b'autocomplete="current-password"',body)
        self.assertNotIn('test-password'.encode(),body)
        status,headers,_=request('GET','/admin/traffic/status')
        self.assertEqual(status,401);self.assertNotIn('WWW-Authenticate',headers)
        login={'username':'管理员','password':'test-password-秘密','next':'/admin#traffic'}
        h={'X-Requested-With':'KindleAdmin','Origin':'http://evil.example'}
        self.assertEqual(request('POST','/auth/login',login,h)[0],403)
        h['Origin']='http://'+host
        status,headers,body=request('POST','/auth/login',login,h)
        self.assertEqual(status,200);self.assertEqual(json.loads(body)['next'],'/admin#traffic')
        cookie=headers['Set-Cookie'].split(';')[0]
        status,_,body=request('GET','/admin',headers={'Cookie':cookie})
        self.assertEqual(status,200);self.assertIn('退出登录'.encode(),body)
        h['Cookie']=cookie
        account={'username':'new-admin','currentPassword':'wrong','newPassword':'new-password-123','confirmation':'new-password-123'}
        self.assertEqual(request('POST','/auth/account',account,h)[0],403)
        account['currentPassword']='test-password-秘密'
        self.assertEqual(request('POST','/auth/account',account,h)[0],200)
        self.assertEqual(request('GET','/auth/session',headers={'Cookie':cookie})[0],401)
        # The old bootstrap Basic credentials must not remain an API back door.
        import base64
        old={'Authorization':'Basic '+base64.b64encode('管理员:test-password-秘密'.encode()).decode()}
        self.assertEqual(request('GET','/admin/traffic/status',headers=old)[0],401)
        self.assertEqual(request('POST','/auth/logout',{},h)[0],200)
        self.assertEqual(request('GET','/auth/session',headers={'Cookie':cookie})[0],401)


if __name__=='__main__':unittest.main()
