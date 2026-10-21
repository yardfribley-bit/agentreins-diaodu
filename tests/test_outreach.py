import unittest,time
import test_mailbox as fixtures
from diaodu.outreach import Outreach
from diaodu.mailbox import MailError
@unittest.skipIf(fixtures.Fernet is None,"Install requirements-mail.txt for mail tests")
class OutreachTests(unittest.TestCase):
 tearDown=fixtures.MailboxTests.tearDown
 def setUp(self):
  fixtures.MailboxTests.setUp(self);self.transport.inbox=lambda c,before=None:{'messages':[],'next_before':None};self.outreach=Outreach(self.mail,'https://example.test/task')
 def lead(self,name='dev'):
  email=name+'@example.cn'
  return self.outreach.add(self.owner,{'email':email,'source_url':'https://example.cn/contact','source_evidence':'AI Agent开发团队业务合作：'+email,'reason':'中国的AI Agent开发者，公开提供项目协作工具，可讨论跨智能体协作','subject':'聊聊Agent协作','text':'你好，我们正在开发Agent协作平台，希望了解贵团队的协作需求。 https://example.test/task','business_contact':True,'region':'CN'})
 def test_no_send_without_optin_and_duplicate_suppression(self):
  lead=self.lead();self.outreach.tick();self.assertEqual(len(self.transport.sends),0)
  self.outreach.configure(self.owner,True);self.outreach.tick();self.assertEqual(len(self.transport.sends),1)
  self.assertEqual(self.lead()['id'],lead['id']);self.outreach.tick();self.assertEqual(len(self.transport.sends),1)
  self.assertIn('不再联系',self.transport.sends[0]['text'])
 def test_unsubscribe_stops_and_daily_quota(self):
  lead=self.lead()
  with self.mail.db() as db:token=db.execute('SELECT token FROM leads WHERE id=?',(lead['id'],)).fetchone()[0]
  self.outreach.unsubscribe(token);self.outreach.configure(self.owner,True);self.outreach.tick();self.assertEqual(len(self.transport.sends),0)
  new=self.lead('next')
  with self.mail.db() as db:
   for i in range(100):db.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?)',(str(i),'ag_a','hash'+str(i),self.mail.seal({'email':'x@example.cn'}),'accepted',time.time(),time.time(),str(i),None))
  self.outreach.tick();self.assertEqual(len(self.transport.sends),0)
 def test_reply_stops_introduction_and_source_is_required(self):
  self.lead('buyer');self.outreach.configure(self.owner,True)
  self.transport.inbox=lambda c,before=None:{'messages':[{'id':'1.2','from':'buyer@example.cn'}],'next_before':None}
  self.outreach.tick();self.assertEqual(len(self.transport.sends),0);self.assertEqual(self.outreach.status(self.owner)['counts']['replied'],1)
  with self.assertRaises(MailError):self.outreach.add(self.owner,{'email':'guess@example.cn','source_url':'https://example.cn/contact','source_evidence':'猜测','region':'CN','business_contact':True})
