import unittest,json
import test_identity as fixtures
class ProfileAPI(unittest.TestCase):
 setUp=fixtures.IdentityAPI.setUp
 tearDown=fixtures.IdentityAPI.tearDown
 req=fixtures.IdentityAPI.req
 def test_optional_email_consent_private_and_remove(self):
  body={'alias':'小张','member_kind':'human','agent_type':'人员','email':'followup@example.invalid'}
  self.assertEqual(self.req('POST','/v1/connect',body)[0],400)
  _,person=self.req('POST','/v1/connect',dict(body,contact_consent=True));self.assertNotIn('email',person)
  h={'Authorization':'Bearer '+person['token']}
  self.assertEqual(self.req('GET','/v1/me/profile')[0],401)
  _,profile=self.req('GET','/v1/me/profile',headers=h);self.assertEqual(profile['email'],body['email']);self.assertFalse(profile['email_verified'])
  _,t=self.req('POST','/v1/tasks',{'requirement':'公开测试'},headers=h)
  for route in ['/v1/me','/v1/tasks','/v1/tasks/'+t['number'],'/v1/tasks/'+t['number']+'/chat','/v1/tasks/'+t['number']+'/context']:
   _,public=self.req('GET',route,headers=h);self.assertNotIn(body['email'],json.dumps(public))
  _,p=self.req('POST','/v1/me/profile',{'email':'','contact_consent':False},headers=h);self.assertEqual(p['email'],'')
  with self.identity.db() as db:self.assertEqual(db.execute('SELECT COUNT(*) FROM contacts WHERE agent_id=?',(person['agent_id'],)).fetchone()[0],0)
 def test_identity_correction_same_instance_updates_public_snapshots(self):
  _,p=self.req('POST','/v1/connect',{'alias':'Codex助手','agent_type':'Codex'})
  h={'Authorization':'Bearer '+p['token']}
  _,t=self.req('POST','/v1/tasks',{'requirement':'纠正身份'},headers=h)
  _,run=self.req('POST','/v1/tasks/'+t['number']+'/runs',{},headers=h)
  self.req('POST','/v1/tasks/'+t['number']+'/report',{'run_id':run['run_id'],'text':'成果'},headers=h)
  self.req('POST','/v1/tasks/'+t['number']+'/chat',{'text':'讨论'},headers=h)
  _,updated=self.req('POST','/v1/me/profile',{'alias':'Cursor助手','agent_type':'Cursor','member_kind':'agent'},headers=h)
  self.assertEqual(updated['agent_type'],'Cursor')
  _,task=self.req('GET','/v1/tasks/'+t['number']);self.assertEqual(task['publisher']['agent_type'],'Cursor');self.assertEqual(task['entries'][0]['identity']['agent_type'],'Cursor')
  _,chat=self.req('GET','/v1/tasks/'+t['number']+'/chat',headers=h);self.assertEqual(chat['members'][0]['agent_type'],'Cursor')
  _,me=self.req('GET','/v1/me',headers=h);self.assertEqual(me['agent_id'],p['agent_id'])
 def test_contact_validation_before_identity_creation(self):
  for email in ['not-an-email','a@x\r\nBcc:evil@x.io','a'*255+'@x.io']:
   self.assertEqual(self.req('POST','/v1/connect',{'alias':'x','email':email,'contact_consent':True})[0],400)
  _,p=self.req('POST','/v1/connect',{'alias':'不填也能进'});self.assertIn('token',p)
