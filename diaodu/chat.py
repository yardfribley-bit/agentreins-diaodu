"""Private task chat; kept outside the public task projection."""
import secrets
from diaodu.identity import AuthError,text
from diaodu.store import now


def member_id(actor):
    return actor.get('agent_id') or actor.get('user_id') if actor else None


def members(task):
    found={}
    for actor in [task.get('publisher')]+[e.get('identity') for e in task['entries']]+task.get('members',[]):
        if member_id(actor):found[member_id(actor)]={k:v for k,v in actor.items() if k not in ('csrf','token')}
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
            library._save(data)
        return {'joined':True,'task_id':task['id'],'member_id':member_id(actor)}


def read(library,task_id,actor,after=0):
    from diaodu.library import _find
    with library._lock:
        task=_find(library._load()['tasks'],task_id);require(task,actor)
        messages=task.get('chat',[])
        if after<0 or after>len(messages):raise ValueError('群聊游标超出范围')
        batch=messages[after:after+100]
        return {'messages':batch,'next_cursor':after+len(batch),'has_more':after+len(batch)<len(messages),'members':list(members(task).values())}


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
        message={'id':'msg_'+secrets.token_hex(12),'sequence':len(messages)+1,'identity':{k:v for k,v in actor.items() if k not in ('csrf','token')},'text':content.strip(),'attachments':[files[i] for i in dict.fromkeys(ids)],'reply_to':reply,'mentions':mentions,'at':now()}
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
