#!/usr/bin/env python3
"""Real-client benchmark through Plex: head, tail index, then a simulated player at the title's own bitrate.
start = seconds until 5 s of video is buffered; rebuffer = seconds of stalls in the following 30 s of reading.
usage: PLEX_URL=http://host:32400 PLEX_TOKEN=... plex_start_bench.py TAG RUNS [WARM_MB]
Stop anything else that reads the mount first, or the numbers mean little. WARM_MB pre-reads the head and tail, which
needs the media paths Plex reports to be readable here."""
import http.client,os,random,statistics as st,sys,time,urllib.parse,urllib.request
import xml.etree.ElementTree as ET
U=urllib.parse.urlsplit(os.environ['PLEX_URL']); H,P=U.hostname,U.port or 32400
tok=os.environ['PLEX_TOKEN']
SECTION=os.environ.get('PLEX_MOVIE_SECTION','1')
def get(p):
    s='&' if '?' in p else '?'
    return ET.fromstring(urllib.request.urlopen(f'http://{H}:{P}{p}{s}X-Plex-Token={tok}',timeout=60).read())
def sessions(): return int(get('/status/sessions').get('size') or 0)
def rng(key,start,want=None,deadline=None,stats=None):
    c=http.client.HTTPConnection(H,P,timeout=120)
    c.request('GET',f'{key}?X-Plex-Token={tok}',headers={'Range':f'bytes={start}-','Accept-Language':'en'})
    r=c.getresponse(); n=0
    while True:
        if want is not None and n>=want: break
        if deadline and time.time()>=deadline: break
        b=r.read(65536)
        if not b: break
        n+=len(b)
        if stats is not None: stats.append((time.time(),n))
    c.close(); return n
items=[]
for v in get(f'/library/sections/{SECTION}/all?type=1').iter('Video'):
    for m in v.iter('Media'):
        for p in m.iter('Part'):
            sz=int(p.get('size') or 0); br=int(m.get('bitrate') or 0)
            if sz>8e9 and br>15000 and os.path.lexists(p.get('file') or ''): items.append((v.get('title'),p.get('key'),sz,br,p.get('file')))
random.shuffle(items); it=iter(items)
def fuse_warm(f,head,tail):
    sz=os.path.getsize(f); fd=os.open(f,os.O_RDONLY)
    for off,n in ((sz-tail,tail),(0,head)):
        g=0
        while g<n:
            b=os.pread(fd,min(4<<20,n-g),off+g)
            if not b: break
            g+=len(b)
    os.close(fd)
def play(tl,t1,rate,t_index):
    "tl is (time,bytes). Start once 5 s are buffered, then consume at rate; returns start, rebuffer."
    t_start=next((x[0] for x in tl if x[1]>=5*rate),None)
    if t_start is None: return None,None
    i=0; pos=0.0; rebuf=0.0; t=t_start; end=tl[-1][0]; n=0
    while t<end:
        while i<len(tl) and tl[i][0]<=t: n=tl[i][1]; i+=1
        if n-pos>=rate*0.1: pos+=rate*0.1
        else: rebuf+=0.1
        t+=0.1
    return round(t_index+(t_start-t1),1),round(rebuf,1)
def one(warm):
    title,key,sz,br,f=next(it)
    if warm:
        fuse_warm(f,warm<<20,16<<20); time.sleep(2)
    t0=time.time(); rng(key,0,1700000); rng(key,sz-(6<<20)); b=time.time()-t0
    if not warm and b<1: return None
    tl=[]; t1=time.time(); rng(key,8265,deadline=t1+30,stats=tl)
    if not tl: return None
    rate=br*125
    s,r=play(tl,t1,rate,b)
    return round(b,1),s,r,round(br/1000),round(tl[-1][1]/1e6/(tl[-1][0]-t1),1)
tag=sys.argv[1]; runs=int(sys.argv[2]); warm=int(sys.argv[3]) if len(sys.argv)>3 else 0; res=[]
while len(res)<runs:
    if sessions(): print('viewer active, waiting',flush=True); time.sleep(30); continue
    x=one(warm)
    if x is None or x[1] is None: continue
    res.append(x); print(tag,'index/playstart/rebuffer/Mbps/MBps',x,flush=True); time.sleep(10)
print('MEDIAN',tag,'index',st.median(x[0] for x in res),'playstart',st.median(x[1] for x in res),'worst',max(x[1] for x in res),'rebuffer total',round(sum(x[2] for x in res),1),'clean',sum(1 for x in res if x[2]==0),'/',len(res),flush=True)
