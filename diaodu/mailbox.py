"""Private, credential-isolated IMAP/SMTP mailbox and reviewable outbox."""
import base64,contextlib,email,email.policy,email.utils,hashlib,imaplib,json,os,re,secrets,smtplib,sqlite3,ssl,time
from pathlib import Path
from email.message import EmailMessage
from html.parser import HTMLParser
from diaodu.identity import AuthError
PROVIDERS={
 'gname':{'label':'Gname / Share Email（需登录验证）','imap':'imap.share-email.com','smtp':'smtp.share-email.com'},
 'qq':{'label':'QQ 邮箱（客户端授权码）','imap':'imap.qq.com','smtp':'smtp.qq.com'},
 'tencent':{'label':'腾讯企业邮箱（客户端授权码）','imap':'imap.exmail.qq.com','smtp':'smtp.exmail.qq.com'},
 '163':{'label':'163 邮箱（客户端授权码）','imap':'imap.163.com','smtp':'smtp.163.com'},
 'gmail':{'label':'Gmail / Workspace（需应用专用密码）','imap':'imap.gmail.com','smtp':'smtp.gmail.com'}}
class MailError(ValueError):pass
def actor_id(actor):return actor.get('agent_id') or actor['user_id']
def address(value):
 if not isinstance(value,str) or len(value)>254 or not re.fullmatch(r'[^\s@<>;,]+@[^\s@<>;,]+\.[^\s@<>;,]+',value):raise MailError('请填写完整邮箱地址，不包含姓名或换行')
 return value
class PlainHTML(HTMLParser):
 def __init__(self):super().__init__();self.parts=[];self.hide=0
 def handle_starttag(self,tag,attrs):
  if tag in ('script','style'):self.hide+=1
  if tag in ('p','br','div','li'):self.parts.append('\n')
 def handle_endtag(self,tag):
  if tag in ('script','style'):self.hide=max(0,self.hide-1)
 def handle_data(self,data):
  if not self.hide:self.parts.append(data)
def parse_message(raw):
 msg=email.message_from_bytes(raw,policy=email.policy.default);body=msg.get_body(preferencelist=('plain','html'));text=''
 if body:
  text=body.get_content()
  if body.get_content_type()=='text/html':p=PlainHTML();p.feed(text);text=''.join(p.parts)
 attachments=[{'name':p.get_filename() or 'attachment','type':p.get_content_type()} for p in msg.iter_attachments()]
 bounced=[]
 for part in msg.walk():
  if part.get_content_type()=='message/delivery-status':
   for block in part.get_payload() if isinstance(part.get_payload(),list) else []:
    if str(block.get('Action','')).lower()=='failed':
     recipient=str(block.get('Final-Recipient','')).split(';')[-1].strip()
     try:bounced.append(address(recipient).lower())
     except MailError:pass
 return {'bounced_addresses':bounced,'subject':str(msg.get('Subject','')),'from':str(msg.get('From','')),'to':str(msg.get('To','')),'reply_to':str(msg.get('Reply-To',msg.get('From',''))),'date':str(msg.get('Date','')),'message_id':str(msg.get('Message-ID','')),'text':text[:100000],'truncated':len(text)>100000,'attachments':attachments,'untrusted':True}
class Transport:
 def imap(self,c):
  client=imaplib.IMAP4_SSL(PROVIDERS[c['provider']]['imap'],993,ssl_context=ssl.create_default_context(),timeout=15)
  try:client.login(c['email'],c['password']);return client
  except Exception:client.shutdown();raise
 def smtp(self,c):
  client=smtplib.SMTP_SSL(PROVIDERS[c['provider']]['smtp'],465,context=ssl.create_default_context(),timeout=15)
  try:client.login(c['email'],c['password']);return client
  except Exception:client.close();raise
 def test(self,c):
  with self.imap(c) as client:
   code,_=client.select('INBOX',readonly=True)
   if code!='OK':raise MailError('无法打开收件箱')
  with self.smtp(c):pass
 def inbox(self,c,before=None):
  with self.imap(c) as client:
   if client.select('INBOX',readonly=True)[0]!='OK':raise MailError('无法打开收件箱')
   validity=client.response('UIDVALIDITY')[1][0].decode();query='ALL'
   if before:
    v,uid=split_id(before)
    if v!=validity:raise MailError('收件箱编号已变化，请刷新列表')
    if int(uid)<=1:return {'messages':[],'next_before':None}
    query='UID 1:'+str(int(uid)-1)
   code,data=client.uid('search',None,query)
   if code!='OK':raise MailError('读取邮件列表失败')
   ids=(data[0] or b'').split();batch=ids[-20:];items=[]
   for uid in reversed(batch):
    code,data=client.uid('fetch',uid,'(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE MESSAGE-ID)])')
    raw=next((x[1] for x in data if isinstance(x,tuple)),None)
    if code=='OK' and raw:items.append(dict(parse_message(raw),id=validity+'.'+uid.decode()))
   return {'messages':items,'next_before':validity+'.'+batch[0].decode() if len(ids)>20 else None}
 def read(self,c,message_id):
  validity,uid=split_id(message_id)
  with self.imap(c) as client:
   if client.select('INBOX',readonly=True)[0]!='OK':raise MailError('无法打开收件箱')
   if client.response('UIDVALIDITY')[1][0].decode()!=validity:raise MailError('邮件编号已变化，请刷新列表')
   code,data=client.uid('fetch',uid,'(RFC822.SIZE)');size=b' '.join(x for x in data if isinstance(x,bytes));match=re.search(rb'RFC822.SIZE (\d+)',size)
   if code!='OK' or not match:raise MailError('邮件不存在')
   if int(match[1])>5*1024*1024:raise MailError('邮件超过5MB，请在原邮箱查看；未下载大邮件')
   code,data=client.uid('fetch',uid,'(BODY.PEEK[])');raw=next((x[1] for x in data if isinstance(x,tuple)),None)
   if code!='OK' or raw is None:raise MailError('无法读取邮件')
   return dict(parse_message(raw),id=message_id)
 def send(self,c,draft):
  msg=EmailMessage();msg['From']=c['email'];msg['To']=', '.join(draft['to']);msg['Subject']=draft['subject'];msg['Message-ID']=draft['message_id'];msg['Date']=email.utils.formatdate(localtime=False)
  if draft.get('in_reply_to'):msg['In-Reply-To']=draft['in_reply_to']
  if draft.get('unsubscribe_url'):
   msg['List-Unsubscribe']='<'+draft['unsubscribe_url']+'>'
   msg['List-Unsubscribe-Post']='List-Unsubscribe=One-Click'
  msg.set_content(draft['text'])
  for a in draft.get('attachments',[]):msg.add_attachment(base64.b64decode(a['content_base64']),maintype='application',subtype='octet-stream',filename=a['name'])
  with self.smtp(c) as client:
   if client.send_message(msg,from_addr=c['email'],to_addrs=draft['to']):raise MailError('部分收件人被拒绝，其他收件人可能已接受；请勿自动重发')
def split_id(value):
 if not isinstance(value,str) or not re.fullmatch(r'[0-9]+\.[0-9]+',value):raise MailError('邮件编号不合法')
 return value.split('.')
class Mailbox:
 def __init__(self,root,key_path=None,transport=None):
  self.path=root/'mailbox.sqlite3';self.transport=transport or Transport();self.cipher=None
  key_path=key_path or os.environ.get('DIAODU_MAIL_KEY_FILE') or (str(Path(os.environ['CREDENTIALS_DIRECTORY'])/'mail.key') if os.environ.get('CREDENTIALS_DIRECTORY') else None)
  if key_path and Path(key_path).is_file():
   from cryptography.fernet import Fernet
   self.cipher=Fernet(Path(key_path).read_bytes().strip())
  with self.db() as db:
   db.execute('CREATE TABLE IF NOT EXISTS accounts(owner TEXT PRIMARY KEY,payload BLOB)')
   db.execute('CREATE TABLE IF NOT EXISTS grants(owner TEXT,agent TEXT,PRIMARY KEY(owner,agent))')
   db.execute('CREATE TABLE IF NOT EXISTS drafts(id TEXT PRIMARY KEY,owner TEXT,author TEXT,request_id TEXT,payload BLOB,state TEXT,created REAL,error TEXT,attempted REAL DEFAULT 0,UNIQUE(owner,author,request_id))')
   db.execute("UPDATE drafts SET state='unknown',error='服务中断，结果未知；请先核对发件记录，不自动重发' WHERE state='sending'")
  os.chmod(self.path,0o600)
 @contextlib.contextmanager
 def db(self):
  db=sqlite3.connect(self.path,timeout=10);db.row_factory=sqlite3.Row
  try:
   with db:yield db
  finally:db.close()
 def seal(self,value):
  if not self.cipher:raise MailError('邮件服务尚未配置加密密钥，不能保存邮箱')
  return self.cipher.encrypt(json.dumps(value,ensure_ascii=False).encode())
 def open(self,value):
  if not self.cipher:raise MailError('邮件服务尚未配置')
  return json.loads(self.cipher.decrypt(value))
 def resolve(self,actor,owner=None):
  key=actor_id(actor);owner=owner or key
  with self.db() as db:
   if owner!=key and not db.execute('SELECT 1 FROM grants WHERE owner=? AND agent=?',(owner,key)).fetchone():raise AuthError('未获此邮箱的授权',403)
   row=db.execute('SELECT payload FROM accounts WHERE owner=?',(owner,)).fetchone()
  if not row:raise MailError('尚未连接邮箱')
  return owner,self.open(row[0])
 def status(self,actor):
  key=actor_id(actor)
  with self.db() as db:
   row=db.execute('SELECT payload FROM accounts WHERE owner=?',(key,)).fetchone();grants=[r[0] for r in db.execute('SELECT agent FROM grants WHERE owner=?',(key,))]
  c=self.open(row[0]) if row else {}
  return {'available':bool(self.cipher),'connected':bool(row),'email':c.get('email'),'provider':c.get('provider'),'tested_at':c.get('tested_at'),'grants':grants,'providers':[{ 'id':k,'label':v['label']} for k,v in PROVIDERS.items()]}
 def connect(self,actor,body):
  if not self.cipher:raise MailError('邮件加密服务尚未配置')
  if body.get('consent') is not True:raise MailError('请同意连接邮箱用于收发邮件')
  c={'email':address(body.get('email')),'provider':body.get('provider'),'password':body.get('password')}
  if c['provider'] not in PROVIDERS:raise MailError('不支持此服务商')
  if not isinstance(c['password'],str) or not 1<=len(c['password'])<=1024:raise MailError('请填写客户端密码或授权码')
  try:self.transport.test(c)
  except Exception:raise MailError('收发验证未通过：请检查服务商、客户端授权和密码；未保存本次凭证') from None
  c['tested_at']=time.time()
  with self.db() as db:
   key=actor_id(actor);existing=db.execute('SELECT payload FROM accounts WHERE owner=?',(key,)).fetchone()
   if existing and (self.open(existing[0])['email'],self.open(existing[0])['provider'])!=(c['email'],c['provider']):raise MailError('更换邮箱前请先断开原邮箱')
   db.execute('INSERT INTO accounts VALUES (?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload',(key,self.seal(c)))
  return self.status(actor)
 def disconnect(self,actor):
  key=actor_id(actor)
  with self.db() as db:
   if db.execute("SELECT 1 FROM drafts WHERE owner=? AND state='sending'",(key,)).fetchone():raise MailError('发送进行中，请稍后再断开')
   db.execute('DELETE FROM accounts WHERE owner=?',(key,));db.execute('DELETE FROM grants WHERE owner=?',(key,));db.execute('DELETE FROM drafts WHERE owner=?',(key,))
  return {'disconnected':True}
 def grant(self,actor,agent,allow):
  self.resolve(actor)
  if not isinstance(agent,str) or not re.fullmatch(r'ag_[a-f0-9]+',agent):raise MailError('请填写完整智能体实例编号')
  with self.db() as db:
   if allow is True:db.execute('INSERT OR IGNORE INTO grants VALUES (?,?)',(actor_id(actor),agent))
   else:db.execute('DELETE FROM grants WHERE owner=? AND agent=?',(actor_id(actor),agent))
  return self.status(actor)
 def inbox(self,actor,owner=None,before=None):
  _,c=self.resolve(actor,owner)
  try:return self.transport.inbox(c,before)
  except MailError:raise
  except Exception:raise MailError('读取收件箱失败，请检查邮箱服务连接') from None
 def read(self,actor,message_id,owner=None):
  _,c=self.resolve(actor,owner);split_id(message_id)
  try:return self.transport.read(c,message_id)
  except MailError:raise
  except Exception:raise MailError('读取邮件失败，请稍后重试') from None
 def draft(self,actor,body):
  owner,c=self.resolve(actor,body.get('owner_id'));author=actor_id(actor);request_id=body.get('request_id')
  if not isinstance(request_id,str) or not 1<=len(request_id)<=128:raise MailError('需要1至128字的request_id避免重复建草稿')
  to=body.get('to');subject=body.get('subject');text=body.get('text')
  if not isinstance(to,list) or not 1<=len(to)<=5:raise MailError('收件人须为1至5个邮箱地址')
  to=[address(x) for x in to]
  if not isinstance(subject,str) or not 1<=len(subject)<=300 or any(x in subject for x in ('\r','\n')):raise MailError('主题须为1至300字且不能换行')
  if not isinstance(text,str) or not 1<=len(text)<=100000:raise MailError('正文须为1至100000字')
  attachments=body.get('attachments',[]);size=0
  if not isinstance(attachments,list) or len(attachments)>10:raise MailError('最多10个附件')
  for a in attachments:
   if not isinstance(a,dict) or not isinstance(a.get('name'),str) or not 1<=len(a['name'])<=200 or any(x in a['name'] for x in ('\r','\n','/','\\')):raise MailError('附件文件名不合法')
   try:raw=base64.b64decode(a.get('content_base64',''),validate=True)
   except Exception:raise MailError('附件编码不合法')
   size+=len(raw)
  if size>5*1024*1024:raise MailError('附件总量最多5MB')
  draft={'to':to,'subject':subject,'text':text,'attachments':attachments,'message_id':email.utils.make_msgid(),'in_reply_to':''}
  if body.get('reply_id'):
   source=self.read(actor,body['reply_id'],owner);draft['in_reply_to']=source['message_id'] if '\r' not in source['message_id'] and '\n' not in source['message_id'] else ''
  with self.db() as db:
   previous=db.execute('SELECT * FROM drafts WHERE owner=? AND author=? AND request_id=?',(owner,author,request_id)).fetchone()
   if previous:
    old=self.open(previous['payload'])
    if any(old[k]!=draft[k] for k in ('to','subject','text','attachments','in_reply_to')):raise MailError('request_id已用于不同内容，请生成新编号')
    return self.shown(previous)
   did='mail_'+secrets.token_hex(12);db.execute('INSERT INTO drafts(id,owner,author,request_id,payload,state,created,error) VALUES (?,?,?,?,?,?,?,?)',(did,owner,author,request_id,self.seal(draft),'draft',time.time(),''))
   return self.shown(db.execute('SELECT * FROM drafts WHERE id=?',(did,)).fetchone())
 def shown(self,row):
  d=self.open(row['payload']);d['attachments']=[{'name':a['name'],'size':len(base64.b64decode(a['content_base64']))} for a in d.get('attachments',[])]
  return dict(d,id=row['id'],owner_id=row['owner'],author_id=row['author'],state=row['state'],created=row['created'],error=row['error'])
 def outbox(self,actor):
  with self.db() as db:return {'drafts':[self.shown(r) for r in db.execute('SELECT * FROM drafts WHERE owner=? ORDER BY created DESC LIMIT 100',(actor_id(actor),))]}
 def send(self,actor,draft_id,confirmed):
  if confirmed is not True:raise MailError('需本人预览草稿后确认发送')
  owner,c=self.resolve(actor)
  with self.db() as db:
   row=db.execute('SELECT * FROM drafts WHERE id=? AND owner=?',(draft_id,owner)).fetchone()
   if not row:raise AuthError('无权发送此草稿',403)
   if row['state']!='draft':return self.shown(row)
   if db.execute("SELECT COUNT(*) FROM drafts WHERE owner=? AND attempted>?",(owner,time.time()-86400)).fetchone()[0]>=100:raise MailError('24小时最多发送100封，请稍后再试')
   if db.execute("SELECT COUNT(*) FROM drafts WHERE owner=? AND state!='draft' AND attempted>?",(owner,time.time()-3600)).fetchone()[0]>=10:raise MailError('每小时最多发送10封，请稍后再试')
   if db.execute("UPDATE drafts SET state='sending',attempted=? WHERE id=? AND state='draft'",(time.time(),draft_id)).rowcount!=1:raise MailError('发送状态已变化，请刷新')
   draft=self.open(row['payload'])
  try:self.transport.send(c,draft);state='accepted';error=''
  except (smtplib.SMTPAuthenticationError,smtplib.SMTPRecipientsRefused):state='failed';error='认证或收件人被拒绝；未确认发送成功'
  except Exception:state='unknown';error='发送结果未知，请核对原邮箱发件记录或向收件人确认；不自动重发'
  with self.db() as db:
   db.execute('UPDATE drafts SET state=?,error=? WHERE id=?',(state,error,draft_id));return self.shown(db.execute('SELECT * FROM drafts WHERE id=?',(draft_id,)).fetchone())
