# 自建工具同步与只读检查

工具 ID：TOOL-040。程序只用 Python 标准库；GitHub 核对和推送使用本机已授权的 gh/SSH。真实总账及本机路径配置不纳入本仓库。

## 配置

在项目根目录，将 `config/tool-sync.example.json` 复制为忽略的 `config/tool-sync.local.json`，填入唯一总账路径；已有文件不覆盖。未配置时默认查找当前用户的 `~/Documents/PKOS-System-Data/tools/TOOL_REGISTRY.md`。也可在子命令前使用 `--registry /path/to/TOOL_REGISTRY.md`。

总账须含 `tool-sync-index` 标记内的 JSON 程序字段，仍在同一个 Markdown 文件内。缺失、无法解析或字段版本不支持时停止，不自动猜测归属。程序索引包含仓库路径、工具主位置、旧入口、已推送提交和已批准验证命令；这是私人本机配置，不能复制进源码仓库。

## 手动只读检查

```sh
python3 maintenance/tool_sync.py check
python3 maintenance/tool_sync.py check --remote
```

默认检查 Git 状态、总账 ID/提交、主位置和软链接、hook、已登记仓库及用户 Skill 发现目录中的未登记 Skill。`--remote` 另通过 GitHub API 核实账号、私有性和 main SHA，不执行 fetch。默认结果中的 origin/main 是缓存引用，不能视为实时远程证据。仅检查明确目录，不覆盖整台电脑或插件内部所有资源。外部安装的 Skill 按已核实路径排除；新的未知条目会报告所有权待确认。

检查不修改总账、Git 索引、远程或源文件，不创建锁文件。JSON 报告输出到标准输出；需要保存时自行重定向。退出码：0 通过，2 一致性问题／验证失败，3 配置、扫描或执行阻断。既有业务验证限制另见总账，不等同于新增同步差异。

## 本轮精确文件同步

```sh
python3 maintenance/tool_sync.py scan --repo REPOSITORY --paths relative/file.py README.md
python3 maintenance/tool_sync.py sync --repo REPOSITORY --paths relative/file.py README.md --message "说明本次具体修改"
python3 maintenance/tool_sync.py verify --repo REPOSITORY
```

`sync` 只暂存给出的变更路径（包含明确删除项），扫描失败不暂存、不提交、不推送。已有暂存内容、分支不是 main、远程改变、非快进或新扫描命中时停止，保留已有工作。仅推送 origin 的 main。成功后记录 SHA，再执行 `verify` 完成临时远程克隆检查；失败如实记录，不自动修复或回滚已推送提交。

验证只执行唯一总账中登记的离线命令，不复制本机 .env、数据库、私人输入或依赖目录。验证命令变更须审阅其副作用；不允许真实聊天采集、部署、上传、备份或读取私人 Vault。验证输出只保存命令和退出码，避免工具日志意外回显秘密；详细失败可在隔离环境另行检查。现有锁文件保持，依赖治理另行安排。

新工具登记和 Skill 移动由 Codex/Pi 按 SYNC_PLAYBOOK 执行，程序不凭目录名推断所有权，也不自动合并或覆盖源码。新项目应先完成登记、原地 Git 与私有远程建立，再安装其 hook；有既有历史/远程变更或分散目录合并需求时先停下。

## pre-push

```sh
python3 maintenance/tool_sync.py install-hooks
```

只在已登记仓库默认 `.git/hooks/pre-push` 新建本机包装入口，指向本程序和本机唯一总账。已有不同 hook、自定义 core.hooksPath、软链接或无法读取的文件均不覆盖；安装前检查全部仓库，发现冲突先报告。不修改全局 Git hooks 配置。

hook 检查实际推送的 ref、每个待发送提交的完整树及提交消息/作者元数据，能发现“先提交秘密、随后又删除”的历史。规则扫描包含密钥格式、邮箱、电话、含密码 URL、凭据字面量、私人运行文件、未知资产及非普通文件；规则检查不能证明绝对无敏感信息。旧批准仅绑定路径、SHA-256、行和类型，内容改变即重新阻断。大文件、超量历史、无法核验 GitHub 或扫描异常均拒绝推送。不存在跳过扫描参数。

Git 自身允许 `--no-verify` 绕过本地 hook；全局指令禁止这样做，本实现不是服务器强制策略。复制仓库到另一台机器或新克隆后，需要重新配置总账并安装 hook。已运行的模型会话是否加载新全局规则应单独确认，不能把文件存在当作全部会话已生效。

## 合成阻断验证

```sh
python3 -B -m unittest discover -s maintenance -p test_tool_sync.py -v
```

测试只创建临时工作仓库、本地裸远程、假 GitHub 元数据和合成敏感样例，结束后删除。不会使用真实 GitHub 仓库、私人聊天、媒体或 Vault。包含实际 Git pre-push 调用、历史中删除的秘密、私有性拒绝、既有 hook 保护、精确暂存、软链接和只读性检查。通过不表示真实业务或完整视频生产已验证。

## 技能卡公开准备与回填

config/publication.example.json提供明确本机路径模板。`python3 -B maintenance/skill_publication.py run`离线核对自建技能卡与逐文件源哈希，按唯一总账的公开配置准备确定性ZIP和人工确认清单；新卡、更新、直接编辑和源变动均覆盖。缺归属/白名单/许可或扫描命中明确记录，不创建批准、不推送。无变化不重复准备。

自动私人入库的tool写入在同一事务写publications/events，配置publication_config后即时处理；中断或失败可重试，宿主定时兜底调用同一命令。公开版变动须先升版本，不覆盖已有发行。

一个主工具可有默认public_distribution与具名public_distributions；share prepare支持--distribution。file_map仅把目标相对路径映射到同一已登记主位置的实际源文件，两侧都检查路径与软链接。所有文件仍走原有独立公开扫描，不继承私有例外。

share publish核实Release后自动本地快照并回填匹配卡片的“拿去用”；失败保留pending状态，可执行skill_publication.py backfill。卡片可见性与私人来源不变。环境变量PKOS_TOOL_REGISTRY可显式覆盖默认总账路径，便于独立公开包运行，不改变默认本机配置。

## 自建 Skill 纳管

`maintenance/skill_lifecycle.py finish` 将明确自建的 Skill 登记到唯一总账、安排主位置、创建或更新私人技能卡、写来源和发行事件，再离线准备公开清单。详见主仓库 `skills/pkos-system/references/skill-lifecycle.md`；独立发行包的说明在根 README 与 `weekly/README.md`。用户级 SessionStart/Stop hook 覆盖开发会话；每小时宿主核对兜底。未知来源由开发上下文分类，第三方排除；实际公开仍逐次确认冻结文件清单。
