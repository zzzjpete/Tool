# 爬虫工具 — 项目概览（请帮我从新用户视角审视）

> 这份文档的目的：把项目目前的状态、最近的改动、核心功能完整地呈现给一个**没有上下文**的 Claude，请你站在**一个刚发现这个工具的新用户**的角度，告诉我哪里还可以改进。
>
> 重点不是代码 review（架构层面已经 review 过了），而是 **onboarding 体验、使用流畅度、文档可读性、心智模型清晰度**这些"外壳"层面的反馈。

---

## 1. 项目是什么

一个用 Python 写的 **B 站 + 知乎 异步爬虫框架**，定位是个人/研究用途。

**典型使用场景**：研究某个话题（比如"电动车"、"国内新能源汽车"）在两个平台的讨论热度 — 收集帖子、回答、评论、点赞数、收藏数等，存到 SQLite，跑 pandas 做趋势分析。

**特点**：
- 异步 (asyncio + curl_cffi)
- 内置速率限制 + 重试 + UA/Sec-CH-UA 轮换
- 双层反爬（curl_cffi TLS 伪装 + Playwright stealth）
- 内置 ML 长时段分析层（每次采集自动写 engagement_snapshots，可看互动增长曲线）
- 自带 Jupyter notebook 模板，Restart-and-Run-All 就能生成一份图文分析报告
- 完整中文 CLI 体验

**运行环境**：Windows / Python 3.12+ / 命令行（PowerShell）

---

## 2. 目录结构（精简版）

```
scraper/
  core/           session (curl_cffi), browser (Playwright), storage, config, ...
  platforms/
    bilibili/     scraper + WBI 签名
    zhihu/        scraper (httpx 路径 + browser 路径混用)
  analyze.py      pandas 助手
  viz.py          matplotlib 绘图助手
  cli.py          所有 CLI 命令
examples/
  notebooks/discussion_volume.ipynb    # 起步分析 notebook
  scrape_*.py                          # 单文件示例
config.example.yaml                    # 配置模板
快速开始.txt                            # 60 行新手快速上手
使用说明.txt                            # ~520 行完整手册
CLAUDE.md                              # 给 AI / 开发者看的架构笔记
README.md                              # 项目简介
```

**数据落在哪**：
- `data/scraped.db` — SQLite 主数据库
- `data/exports/<关键词>_run<编号>_{posts,comments}.csv` — 每次采集自动导出 CSV（UTF-8 BOM，Excel 直接打开）
- `data/cookies/<platform>.json` — 持久化的 HTTP cookie jar
- `data/browser-profile/<platform>/` — 持久化的浏览器 profile

---

## 3. CLI 全貌

```
python -m scraper              # 不带子命令 → 进引导式交互模式（推荐新手）
python -m scraper init         # 首次设置向导（config + 浏览器登录 + Playwright 安装）
python -m scraper login zhihu  # 浏览器登录抓 cookie（替代手动 F12 复制）
python -m scraper doctor       # 体检：缺啥提示啥 + 修复命令
python -m scraper status       # 数据库现状（行数、最近 run、Cookie 状态）

python -m scraper scrape "电动车" --count 10            # 双平台一键采
python -m scraper bili scrape "电动车" --count 5        # 只采 B 站
python -m scraper zhihu scrape "电动车" --count 3       # 只采知乎

python -m scraper interactive                           # 引导式向导（循环模式）
python -m scraper view video <BVID>                     # 查看单条记录
python -m scraper view answer 2199786648 --comments 20  # 看回答 + 评论
python -m scraper export --format csv --table ...       # 手动导出
```

---

## 4. 最近做的两批改进

### 第一批：onboarding / UX 友好化

**痛点**：旧版本要求新用户做这些事才能跑起来：
1. `pip install -r requirements.txt`
2. 复制 `config.example.yaml` → `config.yaml`
3. 浏览器 F12 打开 → Network → 刷新 → 找到 cookie → 复制整段 → 粘贴到 yaml
4. 装 Playwright + Chromium（150 MB）
5. 学 `scrape "关键词" --count N` 这些 flag

→ 7 步才能开始第一次爬。

**改进后**：
1. `pip install -e .`
2. `python -m scraper`  ← 一条命令搞定剩下所有事

具体改了什么：
- **新增 `login` 命令** — 弹出真实浏览器，用户正常登录（账号密码 / 短信 / 扫码均可），按回车 → cookie 自动写入 config.yaml。完全替代 F12 流程。
- **`interactive` 改成循环式** — 设置只问一次，然后循环 prompt 关键词。`q` 退出，`s` 改设置。
- **`interactive` 自检 + 自愈** — 启动时如果 config.yaml 不存在或 zhihu cookie 没配，会**就地引导**用户走 init / login，不需要中断重来。
- **`python -m scraper` 默认进 interactive** — 没装命令名也能用。
- **浏览器 profile 持久化** — `login zhihu` 把登录态写到 `data/browser-profile/zhihu/`，后续 zhihu scrape 自动复用，登录一次就够。

### 第二批：反爬防御加强

**目标**：在不增加用户使用复杂度的前提下，提升对抗 anti-bot 的能力。

加了 4 层防御，全部对用户透明：

| 层 | 加了什么 | 实现 |
|---|---|---|
| TLS 指纹 | curl_cffi 替换 httpx，TLS 握手 + HTTP/2 帧顺序伪装成真实 Chrome 120–136 | `core/session.py` 用 `AsyncSession(impersonate="chrome…")` |
| HTTP 头 | 加齐 Sec-Fetch-Site / Mode / Dest（现代浏览器必发，缺一个就是 bot 信号） | `core/session.py` base_headers |
| Cookie 身份 | HTTP cookie jar 跨会话持久化 + 浏览器 profile 跨会话持久化 | `data/cookies/` + `data/browser-profile/` |
| 浏览器指纹 | 在原有手写 stealth 上加 `playwright-stealth` 插件 (~20 个额外指纹向量) | `core/browser.py` 双层 stealth |

实测：B 站 + 知乎一次跑完，0 SoftBanned，0 40362（Zhihu 反爬码）。

---

## 5. 一个新用户的典型路径

假设小明从零开始：

```
# 拉代码
git clone <repo> && cd 爬虫工具

# 装
pip install -e .

# 跑
python -m scraper
```

接下来发生什么：

```
=== 爬虫工具 — guided scrape ===
(关键词回车开始爬，空回车或 q 退出，s 修改设置)

[!] config.yaml 不存在。
    现在运行 init 向导? (会创建 config.yaml + 提示登录 Zhihu) [Y/n]: ↵

[init 向导走完，包括安装 playwright + Chromium]

[!] Zhihu cookie 未配置 — 只能爬 Bilibili。
    现在打开浏览器登录 Zhihu? (推荐 — 比手动粘贴 cookie 简单) [Y/n]: ↵

[弹出 Chromium 窗口 → 小明用账号密码登录知乎]

[小明回到终端按 Enter]

[OK] zhihu cookie 已写入 config.yaml (2514 字符)

Posts per platform (帖子数) [10]: ↵
Platforms (both, bili, zhihu) [both]: ↵
Include comments per post? (slower) [Y/n]: ↵
Export results to CSV when done? [Y/n]: ↵

Keyword 关键词 (回车退出 / s 改设置): 电动车

Running: scrape '电动车' count=10 platforms=bili,zhihu comments_pages=1 answers_per_q=5 ...

--- bili: 电动车 ---
2026-05-11 22:46:33 | INFO | bili search: keyword='电动车' page=1 → 24 hits
2026-05-11 22:46:35 | INFO | bili scrape: video=BV1xxxx ok (comments=18)
2026-05-11 22:46:37 | INFO | bili scrape: video=BV1yyyy ok (comments=22)
2026-05-11 22:46:39 | INFO | bili scrape: video=BV1zzzz ok (comments=15)
... (一直输出，不会静默)
2026-05-11 22:48:01 | INFO | bili scrape: keyword='电动车' videos=10 comments=30
  videos=10 comments=30

--- zhihu: 电动车 ---
2026-05-11 22:46:33 | INFO | zhihu search: keyword='电动车' → 10 questions
2026-05-11 22:46:45 | INFO | zhihu scrape: q=536080693 ok (answers=5)
2026-05-11 22:46:58 | INFO | zhihu scrape: q=412345678 ok (answers=5)
...
2026-05-11 22:48:11 | INFO | zhihu scrape: questions=10 answers=50
  questions=10 answers=50 comments=0

  CSV (posts): data/exports/电动车_run13_posts.csv
  CSV (comments): data/exports/电动车_run13_comments.csv

=== Quick preview (volume_by_day) ===
[小明马上看到一张 pandas 表格 — 不用打开 notebook 就能瞄一眼]

Keyword 关键词 (回车退出 / s 改设置): 新能源
[继续下一轮，整个过程小明能一直看到进度...]
```

整个 2 分钟里**一直有滚动的日志输出** — 视频一条条进、评论一批批进，不会让用户对着静默屏幕怀疑卡死。结束后还会自动跑一遍 pandas 预览。

第一次跑就拿到数据。然后：

```
jupyter notebook examples/notebooks/discussion_volume.ipynb
# 改 KEYWORD = "电动车" → Restart & Run All
# 自动出一份带图表的分析报告
```

---

## 6. 已知限制 / 没解决的事

- **知乎单回答评论拉取**：用 BrowserSession 滚动+点击触发懒加载，~5–10% 概率超时（log 里 `lazy-load didn't fire`）。回退策略是只记录 comment_count，跳过具体评论文本。
- **知乎深度分页**：每个问题只能拿到 SSR 内联的 ~5 个回答（initialData blob 里的部分）。要更多就得做 x-zse-96 签名 + 滚动加载，目前没做。
- **代理**：config 支持单个 proxy 字段，但没有代理池轮换。
- **仅 Windows 测试过**：理论上跨平台，但只在 Windows 11 + Python 3.14 上验证过。

---

## 7. 文档分布

按受众分了三档：
- **快速开始.txt** (~70 行) — 2 步入门，超简化
- **使用说明.txt** (~520 行) — 完整手册，按"准备 → 基本 → 分析 → 高级 → FAQ"组织
- **CLAUDE.md** — 给 AI 助手 / 开发者看的架构笔记
- **README.md** — 项目简介，PR 风格

---

## 8. 想听你说什么 — 请你以新用户视角告诉我

请围绕下面这些点给反馈（不需要每条都答，挑你看法最强的几点说就行）：

### 🟡 onboarding 体验
1. 假设你是小明 — 上面第 5 节那个交互流程，**哪一步会让你最困惑或者最容易放弃**？
2. `python -m scraper` 不带任何参数直接进交互模式 — 这个默认行为对新手友好还是反而让人摸不着头脑？
3. 三份文档（快速开始 / 使用说明 / README）的边界清楚吗？还是有重复 / 错位？

### 🟡 心智模型
4. CLI 有 10 个子命令（`bili`, `zhihu`, `scrape`, `interactive`, `init`, `login`, `doctor`, `status`, `view`, `export`），对新用户来说**会不会太多**？哪些可以合并 / 隐藏 / 推到 advanced？
5. "interactive vs scrape" 这两条路径并存（一个交互式循环、一个一次性命令）— 是清晰的二选一还是会让用户分不清"我该用哪个"？

### 🟡 不知不觉的复杂度
6. 反爬层完全透明 — 但万一出问题用户会怎么排查？`doctor` 提供的修复指引够不够？
7. 数据落在 `data/` 下分了 4 个子目录（scraped.db / exports / cookies / browser-profile）— 用户能感知到这些都是"我"的数据吗？还是只关心 CSV？

### 🟡 文档/语言
8. 文档以中文为主，但代码注释、CLAUDE.md 大部分是英文 — 这个混搭对中文用户友好还是反而割裂？
9. 有什么"我以为新手都懂、其实他们不懂"的东西藏在这份概览里？（你作为新用户，看这份文档的时候哪里需要停下来查一下？）

### 🟡 缺什么
10. 一个**绝对的新手**（连命令行都不熟），看完这份文档后最可能问的问题是什么？这些问题在现有文档里能找到答案吗？

---

谢谢！把你的反馈整理成清单回我就行，越具体越好（"X 这里我会卡住，因为 Y"比"这里不清晰"有用得多）。
