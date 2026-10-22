"""Local client for the explicitly delegated outreach agent. Never prints credentials."""
import argparse,json,re,subprocess,sys
from pathlib import Path
from urllib.parse import urlencode
p=argparse.ArgumentParser();p.add_argument('--credentials',required=True);p.add_argument('action',choices=['assignments','status','add','inbox','read','draft','reply']);p.add_argument('--owner');p.add_argument('--id');p.add_argument('--before');a=p.parse_args()
c=json.loads(Path(a.credentials).read_text());token=c['token']
if not re.fullmatch(r'da_[A-Za-z0-9_-]+',token):raise SystemExit('Invalid instance credential')
paths={'assignments':'assignments','status':'outreach','add':'leads','inbox':'inbox','read':'message','draft':'drafts','reply':'reply'};query={}
if a.owner:query['owner_id']=a.owner
if a.id:query['id']=a.id
if a.before:query['before']=a.before
url='https://www.chuhaijian.com/task/v1/mail/'+paths[a.action]+(('?'+urlencode(query)) if query else '')
config='url = '+json.dumps(url)+'\nheader = '+json.dumps('Authorization: Bearer '+token)+'\n'
if a.action in ('add','draft','reply'):
 body=json.load(sys.stdin)
 if a.owner:body['owner_id']=a.owner
 config+='header = "Content-Type: application/json"\nrequest = "POST"\ndata = '+json.dumps(json.dumps(body,ensure_ascii=False),ensure_ascii=False)+'\n'
r=subprocess.run(['curl','--noproxy','*','--silent','--show-error','--fail-with-body','--max-time','90','--config','-'],input=config.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE)
if r.stdout:sys.stdout.buffer.write(r.stdout)
if r.returncode:raise SystemExit('Private mail request failed; see response status, no automatic retry of sending')
