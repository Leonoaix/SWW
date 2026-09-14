# WaterlooWorks 本地简历匹配

## 使用

1. 在仓库根目录运行 `./scripts/setup-matcher.sh`。脚本安装独立 Python 环境、Chromium 和前端依赖并构建页面。需要 Python 3.10+、Node.js 20+；macOS 系统 Python 3.9 不够，脚本会优先回退到 `/opt/homebrew/bin/python3`，也可设置 `SWW_PYTHON`。
2. 运行 `./scripts/start-matcher.sh`，打开 <http://127.0.0.1:8765/waterlooworks>。
3. 上传文字可选中的 PDF 简历，最大 10 MiB、20 页。加密或纯扫描 PDF 会提示先解密或 OCR。简历只在服务内存中解析，重启后重新上传。
4. 点击打开登录浏览器，在弹出的独立 Chromium 内自行完成 WaterlooWorks 登录和学校双重验证，然后打开 Co-op Full-Cycle 职位列表。无需向工具输入或提供密码。
5. 检查职位列表的筛选条件。爬虫读取当前账号可见且符合该列表筛选的职位；如需全量，请清除不需要的筛选并切到表格视图。
6. 返回本地页面开始采集。先遍历列表分页，再逐条读取职位详情；请求间隔至少两秒。默认上限 100 页、3,000 个职位，可以调整。采集几千条详情可能需要数小时。可以停止，保留已取得的部分数据。
7. 输入可选的目标岗位、地点、排除词，生成排序，查看匹配理由并下载 CSV。结果只包含实际取得的职位，少于 100 个时返回实际数量。

服务仅在 `127.0.0.1:8765` 运行。停止终端进程会关闭采集浏览器。原 JSM 功能可按原 README 单独启动；匹配服务不代理它的 Go API。

若没有安装 Playwright Chromium，服务会尝试本机 Chrome，仍使用完全独立的采集会话。也可用 `SWW_BROWSER_CHANNEL=chrome ./scripts/start-matcher.sh` 明确选择 Chrome（或 `msedge`）。只提供弹窗入口的职位链接会打开职位列表，请按显示的职位 ID 查找。

## 匹配规则

默认采用可解释的本地评分：技能覆盖 60 分、简历与职位描述的 TF-IDF 文本相关性 30 分、岗位方向 10 分。填写地点偏好时，技能为 55 分，地点为 5 分；方向和地点是加分偏好，排除词才是过滤条件。

- 技能覆盖：职位中识别出的技能，有多少出现在简历中。技能别名会合并，例如 JS/JavaScript。
- 文本相关性：使用本次职位集合计算 TF-IDF 和余弦相似度，标题加权。
- 方向匹配：依据简历明确出现的岗位方向，或你输入的目标岗位。
- 过滤明确关闭、截止日期已过、命中排除词的职位，并去重。没有或无法识别截止日期时保留并提醒核实；日期按 Toronto 时区处理。

评分是文本匹配指标，不代表录用概率。技能表是本地词库，未识别出的技能、中文描述、隐含经验和跨领域能力可能降低准确度。“未在简历中出现的技能”不代表你不会，也不代表雇主全部强制要求。岗位的专业、年级、工期、地区或工作许可等条件须查看原文核对。排名只比较本次采集或导入的数据。

页面会显示采集状态、错误和缺失详情。只有分页总数与详情均能核对且没有警告时，才标为完整。列表或详情失败时允许基于部分结果排序，并保留不完整提示。一次新采集完全没有获得职位时会保留上次缓存并提示；注意数据采集时间。

## 本机数据

- `.sww/browser/`：专用浏览器会话，仅保存在本机。
- `.sww/jobs.json`：职位缓存、采集时间和完整性状态；目录权限 700，文件权限 600。
- 简历 PDF 和提取正文不会写入磁盘，也不会发给第三方模型。
- 页面从本地加载资源；采集浏览器需要连接 WaterlooWorks 和学校登录服务。
- CSV 由你主动下载到浏览器下载目录。CSV 中以公式符号开头的职位文本会转为文本值。
- `.sww/`、`.venv/`、`uploads/`、`output/` 已加入 `.gitignore`，不会推送到 GitHub。

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

**当前真实站点验证边界：** 未获得用户登录和简历前，无法声称已验证当前账号的全部真实职位或生成个人 Top 100。采集器实现参考公开的近期 WaterlooWorks DOM 记录，并以合成列表、弹窗和分页测试验证；如站点结构变化，会报告解析/分页失败，仍需要在实际账号上校准。

实现参考：[Playwright 登录会话](https://playwright.dev/python/docs/auth)、[pypdf 文本提取](https://pypdf.readthedocs.io/en/stable/user/extract-text.html)、[WaterlooWorks 官方改版说明](https://uwaterloo.ca/co-operative-education/news/waterlooworks-updates-improved-user-experience)、[公开采集器作者的 DOM 工作流](https://github.com/jerryzhang1011/waterlooworks-application-plugins/blob/main/plugins/waterlooworks-jobs-codex/skills/ww-scrape-jobs/references/chrome-scrape-workflow.md)。
