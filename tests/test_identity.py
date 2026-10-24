import json
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request,urlopen
from urllib.error import HTTPError
from diaodu.library import Library
from diaodu.identity import Identity,AuthError
from diaodu.server import make_handler

class IdentityAPI(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        root=Path(self.tmp.name)
        self.library=Library(root)
        self.identity=Identity(root/'identity.sqlite3','test-invite')
        self.users=[];self.cookies=[];self.csrf=[];self.agents=[]
        for i in range(2):
            user=self.identity.register('person'+str(i),'张三' if i==0 else '李四','a-secure-password','test-invite')
            token,csrf=self.identity.login('person'+str(i),'a-secure-password')
            self.users.append(user);self.cookies.append('diaodu_session='+token);self.csrf.append(csrf)
            self.agents.append(self.identity.create_agent(user['user_id'],'开发助手','Codex'))
        self.server=ThreadingHTTPServer(('127.0.0.1',0),make_handler(None,self.library,self.identity))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.base='http://127.0.0.1:'+str(self.server.server_address[1])
        _,self.task=self.req('POST','/v1/tasks',{'requirement':'共同完成任务'},user=0)
    def tearDown(self):
        self.server.shutdown();self.thread.join();self.server.server_close();self.tmp.cleanup()
    def req(self,method,path,body=None,user=None,agent=None,headers=None,raw=None,binary=False):
        h={} if headers is None else dict(headers)
        if user is not None: h.update(Cookie=self.cookies[user],**{'X-CSRF-Token':self.csrf[user]})
        if agent is not None: h['Authorization']='Bearer '+self.agents[agent]['token']
        data=raw if raw is not None else json.dumps(body).encode() if body is not None else None
        request=Request(self.base+path,data=data,headers=h,method=method)
        try:
            with urlopen(request,timeout=5) as response:
                return response.status,response.read() if binary else json.load(response)
        except HTTPError as exc:
            with exc:return exc.code,json.load(exc)
    def run_for(self,agent=0,task=None):
        status,run=self.req('POST','/v1/tasks/'+(task or self.task['id'])+'/runs',{},agent=agent)
        self.assertEqual(status,201);return run['run_id']
    def test_same_tool_different_people_and_runs(self):
        path='/v1/tasks/'+self.task['id'];first=self.run_for(0);second=self.run_for(1)
        for i,run in enumerate([first,second]):
            status,task=self.req('POST',path+'/report',{'run_id':run,'text':'完成','request_id':'same'},agent=i)
            self.assertEqual(status,200)
            identity=task['entries'][-1]['identity']
            self.assertEqual(identity['agent_type'],'Codex');self.assertEqual(identity['user_id'],self.users[i]['user_id']);self.assertEqual(identity['run_id'],run)
        self.assertNotEqual(task['entries'][0]['identity']['agent_id'],task['entries'][1]['identity']['agent_id'])
        self.assertEqual(task['status'],'open')
        status,_=self.req('POST',path+'/report',{'run_id':first,'text':'完成','request_id':'same'},agent=0)
        self.assertEqual(status,200);self.assertEqual(len(self.library.read_task(self.task['id'])['entries']),2)

    def test_spoofing_run_and_credential_revoke(self):
        path='/v1/tasks/'+self.task['id'];run=self.run_for()
        for field in ('name','identity','user_id','agent_id','agent_type'):
            status,_=self.req('POST',path+'/report',{'run_id':run,'text':'伪造',field:'someone'},agent=0)
            self.assertEqual(status,400)
        status,_=self.req('POST',path+'/report',{'run_id':run,'text':'冒用'},agent=1);self.assertEqual(status,403)
        status,_=self.req('POST',path+'/report',{'text':'缺执行编号'},agent=0);self.assertEqual(status,400)
        status,_=self.req('POST',path+'/report',{'run_id':run,'text':'人员直接提交'},user=0);self.assertEqual(status,403)
        status,_=self.req('POST','/v1/agents/'+self.agents[0]['agent_id']+'/revoke',{},user=1);self.assertEqual(status,403)
        status,_=self.req('POST','/v1/agents/'+self.agents[0]['agent_id']+'/revoke',{},user=0);self.assertEqual(status,200)
        status,_=self.req('GET','/v1/me',agent=0);self.assertEqual(status,401)
        status,_=self.req('POST',path+'/report',{'run_id':run,'text':'失效'},agent=0);self.assertEqual(status,401)
    def test_public_read_csrf_and_connected_publish(self):
        status,_=self.req('GET','/v1/tasks');self.assertEqual(status,200)
        status,_=self.req('POST','/v1/tasks',{'requirement':'x'},agent=0);self.assertEqual(status,201)
        status,_=self.req('POST','/v1/agents',{'agent_name':'x','agent_type':'Codex'},headers={'Cookie':self.cookies[0]});self.assertEqual(status,403)
        status,_=self.req('POST','/v1/auth/login',{'username':'person0','password':'a-secure-password'},headers={'Origin':'https://evil.example'});self.assertEqual(status,403)
        status,me=self.req('GET','/v1/me',user=0);self.assertEqual(status,200);self.assertNotIn('password',me)
        status,_=self.req('POST','/v1/auth/logout',{},user=0);self.assertEqual(status,200)
        status,_=self.req('GET','/v1/me',user=0);self.assertEqual(status,401)
    def test_public_attachment_provenance_and_cross_run_rejected(self):
        path='/v1/tasks/'+self.task['id'];run=self.run_for(0)
        status,file=self.req('POST',path+'/files?name=demo.mp4&run_id='+run,raw=b'\x00\xffdemo',agent=0);self.assertEqual(status,201)
        status,_=self.req('GET',file['url'],binary=True);self.assertEqual(status,200)
        status,data=self.req('GET',file['url'],agent=1,binary=True);self.assertEqual(data,b'\x00\xffdemo')
        other=self.run_for(1)
        status,_=self.req('POST',path+'/report',{'run_id':other,'attachments':[file['id']]},agent=1);self.assertEqual(status,409)
        status,task=self.req('POST',path+'/report',{'run_id':run,'attachments':[file['id']]},agent=0);self.assertEqual(status,200)
        self.assertEqual(task['entries'][-1]['identity']['agent_id'],self.agents[0]['agent_id'])
        other_task=self.library.publish('其他任务')
        status,_=self.req('POST','/v1/tasks/'+other_task['id']+'/report',{'run_id':run,'text':'跨任务'},agent=0);self.assertEqual(status,403)
    def test_token_never_returned_in_lists_and_invite_required(self):
        status,data=self.req('GET','/v1/agents',user=0);self.assertEqual(status,200)
        self.assertNotIn('token',json.dumps(data));self.assertEqual(len(data['agents']),1)
        with self.assertRaises(AuthError):self.identity.register('attacker','冒充','a-secure-password','wrong')
        with self.assertRaises(AuthError):self.identity.login('person0','wrong')

    def test_same_person_multiple_codex_instances(self):
        another=self.identity.create_agent(self.users[0]['user_id'],'代码检查助手','Codex')
        first=self.identity.agent(self.agents[0]['token'])
        second=self.identity.agent(another['token'])
        self.assertEqual(first['user_id'],second['user_id'])
        self.assertNotEqual(first['agent_id'],second['agent_id'])
        self.assertEqual(first['agent_type'],second['agent_type'])
        with self.identity.db() as db:
            stored=db.execute('SELECT token_hash FROM agents WHERE id=?',(another['agent_id'],)).fetchone()[0]
        self.assertNotEqual(stored,another['token'])
        runs=[self.identity.start_run(first,self.task['id']) for _ in range(2)]
        self.assertNotEqual(runs[0]['run_id'],runs[1]['run_id'])

    def test_production_cookie_flags(self):
        self.library.public_base_path='/task'
        self.server.RequestHandlerClass=make_handler(None,self.library,self.identity,True,'https://www.chuhaijian.com')
        req=Request(self.base+'/v1/auth/login',data=json.dumps({'username':'person0','password':'a-secure-password'}).encode(),headers={'Origin':'https://www.chuhaijian.com'},method='POST')
        with urlopen(req) as response:
            cookie=response.headers['Set-Cookie']
            for required in ('HttpOnly','Secure','SameSite=Strict','Path=/task/'):
                self.assertIn(required,cookie)
            self.assertEqual(response.headers['Cache-Control'],'no-store')

    def test_no_registration_alias_connection_and_browser_speech(self):
        status,first=self.req('POST','/v1/connect',{'alias':'小张','agent_type':'Codex'})
        self.assertEqual(status,201)
        status,second=self.req('POST','/v1/connect',{'alias':'小张','agent_type':'Codex'})
        self.assertEqual(status,201)
        self.assertNotEqual(first['agent_id'],second['agent_id'])
        headers={'Cookie':'diaodu_session='+first['token'],'X-CSRF-Token':first['csrf']}
        status,task=self.req('POST','/v1/tasks',{'requirement':'免注册发布'},headers=headers)
        self.assertEqual(status,201)
        status,run=self.req('POST','/v1/tasks/'+task['id']+'/runs',{},headers=headers)
        self.assertEqual(status,201)
        status,task=self.req('POST','/v1/tasks/'+task['id']+'/say',{'run_id':run['run_id'],'text':'自由发言'},headers=headers)
        self.assertEqual(status,200)
        self.assertEqual(task['entries'][0]['identity']['agent_name'],'小张')
        self.assertNotIn('csrf',task['entries'][0]['identity'])
        status,changed=self.req('POST','/v1/me/alias',{'alias':'开发助手'},headers=headers)
        self.assertEqual(status,200)
        self.assertEqual(changed['agent_id'],first['agent_id'])
        status,me=self.req('GET','/v1/me',headers=headers)
        self.assertEqual(me['agent_name'],'开发助手')
        self.assertEqual(me['user_id'],first['user_id'])
        status,_=self.req('POST','/v1/me/alias',{'alias':'伪造'},headers={'Cookie':'diaodu_session='+first['token']})
        self.assertEqual(status,403)

    def test_short_number_routes_normalize_execution_identity(self):
        number=self.task['number']
        status,task=self.req('GET','/v1/tasks/'+number)
        self.assertEqual(status,200)
        self.assertEqual(task['id'],self.task['id'])
        run=self.run_for(0,number)
        status,result=self.req('POST','/v1/tasks/'+self.task['id']+'/report',{'run_id':run,'text':'短编号创建执行，原编号提交'},agent=0)
        self.assertEqual(status,200)
        status,changes=self.req('GET','/v1/tasks/'+number+'/entries?after=0')
        self.assertEqual(status,200)
        self.assertEqual(changes['next_cursor'],1)

    def test_one_column_per_instance_across_runs_and_alias_changes(self):
        path='/v1/tasks/'+self.task['id']
        first=self.run_for(0)
        self.req('POST',path+'/report',{'run_id':first,'text':'开始','status':'in_progress'},agent=0)
        second=self.run_for(0)
        _,task=self.req('POST',path+'/report',{'run_id':second,'text':'完成'},agent=0)
        self.assertEqual(len(task['entries']),2)
        self.assertEqual(len(task['columns']),1)
        column=task['columns'][0]
        self.assertEqual(column['status'],'completed')
        self.assertEqual(len(column['entry_ids']),2)
        self.assertEqual(column['latest_report_id'],task['entries'][-1]['id'])
        other=self.run_for(1)
        _,task=self.req('POST',path+'/report',{'run_id':other,'text':'另一实例'},agent=1)
        self.assertEqual(len(task['columns']),2)
        _,task=self.req('POST',path+'/say',{'run_id':second,'text':'补充讨论'},agent=0)
        self.assertEqual(len(task['columns']),2)
        self.assertEqual(task['columns'][0]['status'],'completed')
        self.assertEqual(len(task['columns'][0]['entry_ids']),3)

    def test_publisher_materials_public_read_and_ownership(self):
        path='/v1/tasks/'+self.task['number']
        for filename,content in [('video.mp4',b'video'),('image.png',b'image'),('document.txt',b'text')]:
            status,file=self.req('POST',path+'/materials?name='+filename,raw=content,user=0)
            self.assertEqual(status,201)
            self.assertEqual(file['purpose'],'material')
            status,data=self.req('GET',file['url'],binary=True)
            self.assertEqual(data,content)
        status,task=self.req('GET',path)
        self.assertEqual(len(task['materials']),3)
        self.assertEqual(len(task['columns']),0)
        self.assertNotIn('csrf',task['materials'][0]['identity'])
        status,_=self.req('POST',path+'/materials?name=bad.txt',raw=b'bad',user=1)
        self.assertEqual(status,403)
        status,_=self.req('POST',path+'/materials?name=bad.txt',raw=b'bad',agent=1)
        self.assertEqual(status,403)

    def test_connect_and_login_use_separate_rate_limit_buckets(self):
        for _ in range(45):
            status, _body = self.req('POST', '/v1/connect', {'alias': '批量接入', 'agent_type': 'Dev'})
            self.assertEqual(status, 201)
        for _ in range(15):
            status, _body = self.req('POST', '/v1/connect', {'alias': '批量接入', 'agent_type': 'Dev'})
            self.assertEqual(status, 201)
        status, body = self.req('POST', '/v1/connect', {'alias': '批量接入', 'agent_type': 'Dev'})
        self.assertEqual(status, 429)
        self.assertEqual(body['code'], 'rate_limited')
        self.assertIsInstance(body['retry_after'], int)
        self.assertGreater(body['retry_after'], 0)
        self.assertIn('秒后重试', body['error'])

        # The sign-in bucket must stay at the strict 20 and be unaffected by connect.
        for _ in range(20):
            status, _body = self.req('POST', '/v1/auth/login',
                                     {'username': 'person0', 'password': 'a-secure-password'})
            self.assertEqual(status, 200)
        status, body = self.req('POST', '/v1/auth/login',
                                {'username': 'person0', 'password': 'a-secure-password'})
        self.assertEqual(status, 429)
        self.assertIsInstance(body['retry_after'], int)


class RateLimitBuckets(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.identity = Identity(Path(self.tmp.name) / 'identity.sqlite3', '')
        self.key = '203.0.113.9'

    def tearDown(self):
        self.tmp.cleanup()

    def count(self, stored):
        with self.identity.db() as db:
            row = db.execute('SELECT count FROM attempts WHERE key=?', (stored,)).fetchone()
        return row['count'] if row else 0

    def test_connect_allows_sixty_then_rejects_with_retry_after(self):
        for _ in range(45):
            self.identity.throttle(self.key, limit=self.identity.CONNECT_LIMIT, scope='connect')
        for _ in range(15):
            self.identity.throttle(self.key, limit=self.identity.CONNECT_LIMIT, scope='connect')
        with self.assertRaises(AuthError) as caught:
            self.identity.throttle(self.key, limit=self.identity.CONNECT_LIMIT, scope='connect')
        error = caught.exception
        self.assertEqual(error.status, 429)
        self.assertIsInstance(error.retry_after, int)
        self.assertTrue(0 < error.retry_after <= self.identity.WINDOW)
        self.assertIn(f'{error.retry_after} 秒后重试', str(error))

    def test_auth_bucket_still_allows_twenty(self):
        for _ in range(20):
            self.identity.throttle(self.key)
        with self.assertRaises(AuthError) as caught:
            self.identity.throttle(self.key)
        self.assertEqual(caught.exception.status, 429)

    def test_buckets_have_independent_quota(self):
        for _ in range(20):
            self.identity.throttle(self.key)
        for _ in range(60):
            self.identity.throttle(self.key, limit=self.identity.CONNECT_LIMIT, scope='connect')
        with self.assertRaises(AuthError):
            self.identity.throttle(self.key)
        with self.assertRaises(AuthError):
            self.identity.throttle(self.key, limit=self.identity.CONNECT_LIMIT, scope='connect')

    def test_rejected_attempt_is_not_counted_and_does_not_extend_window(self):
        for _ in range(20):
            self.identity.throttle(self.key)
        with self.assertRaises(AuthError) as first:
            self.identity.throttle(self.key)
        with self.assertRaises(AuthError) as later:
            self.identity.throttle(self.key)
        self.assertEqual(self.count('auth:' + self.key), 20)
        self.assertLessEqual(later.exception.retry_after, first.exception.retry_after)
