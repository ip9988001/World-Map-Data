import json,os,glob,csv,collections,re
ROOT="raw/openaddresses-master/sources/us"
rows=[]
for f in sorted(glob.glob(ROOT+"/*/*.json")):
    st_dir=f.split("/")[-2]
    sid="us/%s/%s"%(st_dir,os.path.basename(f)[:-5])
    try: d=json.load(open(f,encoding="utf-8"))
    except Exception as e: continue
    if not isinstance(d,dict) or "layers" not in d: continue
    cov=d.get("coverage") or {}
    census=cov.get("US Census") or {}
    # only address layers
    for lay in (d["layers"].get("addresses") or []):
        if not isinstance(lay,dict): continue
        conf=lay.get("conform") or {}
        data=lay.get("data")
        if isinstance(data,list): data=data[0] if data else ""
        url=str(data or "")
        proto=str(lay.get("protocol") or "").lower()
        # infer collection channel
        if "FeatureServer" in url or "MapServer" in url or "/rest/services" in url: chan="ARCGIS"
        elif proto in ("esri","arcgis"): chan="ARCGIS"
        elif url.endswith(".csv") or ".csv" in url.lower(): chan="CSV_DIRECT"
        elif url.endswith(".zip") or proto in ("http","ftp"): chan="FILE_DOWNLOAD"
        elif url.endswith(".shp") or ".shp" in url.lower(): chan="SHP"
        elif not url: chan="NO_URL"
        else: chan="OTHER"
        ln=lay.get("coverage") or {}
        rows.append({
         "source_id":sid,"state":(cov.get("state") or st_dir).upper(),"county":cov.get("county") or census.get("name") or "",
         "city":cov.get("city") or "","geoid":str(census.get("geoid") or ""),
         "scope":"statewide" if not cov.get("county") and not cov.get("city") else ("city" if cov.get("city") else "county"),
         "layer":lay.get("name") or "","attribution":(lay.get("attribution") or "")[:80],
         "protocol":proto,"channel":chan,"url":url[:300],
         "website":str(lay.get("website") or "")[:200],
         "license":(lambda L:(str(L.get("url") or "") if isinstance(L,dict) else str(L or "")))(lay.get("license"))[:200],
         "fmt":str(conf.get("format") or ""),
         "number":str(conf.get("number") or ""),"street":str(conf.get("street") or ""),
         "city_f":str(conf.get("city") or ""),"postcode":str(conf.get("postcode") or ""),
         "region":str(conf.get("region") or ""),"unit":str(conf.get("unit") or ""),
         "has_number": bool(conf.get("number")),"has_street": bool(conf.get("street")),
        })
os.makedirs("data",exist_ok=True)
with open("data/源总表.csv","w",newline="",encoding="utf-8-sig") as fh:
    w=csv.DictWriter(fh,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
print("地址层记录总数:",len(rows))
print("独立源数:",len(set(r["source_id"] for r in rows)))
print("\n== 按采集通道(channel) ==")
for k,v in collections.Counter(r["channel"] for r in rows).most_common(): print(f"  {k:15s} {v}")
print("\n== 按 protocol ==")
for k,v in collections.Counter(r["protocol"] for r in rows).most_common(12): print(f"  {k or '(空)':15s} {v}")
print("\n== 按覆盖范围 scope ==")
for k,v in collections.Counter(r["scope"] for r in rows).most_common(): print(f"  {k:12s} {v}")
print("\n== 按数据格式 fmt ==")
for k,v in collections.Counter(r["fmt"] for r in rows).most_common(8): print(f"  {k or '(空)':12s} {v}")
print("\n== 有门牌号映射 / 有街道映射 ==")
print("  has_number:",sum(1 for r in rows if r["has_number"]),"| has_street:",sum(1 for r in rows if r["has_street"]))
print("\n== 州分布 Top15 ==")
for k,v in collections.Counter(r["state"] for r in rows).most_common(15): print(f"  {k} {v}")
print("\n== 州覆盖数 ==",len(set(r["state"] for r in rows)))
