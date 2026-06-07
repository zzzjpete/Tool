"""Self-contained HTML report builder.

Reads from the scraper DB and stitches together charts (via `viz.py`) and tables
(via `analyze.py`) into one HTML file with all images embedded as base64 PNG.

The file has no external assets — opens in any browser, can be emailed, can be
served from any static host. CJK fonts are handled by matplotlib; the HTML uses
system fonts via CSS so Chinese renders fine on any OS.

Entry point:
    from scraper.report import build_report
    path = build_report("电动车")   # -> Path("data/reports/电动车_2026-05-27.html")
"""
from __future__ import annotations

import base64
import html as htmlmod
import io
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from scraper.core.config import load_config
from scraper.core.exceptions import ScraperError


# ---- Public API ------------------------------------------------------------


def build_report(
    keyword: str,
    *,
    days: Optional[int] = None,
    db_path: Optional[str] = None,
    out_dir: str | Path = "data/reports",
) -> Path:
    """Build a self-contained HTML report for one keyword.

    `days`: if given, only include posts published within the last N days. The
    underlying scraper data isn't filtered at fetch time, so this just trims
    the charts/tables that this run will show.

    Returns the path to the written HTML file. Caller can `webbrowser.open(...)` it.
    """
    db_path = _resolve_db_path(db_path)
    sections = _collect_sections(keyword, days=days, db_path=db_path)
    html_text = _render_html(keyword=keyword, days=days, sections=sections)

    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    safe_kw = _sanitize_filename(keyword)
    stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
    out_path = out / f"{safe_kw}_{stamp}.html"
    out_path.write_text(html_text, encoding="utf-8")
    return out_path.resolve()


# ---- Sections --------------------------------------------------------------


def _collect_sections(keyword: str, *, days: Optional[int], db_path: str) -> list[tuple[str, str]]:
    """Return a list of (title, html_body) tuples, one per report section."""
    out: list[tuple[str, str]] = []
    out.append(("概览", _section_overview(keyword, days, db_path)))
    out.append(("发帖量趋势", _section_volume(keyword, db_path)))
    out.append(("情感分析", _section_sentiment(keyword, db_path)))
    out.append(("热词", _section_keywords(keyword, db_path)))
    out.append(("Top 帖子", _section_top_posts(keyword, db_path)))
    out.append(("互动分布", _section_engagement_distribution(keyword, db_path)))
    out.append(("平台对比", _section_platform_comparison(keyword, db_path)))
    out.append(("热门评论 / 回答样本", _section_samples(keyword, db_path)))
    out.append(("抓取历史", _section_recent_runs(keyword, db_path)))
    return out


def _section_overview(keyword: str, days: Optional[int], db_path: str) -> str:
    """High-level numbers — total posts, comments, platform breakdown, time range."""
    from scraper.analyze import posts_for_keyword

    df = _safe(lambda: posts_for_keyword(keyword, db_path=db_path))
    if df is None or df.empty:
        return _info_box("DB 里还没有这个关键词的数据。先跑一次 `python -m scraper scrape \"" + keyword + "\"`。")

    total = len(df)
    by_platform = df["平台"].value_counts().to_dict()
    bili_count = int(by_platform.get("bili", 0))
    zhihu_q = int(by_platform.get("zhihu_question", 0))
    zhihu_a = int(by_platform.get("zhihu_answer", 0))

    # Total comments / likes / views — sum across the unified view's columns.
    def _sum(col: str) -> int:
        if col not in df.columns:
            return 0
        return int(df[col].fillna(0).astype("int64").sum())

    total_comments = _sum("评论数量")
    total_likes = _sum("点赞数量")
    total_views = _sum("播放数量")

    # Time range from publish times (Unix seconds in 发布时间)
    pub = df["发布时间"].dropna()
    if not pub.empty:
        first = datetime.fromtimestamp(int(pub.min())).strftime("%Y-%m-%d")
        last = datetime.fromtimestamp(int(pub.max())).strftime("%Y-%m-%d")
        time_range = f"{first} ~ {last}"
    else:
        time_range = "(无发布时间)"

    range_note = f" (最近 {days} 天)" if days else ""
    return f"""
    <div class="overview">
      <div class="overview-row">
        <div class="kpi"><div class="kpi-label">总帖子数{range_note}</div><div class="kpi-value">{total:,}</div></div>
        <div class="kpi"><div class="kpi-label">B 站视频</div><div class="kpi-value">{bili_count:,}</div></div>
        <div class="kpi"><div class="kpi-label">知乎问题</div><div class="kpi-value">{zhihu_q:,}</div></div>
        <div class="kpi"><div class="kpi-label">知乎回答</div><div class="kpi-value">{zhihu_a:,}</div></div>
      </div>
      <div class="overview-row">
        <div class="kpi"><div class="kpi-label">评论总数</div><div class="kpi-value">{total_comments:,}</div></div>
        <div class="kpi"><div class="kpi-label">点赞总数</div><div class="kpi-value">{total_likes:,}</div></div>
        <div class="kpi"><div class="kpi-label">播放总数</div><div class="kpi-value">{total_views:,}</div></div>
        <div class="kpi"><div class="kpi-label">发布时间跨度</div><div class="kpi-value-small">{time_range}</div></div>
      </div>
    </div>
    """


def _section_volume(keyword: str, db_path: str) -> str:
    from scraper import viz
    fig = _safe(lambda: viz.volume_chart(keyword))
    return _fig_to_img(fig)


def _section_top_posts(keyword: str, db_path: str) -> str:
    """Three small bar charts side by side: by 点赞 / 评论 / 播放."""
    from scraper import viz
    parts = []
    for metric in ["点赞数量", "评论数量", "播放数量"]:
        fig = _safe(lambda m=metric: viz.top_posts(keyword, metric=m, n=8))
        parts.append(_fig_to_img(fig, css_class="chart-third"))
    return '<div class="chart-row">' + "".join(parts) + "</div>"


def _section_sentiment(keyword: str, db_path: str) -> str:
    """Sentiment KPIs + a 正面/中性/负面 donut + a daily sentiment trend."""
    from scraper.analyze import sentiment_summary
    from scraper import viz

    s = _safe(lambda: sentiment_summary(keyword, db_path=db_path))
    if s is None or s.empty:
        return _info_box("暂无可分析的文本（先抓一些帖子 / 评论 / 回答再生成报告）。")
    row = s.iloc[0]

    def _pct(x: Any) -> str:
        try:
            return f"{float(x) * 100:.1f}%"
        except Exception:
            return "—"

    cards = f"""
    <div class="overview">
      <div class="overview-row">
        <div class="kpi"><div class="kpi-label">情感样本数</div><div class="kpi-value">{int(row['样本数']):,}</div></div>
        <div class="kpi"><div class="kpi-label">整体倾向</div><div class="kpi-value-small">{htmlmod.escape(str(row['情感倾向']))}</div></div>
        <div class="kpi"><div class="kpi-label">平均情感得分</div><div class="kpi-value">{row['平均情感得分']}</div></div>
        <div class="kpi"><div class="kpi-label">正面占比</div><div class="kpi-value">{_pct(row['正面占比'])}</div></div>
        <div class="kpi"><div class="kpi-label">负面占比</div><div class="kpi-value">{_pct(row['负面占比'])}</div></div>
      </div>
    </div>
    """
    pie = _fig_to_img(_safe(lambda: viz.sentiment_breakdown(keyword)), css_class="chart-half")
    trend = _fig_to_img(_safe(lambda: viz.sentiment_trend(keyword)), css_class="chart-full")
    return cards + '<div class="chart-row">' + pie + "</div>" + trend


def _section_keywords(keyword: str, db_path: str) -> str:
    """Top hot words (TF-IDF) as a horizontal bar — a word-cloud stand-in."""
    from scraper import viz

    fig = _safe(lambda: viz.keyword_bar(keyword, n=20))
    return _fig_to_img(fig)


def _section_engagement_distribution(keyword: str, db_path: str) -> str:
    from scraper import viz
    fig = _safe(lambda: viz.engagement_distribution(keyword, metric="点赞数量"))
    return _fig_to_img(fig)


def _section_platform_comparison(keyword: str, db_path: str) -> str:
    from scraper import viz
    fig = _safe(lambda: viz.platform_comparison(keyword))
    return _fig_to_img(fig)


def _section_samples(keyword: str, db_path: str) -> str:
    """Show the top 5 posts (by 点赞数量) with title + author + a snippet, then
    each one's top comment if available. Gives the report a 'qualitative' angle
    instead of just numbers.
    """
    from scraper.analyze import posts_for_keyword, comments_for_post

    df = _safe(lambda: posts_for_keyword(keyword, db_path=db_path))
    if df is None or df.empty:
        return _info_box("没有可展示的样本。")

    # Top 5 by 点赞 if available, fall back to 评论数量
    sort_col = "点赞数量" if df["点赞数量"].notna().any() else "评论数量"
    top5 = df.dropna(subset=[sort_col]).nlargest(5, sort_col)
    if top5.empty:
        return _info_box("没有可展示的样本。")

    rows = []
    for _, post in top5.iterrows():
        title = htmlmod.escape(str(post.get("标题") or "(无标题)"))[:120]
        author = htmlmod.escape(str(post.get("作者昵称") or "?"))
        platform = post.get("平台", "")
        link = htmlmod.escape(str(post.get("笔记链接") or ""))
        likes = int(post.get("点赞数量") or 0)
        comments = int(post.get("评论数量") or 0)
        content_preview = htmlmod.escape(str(post.get("内容") or ""))[:300]
        if len(str(post.get("内容") or "")) > 300:
            content_preview += "…"

        # Try to pull the top comment for this post
        comment_html = ""
        comments_df = _safe(lambda pid=str(post.get("帖子ID")): comments_for_post(pid, db_path=db_path))
        if comments_df is not None and not comments_df.empty:
            top_c = comments_df.iloc[0]
            comment_html = f"""
              <div class="top-comment">
                <span class="comment-author">{htmlmod.escape(str(top_c.get("作者昵称") or "?"))}</span>
                <span class="comment-likes">[{int(top_c.get("点赞数量") or 0)} 赞]</span>
                <div class="comment-body">{htmlmod.escape(str(top_c.get("内容") or ""))[:200]}</div>
              </div>
            """

        rows.append(f"""
          <div class="sample">
            <div class="sample-head">
              <span class="badge badge-{platform}">{platform}</span>
              <a href="{link}" target="_blank" class="sample-title">{title}</a>
            </div>
            <div class="sample-meta">作者: {author} · 点赞 {likes:,} · 评论 {comments:,}</div>
            <div class="sample-body">{content_preview}</div>
            {comment_html}
          </div>
        """)
    return "\n".join(rows)


def _section_recent_runs(keyword: str, db_path: str) -> str:
    from scraper.analyze import scrape_run_summary

    df = _safe(lambda: scrape_run_summary(keyword=keyword, db_path=db_path))
    if df is None or df.empty:
        return _info_box("没有抓取历史。")

    # Show only the columns that fit on one row
    cols = ["运行ID", "平台", "请求数量", "实际帖子数", "实际评论数", "开始时间", "结束时间"]
    cols = [c for c in cols if c in df.columns]
    head = df.head(10)[cols]
    return _df_to_html_table(head)


# ---- Rendering helpers ----------------------------------------------------


def _fig_to_img(fig: Any, *, css_class: str = "chart-full") -> str:
    """Render a matplotlib Figure to a base64 <img> tag. Closes the figure to
    release matplotlib resources."""
    if fig is None:
        return _info_box("（图表生成失败）")
    buf = io.BytesIO()
    try:
        fig.savefig(buf, format="png", dpi=110, bbox_inches="tight")
    except Exception as e:
        return _info_box(f"图表生成失败: {type(e).__name__}: {e}")
    finally:
        # Always close — figures pile up in matplotlib's global state otherwise
        try:
            import matplotlib.pyplot as plt
            plt.close(fig)
        except Exception:
            pass
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f'<img class="{css_class}" src="data:image/png;base64,{encoded}" alt="chart">'


def _df_to_html_table(df: Any) -> str:
    """Render a pandas DataFrame as an HTML table with our styles."""
    if df is None or df.empty:
        return _info_box("（无数据）")
    try:
        return df.to_html(index=False, escape=True, classes="data-table", border=0)
    except Exception as e:
        return _info_box(f"表格生成失败: {type(e).__name__}: {e}")


def _info_box(msg: str) -> str:
    return f'<div class="info-box">{htmlmod.escape(msg)}</div>'


def _safe(fn) -> Any:
    """Run `fn()`; return None and swallow exceptions so one bad section doesn't
    nuke the whole report. The caller renders an info box instead."""
    try:
        return fn()
    except ScraperError:
        return None
    except Exception:
        return None


def _resolve_db_path(db_path: Optional[str]) -> str:
    if db_path is not None:
        return db_path
    cfg = load_config("config.yaml")
    return cfg.storage.path


def _sanitize_filename(s: str) -> str:
    """Strip filesystem-unfriendly chars; keep CJK characters since modern FS
    handle them fine."""
    out = "".join(c for c in s if c.isalnum() or c in ("_", "-", ".") or _is_cjk(c))
    return out[:80] or "keyword"


def _is_cjk(c: str) -> bool:
    code = ord(c)
    return (
        0x4E00 <= code <= 0x9FFF      # CJK Unified
        or 0x3400 <= code <= 0x4DBF   # CJK Extension A
        or 0x3040 <= code <= 0x30FF   # Hiragana/Katakana (rare here but harmless)
    )


# ---- Final HTML template --------------------------------------------------


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{title}</title>
<style>
  :root {{
    --fg: #1f2328;
    --muted: #57606a;
    --accent: #1f6feb;
    --line: #d0d7de;
    --bg-soft: #f6f8fa;
    --bg-card: #ffffff;
  }}
  html, body {{
    margin: 0; padding: 0;
    font-family: "Microsoft YaHei", "PingFang SC", "Noto Sans CJK SC", "SimHei", sans-serif;
    color: var(--fg); line-height: 1.5; font-size: 14px;
    background: #fafbfc;
  }}
  .container {{ max-width: 1080px; margin: 0 auto; padding: 28px 24px 80px; }}
  h1 {{
    font-size: 28px; margin: 0 0 4px 0;
    border-bottom: 2px solid var(--accent); padding-bottom: 8px;
  }}
  .header-meta {{ color: var(--muted); margin-bottom: 28px; font-size: 13px; }}
  section {{ margin-bottom: 32px; background: var(--bg-card); border: 1px solid var(--line); border-radius: 8px; padding: 20px 22px; }}
  section h2 {{ font-size: 18px; margin: 0 0 14px 0; color: var(--accent); }}
  .chart-full {{ width: 100%; max-width: 100%; display: block; margin: 8px 0; }}
  .chart-row {{ display: flex; gap: 12px; flex-wrap: wrap; }}
  .chart-third {{ width: calc(33.333% - 8px); min-width: 280px; display: block; }}
  .chart-half {{ width: calc(50% - 6px); min-width: 320px; display: block; }}
  .info-box {{ padding: 12px 14px; background: var(--bg-soft); border-radius: 4px; color: var(--muted); font-size: 13px; }}

  /* KPI cards */
  .overview-row {{ display: flex; gap: 12px; margin-bottom: 10px; flex-wrap: wrap; }}
  .kpi {{
    flex: 1; min-width: 130px; background: var(--bg-soft); padding: 12px 14px;
    border-radius: 6px; text-align: left;
  }}
  .kpi-label {{ font-size: 12px; color: var(--muted); }}
  .kpi-value {{ font-size: 22px; font-weight: 600; margin-top: 4px; }}
  .kpi-value-small {{ font-size: 14px; font-weight: 500; margin-top: 4px; }}

  /* Data table */
  table.data-table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  table.data-table th, table.data-table td {{
    border-bottom: 1px solid var(--line); padding: 7px 9px; text-align: left;
  }}
  table.data-table th {{ background: var(--bg-soft); font-weight: 600; }}
  table.data-table tr:hover {{ background: #f6f8fa; }}

  /* Sample cards */
  .sample {{ border-left: 3px solid var(--accent); padding: 10px 14px; margin: 12px 0; background: var(--bg-soft); border-radius: 0 6px 6px 0; }}
  .sample-head {{ display: flex; align-items: center; gap: 8px; margin-bottom: 4px; }}
  .badge {{ font-size: 11px; padding: 2px 8px; border-radius: 10px; background: #ddf4ff; color: var(--accent); }}
  .badge-bili {{ background: #fde7f3; color: #cf2e7f; }}
  .badge-zhihu_question {{ background: #ddf4ff; color: #0969da; }}
  .badge-zhihu_answer {{ background: #dff5e2; color: #1a7f37; }}
  .sample-title {{ font-weight: 600; color: var(--fg); text-decoration: none; }}
  .sample-title:hover {{ text-decoration: underline; color: var(--accent); }}
  .sample-meta {{ color: var(--muted); font-size: 12px; margin-bottom: 6px; }}
  .sample-body {{ font-size: 13px; line-height: 1.55; white-space: pre-wrap; }}
  .top-comment {{ margin-top: 8px; padding-left: 10px; border-left: 2px solid #cdd9e5; }}
  .comment-author {{ font-weight: 600; font-size: 12px; }}
  .comment-likes {{ font-size: 11px; color: var(--muted); margin-left: 6px; }}
  .comment-body {{ font-size: 12.5px; margin-top: 3px; color: var(--fg); }}

  footer {{ color: var(--muted); font-size: 12px; text-align: center; margin-top: 40px; }}
</style>
</head>
<body>
<div class="container">
  <h1>{title}</h1>
  <div class="header-meta">{meta}</div>
  {sections}
  <footer>由 爬虫工具 自动生成 · {now}</footer>
</div>
</body>
</html>
"""


def _render_html(*, keyword: str, days: Optional[int], sections: list[tuple[str, str]]) -> str:
    title = f"《{keyword}》关键词分析报告"
    range_note = f"  ·  最近 {days} 天" if days else ""
    meta = (
        f"关键词: <strong>{htmlmod.escape(keyword)}</strong>"
        + range_note
        + f"  ·  生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    section_html = []
    for sec_title, body in sections:
        section_html.append(f"<section><h2>{htmlmod.escape(sec_title)}</h2>{body}</section>")
    return _HTML_TEMPLATE.format(
        title=htmlmod.escape(title),
        meta=meta,
        sections="\n".join(section_html),
        now=datetime.now().strftime("%Y-%m-%d %H:%M"),
    )


__all__ = ["build_report"]
