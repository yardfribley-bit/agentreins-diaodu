import json,unittest
from unittest.mock import patch
import test_identity as fixtures
from diaodu import chat

class ChatAPI(unittest.TestCase):
    setUp=fixtures.IdentityAPI.setUp
    tearDown=fixtures.IdentityAPI.tearDown
    req=fixtures.IdentityAPI.req
    run_for=fixtures.IdentityAPI.run_for

    def test_member_only_messages_and_attachments_no_public_leak(self):
        path='/v1/tasks/'+self.task['number']
        self.assertEqual(self.req('GET',path+'/chat')[0],401)
        self.assertEqual(self.req('GET',path+'/chat',user=1)[0],403)
        self.assertEqual(self.req('POST',path+'/chat',{'text':'不允许'},user=1)[0],403)
        status,file=self.req('POST',path+'/chat/files?name=private-video.mp4',raw=b'secretvideo',user=0)
        self.assertEqual(status,201)
        status,message=self.req('POST',path+'/chat',{'text':'private-secret-discussion','attachments':[file['id']]},user=0)
        self.assertEqual(status,201)
        for public_path in ('/v1/tasks',path,path+'/entries?after=0'):
            _,data=self.req('GET',public_path)
            serialized=json.dumps(data)
            self.assertNotIn('private-secret-discussion',serialized)
            self.assertNotIn(file['id'],serialized)
            self.assertNotIn('private-video',serialized)
        self.assertEqual(self.req('GET',file['url'])[0],401)
        self.assertEqual(self.req('GET',file['url'],user=1)[0],403)
        self.assertEqual(self.req('GET',path+'/files/'+file['id'])[0],404)
        self.assertEqual(self.req('POST',path+'/join',{},user=1)[0],200)
        status,data=self.req('GET',path+'/chat',user=1)
        self.assertEqual(status,200)
        self.assertEqual(data['messages'][0]['text'],'private-secret-discussion')
        self.assertEqual(self.req('GET',file['url'],user=1,binary=True)[1],b'secretvideo')
        status,reply=self.req('POST',path+'/chat',{'text':'回复','reply_to':message['id']},user=1)
        self.assertEqual(status,201)
        status,data=self.req('GET',path+'/chat?after=1',user=0)
        # 加入任务产生一条系统事件（sequence 2），它同样占游标，所以这页是 2、3。
        self.assertEqual([m['sequence'] for m in data['messages']],[2,3])
        self.assertEqual(data['next_cursor'],3)
        self.assertEqual(data['messages'][-1]['text'],'回复')
        self.assertEqual(self.req('POST',path+'/chat',{'text':'坏引用','reply_to':'missing'},user=1)[0],400)

    def test_cursor_by_next_cursor_or_last_sequence_is_identical(self):
        path='/v1/tasks/'+self.task['number']
        self.req('POST',path+'/chat',{'text':'m1'},user=0)
        self.req('POST',path+'/join',{},user=1)
        self.req('POST',path+'/chat',{'text':'m2'},user=0)
        self.req('POST',path+'/chat',{'text':'m3'},user=0)
        with patch.object(chat,'PAGE_SIZE',2):
            status,first=self.req('GET',path+'/chat?after=0',user=0)
            self.assertEqual(status,200)
            self.assertEqual([m['sequence'] for m in first['messages']],[1,2])
            self.assertTrue(first['has_more'])
            last_sequence=first['messages'][-1]['sequence']
            by_cursor=self.req('GET',path+f"/chat?after={first['next_cursor']}",user=0)[1]
            by_sequence=self.req('GET',path+f'/chat?after={last_sequence}',user=0)[1]
            self.assertEqual(by_cursor['messages'],by_sequence['messages'])
            self.assertEqual(by_cursor['next_cursor'],by_sequence['next_cursor'])
            seen=[];cursor=0
            while True:
                page=self.req('GET',path+f'/chat?after={cursor}',user=0)[1]
                seen += [m['sequence'] for m in page['messages']]
                if not page['has_more']:break
                cursor=page['next_cursor']
            self.assertEqual(seen,list(range(1,len(seen)+1)))
            updates=self.req('GET',path+f"/updates?chat_after={first['next_cursor']}",user=0)[1]
            updates_by_sequence=self.req('GET',path+f'/updates?chat_after={last_sequence}',user=0)[1]
            self.assertEqual(updates['chat']['messages'],updates_by_sequence['chat']['messages'])
            self.assertEqual(updates['chat']['next_cursor'],updates_by_sequence['chat']['next_cursor'])

    def test_run_join_membership_is_instance_bound(self):
        path='/v1/tasks/'+self.task['id']
        self.assertEqual(self.req('GET',path+'/chat',agent=0)[0],403)
        self.run_for(0)
        self.assertEqual(self.req('GET',path+'/chat',agent=0)[0],200)
        self.assertEqual(self.req('GET',path+'/chat',agent=1)[0],403)
        self.assertEqual(self.req('POST',path+'/chat',{'text':'x','agent_id':'spoof'},agent=0)[0],400)
        self.identity.revoke(self.users[0]['user_id'],self.agents[0]['agent_id'])
        self.assertEqual(self.req('GET',path+'/chat',agent=0)[0],401)

    def test_mentions_must_reference_members(self):
        path='/v1/tasks/'+self.task['id']
        outsider=self.agents[1]['agent_id']
        self.assertEqual(self.req('POST',path+'/chat',{'text':'@别人','mentions':[outsider]},user=0)[0],400)
        self.req('POST',path+'/join',{},agent=1)
        status,message=self.req('POST',path+'/chat',{'text':'请补充','mentions':[outsider,outsider]},user=0)
        self.assertEqual(status,201)
        self.assertEqual(len(message['mentions']),1)
        self.assertEqual(message['mentions'][0]['member_id'],outsider)
        self.assertEqual(message['mentions'][0]['alias'],'开发助手')
        _,public=self.req('GET',path)
        self.assertNotIn(message['id'],str(public))
