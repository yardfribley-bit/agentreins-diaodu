"""Credential-backed agent client. Credentials are read from environment only."""
import argparse
import json
import os
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request,urlopen


def main():
    parser=argparse.ArgumentParser(description="AgentReins智能体接入")
    parser.add_argument('--url',default='http://127.0.0.1:8765')
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('list')
    commands.add_parser('me')
    parser.add_argument('--state-file',type=Path,default=Path.home()/'.diaodu'/'client.json')
    connect=commands.add_parser('connect');connect.add_argument('--alias',required=True);connect.add_argument('--type',default='其他')
    for verb in ('read','run','say','report','changes','download','context','updates','join','chat','alias'):
        command=commands.add_parser(verb)
        command.add_argument('task')
        if verb=='alias':command.add_argument('--name',required=True)
        if verb=='updates':
            command.add_argument('--entry-after',type=int,default=0);command.add_argument('--chat-after',type=int,default=0)
        if verb=='chat':
            command.add_argument('--text',required=True);command.add_argument('--mention',action='append',default=[])
        if verb in ('say','report'):
            command.add_argument('--run-id',required=True)
            command.add_argument('--text',default='')
        if verb=='changes': command.add_argument('--after',type=int,default=0)
        if verb=='download':
            command.add_argument('file_id')
            command.add_argument('--output',type=Path,required=True)
        if verb=='report':
            command.add_argument('--status',choices=('in_progress','completed','blocked'),default='completed')
            command.add_argument('--remaining',default='')
            command.add_argument('--next-step',default='')
            command.add_argument('--reference',action='append',default=[])
            command.add_argument('--attachment',action='append',default=[])
            command.add_argument('--file',type=Path,action='append',default=[])
            command.add_argument('--request-id')
    args=parser.parse_args()
    token=os.environ.get('DIAODU_AGENT_TOKEN','')
    saved={}
    if args.state_file.exists():
        saved=json.loads(args.state_file.read_text())
    key=args.url.rstrip('/')
    token=token or saved.get(key,{}).get('token','')
    if not token and args.command not in ('connect','list','read','context'):parser.error('先connect或设置DIAODU_AGENT_TOKEN')
    def request(path,payload=None,raw=None,binary=False):
        data=raw if raw is not None else json.dumps(payload).encode() if payload is not None else None
        headers={'Content-Type':'application/octet-stream' if raw is not None else 'application/json'}
        if token:headers['Authorization']='Bearer '+token
        req=Request(args.url.rstrip('/')+path,data=data,headers=headers)
        with urlopen(req,timeout=120) as response:
            return response.read() if binary else json.load(response)
    try:
        if args.command=='connect':
            if token:
                result=request('/v1/me')
                result['reused']=True
            else:
                result=request('/v1/connect',{'alias':args.alias,'agent_type':args.type})
                secret=result.pop('token');result.pop('csrf',None)
                saved[key]={'token':secret}
                args.state_file.parent.mkdir(parents=True,exist_ok=True)
                fd=os.open(args.state_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
                os.fchmod(fd,0o600)
                with os.fdopen(fd,'w') as file:json.dump(saved,file)
                result['credential_saved']=str(args.state_file)
        elif args.command=='list': result=request('/v1/discover')
        elif args.command=='me': result=request('/v1/me')
        else:
            path='/v1/tasks/'+quote(args.task,safe='')
            if args.command=='context':result=request(path+'/context')
            elif args.command=='updates':result=request(path+f'/updates?entry_after={args.entry_after}&chat_after={args.chat_after}')
            elif args.command=='join':result=request(path+'/join',{})
            elif args.command=='chat':result=request(path+'/chat',{'text':args.text,'mentions':args.mention})
            elif args.command=='alias':result=request('/v1/me/alias',{'alias':args.name})
            elif args.command=='read': result=request(path)
            elif args.command=='changes': result=request(path+'/entries?after='+str(args.after))
            elif args.command=='run': result=request(path+'/runs',{})
            elif args.command=='download':
                content=request(path+'/files/'+quote(args.file_id,safe=''),binary=True)
                with args.output.open('xb') as file: file.write(content)
                result={'saved':str(args.output)}
            else:
                ids=list(getattr(args,'attachment',[]))
                files=getattr(args,'file',[])
                if not args.text.strip() and not files and not ids: parser.error('需要 --text 或附件')
                if len(files)+len(ids)>20: parser.error('最多 20 个附件')
                if files and getattr(args,'request_id',None): parser.error('幂等重试使用 --attachment 复用已上传附件编号')
                for file in files:
                    if not file.is_file() or file.stat().st_size>100*1024*1024: parser.error('附件不存在或超过 100 MB')
                for file in files:
                    uploaded=request(path+'/files?name='+quote(file.name)+'&run_id='+quote(args.run_id),raw=file.read_bytes())
                    ids.append(uploaded['id'])
                body={'run_id':args.run_id,'text':args.text}
                if args.command=='report': body.update(attachments=ids,status=args.status,remaining=args.remaining,next_step=args.next_step,references=args.reference,request_id=args.request_id)
                result=request(path+'/'+args.command,body)
    except HTTPError as exc:
        try: message=json.load(exc).get('error',str(exc.code))
        finally: exc.close()
        parser.exit(1,'请求失败：'+message+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
