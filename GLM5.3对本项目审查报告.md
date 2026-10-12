审查完成。以下报告按交办书第 6 节格式输出，可直接原样转发给 pi agent 逐条核验。全部 P0/P1 均已在本地用锁定 commit 78cb045 的代码实测复现（复现脚本存放在 
外部审查-复现脚本-78cb045.py
，克隆的仓库在 临时项目/World-Map-Data/，临时产物已清理，未改动仓库任何文件）。

外部审查报告 · World-Map-Data @ 78cb045
按交办书第 4 节清单审查。分级：P0 × 3，P1 × 7，P2 × 9，另有铁律挑战 1 条。除标注「推测」外全部经本地复现验证。

P0 —— 开跑前必须修
P0-1 下载完整性对账被成功路径无条件抹掉：部分下载会永久静默缺数据
位置：collectors/fetch_raw.py:213（man["error"] = ""）；配合 fetch_raw.py:93-109（_integrity）、collectors/pipeline.py:77（if man.get("error"):）
复现（已实测）：构造 man={"done":True}，调 _integrity(man, actual=500, declared=1000) → 得 error="SHORT_DOWNLOAD: …（50%）", done=False, defect="SHORT"；随后模拟 fetch_raw.py:213 执行 man["error"]="" → error="" 但 done=False, defect="SHORT" 仍在。
链路：pipeline.collect_one 只看 man.get("error") 识别缺陷 → 判 False → 照常进 run_rules → 90% 的部分档案上覆盖率/反造数门正常通过 → state.done=True → 下一窗口 plan_batch 按 done 跳过 → 缺的那 10% 永远不补。这正是第 6 步「下载完整性对账」要防的事，但标记在写 manifest 前一行被清空。
触发条件（真实存在）：ArcGIS 键集分页中服务端某页提前返回短页（arcgis.py:123 short_page → break，done=True），或分页中途丢页但未报错。
期望 vs 实际：期望 _integrity 标记的 SHORT_DOWNLOAD/EMPTY_DOWNLOAD 缺陷到达 pipeline 的缺陷分支（DOWNLOAD_DEFECT 补采）；实际被 fetch_raw.py:213 吞掉。
最小改法：fetch_raw.py:213 改为 if not man.get("defect"): man["error"] = ""；或 pipeline.py:77 改判 man.get("error") or man.get("defect")。前者更优（守在源头）。
P0-2 field_guess 错列产物能过全部四道门，且「选洗出最多」会主动选它
位置：collectors/rules/field_guess.py:107（ok_number 选列条件）、collectors/local_cascade.py:161-174（_record_ok）、local_cascade.py:177-216（_gate）、local_cascade.py:379（passing.sort(key=lambda x: -x[0])）
复现（已实测三段）：
构造 3 行样本 {"THE_STREET":"MAIN ST",…,"YEAR_BUILT":"1998",…}，warm() 后对行 {"YEAR_BUILT":"1998","THE_STREET":"MAIN ST","ZIPC":"62701","STATEC":"IL"} 调 _columns → number 列选中 YEAR_BUILT（ok_number 只要求数字率≥30%、中位长≤6、非序号列；YEAR_BUILT 四位数、取值集中，全部满足）。
_record_ok({"number":"1998","street":"MAIN ST"}, raw) → True（1998 是行内子串）。{"number":"3",…}（卧室数）、{"number":"0"}（lstrip("0") 得空串、"" in blob 恒真）→ 全部 True。
行级判据同样放行：_is_contentful 的分列分支＝「有短数字值 + 有带街名后缀的值」→ BEDROOMS+STREET 组合直接命中；_true_addr 用同一组合抬高分母 → 反造数门 kept ≤ 1.5×true_n 对错列产物失效（不是凭空造，是错列）。
放大器：run_rules 不早收工、最后按「洗出最多」选规则（local_cascade.py:363-379）。错列规则不丢行（ratio≈100%），真规则在含空置地块的源上 ratio 常只有 40-70% → 错列垃圾产出 > 真规则产出时垃圾规则胜出并落盘。注释「质量由闸门 + 产物级防垃圾保证」在错列场景下两道防线都不设防。
期望 vs 实际：期望反造数门拦住「号在行里但语义不是号」的产物；实际四道门全过、还可能挤掉真规则。
最小改法（三处任一即可显著收敛，建议全做）：
field_guess.plan_columns 的号列候选加「值域常识」：号列中位数 ≤5 位且 min≥1（排 1700-2026 年份段、0-12 房间段可用 distinct 异常低联合判断）；
_record_ok 从子串改词边界：re.search(rf"\b{re.escape(num)}\b", blob)，并显式拒绝 num=="0"；
run_rules 选规则时把 PASS_DOWNGRADED/字段完整性纳入 tie-break（同等量级优先 verdict=PASS 者），错列源通常 city/zip 全空会被压下去。
P0-3 「被降级的源永不当 A 级」在第 13 步没有任何执行点：降级源记录照进 site/ 站用子集
位置：scripts/build_release.py:219-248（fill_and_grade，A 级判定＝五要素非空）；build_release.py:75-84（collect_new 只读 parsed/**.ndjson）；collectors/local_cascade.py:229-241（第 9 步 PASS_DOWNGRADED/grade_hint）
链路：第 9 步判 INCOMPLETE（city 缺 30%>20%）→ state.json 记 verdict=PASS_DOWNGRADED, grade_hint=B——但这条信息只进 state，不进 parsed 产物、不被 build_release 读取。fill_and_grade 只看逐条记录五要素：该源 70% 记录 city/zip 有值（或被字典补齐）→ 全部定 A → 写进 site/<ST>.ndjson.gz（生成器直接取用的对外产物）。
端到端实测：微缩发布（3 条合成记录）确认 fill_and_grade 与 verdict 完全解耦——降级信息从 silver 到 release 的通道不存在。
违反：铁律 3「被豁免的源交付照样降级、永不当 A 级」、对照表第 9/13 步验收「永不当 A 级」。这是对硬底线承诺的系统性违反，不是边角案例。
附带发现（同根）：fill_and_grade 的 A 级不区分采集值与 zip_map 推断值——city 空、zip 有 → 字典补出 city（city_src=zip_map）→ 五要素齐 → A 级进 site/。站用方拿到的「A 级」里 city 是 GeoNames place name（README 自己承认与 USPS preferred name 不一致）。
最小改法：run_rules 落盘 ndjson 时把 grade_hint/verdict 写进每条记录（如 _gh:"B"）；fill_and_grade 里 if rec.pop("_gh","A") != "A": return max(g, "B")。推断值问题可在 A 级判定加 not any(rec.get(f+"_src") in ("zip_map","derived") for f in ("city","zip"))（放给用户拍板口径，见「铁律挑战」）。
P1 —— 建议开跑前修
P1-1 前置门（第 1/2/4 步）的拒收判决没有任何消费者，整道门是空转的
位置：scripts/source_intake.py:204（REJECTED_P0 只写预检卡）；collectors/plan_batch.py:39-95（load_sources 只读黑名单/剔除表/计数，不读预检卡、不读预检汇总）
证据：全仓 grep——预检卡/预检汇总/REJECTED_P0/REJECTED_P2 的引用只有 source_intake.py 自己和 factory.yml 的回写步骤，没有任何读取方。现状 预检汇总.csv 里已有 1 个 REJECTED_P0 源：它不进黑名单、不在剔除表 → plan_batch 照常排进泳道 → 全量采集烧预算 → 大概率在本地分诊再撞一次南墙。
期望 vs 实际：对照表说前置门「判采不采」；实际只「记录」，不「判」。P2 契约体检声称「不合格的源在这里就挡掉，省下全量采集的时间」——没有挡的机制。
最小改法：plan_batch.load_sources 增加一个参数读 data/预检汇总.csv，verdict 以 REJECTED_ 开头的源跳过（TRANSIENT/NEEDS_DOWNLOAD 保留）；或 source_intake 对 REJECTED 判决直接 blacklist_add(sid, kind, reason, "前置门拒收")（复检日机制已有，可救误杀）。
P1-2 契约桥（第 3 步→第 9 步）三处静默失败，「只能更严」铁律被字面打破
位置：collectors/local_cascade.py:85、local_cascade.py:61-92、collectors/rule_author.py:465-497
复现（已实测）：
契约行声明 max_junk=0（最严）→ load_contracts 生效值 0.2。min(MAX_JUNK, float(r.get("max_junk") or MAX_JUNK))——0 or MAX_JUNK 短路成全局默认。声明「比全局严」反被放宽，违反铁律 3 的 min 语义。同源 city_max_miss=0.10 正确生效（那里没有 or）。
契约 CSV 一行坏（如未闭合引号）→ 该行与后续行合并/丢失（实测 c 行的更严阈值静默消失），无任何告警。
整表读不动时 except Exception: return {} → 全部源契约静默退回全局默认，豁免与更严阈值同时失效。
最小改法：max_junk 行改为先取 v=(r.get("max_junk") or "").strip()，v!="" 才 min(MAX_JUNK, float(v))（照抄 city/zip 的写法）；load_contracts 的 except 分支至少 print 到 stderr 并在返回值里带 "_load_error"；emit_contract 写入前对读回的行数做断言（写 N 行读回必须 N 行）。
P1-3 merge_group 的 county 三连问题：采集值被丢、源覆盖县压过 ZIP 级县、州 FIPS 被当县 FIPS
位置：scripts/build_release.py:52（CORE_FIELDS 不含 county）、build_release.py:199-215（county 兜底）
复现（已实测 + 端到端确认）：
规则产物记录带 county="ADAMS", county_fips="17001"（采集值）→ 合并输出 county_src="zip_map"、county 来自字典——采集的 county 在 merge_group 里根本不被读（不在 CORE_FIELDS 循环），被推断值顶替。当前规则（mk_record）不产 county 所以暂无实害，但这是「规则一旦开始产 county 就静默丢弃」的埋雷。
源总表兜底无 if not rec.get("county") 守卫：采集值 HANCOCK/17067 → 合并后 ADAMS/17001 county_src=source_meta（实测）。且兜底优先于 fill_and_grade 的 z2c（source_meta 有 county 就轮不到 ZIP 级）——「源覆盖县」是粗粒度，跨县城市必错。
全州性源 geoid 存的是州 FIPS（源总表实测：us/ak/statewide geoid=02、us/az/statewide geoid=04）→ 兜底写进 county_fips="02"（实测）。5 位县 FIPS 字段出现 2 位州码，下游按 FIPS 关联必错。
最小改法：① CORE_FIELDS 循环外补一段 county/county_fips 的「非空优先 × 优先级」合并（同 sources 并集逻辑）；② 源总表兜底加 if not rec.get("county") 前置守卫，并移到 fill_and_grade 的 z2c 之后；③ load_source_meta 里对 geoid 做长度过滤（len==5 才当 county_fips）。
P1-4 verify_release 的校验盲区：站用子集零校验、坏州码硬门永不触发、C 级豁免一切 schema 检查
位置：scripts/verify_release.py:34-35、verify_release.py:94-98、verify_release.py:118
证据：
data/州码白名单.json 不存在于仓库（find + git ls-files 均无）→ WHITELIST=set() → if WHITELIST and ks not in WHITELIST 恒 False → bad_state 硬门永远不触发。cross_state 只计数不报错（设计如此，但白名单缺失让「州码不在白名单=解析错位」这道防线完全不存在）。
全部校验循环只扫 manifest.states 里的全量分片；site/<ST>.ndjson.gz（对外、生成器直接吃的产物）不在任何校验范围——行数、空行、schema、sha256 一律不查。上游 build_release 对 site 写入同样只有写入时过滤（A 级），无回读审计。
check_schema 对 _grade=="C" 的记录 continue——跳过必填、跳过 key 重复、跳过州码白名单检查。若 ZZ 桶或坏州码记录集中在 C 级，校验完全看不见。
最小改法：把 51 州码+DC/PR 等白名单内置为代码常量（不依赖外部文件），并在 verify_release.py:35 缺文件时回退到内置；main 里对 site/ 目录复用 scan_shard+check_schema（REQUIRED 同口径）；C 级只豁免五要素完整性，不豁免 bad_state/dup_key。
P1-5 首轮全量构建大概率磁盘爆：这不是「随增长」问题，是第一次就会炸
位置：scripts/build_release.py:253-300（all.tsv）、build_release.py:303-362（sorted + 分片 + site + merged 并存）、factory.yml:505（publish 单 job timeout 60 分钟）
量化（按仓库实测数据推算）：data/源计数全量.csv 合计 141,430,314 条（status=OK 1491 源）。参考旧发布（63M 行 → merged 440MB gz ≈ 解压后 ~8GB ndjson），新链路全量 ≈ 18GB 解压 ndjson。发布 job 峰值需同时容纳：cache-parsed tar（1×）+ 解出 parsed（1×）+ all.tsv（≈1.1×，key+双份 payload）+ all.tsv.sorted（1.1×）+ 53 个州分片 + site 分片 + merged（≈1×）≈ 5× ≈ 90GB，远超 ubuntu runner 的 14~20GB 可用磁盘。sort -S 192M 会让外排临时文件再叠一层。时间与内存是流式的没有问题（sha256 回读 63M 行量级在 60 分钟内可完成）——先炸的是磁盘，且是第一次全量发布就炸。
附带：merged.clean.ndjson.gz 在硬门判定之前写出（build_release.py:534-543 早于 545-549）；构建失败（exit 2）不写新 manifest，而 factory.yml checkout 的 main 里躺着 v1 旧 manifest（63M 行那批） → verify 步骤若被执行会拿旧清单对新分片对账——碰巧会挡住发布，但报错全是「行数不符/sha 不符」，掩盖真实原因「构建根本没成功」。
最小改法：① build_global_tsv/write_shards 改为逐州构建 + 跨州 key 用小得多的「跨州 key 索引文件」二次归并（key 含州码，跨州同 key 只在州界地址，索引远小于全量）；短期先做 ②：构建开始前打印 shutil.disk_usage 并在 all.tsv 写入过程中监控，余量 <3GB 时降级为「分批州构建」明确失败；③ merged 移到硬门之后；④ verify 步骤前判断 [ -f out/release/merged.clean.ndjson.gz ] 且 build 步骤成功标记，否则直接跳过并显式报「构建未完成」。
P1-6 补采与续拿的预算黑洞：Socrata 续拿未实现 + DOWNLOAD_DEFECT 无重试上限
位置：collectors/fetch_raw.py:160-191（_socrata_paged）、fetch_raw.py:187（next_offset 记了没人读）、collectors/pipeline.py:190（attempts 只累加不判）
证据：① _socrata_paged 预算到点 done=False 并在 manifest 记 next_offset，但 fetch_raw 重跑时 _socrata_paged 用 open(out,"w") 从 offset=0 覆盖重写——「下次续拿」的承诺没实现，每次重下全部已得部分。② Socrata 路径 _integrity(man, n) 不传 declared → SHORT_DOWNLOAD 检测对 Socrata 盲（没查服务端总数）。③ DOWNLOAD_DEFECT 源每窗口重试、attempts 无上限——一个永远 0 字节的 URL 会无限烧窗口预算（礼貌间隔 0.8s×N）。
最小改法：① 重跑时读 manifest next_offset 以 "a" 续写并从该 offset 起翻页；② _socrata_paged 前用 ?$select=:id&limit=0 或 X-Total-Count 拿总数传给 _integrity（拿不到就退化为现状，注明）；③ run_queue 开新源前查 prev.get("attempts") >= 5 则跳过并标 kind="GIVE_UP" 待人工。
P1-7 状态与账本的一个低概率重损路径：fetch 失败 + push -f 孤儿分支
位置：factory.yml:322-327（git fetch origin data --depth=1 || true → 失败继续 → git checkout --orphan data）、factory.yml:364（git push -f origin data）
链路（推测，未在 Actions 实测——机制由 Git 语义决定）：fetch 瞬时失败 → 本地无 origin/data 引用 → 走 --orphan 分支 → git rm -rf --cached 后只提交本 run 的 state/账本 → push -f 把 data 分支整体替换，历史全部 state 版本链丢失。concurrency 串行使日常无并发覆盖，但这个孤儿分支路径不依赖并发。
最小改法：fetch 失败时 exit 1（宁可本窗口丢回写，也不孤儿重建）；或 push -f 改 push --force-with-lease=refs/heads/data:<期望的远端 SHA>。
P2 —— 可边跑边修
norm_number 把 "820 1/2" 洗成 "8201"（实测）：normalize.py:76-82 先 re.sub(r"\s+","",…) 再截前导数字。最小改法：先按 ^[0-9]+[A-Z]?$ 匹配整串，不匹配再走截断，且截断前剥掉 \d+/\d+ 分数尾巴。影响：号变 4 位、仍是行内子串 → 过 _record_ok，产假号。
常量多处定义、口径已经漂移（C12 的活例）：MAX_MISS 两份（source_intake.py:49 / local_cascade.py:56）；NUM_RE 四份且形态不同（normalize._LEAD_NUM、_common.NUMERIC_RE、field_guess.NUM_RE、rule_author.NUM_RE 多个 \d?、verify_release.NUM_RE 允许 "123A4" 而产物永不产生这种形态）；旧 _gate 与 GATE_MIN_RATIO 仍活在 cascade.py:39（被 triage CLI 引用）。改法：收进一个 collectors/constants.py。
双分诊并存：triage.classify（旧引擎链）与 local_cascade.diagnose（新）语义不同、都可达（前者经 python -m collectors.triage）。旧链 workflow 已 disabled，建议 triage CLI 的 --source 路径改调 diagnose，classify 只留 --from-state 兼容。
rule_author.main 里 build_prompt 连调两次（rule_author.py:627 重复 621-626 已调过的一次）——纯浪费，删一行。
溯源表两处口径松动：build_sources_tables 的 fetched_at 允许空串（state 丢失时静默空，verify 不查）；url 是源总表当前值而非采集时快照，源换 URL 后历史记录溯源指向新接口。改法：entry 里 fetched_at 为空时写 "unknown"（与 license 同口径）；bronze manifest 已存每次抓取的 url，可在 sources 表加 url_at_collect。
产品（D13）：site/ 按 key 排序 → 同城记录物理相邻，「点一下给一条」若顺序读会连续吐同一城市的地址；gzip 无随机访问。最小路径：发布时每州附 index.json（行数、sha、每 1 万行的 byte offset checkpoint），生成器随机选 checkpoint 再随机跳行；或 site/ 另出一份预混洗版。
产品（D14）：GeoNames CC BY 4.0 的署名目前只落在 dict/README.md；交付分片、release notes、未来站页都没有署名落点。建议 sources.json 顶层加 dictionary_attribution 字段 + release notes 模板补一行。
A 级语义未校验 city↔zip 一致性（交办书 A3 点名）：city=QUINCY + zip=90210 照过 A 级；fill_and_grade 补 city 时查了 d["state"]==st（好），但已有值之间不查。verify_release.check_schema 也不查。最小改法：verify 增加抽样一致性检查（zip 在 z2c 且 city 与 z2c[zip].city 完全不同的比例 >X% 报错）——注意换 city 别名的合法场景（preferred vs alias），只做信息项先观察。
_direct 的文件名来自 URL 末段（fetch_raw.py:113）：查询串截断后无扩展名的路径（REST 风格 /api/addresses）落成无后缀文件 → flatten.iter_rows_from_archive 按扩展名分派 → 静默不读（无 _read_error，行数为 0 → 走 DOWNLOAD_EMPTY 分诊，能兜住但归因错误）。改法：无扩展名时嗅探魔数（{→json、PK→zip）改存对应后缀。
铁律挑战（交办书第 6.5 条）
铁律 3 的「永不当 A 级」与第 12 步字典补齐存在口径冲突，建议明确 A 级定义（与 P0-3 同根，但值得单独拍板）：当前 A＝五要素非空，不区分采集值与 zip_map/derived 推断值。两种可执行口径：(a) A＝五要素全为采集值（推断值补齐最多 B+）——最诚实，但 site/ 量会显著变小；(b) A＝五要素齐且 city/zip 的推断标记 ≤1 个——折中。无论选哪个，先把「源级降级标记穿透到记录级」（P0-3 改法）做了，两条口径才有执行点。不建议维持现状：现状下 site/ 的「A 级」混合了「全采集」「半推断」「降级源洗出来的」，与「如实、可点、可复核」的硬底线不一致。

未能验证的部分（缺材料/缺环境）
真实数据形态：未索取分片，field_guess 错列（P0-2）的实际发生率无法估计——机制已实测，频率需在 5-10 个真实源的 bronze 上跑 --compare 验证。
GitHub Actions 实跑：bronze 回存竞态（422 重试路径）、16 泳道 matrix、窗口闸门时区行为、cache-parsed 上传失败的旧快照发布路径——均只做了代码级推演。
LLM 造刀质量：无 DEEPSEEK key，rule_author 的验证闸门只能审代码不能审真实输出（examples 原样复制的要求是否被模型遵守等）。
旧 6303 万数据集 / 现有 Release 资产：按交办书要求未下载，v0.1.0-dataset 现存资产与 manifest 的一致性未验。
P1-7（孤儿分支强推）标注为推测：Git 语义上成立，但未在 Actions 环境复现 fetch 失败场景。
给你的两点转发备注（不属报告正文）：① P0-1/P0-3 的修复都不超过 10 行，建议连同 P1-1（前置门接线）一起在开跑前做掉，其余可以开跑后排队；② 我在本地微缩端到端时 build_release 依赖系统 sort（Windows 下撞了 sort.exe），这不是仓库问题——ubuntu runner 上是 GNU sort，仅作记录免得复现脚本在 Windows 上跑失败时误判。
