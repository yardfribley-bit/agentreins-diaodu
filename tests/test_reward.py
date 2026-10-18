import unittest
import test_identity as fixtures
class RewardAPI(unittest.TestCase):
    setUp=fixtures.IdentityAPI.setUp
    tearDown=fixtures.IdentityAPI.tearDown
    req=fixtures.IdentityAPI.req
    def test_reward_is_optional_and_exact(self):
        for value,expected in [(None,None),('',None),('0',None),('88.80','88.80')]:
            status,task=self.req('POST','/v1/tasks',{'requirement':'自愿奖励任务','reward_amount':value},user=0)
            self.assertEqual(status,201)
            if expected:self.assertEqual(task['reward']['amount'],expected)
            else:self.assertIsNone(task['reward'])
    def test_invalid_reward_rejected(self):
        for value in ('-1','nan','Infinity','1.001',True,'1000000000'):
            self.assertEqual(self.req('POST','/v1/tasks',{'requirement':'x','reward_amount':value},user=0)[0],400)
