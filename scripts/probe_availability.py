import csv,json,urllib.request,urllib.parse,collections,concurrent.futures,time,random,os
UA={"user-agent":"Mozilla/5.0 (compatible; address-data-audit/1.0)"}
rows=list(csv.DictReader(open("data/源总表-执行序.csv",encoding="utf-8-sig")))
P0=[r for r in rows if r["priority"]=="P0"]
P1=[r for r in rows if r["priority"]=="P1"]; random.seed(7); random.shuffle(P1)
P2=[r for r in rows if r["priority"]=="P2"]; random.shuffle(P2)
P34=[r for r in rows if r["priority"] in ("P3","P4")]; random.shuffle(P34)
sample=P0+P1[:40]+P2[:12]+P34[:12]
print("抽样源数:",len(sample),"（P0全部 %d）"%len(P0))
def get(u,t=25):
    return urllib.request.urlopen(urllib.request.Request(u,headers=UA),timeout=t)
def probe(r):
    url=r["url"]; ch=r["channel"]; out=dict(source_id=r["source_id"],state=r["state"],scope=r["scope"],priority=r["priority"],channel=ch,url=url[:180])
    t0=time.time()
    try:
        if ch=="ARCGIS":
            q=url.rstrip("/")
            if not q.split("/")[-1].isdigit(): q+="/0"
            u=q+"/query?"+urllib.parse.urlencode({"where":"1=1","returnCountOnly":"true","f":"json"})
            d=json.load(get(u))
            if "error" in d: out.update(status="ERROR",detail=str(d["error"].get("message"))[:60],count="")
            else: out.update(status="OK",detail="",count=str(d.get("count","")))
        else:
            req=urllib.request.Request(url,headers=UA,method="HEAD")
            try: resp=urllib.request.urlopen(req,timeout=25); out.update(status="OK",detail="",count="")
            except Exception:
                resp=None
            if resp is None:
                ctx=get(url); out.update(status="OK",detail="GET-ok(head blocked)",count="")
    except Exception as e:
        m=str(e)[:70]
        st="TIMEOUT" if "timed out" in m.lower() else ("AUTH" if ("401" in m or "Token" in m or "403" in m) else "FAIL")
        out.update(status=st,detail=m,count="")
    out["ms"]=int((time.time()-t0)*1000)
    time.sleep(0.6)
    return out
res=[]
with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
    for r in ex.map(probe,sample): res.append(r); print(".",end="",flush=True)
print()
with open("data/源可用性抽样.csv","w",newline="",encoding="utf-8-sig") as fh:
    w=csv.DictWriter(fh,fieldnames=list(res[0].keys())); w.writeheader(); w.writerows(res)
print("== 状态分布 =="); 
for k,v in collections.Counter(r["status"] for r in res).most_common(): print(f"  {k:10s} {v}")
ok=[r for r in res if r["status"]=="OK"]
print("\n== 活源 %d/%d，其中有记录数的 =="%(len(ok),len(res)))
c=[(int(r["count"]),r["source_id"],r["state"]) for r in ok if r["count"].isdigit()]
c.sort(reverse=True)
print("  有 count 的源数:",len(c))
print("  记录数 Top10:"); [print(f"    {n:>10,}  {s}") for n,s,_ in c[:10]]
print("  记录数合计: {:,}".format(sum(n for n,_,_ in c)))
print("\n== 失败源明细 ==")
for r in res:
    if r["status"]!="OK": print(f"  {r['status']:8s} {r['source_id']:28s} {r['detail'][:60]}")
