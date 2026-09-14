# WaterlooWorks 本地简历匹配

## 使用

1. 在仓库根目录运行 `./scripts/setup-matcher.sh`。脚本安装独立 Python 环境、Chromium 和前端依赖并构建页面。需要 Python 3.10+、Node.js 20+；macOS 系统 Python 3.9 不够，脚本会优先回退到 `/opt/homebrew/bin/python3`，也可设置 `SWW_PYTHON`。
2. 运行 `./scripts/start-matcher.sh`，打开 <http://127.0.0.1:8765/waterlooworks>。
3. 上传文字可选中的 PDF 简历，最大 10 MiB、20 页。加密或纯扫描 PDF 会提示先解密或 OCR。简历只在服务内存中解析，重启后重新上传。
4. 点击打开登录浏览器，在弹出的独立 Chromium 内自行完成 WaterlooWorks 登录和学校双重验证，然后打开 Co-op Full-Cycle 职位列表。无需向工具输入或提供密码。
5. 检查职位列表的筛选条件。爬虫读取当前账号可见且符合该列表筛选的职位；如需全量，请清除不需要的筛选并切到表格视图。
6. 返回本地页面开始采集。先遍历列表分页，再逐条读取职位详情；请求间隔至少两秒。默认上限 100 页、3,000 个职位，可以调整。采集几千条详情可能需要数小时。可以停止，保留已取得的部分数据。
7. 将 DeepSeek API key 放入根目录 `deepseek_api.txt`（只放 key，或 `DEEPSEEK_API_KEY=sk-...`），也可设置环境变量 `DEEPSEEK_API_KEY`。页面会显示是否配置成功。默认模型为经接口核实可用的 `deepseek-flash`，可用 `DEEPSEEK_MODEL` 指定其他账号可用模型；服务只连接官方 `https://api.deepseek.com`。
8. 输入可选的目标岗位、地点、排除词，选择 DeepSeek 模式并生成排序。默认评估上限 500 个职位；超过时需要提高上限或筛选数据，程序不会用关键词偷偷缩减候选。可查看进度、停止、重新运行复用缓存，然后下载含证据明细的 CSV。少于 100 个成功评估时返回实际数量。

服务仅在 `127.0.0.1:8765` 运行。停止终端进程会关闭采集浏览器。原 JSM 功能可按原 README 单独启动；匹配服务不代理它的 Go API。

若没有安装 Playwright Chromium，服务会尝试本机 Chrome，仍使用完全独立的采集会话。也可用 `SWW_BROWSER_CHANNEL=chrome ./scripts/start-matcher.sh` 明确选择 Chrome（或 `msedge`）。只提供弹窗入口的职位链接会打开职位列表，请按显示的职位 ID 查找。

## 匹配规则

DeepSeek 模式直接评估每个未被明确规则排除的职位，不依赖向量库召回或 TF-IDF 截断候选集：

1. 本地去重，过滤明确关闭、过期和命中排除词的职位；不确定的截止日期保留并提示。
2. 模型按岗位实际要求拆出技术、经验、领域和资格条件，区分必需与优先条件，并引用简历中的直接经验或可迁移经验。岗位和简历文本被明确标为不可信资料，不能作为执行指令。
3. 代码核对每条引用确实存在于输入原文。岗位引用无效则丢弃该条；简历引用无效则降为未知、不给匹配分。不接受模型自报的总分。
4. 技术/经验/领域基础权重为 35/40/25，仅在模型识别出的维度内归一。各维度内部，必需条件权重 2，优先条件权重 1；直接证据系数 1，可迁移证据 0.65，缺失/未知为 0。填写偏好时预留 10 分用于明确的目标标题和地点匹配。只有资格必需条件在双方原文中有明确矛盾时才把分数上限设为 20；信息缺失不视为不符合资格。
5. 在 Top 100 边界和靠前位置选择至多 6 对相差不超过 3 分的岗位，正反顺序各比较一次。只有两次指向同一岗位、引用均可核对时才调整最多 ±1 分；冲突或证据不足时保留原分数。

默认单次并发 3 个评估。每个职位结果按模型、提示词版本、简历、岗位和偏好哈希缓存；改变任一项会重新评估。429/服务错误有限重试，认证/余额错误停止整次任务。结构错误或缺少职位详情的岗位单独报告失败，不把本地分数冒充 AI 分数。排名结果显示成功评估数、失败数、缓存数和本次 API token 用量。停止任务会取消本地请求，服务端已处理的请求仍可能收费。

简历 AI 输入上限 40,000 字符、单岗位 60,000 字符，超出会提示而非静默截断。模型最多返回 24 项主要要求，长岗位仍可能有遗漏。引用存在不等于模型解释正确；能力推断、要求的重要性和语义矛盾判断仍需人工核对。分数未用真实录用结果校准，不是录用概率。

页面仍可手动选择本地基础匹配：技能覆盖 60 分、TF-IDF 30 分、方向 10 分；设置地点偏好时技能 55 分、地点 5 分。该模式不调用 DeepSeek。AI 失败不会自动切换到它。

页面会显示采集状态、错误和缺失详情。只有分页总数与详情均能核对且没有警告时，才标为完整。列表或详情失败时允许基于部分结果排序，并保留不完整提示。一次新采集完全没有获得职位时会保留上次缓存并提示；注意数据采集时间。

## 本机数据

- `.sww/browser/`：专用浏览器会话，仅保存在本机。
- `.sww/jobs.json`：职位缓存、采集时间和完整性状态；目录权限 700，文件权限 600。
- PDF 原文件和完整提取正文保留在服务内存中。DeepSeek 模式会先尽力去除常见邮箱、电话和链接，再将简历正文、职位标题/公司/地点/描述/要求和偏好发送至 DeepSeek；姓名、学校、项目等仍可能识别个人，不能视为完全匿名。
- `.sww/ai-cache/`：单职位 AI 评估，包含简历/职位的证据摘录，文件权限 600；不会保存 API key 或模型隐藏推理内容。删除该目录可清除评估缓存。
- `deepseek_api.txt`：仅由后端读取；优先使用 `DEEPSEEK_API_KEY`，其次 `DEEPSEEK_API_KEY_FILE` 指定文件，再其次默认根目录文件。密钥不会发给浏览器、进入模型提示词或提交到 Git。服务不接受前端传入 API 域名。
- 页面从本地加载资源；采集浏览器需要连接 WaterlooWorks 和学校登录服务。
- CSV 由你主动下载到浏览器下载目录。CSV 中以公式符号开头的职位文本会转为文本值。
- `.sww/`、`.venv/`、`uploads/`、`output/`、`deepseek_api.txt` 已加入 `.gitignore`，不会推送到 GitHub。

需要清除登录会话时，先停止服务，再删除 `.sww/browser/`；删除 `.sww/jobs.json` 可清除职位缓存。不要把这些个人数据提交到仓库。

## JSON 导入

可以导入自己保存的职位 JSON，用来重新排序或在采集器需要适配时继续使用：

```json
{
  "jobs": [
    {
      "id": "123456",
      "title": "Software Developer",
      "company": "Example employer",
      "location": "Waterloo, ON",
      "description": "Build Python services and SQL data pipelines.",
      "requirements": "Python, SQL, Git",
      "deadline": "2027-01-20 11:59 PM",
      "url": "https://waterlooworks.uwaterloo.ca/myAccount/co-op/full/jobs.htm?jobId=123456"
    }
  ]
}
```

这是格式示例，不是真实职位。最多 10,000 条、整个请求最多 12 MiB。`title` 必填，其他字段可省略。非空链接必须属于 WaterlooWorks HTTPS 域名。导入结果始终标记为完整性未经核实。

## 开发与验证

```bash
.venv/bin/python -m pytest matcher/tests -q
cd frontend
npx tsc --noEmit
npm run build
```

开发前端可以运行 `npm --prefix frontend run dev -- --host 127.0.0.1 --port 5173`，打开 `http://127.0.0.1:5173/waterlooworks`。`/matcher-api` 代理到本地匹配服务；跨来源写请求受 Origin、Host 和 `X-SWW-Client: 1` 校验保护。

Python 依赖固定在 `matcher/requirements.lock`。CI 会检查 PDF、排序、API、HTML 解析及浏览器分页测试，浏览器测试使用合成 HTML，不访问真实账号。原 Go 后端的验证保留在原 CI 中。

AI 单元/接口测试使用模拟 API，不消耗额度。可显式运行以下合成案例评估，调用真实 DeepSeek，使用独立的 `.sww/evaluation-cache/`：

```bash
.venv/bin/python scripts/evaluate-deepseek.py --live
```

首次实测（`deepseek-flash`，5 个合成岗位）：基础排序把关键词密集的营销岗位和工期冲突岗位排在语义匹配岗位之前；新排序把相关后台/数据经历排到前面，并识别四个月与强制八个月的冲突。在这 5 个手工设定相关性等级的案例上，NDCG 从 0.6753 到 1.0000，使用 5 次请求、3,157 输入 / 4,192 输出 tokens。此结果只证明这些回归案例有效，不代表真实职位上的准确率或普遍提升。要可靠比较，还需用你人工标注的一批真实职位评估。

新增接口：`POST /matcher-api/ai/rank` 启动后台任务，`GET /matcher-api/status` 查询进度和配置状态，`GET /matcher-api/ai/result` 取已完成结果，`POST /matcher-api/ai/cancel` 停止。`/rank` 继续提供显式本地基础匹配。Go API 尚未参与这一流程。

DeepSeek 接口参考：[JSON 输出要求](https://api-docs.deepseek.com/guides/json_mode)、[思考模式配置](https://api-docs.deepseek.com/guides/thinking_mode/)。

**当前真实站点验证边界：** 未获得用户登录和简历前，无法声称已验证当前账号的全部真实职位或生成个人 Top 100。采集器实现参考公开的近期 WaterlooWorks DOM 记录，并以合成列表、弹窗和分页测试验证；如站点结构变化，会报告解析/分页失败，仍需要在实际账号上校准。

实现参考：[Playwright 登录会话](https://playwright.dev/python/docs/auth)、[pypdf 文本提取](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[WaterlooWorks 官方改版说明](https://uwaterloo.ca/co-operative-education/news/waterlooworks-updates-improved-user-experience)、[公开采集器作者的 DOM 工作流](https://github.com/jerryzhang1011/waterlooworks-application-plugins/blob/main/plugins/waterlooworks-jobs-codex/skills/ww-scrape-jobs/references/chrome-scrape-workflow.md)。
