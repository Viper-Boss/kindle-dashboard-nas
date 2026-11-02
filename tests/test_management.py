import io,json,sys,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import management

class Handler:
    def __init__(self,path,method="GET",body=None,headers=None):
        self.path,self.command=path,method
        raw=json.dumps(body).encode() if body is not None else b""
        self.headers={"Host":"nas:8787","Content-Length":str(len(raw)),**(headers or {})}
        self.rfile,self.wfile=io.BytesIO(raw),io.BytesIO();self.response={}
    def send_response(self,code):self.code=code
    def send_header(self,key,value):self.response[key]=value
    def end_headers(self):pass

class ManagementTests(unittest.TestCase):
    def test_authentication_protects_all_feature_endpoints(self):
        for path in ["traffic/status","traffic/export","maintenance/status","maintenance/log?job=bad"]:
            h=Handler("/admin/"+path);management.handle(h,lambda headers:False)
            self.assertEqual(h.code,401);self.assertEqual(h.response["Cache-Control"],"no-store")
    def test_cross_origin_and_missing_csrf_header_are_rejected(self):
        for headers in [{},{"X-Requested-With":"KindleAdmin","Origin":"http://evil.example"}]:
            h=Handler("/admin/maintenance/job","POST",{"kind":"upgrade"},headers)
            with patch.object(management.maintenance,"enqueue") as enqueue:
                management.handle(h,lambda headers:True);self.assertEqual(h.code,403);enqueue.assert_not_called()
    def test_csv_formulas_are_escaped_and_counts_remain_numeric(self):
        h=Handler("/admin/traffic/export?kind=clients")
        data={"clients":[{"id":"192.168.1.2","label":" =HYPERLINK(1)","upload":3,"download":4}]}
        with patch.object(management.traffic_stats,"TrafficStore") as store:
            store.return_value.summary.return_value=data
            management.handle(h,lambda headers:True)
        self.assertEqual(h.code,200);self.assertTrue(h.wfile.getvalue().startswith(b"\xef\xbb\xbf"))
        self.assertIn("' =HYPERLINK(1)",h.wfile.getvalue().decode("utf-8-sig"))
        self.assertIn(",3,4,7",h.wfile.getvalue().decode("utf-8-sig"))
    def test_valid_upgrade_request_uses_only_fixed_job_kind(self):
        h=Handler("/admin/maintenance/job","POST",{"kind":"upgrade"},{"X-Requested-With":"KindleAdmin","Origin":"http://nas:8787"})
        with patch.object(management.maintenance,"enqueue",return_value={"id":"a"*32}) as enqueue:
            management.handle(h,lambda headers:True);enqueue.assert_called_once_with("upgrade")
        self.assertEqual(h.code,202)
