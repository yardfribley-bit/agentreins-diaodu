import unittest
import test_identity as fixtures
from diaodu import mcp
class WorkflowAPI(unittest.TestCase):
 setUp=fixtures.IdentityAPI.setUp
 tearDown=fixtures.IdentityAPI.tearDown
 req=fixtures.IdentityAPI.req
 def change(self,body,agent=0,user=None):return self.req('POST','/v1/tasks/'+self.task['number']+'/workflow',body,agent=agent if user is None else None,user=user)
 def test_parallel_ownership_handoff_accept_and_revision(self):
  code,w=self.change({'action':'claim','revision':0,'scope':'接口','summary':'开发中'});self.assertEqual(code,200)
  self.assertEqual(self.change({'action':'claim','revision':0,'scope':'页面'},agent=1)[0],409)
  code,w=self.change({'action':'claim','revision':1,'scope':'页面'},agent=1);self.assertEqual(len(w['work']),2)
  _,w=self.change({'action':'handoff','revision':2,'target_id':self.agents[1]['agent_id']})
  self.assertEqual(w['work'][0]['actor']['agent_id'],self.agents[0]['agent_id']);self.assertEqual(w['work'][0]['handoff']['agent_id'],self.agents[1]['agent_id'])
  _,w=self.change({'action':'accept','revision':3},agent=1)
  self.assertEqual(w['work'][0]['stage'],'handed_off');self.assertIn('接口',w['work'][1]['scope']);self.assertIn('页面',w['work'][1]['scope']);self.assertEqual(w['revision'],4)
  _,t=self.req('GET','/v1/tasks/'+self.task['number']+'/context',agent=0);self.assertEqual(t['task']['workflow']['revision'],4)
 def test_permissions_close_reopen_and_report_does_not_close(self):
  self.assertEqual(self.change({'action':'update','revision':0,'stage':'blocked'},agent=1)[0],403)
  self.assertEqual(self.change({'action':'finish','revision':0})[0],403)
  _,w=self.change({'action':'claim','revision':0,'scope':'开发'})
  _,w=self.change({'action':'update','revision':1,'stage':'waiting_review'})
  self.assertEqual(w['stage'],'waiting_review')
  _,run=self.req('POST','/v1/tasks/'+self.task['number']+'/runs',{},agent=0)
  self.req('POST','/v1/tasks/'+self.task['number']+'/report',{'run_id':run['run_id'],'text':'本轮完成','status':'completed'},agent=0)
  self.assertEqual(self.library.read_task(self.task['id'])['workflow']['stage'],'waiting_review')
  _,w=self.change({'action':'finish','revision':2},user=0);self.assertEqual(w['stage'],'completed')
  self.assertEqual(self.change({'action':'claim','revision':3,'scope':'继续'})[0],400)
  _,w=self.change({'action':'reopen','revision':3},user=0);self.assertEqual(w['stage'],'in_progress')
 def test_unconfirmed_private_fields_validation_and_mcp(self):
  _,t=self.req('GET','/v1/tasks/'+self.task['number']);self.assertFalse(t['workflow']['confirmed'])
  self.assertEqual(self.req('POST','/v1/tasks/'+self.task['number']+'/workflow',{'action':'claim','revision':0,'scope':'a'})[0],401)
  self.assertEqual(self.change({'action':'claim','revision':True,'scope':'a'})[0],409)
  actor=self.identity.agent(self.agents[0]['token'])
  result,_=mcp.handle({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'diaodu_update_workflow','arguments':{'task':self.task['number'],'action':'claim','revision':0,'scope':'MCP工作'}}},None,actor,self.identity,self.library,'http://test/v1',lambda:None)
  self.assertFalse(result['result']['isError'])
  import json
  self.assertNotIn(self.agents[0]['token'],json.dumps(self.library.read_task(self.task['id'])['workflow']))
