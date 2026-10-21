"""Opt-in outreach queue. Agents research; the service sends within mailbox limits."""
import email.utils,hashlib,json,re,secrets,time,threading
from datetime import datetime,timedelta
from zoneinfo import ZoneInfo
from urllib.parse import urlparse
from diaodu.mailbox import actor_id,address,MailError
AUDIENCE='国内 AI Agent 开发者、独立开发者和小型 AI 团队；中文；仅公开业务联系邮箱'
class Outreach:
 def __init__(self,mail,base_url,identity=None):
  self.identity=identity
  self.mail=mail;self.base_url=base_url.rstrip('/');self.stop=threading.Event()
  with mail.db() as db:
   db.execute('CREATE TABLE IF NOT EXISTS campaigns(owner TEXT PRIMARY KEY,enabled INTEGER,updated REAL)')
   db.execute('CREATE TABLE IF NOT EXISTS leads(id TEXT PRIMARY KEY,owner TEXT,address_hash TEXT,payload BLOB,state TEXT,created REAL,attempted REAL,token TEXT UNIQUE,draft_id TEXT,UNIQUE(owner,address_hash))')
   db.execute("UPDATE leads SET state='needs_attention' WHERE state='preparing'")
 def start(self):
  def worker():
   while not self.stop.wait(60):
    try:self.tick()
    except Exception:pass # Outcome stays in the private ledger; never dump mail to logs.
  threading.Thread(target=worker,name='mail-outreach',daemon=True).start()
 def assignments(self,actor):
  key=actor_id(actor)
  with self.mail.db() as db:owners=[r[0] for r in db.execute('SELECT owner FROM campaigns WHERE enabled=1 AND (owner=? OR owner IN (SELECT owner FROM grants WHERE agent=?))',(key,key))]
  return {'campaigns':[self.status(actor,o,include_leads=False) for o in owners]}
 def status(self,actor,owner=None,include_leads=True):
  owner,c=self.mail.resolve(actor,owner)
  with self.mail.db() as db:
   cfg=db.execute('SELECT enabled FROM campaigns WHERE owner=?',(owner,)).fetchone();counts={r[0]:r[1] for r in db.execute('SELECT state,COUNT(*) FROM leads WHERE owner=? GROUP BY state',(owner,))}
   leads=[self.shown(r) for r in db.execute('SELECT * FROM leads WHERE owner=? ORDER BY created DESC LIMIT 200',(owner,))] if include_leads else []
  return {'owner_id':owner,'email':c['email'],'enabled':bool(cfg and cfg[0]),'audience':AUDIENCE,'daily_limit':100,'hourly_limit':10,'counts':counts,'leads':leads,'note':'每天最多100位新客户，最多每小时10封；不足不凑数。只发送已入队且带来源的介绍邮件，不自动决定价格或合作承诺。客户回复后停止该地址的介绍发送。'}
 def configure(self,actor,enabled):
  owner,_=self.mail.resolve(actor)
  if type(enabled) is not bool:raise MailError('请明确是否启用每日自动联系')
  with self.mail.db() as db:db.execute('INSERT INTO campaigns VALUES (?,?,?) ON CONFLICT(owner) DO UPDATE SET enabled=excluded.enabled,updated=excluded.updated',(owner,int(enabled),time.time()))
  return self.status(actor)
 def shown(self,row):
  return dict(self.mail.open(row['payload']),id=row['id'],state=row['state'],created=row['created'],attempted=row['attempted'],draft_id=row['draft_id'])
 def add(self,actor,body):
  owner,c=self.mail.resolve(actor,body.get('owner_id'))
  target=address(body.get('email')).lower();source=body.get('source_url','');evidence=body.get('source_evidence','');reason=body.get('reason','');subject=body.get('subject','');text=body.get('text','')
  u=urlparse(source) if isinstance(source,str) else None
  if not u or u.scheme!='https' or not u.hostname or u.username or len(source)>2000:raise MailError('需要公开业务邮箱所在的HTTPS来源页面')
  if not isinstance(evidence,str) or len(evidence)>2000 or target not in evidence.lower():raise MailError('请提供包含该邮箱的公开业务联系原文，不得猜测地址')
  if body.get('business_contact') is not True or body.get('region')!='CN':raise MailError('仅接受中国客户公开业务联系邮箱')
  if not isinstance(reason,str) or not 10<=len(reason)<=1000:raise MailError('请说明与国内Agent开发者或团队的匹配理由')
  if not isinstance(subject,str) or not 1<=len(subject)<=200 or '\r' in subject or '\n' in subject:raise MailError('主题不合法')
  if not isinstance(text,str) or not 20<=len(text)<=4000:raise MailError('介绍正文须为20至4000字')
  if target==c['email'].lower():raise MailError('客户不能是发件邮箱本身')
  if self.base_url not in text:raise MailError('正文需要包含平台真实链接')
  payload={k:body[k] for k in ('source_url','source_evidence','reason','subject','text')};payload['email']=target
  key=hashlib.sha256(target.encode()).hexdigest()
  with self.mail.db() as db:
   old=db.execute('SELECT * FROM leads WHERE owner=? AND address_hash=?',(owner,key)).fetchone()
   if old:return self.shown(old)
   if db.execute("SELECT COUNT(*) FROM leads WHERE owner=? AND state='queued'",(owner,)).fetchone()[0]>=500:raise MailError('待联系队列最多500条，请先处理现有客户')
   lid='lead_'+secrets.token_hex(12);db.execute('INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?)',(lid,owner,key,self.mail.seal(payload),'queued',time.time(),0,secrets.token_urlsafe(32),None))
   return self.shown(db.execute('SELECT * FROM leads WHERE id=?',(lid,)).fetchone())
 def unsubscribe(self,token):
  if not isinstance(token,str) or not re.fullmatch(r'[A-Za-z0-9_-]{30,60}',token):raise MailError('退订链接无效')
  with self.mail.db() as db:db.execute("UPDATE leads SET state='opted_out' WHERE token=?",(token,))
  return {'unsubscribed':True,'message':'已停止对此地址的后续推广联系。'}
 def remove(self,actor,lid):
  owner,_=self.mail.resolve(actor)
  with self.mail.db() as db:db.execute("UPDATE leads SET state='opted_out' WHERE id=? AND owner=?",(lid,owner))
  return self.status(actor)
 def tick(self):
  with self.mail.db() as db:owners=[r[0] for r in db.execute('SELECT owner FROM campaigns WHERE enabled=1')]
  for owner in owners:
   if self.identity:
    with self.identity.db() as db:
     valid=db.execute('SELECT revoked FROM agents WHERE id=?',(owner,)).fetchone() if owner.startswith('ag_') else db.execute('SELECT 0 FROM users WHERE id=?',(owner,)).fetchone()
    if valid is None or valid[0]:
     with self.mail.db() as db:db.execute('UPDATE campaigns SET enabled=0 WHERE owner=?',(owner,))
     continue
   actor={'agent_id':owner}
   # Reply and opt-out detection precede every send. Inbox failures stop sends.
   try:
    inbox=self.mail.inbox(actor)
    for header in inbox['messages']:
     sender=email.utils.parseaddr(header.get('from',''))[1].lower()
     with self.mail.db() as db:lead=db.execute('SELECT * FROM leads WHERE owner=? AND address_hash=?',(owner,hashlib.sha256(sender.encode()).hexdigest())).fetchone()
     if not lead:
      if 'mailer-daemon' in sender or 'postmaster' in sender:
       message=self.mail.read(actor,header['id'])
       with self.mail.db() as db:
        for bounced in message.get('bounced_addresses',[]):db.execute("UPDATE leads SET state='bounced' WHERE owner=? AND address_hash=? AND state!='opted_out'",(owner,hashlib.sha256(bounced.encode()).hexdigest()))
      continue
     if lead['state'] in ('opted_out','replied'):continue
     message=self.mail.read(actor,header['id']);stop=any(w in message.get('text','') for w in ('不再联系','不要联系','退订','停止发送'))
     with self.mail.db() as db:db.execute("UPDATE leads SET state=? WHERE id=? AND state!='opted_out'",('opted_out' if stop else 'replied',lead['id']))
   except Exception:continue
   day=datetime.now(ZoneInfo('Asia/Shanghai')).replace(hour=0,minute=0,second=0,microsecond=0).timestamp()
   with self.mail.db() as db:
    if db.execute('SELECT COUNT(*) FROM leads WHERE owner=? AND attempted>?',(owner,day)).fetchone()[0]>=100:continue
    if db.execute('SELECT COUNT(*) FROM leads WHERE owner=? AND attempted>?',(owner,time.time()-3600)).fetchone()[0]>=10:continue
    row=db.execute("SELECT * FROM leads WHERE owner=? AND state='queued' ORDER BY created LIMIT 1",(owner,)).fetchone()
    if not row:continue
    if db.execute("UPDATE leads SET state='preparing',attempted=? WHERE id=? AND state='queued'",(time.time(),row['id'])).rowcount!=1:continue
   try:
    p=self.mail.open(row['payload']);link=self.base_url+'/v1/mail/unsubscribe?token='+row['token']
    text=p['text']+'\n\n——\n这是 AgentReins Hub 的产品推广介绍。如果不希望收到后续联系，请回复“不再联系”，或使用此链接停止：\n'+link
    d=self.mail.draft(actor,{'to':[p['email']],'subject':'[产品推广] '+p['subject'],'text':text,'request_id':'outreach-'+row['id']})
    with self.mail.db() as db:
     payload=self.mail.open(db.execute('SELECT payload FROM drafts WHERE id=?',(d['id'],)).fetchone()[0]);payload['unsubscribe_url']=link
     db.execute('UPDATE drafts SET payload=? WHERE id=?',(self.mail.seal(payload),d['id']))
     db.execute('UPDATE leads SET draft_id=? WHERE id=?',(d['id'],row['id']))
    # Recheck pause/opt-out immediately before SMTP.
    with self.mail.db() as db:
     cfg=db.execute('SELECT enabled FROM campaigns WHERE owner=?',(owner,)).fetchone();state=db.execute('SELECT state FROM leads WHERE id=?',(row['id'],)).fetchone()[0]
    if not cfg or not cfg[0] or state=='opted_out':
     if state!='opted_out':
      with self.mail.db() as db:db.execute("UPDATE leads SET state='queued' WHERE id=?",(row['id'],))
     continue
    result=self.mail.send(actor,d['id'],True)
    with self.mail.db() as db:db.execute("UPDATE leads SET state=? WHERE id=? AND state!='opted_out'",(result['state'],row['id']))
   except Exception:
    with self.mail.db() as db:db.execute("UPDATE leads SET state='needs_attention' WHERE id=? AND state!='opted_out'",(row['id'],))
