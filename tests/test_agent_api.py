import unittest,json,subprocess,sys,os
from pathlib import Path
import test_identity as fixtures
class AgentAPI(unittest.TestCase):
 setUp=fixtures.IdentityAPI.setUp
 tearDown=fixtures.IdentityAPI.tearDown
 req=fixtures.IdentityAPI.req
 run_for=fixtures.IdentityAPI.run_for
 def test_context_visibility_cursors_and_mentions(self):
  path='/v1/tasks/'+self.task['number']
  self.req('POST',path+'/chat',{'text':'private-context-secret'},user=0)
  _,anonymous=self.req('GET',path+'/context')
  self.assertIsNone(anonymous['chat']);self.assertNotIn('private-context-secret',str(anonymous))
  _,owner=self.req('GET',path+'/context',user=0)
  self.assertEqual(owner['chat']['messages'][0]['text'],'private-context-secret')
  run=self.run_for(0)
  self.req('POST',path+'/chat',{'text':'please check','mentions':[self.agents[0]['agent_id']]},user=0)
  self.req('POST',path+'/report',{'run_id':run,'text':'done'},agent=0)
  _,context=self.req('GET',path+'/context',agent=0)
  self.assertEqual(context['columns'][0]['latest_report']['text'],'done')
  self.assertEqual(len(context['chat']['mentions_me']),1)
  _,updates=self.req('GET',path+'/updates?entry_after=1&chat_after=2',agent=0)
  self.assertEqual(updates['entries'],[]);self.assertEqual(updates['chat']['messages'],[])
  _,other=self.req('GET',path+'/context',agent=1);self.assertIsNone(other['chat'])
  self.assertEqual(self.req('GET',path+'/updates?chat_after=1',agent=1)[0],403)
 def test_summary_and_machine_contract(self):
  _,data=self.req('GET','/v1/tasks?view=summary&limit=1')
  self.assertNotIn('entries',data['tasks'][0]);self.assertIn('next_offset',data)
  status,spec=self.req('GET','/v1/openapi.json');self.assertEqual(status,200)
  self.assertIn('/tasks/{task}/context',spec['paths'])
  status,error=self.req('GET','/v1/tasks?view=summary&limit=200');self.assertEqual(status,400)
  self.assertEqual(error['code'],'invalid_request');self.assertFalse(error['retryable'])
 def test_client_persists_and_reuses_identity(self):
  script=Path(__file__).resolve().parents[1]/'diaodu/client.py'
  state=Path(self.tmp.name)/'client.json'
  env={k:v for k,v in os.environ.items() if k!='DIAODU_AGENT_TOKEN'}
  def cli(*args):return json.loads(subprocess.check_output([sys.executable,str(script),'--url',self.base,'--state-file',str(state),*args],env=env))
  first=cli('connect','--alias','worker','--type','Codex')
  second=cli('connect','--alias','ignored')
  self.assertEqual(first['agent_id'],second['agent_id']);self.assertTrue(second['reused'])
  self.assertNotIn('token',first);self.assertEqual(state.stat().st_mode&0o777,0o600)
  context=cli('context',self.task['number']);self.assertIsNone(context['chat'])
  cli('run',self.task['number']);self.assertIsNotNone(cli('context',self.task['number'])['chat'])
 def test_discovery_full_descriptions_and_counts(self):
  long_text='完整描述'*200
  task=self.library.publish(long_text)
  for path in ('/v1','/v1/discover'):
   status,data=self.req('GET',path)
   self.assertEqual(status,200);self.assertEqual(data['total_tasks'],2)
   self.assertEqual(data['tasks'][0]['number'],task['number'])
   self.assertEqual(data['tasks'][0]['description'],long_text)
   self.assertTrue(data['tasks'][0]['read_url'].endswith(task['number']+'/context'))
  _,page=self.req('GET','/v1/discover?limit=1')
  self.assertEqual(page['returned_tasks'],1);self.assertIsNotNone(page['next_url'])
  self.library.delete(task['id'],None) if False else None
  _,context=self.req('GET','/v1/tasks/'+task['number']+'/context')
  self.assertEqual(context['task']['requirement'],long_text)
