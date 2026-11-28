# AGENTS.md：给 AI 的搭建与使用手册

你在“AI 测试管线”框架的仓库里。用户把它从 GitHub 拿下来，希望你一步步带他把整套流程搭起来、跑起来。CLAUDE.md、GEMINI.md 都指向这份文件。

## 你的角色

- 搭建向导：带用户完成环境、示例试跑、建项目工作区
- 建好工作区后，日常工作在**工作区文件夹**里做（那里有自己的 AGENTS.md），请用户在工作区里开新会话

## 每次开工

1. 运行 `python qa.py status`（Windows 不认 `python` 就用 `py`，macOS / Linux 可能是 `python3`）
2. 运行 `python qa.py next`，照着做下一步
3. 一次只做一件事；需要用户动手的地方，给出准确的操作（点哪里、输入什么），等用户说“好了”再用 `status` 核对

## 推荐路线

1. 检查环境：`python qa.py doctor`，缺依赖时**先问用户**再运行 `python qa.py setup`
2. 先用示例跑一遍（约 30 分钟，强烈推荐）：`python qa.py init <空文件夹> --example`，然后在那个文件夹里走完 status 列出的上手步骤。示例自带资料、接口文档、需求和一个能本地启动的被测服务，能看到每一步的样子
3. 建自己项目的工作区：先问用户是哪种环境（无代码权限 / 只开放接口或协议 / 能看代码），`python qa.py init <空文件夹> --name <项目名> --access blackbox|interface|source`，按 status 一步步做。三种环境的区别见 [docs/上手/08_环境与权限.md](docs/上手/08_环境与权限.md)
   - 再问用户资料多不多：策划文档几百份、配置表几千张、缺陷系统有导出的，资料留在原处登记、建索引，见 [docs/大批资料与检索.md](docs/大批资料与检索.md)
   - 项目已经做了很久的（老项目接入）：知识库先建模块清单，再按模块分批整理，不用等全部整理完，见 [docs/日常.md](docs/日常.md) 的“老项目中途接入”
4. 工作区建好后，请用户在**工作区文件夹**里开新会话；那里的 README.md 有“什么时候对 AI 说什么”，AGENTS.md 有规则。框架和工作区每个目录放什么见 [docs/目录说明.md](docs/目录说明.md)

## 人工关口：到这些地方必须停下来等用户

- 原始资料里的事实对不对、缺什么
- 知识库改动提交前（`wiki commit` 之前）
- 测试点审核：结论、否决原因、修改、补充，全部由用户定，你只能照用户说的记
- 接口用例清单：用户确认后才写代码
- 失败归因：用户确认后才登记缺陷
- 缺陷是否“被最终测试点覆盖”
- 模型对比的盲审：用户填完审核表之前，不要透露哪个方案是哪个模型
- 装软件、调用要花钱的模型接口、改被测项目代码、动工作区以外的文件：先问

## 不要做

- 不要把工作区建在框架文件夹里面（框架要上传 GitHub，工作区里是私有资料）
- 不要把密钥写进任何文件；模型密钥只放环境变量，配置里只写环境变量名
- 不要提交 qa.local.json、models.local.json（已在 .gitignore 里）
- 不要为了让用例通过去改断言

## 你没法执行命令时

请用户在终端里执行，把输出贴回来，你再判断下一步。

## 命令速查

| 要做什么 | 命令 |
|---|---|
| 进度和待办 | `python qa.py status` |
| 下一步做法 | `python qa.py next` |
| 检查环境 / 装依赖 | `python qa.py doctor` / `python qa.py setup` |
| 框架自检（不碰用户文件） | `python qa.py selftest`；改了框架代码再跑 `python -m pytest tests`（代码结构见 [docs/开发说明.md](docs/开发说明.md)） |
| 建工作区 | `python qa.py init <文件夹> --name <项目名>`（加 `--example` 用示例） |
| 环境权限（无代码 / 开放接口 / 完整代码） | `python qa.py access [blackbox / interface / source] [--source 代码文件夹]` |
| 导入资料 / 接口文档 / 抓包 | `python qa.py ingest <文件或文件夹>` / `... --api` / `--har 抓包.har`（有敏感信息加 `--redact`） |
| 资料有变化时一键刷新 | `python qa.py ingest --refresh` |
| 大批资料：登记目录、建索引 | `python qa.py corpus add <文件夹> --kind doc / config / defect`、`python qa.py index` |
| 检索策划文档 / 找表查表 / 历史缺陷 | `python qa.py search "关键词"` / `cfg find`、`cfg show 表名 --where 字段=值 [--expand]`、`cfg links [表名]`（表之间的关联） / `history find`、`history stats` |
| 看索引里某份资料的全文（Word、PDF 也行） | `python qa.py index show 文件名` |
| 向量模型（本地 Ollama） | `python qa.py embed set ollama --model bge-m3`、`embed test` |
| 手工执行记录 | `python qa.py exec R001 --round v1.0` |
| 按代码改动推荐回归范围（完整代码权限） | `python qa.py impact --range v1.2..v1.3` |
| 交给 AI 的任务说明 | `python qa.py prompt <wiki-build [--module 模块] / wiki-update / testpoints R001 / wiki-merge R001 / req-change R001 / cfg-check R001 / tests-plan / tests-write / triage / feedback>` |
| 模块清单（统一模块名、整理进度） | `python qa.py modules`；需求涉及哪些模块：`python qa.py req modules R001 [--set 模块1,模块2]` |
| 知识库检查 / 提交 / 待确认清单 | `python qa.py wiki check` / `python qa.py wiki commit -m "说明"` / `python qa.py wiki todo` |
| 项目仓库（自动上传） | `python qa.py remote set <私有仓库地址>`；第一次登录 `python qa.py sync`（在用户自己的终端里运行） |
| 新需求 | `python qa.py req new "标题" --from 需求文件 --module 模块` |
| 审核表 / 定稿 | `python qa.py review R001` / `python qa.py accept R001` |
| 定稿后新规则并进知识库 | `python qa.py prompt wiki-merge R001` → 人审 → `wiki commit --merged R001`；没有新规则 `python qa.py req merged R001` |
| 需求原文改了 | `python qa.py req refresh R001 [--from 新文件]` → `prompt req-change R001` → `review` → `accept` |
| 配置出来或改了，校对测试点 | `python qa.py prompt cfg-check R001` → 人确认 → `python qa.py req cfg-checked R001` |
| 版本回归 | `python qa.py regress new v1.3 [--modules …] [--level 高,中] [--cases 旧用例.xlsx]` → `regress run v1.3 --start` → 手工填完 → `regress check v1.3` |
| 模块耦合 | `python qa.py coupling [R001]`、两两组合 `python qa.py combos <因素表>` |
| 直接调模型做某一步（不用 AI 工具；网页、钩子、Jenkins 都走这里） | `python qa.py gen <testpoints R001 / wiki-build / wiki-update / wiki-merge R001 / req-change R001 / cfg-check R001 / tests-plan / tests-write / triage / feedback / wiki-lint> [--model 模型1,模型2]` |
| 模型对比 | `python qa.py bench testpoints R001 --models a,b,c` → 盲审 → `bench report R001` |
| 模型清单 / 试调 | `python qa.py models` / `python qa.py models --test <模型>` |
| 被测服务 | `python qa.py target list` / `python qa.py target set <名字> --url ... --start ...` |
| 跑接口用例 | `python qa.py autotest run [--start] [--target 名字] [--exit-code]`（`--exit-code`：有失败时退出码 1，给 Jenkins 用） |
| 缺陷 | `python qa.py defect new "标题" --req R001` / `defect list` |
| 指标 / 用量 | `python qa.py metrics` / `python qa.py usage` |
| 扫描密钥 | `python qa.py check` |

更多说明：[docs/架构.md](docs/架构.md)（怎么运作）、[docs/日常.md](docs/日常.md)（日常用法）、[docs/多模型与选型.md](docs/多模型与选型.md)、[docs/扩展.md](docs/扩展.md)。
