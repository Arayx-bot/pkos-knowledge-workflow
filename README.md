# PKOS 知识整理与维护

把自己的聊天与实践记录整理为有出处的知识卡，支持四来源周审阅、逐问访谈、明确授权的增量学习入库、卡片校验和自建工具公开发行准备。

## 环境

Python 3.10+、PyYAML 6.x、一个能加载Skill并操作本机文件的AI。`python3 -m pip install -r requirements.txt`安装依赖前，先确认解释器与环境，建议使用自己选择的虚拟环境。主要流程已在macOS实测；Windows/Linux仅文档覆盖，尚未实机验证。无需GPU或付费API；AI宿主自己的费用由使用者承担。

## 从空库开始

在包根运行 `python3 scripts/init_workspace.py --output <新的工作目录>`。输出是空Vault、私人系统数据、空索引与自己的工具总账，不包含任何示例聊天。目标已存在时拒绝覆盖。输入和运行产物保留在该工作目录，源码安装目录不保存私人数据。

已有知识库不要初始化覆盖，显式配置自己的Vault、系统数据、卡片与来源索引；模块名称只从 config/modules.json 读取。校验命令示例：`python3 validation/validate.py --vault <工作目录>/vault --system-data <工作目录>/system --card-index <工作目录>/system/cards.json --module-config config/modules.json`。

## 周整理与自动私人入库

先填写自己的来源清单，明确官方导出、网页库、Codex或Pi会话范围；不要默认读取全账户。设置环境变量 PKOS_TOOL_REGISTRY 指向自己的工具总账，再按 weekly/README.md 运行周prepare、审阅与访谈。库外预览不自动变成正式卡。

自动私人入库先取得具体会话范围授权，复制 config/automatic.example.json 到用户工作目录并配置路径、会话、分类路由与授权引用。`python3 -m weekly.automatic --config <配置> prepare`只准备证据；AI按实际材料编辑计划，apply执行有快照的事务写入。访谈与知识真实性仍依赖本人和AI判断，机械检查不证明内容真实。

## 自建 Skill 登记、建卡与公开准备

先按 weekly/README.md 配置自建来源与主位置，执行 `maintenance/skill_lifecycle.py finish` 自动登记和私人建卡；已有人工正文保留，来源和实际测试范围分别记录。可以配置用户级会话 hook 和定时兜底，需按 AI 宿主要求信任 hook。

`python3 maintenance/skill_publication.py --config <自己的publication配置> run`检查新建、更新和直接编辑过的自建技能卡，按唯一总账的逐文件白名单准备确定性ZIP和确认清单。缺归属、许可、版本或白名单会明确阻断；不会猜测公开范围。apply的工具卡事件保存在系统数据publications/events，准备失败仍可重试。

只有用户确认冻结清单，才通过share publish创建公开仓库与Release。发布核实后才回填“拿去用”；原始卡片与个人资料保持private。程序不自行创建批准，不自动上传私人仓库或其历史。

## 验证与范围

包根运行 `python3 -B -m unittest discover -s tests -p 'test_*.py' -v`、`python3 -B -m unittest discover -s validation -p 'test_validate.py' -v`及`python3 -B -m unittest discover -s maintenance -p 'test_*.py' -v`。均使用临时合成资料；真实资料质量、源覆盖和长期检索效果另行判断。

MIT，见LICENSE。此公开包不包含浏览器采集扩展、原始聊天、正式知识卡、个人账户配置、备份凭据或私有Git历史。浏览器采集器可单独安装；二者通过明确提供的数据库路径衔接。
