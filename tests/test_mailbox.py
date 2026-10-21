import unittest,tempfile,json,threading
from pathlib import Path
try:
 from cryptography.fernet import Fernet
except ImportError:
 Fernet=None
from diaodu.mailbox import Mailbox,MailError,parse_message
from diaodu.identity import AuthError
class FakeMail:
 def __init__(self):self.sends=[];self.fail=False
 def test(self,c):
  if c['password']=='bad':raise RuntimeError('secret leak')
 def inbox(self,c,before=None):return {'messages':[{'id':'1.2','subject':'客户来信','from':'buyer@example.cn'}],'next_before':None}
 def read(self,c,mid):return {'id':mid,'subject':'咨询','from':'buyer@example.cn','reply_to':'buyer@example.cn','text':'介绍一下','message_id':'<customer@example.cn>','attachments':[]}
 def send(self,c,d):
  self.sends.append(d)
  if self.fail:raise TimeoutError('password must not leak')
@unittest.skipIf(Fernet is None,"Install requirements-mail.txt for mail tests")
class MailboxTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name);key=self.root/'key';key.write_bytes(Fernet.generate_key());self.transport=FakeMail();self.mail=Mailbox(self.root,key,self.transport);self.owner={'agent_id':'ag_a','user_id':'us_a'};self.other={'agent_id':'ag_b','user_id':'us_b'}
  self.mail.connect(self.owner,{'email':'owner@example.cn','provider':'gname','password':'private_password','consent':True})
 def tearDown(self):self.tmp.cleanup()
 def draft(self,**kw):return self.mail.draft(self.owner,dict(to=['buyer@example.cn'],subject='介绍平台',text='AgentReins Hub',request_id='one',**kw))
 def test_isolation_encryption_grant_and_disconnect(self):
  with self.assertRaises(AuthError):self.mail.inbox(self.other,'ag_a')
  self.mail.grant(self.owner,'ag_b',True);self.assertEqual(len(self.mail.inbox(self.other,'ag_a')['messages']),1)
  d=self.mail.draft(self.other,{'owner_id':'ag_a','to':['buyer@example.cn'],'subject':'介绍','text':'草稿','request_id':'x'})
  with self.assertRaises((AuthError,MailError)):self.mail.send(self.other,d['id'],True)
  self.assertNotIn(b'private_password',(self.root/'mailbox.sqlite3').read_bytes());self.assertNotIn('password',json.dumps(self.mail.status(self.owner)))
  self.mail.disconnect(self.owner);self.assertFalse(self.mail.status(self.owner)['connected']);self.assertEqual(self.mail.outbox(self.owner)['drafts'],[])
 def test_drafts_retry_unknown_and_exact_content(self):
  d=self.draft();self.assertEqual(d['id'],self.draft()['id']);self.assertEqual(len(self.transport.sends),0)
  with self.assertRaises(MailError):self.mail.draft(self.owner,{'to':['buyer@example.cn'],'subject':'不同','text':'x','request_id':'one'})
  self.transport.fail=True;r=self.mail.send(self.owner,d['id'],True);self.assertEqual(r['state'],'unknown');self.mail.send(self.owner,d['id'],True);self.assertEqual(len(self.transport.sends),1);self.assertNotIn('password',r['error'])
 def test_header_validation_reply_and_approval(self):
  with self.assertRaises(MailError):self.mail.draft(self.owner,{'to':['a@example.cn\r\nBcc:b@example.cn'],'subject':'a','text':'x','request_id':'bad'})
  d=self.draft(reply_id='1.2');self.assertEqual(d['in_reply_to'],'<customer@example.cn>')
  with self.assertRaises(MailError):self.mail.send(self.owner,d['id'],False)
  r=self.mail.send(self.owner,d['id'],True);self.assertEqual(r['state'],'accepted');self.mail.send(self.owner,d['id'],True);self.assertEqual(len(self.transport.sends),1)
 def test_html_is_text_not_remote_content(self):
  msg=parse_message(b'From: a@example.cn\r\nContent-Type: text/html; charset=utf-8\r\n\r\n<p>Hello</p><script>steal()</script><img src="https://evil.test/pixel">')
  self.assertIn('Hello',msg['text']);self.assertNotIn('steal()',msg['text']);self.assertNotIn('pixel',msg['text'])

import test_identity as api_fixtures
@unittest.skipIf(Fernet is None,"Install requirements-mail.txt for mail tests")
class MailAPITests(unittest.TestCase):
 setUp=api_fixtures.IdentityAPI.setUp
 tearDown=api_fixtures.IdentityAPI.tearDown
 req=api_fixtures.IdentityAPI.req
 def test_rest_private_grants_and_confirmation(self):
  mail=self.library.mailbox;mail.cipher=Fernet(Fernet.generate_key());fake=FakeMail();mail.transport=fake
  self.assertEqual(self.req('GET','/v1/mail/status')[0],401)
  code,status=self.req('POST','/v1/mail/connect',{'email':'private@example.cn','provider':'gname','password':'private_password','consent':True},agent=0);self.assertEqual(code,200);self.assertTrue(status['connected'])
  owner=self.agents[0]['agent_id']
  self.assertEqual(self.req('GET','/v1/mail/inbox?owner_id='+owner,agent=1)[0],403)
  self.req('POST','/v1/mail/grant',{'agent_id':self.agents[1]['agent_id'],'allow':True},agent=0)
  self.assertEqual(self.req('GET','/v1/mail/inbox?owner_id='+owner,agent=1)[0],200)
  _,d=self.req('POST','/v1/mail/drafts',{'owner_id':owner,'to':['buyer@example.cn'],'subject':'介绍','text':'内容','request_id':'agentdraft'},agent=1)
  route='/v1/mail/drafts/'+d['id']+'/send'
  self.assertEqual(self.req('POST',route,{'confirmed':True},agent=1)[0],400)
  self.assertEqual(self.req('POST',route,{'confirmed':False},agent=0)[0],400)
  _,sent=self.req('POST',route,{'confirmed':True},agent=0);self.assertEqual(sent['state'],'accepted');self.assertEqual(len(fake.sends),1)
  _,public=self.req('GET','/v1/tasks');self.assertNotIn('private@example.cn',json.dumps(public));self.assertNotIn('private_password',json.dumps(public))
 def test_campaign_is_default_off_and_requires_owner_mailbox(self):
  mail=self.library.mailbox;mail.cipher=Fernet(Fernet.generate_key());mail.transport=FakeMail()
  self.req('POST','/v1/mail/connect',{'email':'private@example.cn','provider':'gname','password':'private_password','consent':True},agent=0)
  _,s=self.req('GET','/v1/mail/outreach',agent=0);self.assertFalse(s['enabled'])
  self.assertEqual(self.req('POST','/v1/mail/outreach',{'enabled':True},agent=1)[0],400)
  _,s=self.req('POST','/v1/mail/outreach',{'enabled':True},agent=0);self.assertTrue(s['enabled'])
