# KATML paper feeds

一个仅用 Python 标准库的公开 RSS 生成器。GitHub Actions 每天 **18:50 UTC**（新西兰夏令时次日 07:50，冬令时次日 06:50）或手动运行，从 OpenAlex 和 OpenReview 抓取论文；两份 RSS 经完整抓取和 XML 验证后，才交给 GitHub Pages 部署。失败时不部署本次结果；已有成功部署时，Pages 保留上次版本。仓库和 RSS 中不存 API key，也不保存抓取状态。

## 先设置

1. 核对 [`config.json`](config.json)：`lookback_days` 是回看天数；`openalex.search_terms` 是用户指定的七个短语；`openreview.invitation_ids` 列出 TMLR、ICLR 2027、AISTATS 2027、UAI 2026、CoLLAs 2026、CoRL 2026。AISTATS 2027 和 CoLLAs 2026 的公开投稿目前尚未确认可读取；UAI 2026 和 CoRL 2026 的投稿时间较早，可能落在回看窗口外。目前使用 OpenReview API v2；旧会议若只在 API v1，不能直接填入。OpenReview 的 [官方检索说明](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc)建议从 venue 的 `submission_id` 或 `submission_name` 确认 invitation ID。
2. 在 [OpenAlex Settings → API key](https://openalex.org/settings/api) 创建免费账户并验证邮箱，复制 API key。到仓库 **Settings → Secrets and variables → Actions → New repository secret**，添加名为 `OPENALEX_API_KEY` 的 secret。程序通过 HTTP `Authorization` header 使用它，不写进 URL、日志或 RSS。
3. 在仓库 **Settings → Pages → Build and deployment → Source** 选择 **GitHub Actions**。
4. 把文件提交并推送到默认分支 `main`；到 **Actions → Publish paper feeds → Run workflow** 手动测试。确认日志里有两个来源的 `fetched` 与 `unique/written` 数量，以及 Pages 部署成功。如果配置窗口内某来源没有论文，程序会拒绝发布空 feed；可核对 invitation、词和回看天数后重跑。

## 运行频率与抓取范围

两个开关彼此独立：

| 想调整什么 | 修改位置 | 当前值 |
| --- | --- | --- |
| Actions 多久运行一次 | [`.github/workflows/publish.yml`](.github/workflows/publish.yml) 中的 `schedule.cron` | `50 18 * * *`，每天 UTC 18:50 |
| 每次向前回看多久 | [`config.json`](config.json) 中的 `lookback_days` | 5 天 |

修改 `cron` 后提交到默认分支即可。例子：`50 18 * * 1,4` 是每周一和周四 UTC 18:50；`50 18 * * 1` 是每周一同一时间。`workflow_dispatch` 始终允许在 Actions 页面手动运行。GitHub 定时任务按 [POSIX cron](https://docs.github.com/en/actions/reference/workflows-and-actions/workflow-syntax) 解释，可能延迟；调整频率时也检查 OpenAlex API 用量。`lookback_days` 建议大于两次计划运行之间的最长间隔，留出漏跑的余量。

每次运行都**不是全平台扫描**，也不只抓运行当天。当前 `lookback_days = 5` 时，OpenAlex 对每个检索词查询从「UTC 今天减 5 天」至「UTC 今天」的**发表日期**（按日期包含两端）；OpenReview 仅查配置的 invitation，按 note 的 `tcdate` 查询过去整整 5×24 小时。程序遍历这两个查询范围内的全部分页结果，在每个平台内部按稳定 ID 去重后重新生成 RSS。改小 `lookback_days` 会缩小补读机会；改大则可能增加 API 用量、RSS 大小，超过连接器 5 MB 上限时会失败而不部署。

## RSS 地址

发布后两个地址是：

- `https://sji71.github.io/KATML-paper-feeds/openalex.xml`
- `https://sji71.github.io/KATML-paper-feeds/openreview.xml`

可在浏览器打开并确认根元素是 `<rss version="2.0">`。如果改了仓库名、用户名或 Pages 自定义域名，以 Pages 设置显示的站点 URL 加 `/openalex.xml`、`/openreview.xml` 为准。不要把 OpenAlex 或 OpenReview API 的原始 JSON URL 填入 Power Automate。

## Power Automate 接入

在原有三个 arXiv Get、Combine arXiv、Select arXiv 旁，分别新增两个并行的标准连接器 **RSS → List all RSS feed items** 分支，填上两个 Pages RSS URL。每个 Get 后添加 **Select**，其 **From** 选择对应 Get 的 body 数组，映射如下；OpenReview 分支的 `source` 改成 `openreview`：

| Select 输出字段 | RSS Get 的字段 / 表达式 |
| --- | --- |
| `id` | `item()?['id']` |
| `title` | `item()?['title']` |
| `summary` | `item()?['summary']` |
| `primaryLink` | `item()?['primaryLink']` |
| `publishDate` | `item()?['publishDate']` |
| `source` | 普通文字 `openalex` 或 `openreview` |

将 `Select arXiv`、`Select OpenAlex`、`Select OpenReview` 三路结果交给 `Combine all`，之后沿用现有的 `Filter relevant` 和 SharePoint 查重。RSS 中的 `guid`、`title`、`description`、`link`、`pubDate` 分别供连接器读取为 `id`、`title`、`summary`、`primaryLink`、`publishDate`。首次运行时建议检查一次 Get 的实际输出字段名；微软连接器版本若不同，以其输出为准。

## 覆盖边界与运行维护

每次运行都会重新生成最近 `lookback_days` 的 feed，因此 Power Automate 漏跑一次后，仍可在论文留在窗口内时补读；SharePoint 以稳定 ID 去重。OpenAlex 这里按 **publication_date** 查询，迟于回看窗口才补录到 OpenAlex 的旧发表论文可能漏掉。[OpenAlex 官方说明](https://help.openalex.org/access/sync/)按数据库 `from_created_date` / `from_updated_date` 增量筛选是付费功能，免费方案不保证零遗漏。OpenReview 按 note 的真实创建时间 `tcdate` 筛选；创建较早、后来才公开或修改的 note，以及旧 API 会议，可能漏掉。GitHub 定时任务也可能延迟或偶尔被丢弃；可在 Actions 运行历史中检查。[RSS 连接器文档](https://learn.microsoft.com/en-us/connectors/rss/)列出 5 MB 内容上限，超出时生成器会失败而不部署。

[OpenAlex 的 `title` 可以为 null](https://help.openalex.org/data/works/attributes/)；这类记录缺少 RSS 必需的标题，会按 ID 计数并跳过。若窗口内因此没有可写的论文，生成任务仍会失败，不部署空 feed。

OpenAlex 摘要倒排索引按位置排序还原；若位置有缺号，只能保留 API 实际给出的词，无法推断缺失文字。分页数量不完整仍会让任务失败。

外部 API 拒绝访问也会让生成任务失败。2026-10-20 从本地匿名读取 OpenReview `/notes` 时返回 `403 ChallengeRequiredError`；邀请 ID 已从公开群组元数据核对，但这不等于 GitHub Actions 已能抓取 note。必须以手动运行的结果确认。失败时不会更新 Pages；若此前从未成功部署，两个 RSS URL 仍可能是 404，Power Automate 的 RSS Get 会失败。

[GitHub 官方说明](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/disable-and-enable-workflows)：公开仓库 **60 天无仓库活动**会自动停用定时工作流。最省事的可验证做法是每月查看一次 **Actions → Publish paper feeds** 的最近运行日期和工作流启用状态，并在 60 天内对仓库做一次真实的配置/文档提交；仅有定时运行和 Pages 部署不应被当成维持仓库活动的保证。若已停用，在该工作流页面点 **Enable workflow**，然后手动 **Run workflow**，确认两个 XML 更新。需要完全无人值守时应另用仓库外监控/触发器；本仓库不加自提交保活工作流。

本地测试：`python3 -m unittest -v`。本地实际生成：`OPENALEX_API_KEY=... python3 generate.py`（先设置真实 invitation）。`dist/` 已在 `.gitignore` 中；Pages 只发布本次运行生成的两份 XML。
