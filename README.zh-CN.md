# jobpipe

[English](README.md)

一套本地优先（local-first）的求职流水线。它从 14+ 个 ATS 招聘板公开 API（Greenhouse、Lever、Ashby、Workday 等）抓取岗位，用一个透明的规则引擎按你的 profile 给每个岗位打分，可选地用一次廉价的 LLM 二次筛查精炼 JD，产出排好序的每日投递清单，追踪你的投递漏斗，并在同一个数据库之上提供一个 Web 看板。一切都在你自己的机器上运行：你的 profile、投递记录和笔记都存在本地 SQLite 文件里，绝不外传——唯一的出站数据是发给你自己配置的 LLM 后端的 JD 文本（前提是你启用了 LLM 精炼）。

## 快速开始

```bash
pip install -r requirements.txt

python jobpipe.py init        # 交互式向导 -> config/profile.yaml + config/answers.yaml
python jobpipe.py verify      # 探测 config/companies.yaml 里的 ATS token 是否有效
python jobpipe.py fetch       # 抓取全部招聘板、落库、打分（首次运行需要一段时间）
python jobpipe.py shortlist   # 今日排序投递清单
python board/app.py           # Web 看板：http://127.0.0.1:5175
```

也可以选择从简历引导生成 profile，省去手动输入：

```bash
pip install pypdf
python jobpipe.py import-resume path/to/resume.pdf   # LLM 起草 profile 字段，你确认后合并
```

## 工作原理

```
config/companies.yaml ─┐
                       ├─> fetch ──> jobs (SQLite) ──> score ──> enrich ──> shortlist ──> data/today.md
config/profile.yaml  ──┘                                 ^                                    │
                                              sponsor_records                                 │  你去投递
                                              (H-1B 数据)                                     v
                                                                              applications + events
                                                                                     │
                                                                                     v
                                                                          dashboard + Web 看板
```

1. **fetch** 并发抓取 `config/companies.yaml` 里的每个招聘板（8 线程），增量 upsert：新岗位插入、已有岗位刷新、招聘板不再返回的岗位标记为下架。一家公司的首次抓取会被标记为存量岗，避免几百个旧岗位伪装成新发布。抓完立即打分，随后自动运行 LLM 精炼（未配置后端时静默跳过）。
2. **score** 是一个纯规则引擎，全部规则来自 `config/profile.yaml`——调参不用改代码，且每个分数都带完整的分项拆解落库，事后可以审计一个岗位为什么排在那个位置。值得一提的几个设计：
   - **年限取自 JD 正文，不是标题。** 标题在两个方向上都会骗人：有的岗位标题只写 "Data Engineer" 正文却要 8+ 年，有的 "Senior" 岗位正文只要 3 年。解析器读正文，并把两种写法分开处理——同一句里的区间（"2–6+ years of experience"）取**下限**（那才是入门门槛）；不同句子之间的要求（"10+ years in X" 加 "2+ years doing Y"）取**最大值**（每条都得满足）。标题资历扣分只在正文没写年限时启用；达到 `yoe.reject_at` 直接淘汰。
   - **关键词按词边界匹配。** 子串匹配是灾难性的：`unity` 会命中每份 JD 都有的 "equal opportunity employer" 套话，`ios` 命中 "scenarios"，`ny` 命中 "Sunnyvale"。词条按词边界匹配；结尾加 `*` 表示前缀匹配（`idempoten*` 同时覆盖 idempotent 和 idempotency）。
   - **美国锚点地点逻辑。** 美国境内任何地点都保留、只做分档加分；纯海外岗位淘汰。列了 "New York or London" 的留下，"Remote – India" 不留。
   - **Sponsorship 检测。** 可配置的正则既能抓出明说不提供 sponsorship / 要求公民身份或 security clearance 的 JD（在你需要 sponsorship 时硬淘汰），也能抓出明说提供 sponsorship 的 JD（加分），再叠加公司层面的 H-1B 历史记录（见下文）。
   - **新鲜度基于真实发布日期。** `posted_at` 是主判据；ATS 的 "updated" 时间戳只说明招聘方碰过这个岗位，所以最多补一点小加分，绝不当作新发布处理。
3. **enrich**（可选）只对新增或正文变化的 JD 跑一个轻量 LLM（按正文哈希判断，绝不重复花钱），提取结构化事实：最低年限、资历档位、sponsorship 态度、是否美国岗位、两句话摘要和红旗提示。它发现的硬性障碍——年限达到淘汰线、明说不 sponsor、非美国岗位——会直接否决该岗位。
4. **shortlist** 输出排序清单到终端和 `data/today.md`。排序是「分数排序 + 新岗位保底名额」，而**不是**「新的无条件排前面」——新鲜度加分已经在分数里，再做硬优先就是重复计算。两个防海投规则：同一公司同时进行的流程不超过 `concurrent_per_company` 个；同公司同标题投过一次就不再推（岗位下架重发会拿到新 ID，没有这条规则它们会反复出现）。
5. **apply / track** 记录每次投递（渠道、简历版本、是否定制），并沿漏斗推进：`applied → screen → tech → onsite → offer`（或 `rejected` / `ghosted` / `withdrawn`）。超过 `ghost_days`（默认 21 天）没回音的投递会被清扫为 `ghosted`，避免稀释回复率。`did` 记录不产生投递的动作（请求内推、networking、follow-up）——这些才是每天能看到的领先指标。
6. **dash / board** 渲染反馈闭环：领先指标卡片、漏斗，以及按渠道、按简历版本拆分的回复率——那张告诉你下周时间该花在哪的表。

## 命令参考

| 命令 | 作用 |
|---|---|
| `init` | 交互式向导：生成 `config/profile.yaml` 和 `config/answers.yaml` |
| `import-resume <pdf>` | 通过 LLM 后端从简历 PDF 起草 profile 字段（需要 `pypdf`），确认后合并 |
| `verify [--only S]` | 探测 `companies.yaml` 里的 ATS token 是否仍然有效——第一步先跑这个 |
| `fetch [--only S]` | 抓取全部招聘板、upsert、打分，然后自动运行 LLM 精炼 |
| `rescore` | 改完 `profile.yaml` 后重新打分（不重抓） |
| `enrich [--limit N]` | 对新增/变化的 JD 跑 LLM 精炼 |
| `prune` | 清理下架的过期岗位并压缩数据库 |
| `shortlist [-n N] [--min-score S] [--all]` | 生成今日排序投递清单 |
| `apply <index> [--channel C] [--resume R] [--tailored] [--note N]` | 按清单序号记录一次投递 |
| `manual --company C --title T [...]` | 记录一次清单之外的投递 |
| `status <id> <stage> [--note N]` | 推进一条投递的漏斗阶段 |
| `did <kind> [--company C] [--note N]` | 记录不产生投递的动作（`referral_ask`、`networking`、`followup`、`recruiter_reply`） |
| `open` | 列出进行中的投递（用来查 ID） |
| `sweep` | 把超过无回音阈值的投递标记为 ghosted |
| `dash [--no-open]` | 生成并打开 HTML 反馈看板 |
| `sponsor-ingest <file>` | 导入 DOL/USCIS H-1B 披露数据文件，然后重新打分 |
| `export` | 把投递记录导出成 CSV 到 `records/` |
| `schedule [--status]` | 生成 macOS launchd 定时任务，让 fetch 一天自动跑三轮 |
| `day` | 每日入口：清扫 ghosted + 出清单 + 导出 + 看板 |

## 配置

`config/` 下三个文件。你真实的 `profile.yaml` 和 `answers.yaml` 由 `init` 生成并且**在 .gitignore 里**——它们包含你的个人数据，永远不会离开你的机器。

### profile.yaml

所有打分规则都在这里；改完运行 `rescore`（不需要重抓）。

| 段 | 控制什么 |
|---|---|
| `candidate` | 你的名字、工作年限，以及 `needs_sponsorship`（设为 `false` 可关闭全部 sponsorship 逻辑） |
| `titles` | 目标标题的三档模式：`strong`（45 分）/ `good`（32）/ `weak`（15）；外加 `title_blockers`（硬淘汰：staff、principal、director、intern 等）、`title_penalties`（仅在 JD 正文没写年限时启用）和 `junior_title_bonus` |
| `yoe` | 针对 JD 最低年限要求的加减分曲线、`reject_at`（硬淘汰阈值）、`above_max_penalty` |
| `keywords` | 带权重的技术栈关键词（由 `keyword_cap` 封顶）和方向偏离的 `keyword_penalties` |
| `locations` | `tiers`（每档含 `match` 模式、`points`，以及可选的 `us_anchor` 标记——它让美国/海外混列的岗位得以保留）、`blockers`（海外模式）、`default_points` |
| `sponsorship` | JD 文本 sponsorship 检测用的 `negative` / `positive` 正则列表 |
| `sponsor_history_bonus`、`bar_adjust` | 各公司级 sponsor 标记对应的加分；手工标注的高门槛公司的调整分 |
| `golden` | 看板黄金页的准入规则：`min_score`、`max_age_days`、`second_window_days`、`max_req_yoe`、`stretch_yoe` / `stretch_min_score`、允许的 `sponsor` 标记、`exclude_senior_title`、`exclude_bar`、`title_tiers` |
| `llm` | `enabled`、`backend`（`auto`/`api`/`cc`/`off`）、`model`、`max_per_run` |
| `freshness` | `posted_bonus` / `updated_bonus` 映射、`fresh_window_days`、`fresh_reserved`（清单里给新岗位的保底名额）、`fresh_lane_min_score` |
| `thresholds` | `daily_shortlist_size`、`shortlist_min_score`、`concurrent_per_company`、`ghost_days`、`stale_days`、`archive_days` |
| `stories` | 可选的项目故事及其触发关键词；清单和看板会按岗位提示该主打哪个故事 |

### answers.yaml

你的申请答题库：一个 `identity` 块（姓名、邮箱、地点、工作许可等）和一个 `standard` 列表，收录高频筛选题和你的标准答案。Web 看板的投递模式会把它们摆在岗位旁边供复制粘贴。

### companies.yaml

目标公司名单——自带 350+ 家公司和实测可用的公开 ATS token，开箱即用。每条字段：`name`、`ats`、`token`（Workday/Eightfold 类源用 `site`）、可选的 `sponsor` 种子猜测、`aliases`（H-1B 匹配用的法人名）、`bar: high`（高门槛公司打分吃扣分并被黄金页排除），以及仅供参考的 `tier`。token 会随着公司迁移 ATS 而失效——隔一两周重跑 `verify`，失效的修掉或删掉。

### 怎么找一家公司的 ATS token

大多数时候 token 就在招聘页 URL 里：

| ATS | 招聘页 URL 长这样 | token / site 取值 |
|---|---|---|
| Greenhouse | `boards.greenhouse.io/{token}` 或 `job-boards.greenhouse.io/{token}` | `{token}` |
| Lever | `jobs.lever.co/{token}` | `{token}` |
| Ashby | `jobs.ashbyhq.com/{token}` | `{token}` |
| Workable | `apply.workable.com/{token}` | `{token}` |
| Recruitee | `{token}.recruitee.com` | `{token}` |
| SmartRecruiters | `careers.smartrecruiters.com/{Token}` | `{Token}` |
| Workday | `{tenant}.wd{N}.myworkdayjobs.com/{Site}` | `site: "{tenant}/wd{N}/{Site}"` |

公司把招聘板挂在自己域名下时，用 Network 面板：打开招聘页 → F12 → Network → 刷新 → 找发往 `boards-api.greenhouse.io`、`api.lever.co`、`api.ashbyhq.com`、`apply.workable.com` 或 `api.smartrecruiters.com` 的请求，请求 URL 里那段路径就是 token。把条目填进 `config/companies.yaml`，跑 `verify` 确认。

Workday 是最脆弱的一个：每家的 tenant/site/wdN 组合都不同，结构还偶尔会变。Workday 条目 `verify` 通不过就直接删掉——不值得纠结。

有两个特殊数据源不需要找 token：`hn` 读 Hacker News 每月的 "Who is hiring?" 帖；`adzuna` 是聚合器，覆盖公司名单之外的长尾（在 developer.adzuna.com 免费注册 API key，通过 `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` 环境变量传入）。

## LLM 精炼

正则打分是第一道粗筛；"8+ years across data engineering" 这类写法会从它指缝里漏过去。精炼这一步对每个保留岗位的 JD 跑一个轻量模型（默认 Haiku），提取的结构化事实由打分器和看板共同使用。后端通过 `profile.yaml` 的 `llm.backend` 设置：

- **`api`** —— Anthropic API（需要 `ANTHROPIC_API_KEY`）。按量计费；典型规模下每天大约几美分，且增量哈希保证同一份 JD 绝不付两次钱。更快也更稳定。
- **`cc`** —— Claude Code 无头模式（`claude -p --model haiku`）。用你已有的订阅额度，零边际成本；每次调用批量处理 12 个岗位以摊薄启动开销。和你自己的交互式使用共享同一额度池。
- **`auto`**（默认）—— 有 API 凭证就用 `api`，否则装了 `claude` 命令就用 `cc`，都没有就静默跳过精炼。
- **`off`** —— 彻底关闭精炼。没有它流水线也完全能用，只是回到纯正则打分。

## H-1B sponsorship 数据

面向需要签证 sponsorship 的求职者——如果你不需要，在 `profile.yaml` 里设置 `candidate.needs_sponsorship: false`，整个子系统随之关闭。

两个公开免费的数据集提供了哪些公司真的 sponsor 的实证：

- **USCIS H-1B Employer Data Hub**（批准层面，证据更强）—— https://www.uscis.gov/tools/reports-and-studies/h-1b-employer-data-hub
- **DOL OFLC LCA 披露文件**（申请层面，量更大）—— https://www.dol.gov/agencies/eta/foreign-labor/performance （xlsx 文件需要 `pip install openpyxl`，或者先另存成 CSV）

下载文件后导入：

```bash
python jobpipe.py sponsor-ingest ~/Downloads/h1b_datahubexport-2024.csv
```

每家公司按批准数量得到一个标记：**heavy**（25+ ——常态化 sponsor 的公司）、**yes**（3–24）、**low**（1–2）、**none**（查无记录）。阈值按真实分布校准过：100+ 批准的几乎全是外包巨头，而许多知名的 sponsor 友好科技公司只有几十条记录。打分通过 `sponsor_history_bonus` 使用这个标记，看板黄金页默认只放行 sponsor 正向的标记。

一个坑：**法人名不等于品牌名**——很多公司用与你熟知的品牌不同的法人实体提交申请。匹配不上就会被误判成「不 sponsor」，所以 `companies.yaml` 支持 `aliases: [...]` 法人名列表，而且「查无记录」只吃很小的扣分（误判的代价比漏掉大）。在你导入真实数据之前，`companies.yaml` 里的 `sponsor:` 种子猜测充当兜底。

## 定时运行

只有 `fetch` 需要定时——其余全是交互式的。macOS 上：

```bash
python jobpipe.py schedule           # 生成 launchd plist
python jobpipe.py schedule --status  # 定时任务在不在跑 + 最近的抓取日志
```

它会写出 `~/Library/LaunchAgents/com.jobpipe.fetch.plist`（9:00、13:00、18:00 各抓一轮），并**打印**一条 `launchctl load` 命令由你自己执行——它从不自行修改系统配置。笔记本合盖漏掉一轮的代价几乎为零：岗位在招聘板上会挂几天到几周，下一轮全部补上。

## Web 看板

```bash
python board/app.py                  # http://127.0.0.1:5175
BOARD_PORT=8080 python board/app.py  # 换端口
```

读同一个 SQLite 数据库，不复制任何业务逻辑。页面：

- **黄金页** —— 最值得优先投的交集：新鲜、高分、年限够得着、sponsor 正向（规则在 `profile.yaml` 的 `golden` 段），分为 TOP 窗口和补漏窗口。
- **岗位页** —— 保留岗位的完整浏览器，带筛选：搜索、岗位类型、资历、最高年限要求、sponsor 标记、最低分、岗位年龄。
- **投递模式** —— 岗位与你 `answers.yaml` 里的身份字段和标准答案并排展示，方便复制粘贴。
- **流程页** —— 投递漏斗与阶段流转。
- **总览页** —— 统计与回复率拆分。

看板只绑定 `127.0.0.1`——这是个人工具，不该对外暴露。

## 善意使用

- 每个适配器调用的都是 ATS **公开、无鉴权的招聘板 API**——就是招聘板前端自己在调的那些接口。绝不绕过任何鉴权。
- **不碰 LinkedIn，不碰 Indeed。** 它们的用户协议明确禁止脚本访问，被检测到会危及你正在用来求职的那个账号。在那里看到的岗位用 `manual` 手动记一笔。
- 温和抓取：抓取器是增量的（每个岗位的正文只抓一次）、低流量的，对需要限速的源会限速。一天三轮是设计节奏——别把它改成爬虫。
- 这个工具自动化的是**发现和筛选**，不是海投。刻意没有自动提交功能：投递质量胜过数量，漏斗的存在就是为了向你证明这一点。

## 许可证

[MIT](LICENSE)。
