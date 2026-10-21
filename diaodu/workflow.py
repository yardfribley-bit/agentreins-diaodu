"""Explicit work ownership; report completion never closes a task."""
import copy
from diaodu.identity import AuthError
from diaodu.library import _find
from diaodu.store import now
STAGES={'in_progress':'进行中','waiting_review':'等待复测 / 验收','blocked':'受阻','completed':'已完成','cancelled':'已取消','unconfirmed':'历史进度待确认'}
def mid(a):return (a or {}).get('agent_id') or (a or {}).get('user_id')
def person(a):return {k:a[k] for k in ('agent_id','user_id','agent_name','user_name','agent_type','member_kind') if k in a}
def snapshot(task):
 w=copy.deepcopy(task.get('workflow'))
 if w:return w
 entries=task.get('entries',[]);latest=entries[-1] if entries else None
 hints={
 'en_d595e9da':('退回修复；含截图与 Codex 实测受阻项','建议 Cursor 继续修复；截图需 Mac 主人解锁'),
 'en_a24ff795':('最新部署已提交，申请复测','建议 Grok 复测，之后由发布者确认'),
 'en_161cea1c':('P0 代码已提交，真机验证未完成','真机测试负责人尚未确认')}
 summary,waiting=hints.get((latest or {}).get('id'),((latest.get('text','').split('\n')[0][:240] or '已有交付记录，当前分工尚未确认') if latest else '等待参与者认领',(latest or {}).get('next_step','')))
 return {'revision':0,'stage':'unconfirmed','summary':summary,'waiting':waiting[:1000],'work':[],'history':[],'confirmed':False,'updated_at':(latest or {}).get('at',task['created']),'source_entry_id':(latest or {}).get('id'),'source_actor':person((latest or {}).get('identity') or {})}
def update(library,task_id,actor,body):
 if not isinstance(body,dict):raise ValueError('需要 JSON 对象')
 action=body.get('action');key=mid(actor)
 if action not in ('claim','update','handoff','accept','finish','cancel','reopen'):raise ValueError('未知进度操作')
 for field in ('summary','waiting','scope'):
  if field in body and (not isinstance(body[field],str) or len(body[field])>1000):raise ValueError(field+' 最多1000字')
 with library._lock:
  data=library._load();task=_find(data['tasks'],task_id);w=snapshot(task)
  if type(body.get('revision')) is not int or body['revision']!=w['revision']:raise AuthError('进度已更新，请刷新后重试',409)
  owner=bool(mid(task.get('publisher'))==key and actor.get('user_id')==(task.get('publisher') or {}).get('user_id'))
  if action in ('finish','cancel','reopen'):
   if not owner:raise AuthError('只有发布者可结束、取消或重新打开任务',403)
   w['stage']={'finish':'completed','cancel':'cancelled','reopen':'in_progress'}[action]
  else:
   if w['stage'] in ('completed','cancelled'):raise ValueError('请发布者先重新打开任务')
   lane=next((x for x in w['work'] if mid(x['actor'])==key),None)
   if action=='claim':
    if not body.get('scope','').strip():raise ValueError('请填写你负责的内容')
    if lane is None:lane={'actor':person(actor)};w['work'].append(lane)
    lane.update(scope=body['scope'].strip(),stage='in_progress',handoff=None)
   elif action=='accept':
    source=next((x for x in w['work'] if mid(x.get('handoff'))==key),None)
    if source is None:raise ValueError('没有等待你接手的交接')
    if lane is None:lane={'actor':person(actor)};w['work'].append(lane)
    scope=source['scope']
    if lane.get('stage')!='handed_off' and lane.get('scope') and lane['scope']!=scope:scope=lane['scope']+'；'+scope
    lane.update(scope=scope,stage='in_progress',handoff=None)
    source.update(stage='handed_off',handoff=None,updated_at=now())
   else:
    if lane is None or lane.get('stage')=='handed_off':raise AuthError('请先认领自己的工作',403)
    if action=='handoff':
     target=body.get('target_id')
     people=[task.get('publisher')]+task.get('members',[])+[e.get('identity') for e in task['entries']]
     target_actor=next((p for p in people if p and mid(p)==target),None)
     if not target_actor or target==key:raise ValueError('请选择其他已参与的实例')
     lane['handoff']=person(target_actor)
    else:
     stage=body.get('stage')
     if stage not in ('in_progress','waiting_review','blocked'):raise ValueError('请选择有效的工作阶段')
     lane['stage']=stage
     if body.get('scope','').strip():lane['scope']=body['scope'].strip()
   lane['actor']=person(actor);lane['updated_at']=now()
   active=[x for x in w['work'] if x.get('stage')!='handed_off']
   w['stage']='blocked' if any(x['stage']=='blocked' for x in active) else 'waiting_review' if active and all(x['stage']=='waiting_review' for x in active) else 'in_progress'
  w.update(revision=w['revision']+1,confirmed=True,updated_at=now(),summary=body.get('summary',w.get('summary','')),waiting=body.get('waiting',w.get('waiting','')))
  w['history'].append({'action':action,'actor':person(actor),'at':w['updated_at'],'stage':w['stage'],'summary':w['summary'],'waiting':w['waiting'],'work':copy.deepcopy(w['work'])})
  task['workflow']=w
  if not any(mid(p)==key for p in task.get('members',[])):task.setdefault('members',[]).append(person(actor))
  library._save(data)
  return copy.deepcopy(w)
