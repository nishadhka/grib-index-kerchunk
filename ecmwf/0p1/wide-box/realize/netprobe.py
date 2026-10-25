from distributed import Client
import sys
def probe(i, nthr, base):
    import time, urllib.request, json, concurrent.futures as cf
    steps=[3,24,48,72,96,120,144,168,192,216,240,264][i::5] or [3]
    rows=[]
    for st in steps[:2]:
        stem=f"20261021/00z/ifs/0p1/enfo/20261021000000-{st}h-enfo-ef-pl"
        idx=urllib.request.urlopen("https://storage.googleapis.com/ecmwf-open-data/"+stem+".index").read().decode().splitlines()
        rows+=[(stem,json.loads(l)) for l in idx if l.strip()][100:100+nthr*3]
    def g(r):
        stem,d=r; a=d["_offset"]; b=a+int(d["_length"]*0.8)
        for u in ([base] if base else ["https://ecmwf-forecasts.s3.eu-central-1.amazonaws.com/","https://storage.googleapis.com/ecmwf-open-data/"]):
            try:
                return len(urllib.request.urlopen(urllib.request.Request(u+stem+".grib2",headers={"Range":f"bytes={a}-{b}"}),timeout=120).read())
            except Exception: pass
        return 0
    t=time.time()
    with cf.ThreadPoolExecutor(nthr) as ex: n=sum(ex.map(g,rows))
    w=time.time()-t
    return dict(n=len(rows), MB=round(n/1e6), MBps=round(n/w/1e6,1), s=round(w,1))
c=Client("tcp://127.0.0.1:8786",timeout=30)
ws=sorted(c.scheduler_info()["workers"])
nthr=int(sys.argv[1]); base=sys.argv[2] if len(sys.argv)>2 else ""
futs=[c.submit(probe,i,nthr,base,workers=[w],pure=False,allow_other_workers=False) for i,w in enumerate(ws)]
res=c.gather(futs); print(nthr, base or "aws->gcs", res, "aggregate MB/s", round(sum(r["MBps"] for r in res),1))
