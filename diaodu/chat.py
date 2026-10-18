"""Private task chat; kept outside the public task projection."""
import secrets
import re
from diaodu.identity import AuthError,text
from diaodu.store import now


def member_id(actor):
    return actor.get('agent_id') or actor.get('user_id') if actor else None


def members(task):
    found={}
    for actor in [task.get('publisher')]+[e.get('identity') for e in task['entries']]+task.get('members',[]):
        if member_id(actor):found[member_id(actor)]={k:v for k,v in actor.items() if k not in ('csrf','token')}
    publisher=member_id(task.get('publisher'))
    for key,person in found.items():
        person['member_kind']=person.get('member_kind','agent')
        person['is_publisher']=key==publisher
        person['entry_count']=sum(member_id(e.get('identity'))==key and e.get('kind')=='report' for e in task['entries'])
    return found


def require(task,actor):
    if not actor or member_id(actor) not in members(task):
        raise AuthError('只有任务参与者可以访问群聊，请先参与任务',403)


def join(library,task_id,actor):
    from diaodu.library import _find
    with library._lock:
        data=library._load();task=_find(data['tasks'],task_id)
        if member_id(actor) not in members(task):
            task.setdefault('members',[]).append({k:v for k,v in actor.items() if k not in ('csrf','token')})
            system(task, (actor.get('agent_name') or actor.get('display_name','参与者'))+' 加入了任务')
            library._save(data)
        return {'joined':True,'task_id':task['id'],'member_id':member_id(actor)}


def read(library,task_id,actor,after=0):
    from diaodu.library import _find
    with library._lock:
        task=_find(library._load()['tasks'],task_id);require(task,actor)
        messages=task.get('chat',[])
        if after<0 or after>len(messages):raise ValueError('群聊游标超出范围')
        batch=messages[after:after+100]
        return {'messages':[decorate(task,m) for m in batch], 'pinned':[decorate(task,m) for m in messages if m.get('pinned')],'next_cursor':after+len(batch),'has_more':after+len(batch)<len(messages),'members':list(members(task).values())}


def post(library,task_id,actor,body):
    from diaodu.library import _find
    if {'name','identity','agent_id','user_id','agent_name','agent_type'}.intersection(body):raise ValueError('身份由凭证决定')
    content=body.get('text','')
    if not isinstance(content,str) or len(content)>20000:raise ValueError('消息文字最多20000字符')
    ids=body.get('attachments',[])
    if not isinstance(ids,list) or len(ids)>20 or any(not isinstance(i,str) for i in ids):raise ValueError('附件编号数组最多20个')
    if not content.strip() and not ids:raise ValueError('消息或附件不能为空')
    with library._lock:
        data=library._load();task=_find(data['tasks'],task_id);require(task,actor)
        files={f['id']:f for f in task.get('chat_files',[])}
        if any(i not in files for i in ids):raise ValueError('附件不属于本任务群聊')
        mentioned=body.get('mentions',[])
        roster=members(task)
        if not isinstance(mentioned,list) or len(mentioned)>50 or any(not isinstance(key,str) or key not in roster for key in mentioned):
            raise ValueError('@对象必须是本任务成员编号，最多50位')
        mentions=[{'member_id':key,'alias':roster[key].get('agent_name') or roster[key].get('display_name'),'agent_type':roster[key].get('agent_type','人员')} for key in dict.fromkeys(mentioned)]
        messages=task.setdefault('chat',[])
        reply=body.get('reply_to')
        if reply is not None and (not isinstance(reply,str) or not any(m['id']==reply for m in messages)):raise ValueError('引用的群聊消息不存在')
        intent=body.get('intent')
        if intent not in (None,'relay','review'):raise ValueError('intent 必须为 relay 或 review')
        message={'kind':'message','intent':intent,'id':'msg_'+secrets.token_hex(12),'sequence':len(messages)+1,'identity':{k:v for k,v in actor.items() if k not in ('csrf','token')},'text':content.strip(),'attachments':[files[i] for i in dict.fromkeys(ids)],'reply_to':reply,'mentions':mentions,'at':now()}
        messages.append(message);library._save(data)
        return message


def upload(library,task_id,actor,filename,content):
    import mimetypes
    from diaodu.library import _find
    filename=filename.replace('\\','/').split('/')[-1].strip()
    if not filename or len(filename)>240:raise ValueError('文件名不合法')
    if len(content)>100*1024*1024:raise ValueError('文件超过100MB')
    with library._lock:
        data=library._load();task=_find(data['tasks'],task_id);require(task,actor)
        file_id='cf_'+secrets.token_hex(16)
        folder=library.root/'chat-files';folder.mkdir(exist_ok=True)
        (folder/file_id).write_bytes(content)
        item={'id':file_id,'name':filename,'size':len(content),'type':mimetypes.guess_type(filename)[0] or 'application/octet-stream','url':f'{library.public_base_path}/v1/tasks/{task["id"]}/chat/files/{file_id}'}
        task.setdefault('chat_files',[]).append(item);library._save(data);return item


def locate(library,task_id,actor,file_id):
    from diaodu.library import _find
    with library._lock:
        task=_find(library._load()['tasks'],task_id);require(task,actor)
        item=next((f for f in task.get('chat_files',[]) if f['id']==file_id),None)
        if item is None:raise LookupError('没有这个群聊附件')
        return item,library.root/'chat-files'/item['id']

def download(library,task_id,actor,file_id):
    item,path=locate(library,task_id,actor,file_id)
    return item,path.read_bytes()


def publisher(task,actor):
    owner=task.get('publisher') or {}
    if not actor or actor.get('user_id')!=owner.get('user_id') or (owner.get('agent_id') and actor.get('agent_id')!=owner['agent_id']) or (not owner.get('agent_id') and actor.get('kind')!='user'):
        raise AuthError('只有发布者可以操作',403)


def system(task,content,ref=None):
    messages=task.setdefault('chat',[])
    messages.append({'id':'msg_'+secrets.token_hex(12),'sequence':len(messages)+1,'kind':'system','identity':{},'text':content,'attachments':[],'mentions':[],'reply_to':None,'at':now(),'refs':[ref] if ref else []})


def decorate(task,message):
    m=dict(message);roster=members(task);mid=member_id(m.get('identity'))
    m['identity']=roster.get(mid,m.get('identity',{}))
    m['kind']=m.get('kind','message')
    m['trust']='publisher' if mid and mid==member_id(task.get('publisher')) else 'participant'
    ids=list(dict.fromkeys(re.findall(r'en_[0-9a-f]{8}',m.get('text',''))+m.get('refs',[])))
    m['refs']=[{'id':e['id'],'title':e['text'].split('\n')[0][:80],'author':e.get('name',''),'attachments':len(e.get('attachments',[])),'review':e.get('review',{'state':'pending'})} for e in task['entries'] if e['id'] in ids]
    reply=next((r for r in task.get('chat',[]) if r['id']==m.get('reply_to')),None)
    m['reply_preview']={'id':reply['id'],'author':reply.get('identity',{}).get('agent_name','参与者'),'text':reply['text'][:80]} if reply else None
    return m


def review(library,task_id,entry_id,actor,body):
    from diaodu.library import _find
    state=body.get('state');note=body.get('note','')
    if state not in ('accepted','returned'):raise ValueError('验收状态须为 accepted 或 returned')
    if not isinstance(note,str) or len(note)>2000:raise ValueError('验收意见最多2000字符')
    if state=='returned' and not note.strip():raise ValueError('退回时请说明原因')
    with library._lock:
        data=library._load();task=_find(data['tasks'],task_id);publisher(task,actor)
        entry=next((e for e in task['entries'] if e['id']==entry_id and e['kind']=='report'),None)
        if not entry:raise LookupError('没有这个成果')
        entry['review']={'state':state,'note':note.strip(),'at':now()}
        task['updated']=now();system(task,'发布者 '+('采纳' if state=='accepted' else '退回')+' 了成果 '+entry_id,entry_id)
        library._save(data);return entry['review']


def action(library,task_id,message_id,actor,operation):
    from diaodu.library import _find
    with library._lock:
        data=library._load();task=_find(data['tasks'],task_id);publisher(task,actor)
        message=next((m for m in task.get('chat',[]) if m['id']==message_id and m.get('kind')!='system'),None)
        if not message:raise LookupError('消息不存在')
        if operation=='pin':
            if member_id(message.get('identity'))!=member_id(actor):raise AuthError('只能置顶发布者自己的消息',403)
            message['pinned']=not message.get('pinned',False);result={'pinned':message['pinned']}
        else:
            if message.get('promoted_entry'):return {'entry_id':message['promoted_entry']}
            eid='en_'+secrets.token_hex(4)
            task['entries'].append({'id':eid,'name':actor.get('agent_name','发布者'),'identity':{k:v for k,v in actor.items() if k not in ('csrf','token')},'kind':'note','text':message['text'],'status':None,'note':None,'at':now(),'attachments':[],'references':[],'remaining':'','next_step':''})
            message['promoted_entry']=eid;task['updated']=now();result={'entry_id':eid}
        library._save(data);return result
