import unittest
from urllib.request import Request,urlopen
from urllib.error import HTTPError
import test_identity as fixtures
class MediaAPI(unittest.TestCase):
    setUp=fixtures.IdentityAPI.setUp
    tearDown=fixtures.IdentityAPI.tearDown
    req=fixtures.IdentityAPI.req
    def test_range_head_suffix_and_invalid_ranges(self):
        path='/v1/tasks/'+self.task['id']
        _,file=self.req('POST',path+'/materials?name=video.mp4',raw=b'0123456789',user=0)
        with urlopen(Request(self.base+file['url'],headers={'Range':'bytes=2-5'})) as r:
            self.assertEqual(r.status,206);self.assertEqual(r.read(),b'2345')
            self.assertEqual(r.headers['Content-Range'],'bytes 2-5/10')
            self.assertEqual(r.headers['Accept-Ranges'],'bytes')
        self.assertEqual(self.req('GET',file['url'],headers={'Range':'bytes=-2'},binary=True)[1],b'89')
        with urlopen(Request(self.base+file['url'],method='HEAD')) as r:
            self.assertEqual(r.headers['Content-Length'],'10');self.assertEqual(r.read(),b'')
        with self.assertRaises(HTTPError) as caught:urlopen(Request(self.base+file['url'],headers={'Range':'bytes=100-200'}))
        self.assertEqual(caught.exception.code,416)
        self.assertEqual(caught.exception.headers['Content-Range'],'bytes */10');caught.exception.close()
    def test_private_video_ranges_require_membership(self):
        path='/v1/tasks/'+self.task['id']
        _,file=self.req('POST',path+'/chat/files?name=private.mp4',raw=b'private-video',user=0)
        self.assertEqual(self.req('GET',file['url'],headers={'Range':'bytes=0-2'})[0],401)
        self.assertEqual(self.req('GET',file['url'],headers={'Range':'bytes=0-2'},user=1)[0],403)
        self.assertEqual(self.req('GET',file['url'],headers={'Range':'bytes=0-2'},user=0,binary=True)[1],b'pri')
