import unittest,json,base64
import test_identity as fixtures
from diaodu import mcp
class DesignAPI(unittest.TestCase):
 setUp=fixtures.IdentityAPI.setUp
 tearDown=fixtures.IdentityAPI.tearDown
 req=fixtures.IdentityAPI.req
 run_for=fixtures.IdentityAPI.run_for
 def test_review_publisher_only_and_persistence(self):
  path='/v1/tasks/'+self.task['number'];run=self.run_for(0)
  _,t=self.req('POST',path+'/report',{'run_id':run,'text':'成果'},agent=0);eid=t['entries'][-1]['id'];url=path+'/entries/'+eid+'/review'
  self.assertEqual(t['entries'][-1]['review']['state'],'pending')
  self.assertEqual(self.req('POST',url,{'state':'accepted'},agent=0)[0],403)
  self.assertEqual(self.req('POST',url,{'state':'accepted'},user=1)[0],403)
  self.assertEqual(self.req('POST',url,{'state':'returned'},user=0)[0],400)
  self.assertEqual(self.req('POST',url,{'state':'returned','note':'缺证据'},user=0)[0],200)
  _,t=self.req('GET',path);self.assertEqual(t['entries'][-1]['review']['note'],'缺证据')
  self.assertEqual(self.req('POST',url,{'state':'accepted'},user=0)[0],200)
 def test_pin_promote_and_privacy(self):
  path='/v1/tasks/'+self.task['number']
  _,m=self.req('POST',path+'/chat',{'text':'私密讨论'},user=0);url=path+'/chat/'+m['id']
  self.assertEqual(self.req('POST',url+'/pin',{},user=1)[0],403)
  self.assertEqual(self.req('POST',url+'/pin',{},user=0)[0],200)
  _,chat=self.req('GET',path+'/chat',user=0);self.assertEqual(chat['pinned'][0]['text'],'私密讨论')
  _,t=self.req('GET',path);self.assertNotIn('私密讨论',str(t))
  _,note=self.req('POST',url+'/promote',{},user=0)
  _,again=self.req('POST',url+'/promote',{},user=0);self.assertEqual(note,again)
  _,t=self.req('GET',path);self.assertEqual(t['entries'][-1]['kind'],'note')
 def test_mcp_session_workflow_and_idempotency(self):
  def invoke(method,params=None,sid=None):
   return mcp.handle({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}},sid,None,self.identity,self.library,self.base+'/v1',lambda:None)
  init,sid=invoke('initialize',{'protocolVersion':'2025-11-25'})
  tools,_=invoke('tools/list',sid=sid);self.assertIn('diaodu_update_workflow',{t['name'] for t in tools['result']['tools']});self.assertEqual(len(tools['result']['tools']),9)
  def tool(name,args):
   r,_=invoke('tools/call',{'name':'diaodu_'+name,'arguments':args},sid);self.assertFalse(r['result']['isError'],r);return json.loads(r['result']['content'][0]['text'])
  person=tool('connect',{'alias':'MCP开发者','agent_type':'Codex'});self.assertNotIn('token',person)
  task=self.task['number'];tool('read_task',{'task':task});tool('read_chat',{'task':task})
  file=tool('upload_file',{'task':task,'name':'test.txt','content_base64':base64.b64encode(b'test').decode()})
  args={'task':task,'text':'成果','attachments':[file['id']],'request_id':'mcp-test'}
  first=tool('report',args);second=tool('report',args);self.assertEqual(first['entries'][-1]['id'],second['entries'][-1]['id'])
  tool('chat',{'task':task,'text':'已提交'});reviews=tool('read_review',{'task':task});self.assertEqual(reviews['reviews'][-1]['review']['state'],'pending')
 def test_human_identity_self_declared(self):
  _,person=self.req('POST','/v1/connect',{'alias':'真人','member_kind':'human'})
  self.assertEqual(person['member_kind'],'human')
  actor=self.identity.agent(person['token']);self.assertEqual(actor['member_kind'],'human')
  self.assertEqual(self.req('POST','/v1/connect',{'alias':'x','member_kind':'admin'})[0],400)
