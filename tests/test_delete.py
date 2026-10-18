import unittest
import test_identity as fixtures
class DeleteAPI(unittest.TestCase):
    setUp=fixtures.IdentityAPI.setUp
    tearDown=fixtures.IdentityAPI.tearDown
    req=fixtures.IdentityAPI.req
    def test_owner_delete_hides_task_and_all_attachments(self):
        path='/v1/tasks/'+self.task['number']
        _,public=self.req('POST',path+'/materials?name=notes.txt',raw=b'notes',user=0)
        _,private=self.req('POST',path+'/chat/files?name=secret.txt',raw=b'secret',user=0)
        self.assertEqual(self.req('POST',path+'/delete',{},user=1)[0],403)
        self.assertEqual(self.req('POST',path+'/delete',{})[0],401)
        self.assertEqual(self.req('POST',path+'/delete',{},user=0)[0],200)
        self.assertEqual(self.req('GET',path)[0],404)
        self.assertEqual(self.req('GET',public['url'])[0],404)
        self.assertEqual(self.req('GET',private['url'],user=0)[0],404)
        self.assertEqual(self.req('GET',path+'/chat',user=0)[0],404)
        self.assertNotIn(self.task['id'],[t['id'] for t in self.library.list_tasks()])
        self.assertEqual(self.library.publish('new')['number'],'T000002')
    def test_guest_rename_does_not_change_delete_ownership(self):
        _,guest=self.req('POST','/v1/connect',{'alias':'主人'})
        h={'Authorization':'Bearer '+guest['token']}
        _,task=self.req('POST','/v1/tasks',{'requirement':'访客任务'},headers=h)
        self.req('POST','/v1/me/alias',{'alias':'新别名'},headers=h)
        _,stranger=self.req('POST','/v1/connect',{'alias':'新别名'})
        path='/v1/tasks/'+task['number']+'/delete'
        self.assertEqual(self.req('POST',path,{},headers={'Authorization':'Bearer '+stranger['token']})[0],403)
        self.assertEqual(self.req('POST',path,{},headers=h)[0],200)
