---
name: pkos-knowledge-workflow
description: 在用户自己的Markdown知识库中核对来源、准备周审阅、逐问访谈，对明确授权的学习聊天增量分类与私人入库，或登记自建 Skill、整理技能卡与公开清单；不自动公开个人资料。
---
# PKOS 知识整理与维护

以本文件真实目录为包根，先读 README.md，再按任务读 weekly/README.md。依赖 Python 3.10+和PyYAML 6.x；能加载Skill并读取本机文件的AI负责语义提炼，脚本负责来源、结构和事务校验。首次检查环境，缺项逐项说明官方来源、网络、磁盘与权限影响，按用户选择安装并复检。

工作区必须是使用者指定目录。可用 `python3 scripts/init_workspace.py --output <新工作目录>` 创建空知识库与私人系统数据区；现有库先检查，不运行初始化覆盖。唯一模块映射在 config/modules.json，通过 --module-config或配置显式提供，不另写目录常量。

“检查卡片ID和双链”：`python3 validation/validate.py --vault <Vault> --system-data <系统数据> --card-index <索引> --module-config config/modules.json`。只读检查但需要解析卡片内容，不回显正文。

“处理上周”：配置自己的来源清单与工具总账，设置 PKOS_TOOL_REGISTRY 指向该总账，执行 `python3 -m weekly.cli --system-data <系统数据> --sources <来源配置> prepare`；依据真实材料生成预览与审阅，不替代本人释义，不编造来源。来源不全就明确缺口。周审阅与访谈的详细命令在 weekly/README.md。

“自动整理学习入口”：先明确授权具体会话与自动私人入库，再填写 config/automatic.example.json 对应的本机配置，运行 `python3 -m weekly.automatic --config <本机配置> prepare`，由AI按返回证据拟计划，再apply。仅本人观点可写思想；外部知识需一手来源核查；先匹配旧卡再新建，默认private。来源不足保留待处理。写前快照，写后校验，失败回滚。

“登记这个自建 Skill／整理技能卡”：读 weekly/README.md 的自建 Skill 纳管一节，按开发上下文确认自建来源，执行 maintenance/skill_lifecycle.py finish；通用主位置与项目归属来自显式配置，第三方明确排除。先整理实际 README、依赖和精确文件配方，自动私人建卡与离线准备，实际公开逐次确认。

工具卡写入会生成发行待办；内部工具必须有“拿去用”，外部工具保留原作者入口。公开准备需自己的工具登记和 publication 配置。运行 `python3 maintenance/skill_publication.py --config <配置> run` 只准备待审包，不授权上传。实际公开逐次确认精确文件、哈希、许可和目标仓库。

访谈一次一问，必须等待本人实际回答，不代答。备份、上传、线上修改、付费和删除前说明具体动作、目标、影响与费用，取得该范围确认。定时检查由使用者选用的宿主配置，本包不会自行注册后台任务；备份配置和系统钥匙串不包含在本包中。
