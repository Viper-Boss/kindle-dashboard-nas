"""Authenticated history and maintenance endpoints for the existing admin console."""
import csv
import io
import json
import urllib.parse
import maintenance
import traffic_stats


def handle(handler, authorized):
    parsed=urllib.parse.urlparse(handler.path)
    if not parsed.path.startswith(('/admin/traffic/','/admin/maintenance/')):return False
    def respond(code, data, content_type='application/json; charset=utf-8', filename=None):
        raw=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        handler.send_response(code);handler.send_header('Content-Type',content_type)
        handler.send_header('Cache-Control','no-store');handler.send_header('Content-Length',str(len(raw)))
        if filename:handler.send_header('Content-Disposition','attachment; filename="'+filename+'"')
        handler.end_headers();handler.wfile.write(raw)
    if not authorized(handler.headers):respond(401,{'error':'请先登录后台'});return True
    try:
        body={}
        if handler.command=='POST':
            origin=handler.headers.get('Origin','')
            if (handler.headers.get('X-Requested-With')!='KindleAdmin'
                    or (origin and urllib.parse.urlparse(origin).netloc!=handler.headers.get('Host',''))):
                respond(403,{'error':'请求来源无效'});return True
            length=int(handler.headers.get('Content-Length','0'))
            if not 0<length<=8192:raise ValueError('请求长度无效')
            body=json.loads(handler.rfile.read(length))
            if not isinstance(body,dict):raise ValueError('请求格式无效')
        path=parsed.path
        if handler.command=='GET' and path in ('/admin/traffic/status','/admin/traffic/export'):
            query=urllib.parse.parse_qs(parsed.query)
            get=lambda key,default=None:query.get(key,[default])[0]
            data=traffic_stats.TrafficStore().summary(get('granularity','day'),get('period'),get('client'))
            if path.endswith('/status'):respond(200,data)
            else:
                clients=get('kind')=='clients';rows=data['clients'] if clients else data['series']
                output=io.StringIO();writer=csv.writer(output)
                writer.writerow(['客户端','备注','上传字节','下载字节','合计字节'] if clients else ['日期','上传字节','下载字节','合计字节'])
                def safe(value):
                    text=str(value or '')
                    return "'"+text if text.lstrip(' \t\r\n').startswith(('=','+','-','@')) or text.startswith(('\t','\r','\n')) else text
                for row in rows:
                    first=[safe(row['id']),safe(row['label'])] if clients else [row['bucket']]
                    writer.writerow(first+[row['upload'],row['download'],row['upload']+row['download']])
                respond(200,('\ufeff'+output.getvalue()).encode(),'text/csv; charset=utf-8','kindle-traffic.csv')
        elif handler.command=='POST' and path=='/admin/traffic/alias':
            traffic_stats.TrafficStore().alias(body.get('client'),body.get('label'));respond(200,{'ok':True})
        elif handler.command=='GET' and path=='/admin/maintenance/status':respond(200,maintenance.status())
        elif handler.command=='GET' and path=='/admin/maintenance/log':
            job=urllib.parse.parse_qs(parsed.query).get('job',[''])[0]
            respond(200,{'log':maintenance.SharedStore().log(job)})
        elif handler.command=='POST' and path=='/admin/maintenance/check':respond(200,maintenance.check_updates(force=True))
        elif handler.command=='POST' and path=='/admin/maintenance/auto-sync':respond(200,maintenance.set_auto_sync(body.get('enabled')))
        elif handler.command=='POST' and path=='/admin/maintenance/log-settings':
            respond(202,maintenance.enqueue('logs',maintenance.validate_log_settings(body)))
        elif handler.command=='POST' and path=='/admin/maintenance/job':respond(202,maintenance.enqueue(body.get('kind')))
        else:respond(404,{'error':'接口不存在'})
    except (ValueError,TypeError,KeyError,OverflowError):respond(400,{'error':'请求参数无效或当前操作不可用'})
    except Exception as error:respond(503,{'error':'服务暂不可用（'+type(error).__name__+'）'})
    return True
