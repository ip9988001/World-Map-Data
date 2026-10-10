import csv,collections,os
rows=list(csv.DictReader(open("data/源总表.csv",encoding="utf-8-sig")))
def prio(r):
    if r["channel"]=="ARCGIS":
        return {"statewide":"P0","county":"P1","city":"P2"}[r["scope"]]
    return {"statewide":"P0","county":"P3","city":"P4"}[r["scope"]]
for r in rows: r["priority"]=prio(r)
rows.sort(key=lambda r:(r["priority"],r["state"],r["county"],r["city"]))
with open("data/源总表-执行序.csv","w",newline="",encoding="utf-8-sig") as fh:
    w=csv.DictWriter(fh,fieldnames=list(rows[0].keys())); w.writeheader(); w.writerows(rows)
cnt=collections.Counter(r["state"] for r in rows)
sw=[r for r in rows if r["scope"]=="statewide"]
L=[]
L.append("# 全美地址数据源总表 · 报告\n")
L.append("> 数据来源：OpenAddresses 源定义库（GitHub 开源，2026-10-09 拉取）\n")
L.append("## 一、总量\n")
L.append("| 指标 | 值 |\n|---|---|")
L.append(f"| 独立数据源 | **{len(set(r['source_id'] for r in rows))}** 个 |")
L.append(f"| 地址图层记录 | **{len(rows)}** 条 |")
L.append(f"| 覆盖州/地区 | **{len(cnt)}** 个 |")
L.append(f"| 100% 有门牌号字段映射 | {sum(1 for r in rows if r['has_number'])} / {len(rows)} |")
L.append("\n## 二、按采集通道（决定要写几个适配器）\n")
L.append("| 通道 | 数量 | 占比 | 说明 |\n|---|---|---|---|")
c=collections.Counter(r["channel"] for r in rows)
for k,v in c.most_common():
    d={"ARCGIS":"ArcGIS FeatureServer，可 API 分页直拉","FILE_DOWNLOAD":"直接下 zip/shp/gdb 文件","CSV_DIRECT":"直接下 CSV"}[k]
    L.append(f"| **{k}** | {v} | {v/len(rows)*100:.1f}% | {d} |")
L.append("\n> 结论：**写 1 个 ArcGIS 适配器 = 覆盖 {:.0f}%；再加 1 个文件下载适配器 = 覆盖 100%**\n".format(c['ARCGIS']/len(rows)*100))
L.append("## 三、按覆盖范围\n")
for k,v in collections.Counter(r["scope"] for r in rows).most_common():
    n={"statewide":"州级（一次拿一个州，价值最高）","county":"县级","city":"市级"}[k]
    L.append(f"- **{k}**：{v} 条 —— {n}")
L.append("\n## 四、州级全量源（P0，共 %d 个）\n"%len(sw))
L.append("| 州 | 源 | 通道 | 数据地址 |\n|---|---|---|---|")
for r in sw[:70]:
    L.append(f"| {r['state']} | {r['source_id'].split('/')[-1]} | {r['channel']} | `{r['url'][:70]}` |")
L.append("\n## 五、各州源数量（覆盖强度）\n")
L.append("| 州 | 源数 | 州 | 源数 | 州 | 源数 | 州 | 源数 |\n|---|---|---|---|---|---|---|---|")
items=sorted(cnt.items(),key=lambda x:-x[1])
for i in range(0,len(items),4):
    chunk=items[i:i+4]; line="|"+"|".join(f"{s} | {n}" for s,n in chunk)+"|"
    while line.count("|")<9: line=line[:-1]+" | |"
    L.append(line)
weak=[s for s,n in items if n<=5]
L.append(f"\n**⚠️ 覆盖薄弱州（≤5 个源）**：{', '.join(weak) if weak else '无'}\n")
L.append("## 六、执行优先级\n")
L.append("| 优先级 | 定义 | 数量 |\n|---|---|---|")
for k in ["P0","P1","P2","P3","P4"]:
    n=sum(1 for r in rows if r["priority"]==k)
    d={"P0":"州级 + ArcGIS","P1":"县级 + ArcGIS","P2":"市级 + ArcGIS","P3":"县级 + 文件下载","P4":"市级 + 文件下载"}[k]
    L.append(f"| {k} | {d} | {n} |")
open("docs/源总表报告.md","w",encoding="utf-8").write("\n".join(L))
print("报告已生成 docs/源总表报告.md")
print("优先序表已生成 data/源总表-执行序.csv")
print("\n== 优先级分布 =="); 
for k in ["P0","P1","P2","P3","P4"]: print(" ",k,sum(1 for r in rows if r["priority"]==k))
print("\n== 覆盖薄弱州(<=5源) ==", [s for s,n in cnt.items() if n<=5])
print("\n== 州级全量源 前12 ==")
for r in sw[:12]: print("  ",r["state"],r["source_id"].split("/")[-1],r["channel"])
