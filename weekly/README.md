# 整理、审阅与访谈

所有路径由使用者明确提供。先检查模块映射、卡片索引、来源索引与授权范围，真实资料保持private。系统数据在Vault外。

## 每周整理

`python3 -m weekly.cli --system-data <系统数据> --sources <自己的sources.json> prepare --week <YYYY-Www>`准备已结束自然周；以Asia/Shanghai计算周界。sources含official_exports、web_database、codex_roots、pi_roots的明确范围。官方包generated_at与complete_confirmed须有实际依据，缺覆盖输出暂定标记。网页仅可证明实时增量的消息按采集时间暂归周；官方匹配优先，未知时间不猜。

程序输出EVIDENCE.jsonl、SOURCE_COVERAGE.json与RUN.json。AI据此匹配旧卡、写预览和访谈问题；不把AI建议、流程指令或转贴当成本人思想。

render计划含candidates与interview_questions。候选含id、title、type、action（新建/更新/忽略/待答）、reason、evidence_ids；写入预览候选含body，更新含existing_id。本人观点attribution=owner_view须有本人消息。访谈3–5问各含id、question、event_anchor与evidence_ids；证据不足不凑数。`python3 -m weekly.cli --system-data <系统数据> render --week <周次> --plan <计划.json>`生成库外审阅表与预览，不正式入库。

## 访谈与月度核对

依次运行weekly.cli的interview-start、interview-next，每次只问一个问题，等待本人回答。interview-answer的--received-file包含text_unchanged、role=user、source_message_id和source_kind；原话保留，不由AI代答。跳过须本人明确，interview-skip可加--whole-week。interview-draft的--plan识别新增观点并生成周记预览，正式写入另按授权范围处理。

`python3 -m weekly.cli --system-data <系统数据> --sources <配置> monthly --month <YYYY-MM>`只生成官方核对与修正报告，不覆盖原始记录。

## 增量学习入库

配置automatic时，authorization必须保留用户明确授权的引用与原话，sessions逐个列真实授权会话ID和路径，routing使用实际模块目录与总览。enabled=false暂停。不能把许可公开源码当成许可读取个人会话。

prepare产生批次证据；AI编辑计划含batch_id、decisions，每条decision含id、action、evidence_ids，写入项另含type、title、core_key、body和attribution。update提供existing_id并保留原路径标题；思考含owner_quotes准确片段，外部知识含verification（URL、实际checked_at、finding），项目和工具含fact_basis。工具必须用subtype=internal/external分类归属，内部工具可附tool_registry_id、skill_name、public_distribution_key与invocation_phrase。

`python3 -m weekly.automatic --config <本机配置> apply --plan <计划.json>`分配稳定ID、写来源、卡片与索引，完整快照后验证，失败回滚。status检查，pause/resume控制；外部编辑冲突先rebase并重读卡片，中断事务先recover，不覆盖后来人工编辑。

工具卡与发行事件同批保存。publication_config提供自己的公开准备配置后立即核对；缺配置的事件保留待重试。公开准备只产出清单和确定性ZIP，实际上传必须另有冻结清单确认；工具公开不改变卡片private属性。定时兜底由使用者的AI宿主选择配置。

## 自建 Skill 纳管

使用 publication.example.json 显式配置自己的路径、已登记仓库与 Skill 根目录，开启 auto_intake_skills；通用主位置由 general_skill_root/general_skill_repository 指定。`maintenance/skill_lifecycle.py finish --path <入口> --self-built --reference <真实开发请求> --public-files <精确配方.json>` 自动登记、建卡、来源留档和准备候选，实际公开另等确认。配方支持同主仓库内 source_directory、files 和 file_map，不复制实现成为第二主位置。

自行配置 AI 宿主 SessionStart/Stop 命令调用 maintenance/skill_lifecycle_hook.py --config <自己的配置>，按宿主要求审阅并信任 hook。没有 hook 时，开发任务结束执行 finish，定时执行 skill_publication.py run 兜底；定时任务由使用者配置。程序不猜测外部 Skill 为自建，不读取会话正文。冲突、新扫描命中或归属未知保留待办；公开准备不会生成批准或上传。
