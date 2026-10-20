import unittest,tempfile,json
from pathlib import Path
from diaodu.attention import Attention
import test_identity as fixtures
class AttentionUnit(unittest.TestCase):
 def test_dedupe_persistence_channels_and_privacy(self):
  with tempfile.TemporaryDirectory() as d:
   a=Attention(Path(d))
   for t in (0,10,1799,1800):a.record('task','browser','browser-one',at=t)
   a.record('task','browser','browser-two',at=1800)
   a.record('task','api','192.0.2.1',at=1800);a.record('task','api','192.0.2.1',at=1801)
   s=Attention(Path(d)).stats('task')
   self.assertEqual((s['page_views'],s['unique_browsers'],s['api_reads'],s['unique_api_sources']),(3,2,2,1))
   with a.db() as db:self.assertNotIn('192.0.2.1',str(db.execute('SELECT * FROM visitors').fetchall()))
   self.assertEqual(a.stats('other')['page_views'],0)
class AttentionAPI(unittest.TestCase):
 setUp=fixtures.IdentityAPI.setUp
 tearDown=fixtures.IdentityAPI.tearDown
 req=fixtures.IdentityAPI.req
 def test_anonymous_views_api_and_invalid_task(self):
  path='/v1/tasks/'+self.task['number']
  h={'User-Agent':'Mozilla/5.0','Cookie':'diaodu_visitor='+'a'*32}
  for _ in range(2):self.assertEqual(self.req('POST',path+'/view',{},headers=h)[0],200)
  _,t=self.req('GET',path)
  self.assertEqual(t['attention']['page_views'],1);self.assertEqual(t['attention']['api_reads'],1)
  self.req('GET','/v1/tasks');self.req('GET','/v1/tasks')
  self.assertEqual(self.library.read_task(self.task['id'])['attention']['api_reads'],1)
  self.assertEqual(self.req('POST','/v1/tasks/T999999/view',{},headers=h)[0],404)
