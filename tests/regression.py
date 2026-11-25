import sys,tempfile,time,asyncio
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app import create_app
from fastapi.testclient import TestClient
from ssstik import SsstikDownloader,RateLimitError
class Fake:
 def __init__(self,path): self.path=path
 async def start_browser(self): pass
 async def close_browser(self): pass
 async def download_url(self,url,number,cb):
  await cb(50,'test'); await asyncio.sleep(.02)
  (self.path/f'{number}.mp4').write_bytes(b'\0\0\0\x18ftypisom'+bytes(32))
  return f'{number}.mp4'
u='https://www.tiktok.com/@example/video/12345'
with tempfile.TemporaryDirectory() as d:
 with TestClient(create_app(d,Fake,launch_interval=0)) as c:
  assert c.post('/api/batch',json={'urls':['https://evil.com']}).status_code==422
  r=c.post('/api/batch',json={'urls':[u,u+'?tracking=1'],'request_id':'once'}).json(); bid=r['batch_id']
  assert r['total']==1
  assert c.post('/api/batch',json={'urls':[u,u+'?tracking=1'],'request_id':'once'}).json()['batch_id']==bid
  for _ in range(100):
   snap=c.get('/api/batch/'+bid).json()
   if snap['status']=='completed':break
   time.sleep(.02)
  assert snap['successful']==1,snap
  with c.websocket_connect('/ws/'+bid) as ws: assert ws.receive_json()['successful']==1
  r=c.post('/api/batch/add',json={'batch_id':bid,'urls':[u.replace('12345','67890')]}).json()
  assert r['total']==2
  time.sleep(.1)
 with TestClient(create_app(d,Fake,launch_interval=0)) as c:
  assert c.get('/api/batch/'+bid).json()['successful']==2
  assert c.post('/api/batch',json={'urls':[u]}).json()['snapshot']['successful']==1
print('PASS: validation, canonical dedupe, idempotency, completion, websocket snapshot, append, restart persistence, archive reuse')
async def saves():
 with tempfile.TemporaryDirectory() as d:
  engine=SsstikDownloader(Path(d))
  class Download:
   async def save_as(self,p): Path(p).write_bytes(b'<html>error</html>')
   async def failure(self):return None
  try: await engine._save_download(Download(),1)
  except RuntimeError:pass
  else:raise AssertionError('accepted HTML')
  assert not list(Path(d).iterdir())
asyncio.run(saves())
print('PASS: invalid download rejection and temporary file cleanup')
