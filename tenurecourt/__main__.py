import argparse,json
from pathlib import Path
from .store import connect,execute,replay
def main():
 p=argparse.ArgumentParser();p.add_argument('command',choices=['tenure','transfer','cleanup','demo','replay']);p.add_argument('--db',default='demo.sqlite');p.add_argument('--fixture');a=p.parse_args()
 if a.command=='replay':
  r=replay(a.db);print(json.dumps(r,ensure_ascii=False));raise SystemExit(0 if r['verified'] else 2)
 db=connect(a.db)
 tasks=[(a.command,a.fixture)] if a.command!='demo' else [('tenure','fixtures/tenure_normal.json'),('cleanup','fixtures/cleanup_normal.json'),('transfer','fixtures/transfer_normal.json'),('tenure','fixtures/tenure_failure.json'),('transfer','fixtures/transfer_failure.json'),('cleanup','fixtures/cleanup_failure.json')]
 result=[]
 for sub,file in tasks:
  if not file:p.error('--fixture required')
  data=json.loads(Path(file).read_text(encoding='utf-8'))
  result.append({'fixture':file,'results':[execute(db,sub,x) for x in (data if isinstance(data,list) else [data])]})
 print(json.dumps(result,ensure_ascii=False,indent=2));db.close()
if __name__=='__main__':main()
