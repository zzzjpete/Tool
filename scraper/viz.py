"""Plot helpers for quick visual exploration of scraped data.

Lazy-imports matplotlib so the scraper itself doesn't depend on it at import-time.
Each function returns a `matplotlib.figure.Figure`; callers can `.savefig()`, modify
axes, or just display in a notebook. Per matplotlib convention these figures are
returned OPEN — the caller owns closing them (`plt.close(fig)`) to free memory.
`report.py`'s `_fig_to_img` does this after embedding; notebook users should too
when generating many. Functions close their own figure if they raise mid-build.

Chinese characters won't render on the default DejaVu Sans font — `_configure_fonts()`
prepends common Chinese fonts (Microsoft YaHei on Windows, PingFang on macOS, Noto
Sans CJK on Linux). Called once on first `_pyplot()` use.
"""

from __future__ import annotations

from typing import Any, Optional

from scraper.analyze import (
    posts_for_keyword,
    answers_for_keyword,
    volume_by_day,
    engagement_history,
    comments_for_post,
)
from scraper.core.exceptions import ScraperError


_FONTS_CONFIGURED = False

# Consistent sentiment palette reused across charts.
_SENTIMENT_COLORS = {"正面": "#2ca02c", "中性": "#b0b0b0", "负面": "#d62728"}
_SENTIMENT_ORDER = ["正面", "中性", "负面"]


def _combine(posts: Any, answers: Any) -> Any:
    """Concat two same-shape DataFrames (posts + zhihu answers), dropping empties.

    Replaces an earlier `DataFrame._append` call (deprecated, slated for removal in
    a future pandas). Both frames come from `posts_unified` so columns align.
    """
    import warnings
    import pandas as pd

    frames = [d for d in (posts, answers) if d is not None and not d.empty]
    if not frames:
        return posts if posts is not None else answers
    if len(frames) == 1:
        return frames[0]
    # pandas 2.x warns that a future concat won't special-case all-NA columns when
    # inferring dtypes (a zhihu-answers frame has all-NULL 播放数量, etc.). Both
    # frames share the posts_unified schema and we only read columns downstream, so
    # the future behavior is fine here — silence the advisory noise.
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", FutureWarning)
        return pd.concat(frames, ignore_index=True)


def _pyplot() -> Any:
    """Lazy import + Chinese-font setup. Returns matplotlib.pyplot."""
    try:
        import matplotlib  # noqa: F401
        import matplotlib.pyplot as plt
    except ImportError as e:
        raise ScraperError("matplotlib is not installed. Run: pip install matplotlib") from e
    global _FONTS_CONFIGURED
    if not _FONTS_CONFIGURED:
        # Prepend CJK fonts so Chinese labels/titles render. Falls back to default
        # if none are installed — labels will show as boxes (tofu) but the chart
        # still produces.
        from matplotlib import rcParams
        rcParams["font.sans-serif"] = [
            "Microsoft YaHei",  # Windows
            "SimHei",           # Windows (older)
            "PingFang SC",      # macOS
            "Hiragino Sans GB", # macOS (older)
            "Noto Sans CJK SC", # Linux
            "WenQuanYi Micro Hei",  # Linux (older)
        ] + rcParams["font.sans-serif"]
        rcParams["axes.unicode_minus"] = False  # use ASCII minus, not the unicode glyph
        _FONTS_CONFIGURED = True
    return plt


def volume_chart(keyword: str, *, figsize: tuple[int, int] = (10, 4)) -> Any:
    """Posts-per-day stacked bar by platform, for one keyword.

    Use to see when a topic spiked vs. trickled in. X-axis is the post's publish
    date (发布时间), not the scrape date.
    """
    plt = _pyplot()
    df = volume_by_day(keyword)
    fig, ax = plt.subplots(figsize=figsize)
    if df.empty:
        ax.text(0.5, 0.5, f"No data for {keyword!r}", ha="center", va="center")
        ax.set_axis_off()
        return fig
    pivot = df.pivot_table(
        index="日期", columns="平台", values="帖子数", aggfunc="sum", fill_value=0,
    )
    pivot.plot(kind="bar", stacked=True, ax=ax, width=0.85)
    ax.set_title(f"《{keyword}》发帖数量（按平台分组）")
    ax.set_xlabel("发布日期")
    ax.set_ylabel("帖子数")
    ax.tick_params(axis="x", rotation=45)
    fig.tight_layout()
    return fig


def top_posts(
    keyword: str,
    *,
    metric: str = "评论数量",
    n: int = 10,
    figsize: tuple[int, int] = (10, 5),
) -> Any:
    """Horizontal bar chart of top N posts by `metric`.

    `metric` is a column name from `posts_unified`: 评论数量, 点赞数量, 收藏数量,
    转发数量, 播放数量.
    """
    plt = _pyplot()
    df = posts_for_keyword(keyword)
    fig, ax = plt.subplots(figsize=figsize)
    if df.empty or metric not in df.columns:
        ax.text(0.5, 0.5, f"No data / unknown metric {metric!r}", ha="center", va="center")
        ax.set_axis_off()
        return fig
    top = df.dropna(subset=[metric]).nlargest(n, metric)
    # Use title if present, fall back to content excerpt for answers
    labels = top["标题"].fillna(top["内容"].str.slice(0, 40)).fillna("(no title)")
    labels = [_truncate(s, 50) for s in labels]
    ax.barh(range(len(top)), top[metric].astype(float), color="#4c72b0")
    ax.set_yticks(range(len(top)))
    ax.set_yticklabels(labels)
    ax.invert_yaxis()
    ax.set_xlabel(metric)
    ax.set_title(f"《{keyword}》 — 按{metric}排名前{n}")
    fig.tight_layout()
    return fig


def engagement_distribution(
    keyword: str,
    *,
    metric: str = "点赞数量",
    figsize: tuple[int, int] = (8, 4),
) -> Any:
    """Histogram of `metric` across all posts for `keyword`. Log-x so the long tail
    is visible — most posts have low engagement, a handful are viral.
    """
    plt = _pyplot()
    import numpy as np

    # Combine questions and the answers underneath for a fuller distribution
    posts = posts_for_keyword(keyword)
    answers = answers_for_keyword(keyword)
    df = _combine(posts, answers)
    fig, ax = plt.subplots(figsize=figsize)
    if df.empty or metric not in df.columns:
        ax.text(0.5, 0.5, f"No data for {keyword!r}", ha="center", va="center")
        ax.set_axis_off()
        return fig
    values = df[metric].dropna().astype(float)
    values = values[values > 0]
    if values.empty:
        ax.text(0.5, 0.5, "All zero / null", ha="center", va="center")
        ax.set_axis_off()
        return fig
    bins = np.logspace(np.log10(values.min()), np.log10(values.max() + 1), 30)
    ax.hist(values, bins=bins, color="#dd8452", edgecolor="white")
    ax.set_xscale("log")
    ax.set_xlabel(f"{metric}（对数尺度）")
    ax.set_ylabel("帖子数")
    ax.set_title(f"《{keyword}》 — {metric} 分布")
    fig.tight_layout()
    return fig


def platform_comparison(
    keyword: str,
    *,
    figsize: tuple[int, int] = (10, 4),
) -> Any:
    """Side-by-side bili vs. zhihu totals across all engagement metrics for `keyword`."""
    plt = _pyplot()
    df = posts_for_keyword(keyword)
    fig, ax = plt.subplots(figsize=figsize)
    if df.empty:
        ax.text(0.5, 0.5, f"No data for {keyword!r}", ha="center", va="center")
        ax.set_axis_off()
        return fig
    # Roll up zhihu_question + zhihu_answer into one "zhihu" group for comparison.
    df = df.copy()
    df["平台"] = df["平台"].replace({"zhihu_question": "zhihu", "zhihu_answer": "zhihu"})
    metrics = ["评论数量", "点赞数量", "收藏数量", "播放数量"]
    avail = [m for m in metrics if m in df.columns]
    agg = df.groupby("平台")[avail].sum(numeric_only=True)
    agg.T.plot(kind="bar", ax=ax, width=0.7)
    ax.set_title(f"《{keyword}》 — 平台总量对比")
    ax.set_xlabel("指标")
    ax.set_ylabel("数量")
    ax.set_yscale("log")
    ax.tick_params(axis="x", rotation=0)
    fig.tight_layout()
    return fig


def engagement_growth(post_id: str, *, figsize: tuple[int, int] = (8, 4)) -> Any:
    """Time-series of one post's engagement metrics across re-scrapes.

    Only meaningful after the same post has been re-scraped at least twice — i.e.,
    you've been running daily scrapes for a while. Single-snapshot posts plot as a
    single dot.
    """
    plt = _pyplot()
    df = engagement_history(post_id)
    fig, ax = plt.subplots(figsize=figsize)
    if df.empty:
        ax.text(0.5, 0.5, f"No snapshots for {post_id}", ha="center", va="center")
        ax.set_axis_off()
        return fig
    import pandas as pd
    df = df.copy()
    df["时间"] = pd.to_datetime(df["时间"])
    for col, color in [
        ("播放数量", "#4c72b0"),
        ("点赞数量", "#dd8452"),
        ("评论数量", "#55a868"),
    ]:
        if col in df.columns and df[col].notna().any():
            ax.plot(df["时间"], df[col], marker="o", label=col, color=color)
    ax.set_title(f"{post_id} — 互动增长")
    ax.set_xlabel("时间")
    ax.set_yscale("log")
    ax.legend()
    fig.autofmt_xdate()
    fig.tight_layout()
    return fig


def sentiment_breakdown(keyword: str, *, figsize: tuple[int, int] = (6, 6)) -> Any:
    """Donut chart of 正面 / 中性 / 负面 share for one keyword's discussion."""
    plt = _pyplot()
    from scraper.analyze import sentiment_for_keyword

    df = sentiment_for_keyword(keyword)
    fig, ax = plt.subplots(figsize=figsize)
    # Close the figure if anything below raises, so a mid-plot error (caught by
    # report.py's _safe) doesn't leak it into matplotlib's global registry.
    try:
        if df.empty:
            ax.text(0.5, 0.5, f"No data for {keyword!r}", ha="center", va="center")
            ax.set_axis_off()
            return fig
        counts = df["情感标签"].value_counts()
        labels, sizes, colors = [], [], []
        for k in _SENTIMENT_ORDER:
            v = int(counts.get(k, 0))
            if v > 0:
                labels.append(f"{k} {v}")
                sizes.append(v)
                colors.append(_SENTIMENT_COLORS[k])
        if not sizes:
            ax.text(0.5, 0.5, "无情感样本", ha="center", va="center")
            ax.set_axis_off()
            return fig
        ax.pie(
            sizes, labels=labels, colors=colors, autopct="%1.1f%%", startangle=90,
            pctdistance=0.78, wedgeprops=dict(width=0.42, edgecolor="white"),
        )
        ax.set_title(f"《{keyword}》情感分布（n={len(df)}）")
        fig.tight_layout()
        return fig
    except Exception:
        plt.close(fig)
        raise


def sentiment_trend(keyword: str, *, figsize: tuple[int, int] = (10, 4.5)) -> Any:
    """Stacked daily 正面/中性/负面 counts (bars) + mean-score line on a twin axis."""
    plt = _pyplot()
    import numpy as np
    from scraper.analyze import sentiment_by_day

    df = sentiment_by_day(keyword)
    fig, ax = plt.subplots(figsize=figsize)
    try:
        if df.empty:
            ax.text(0.5, 0.5, f"No data for {keyword!r}", ha="center", va="center")
            ax.set_axis_off()
            return fig
        pos = np.arange(len(df))
        p, m, n = df["正面"].values, df["中性"].values, df["负面"].values
        ax.bar(pos, p, color=_SENTIMENT_COLORS["正面"], label="正面")
        ax.bar(pos, m, bottom=p, color=_SENTIMENT_COLORS["中性"], label="中性")
        ax.bar(pos, n, bottom=p + m, color=_SENTIMENT_COLORS["负面"], label="负面")
        ax.set_xticks(pos)
        ax.set_xticklabels(df["日期"], rotation=45, ha="right")
        ax.set_xlabel("发布日期")
        ax.set_ylabel("数量")

        ax2 = ax.twinx()
        ax2.plot(pos, df["平均情感得分"].values, color="#1f6feb", marker="o", label="平均情感得分")
        ax2.axhline(0, color="#888", linewidth=0.8, linestyle="--")
        ax2.set_ylabel("平均情感得分")
        ax2.set_ylim(-1, 1)

        # Merge the two legends into one box.
        h1, l1 = ax.get_legend_handles_labels()
        h2, l2 = ax2.get_legend_handles_labels()
        ax.legend(h1 + h2, l1 + l2, loc="upper left", fontsize=9)
        ax.set_title(f"《{keyword}》情感趋势（按发布日期）")
        fig.tight_layout()
        return fig
    except Exception:
        plt.close(fig)
        raise


def keyword_bar(keyword: str, *, n: int = 20, figsize: tuple[int, int] = (8, 6)) -> Any:
    """Horizontal bar of the top-N TF-IDF hot words for a keyword's discussion.

    A dependency-free stand-in for a word cloud (wordcloud isn't a project dep).
    """
    plt = _pyplot()
    from scraper.analyze import top_keywords

    df = top_keywords(keyword, topK=n)
    fig, ax = plt.subplots(figsize=figsize)
    try:
        if df.empty:
            ax.text(0.5, 0.5, f"No keywords for {keyword!r}", ha="center", va="center")
            ax.set_axis_off()
            return fig
        df = df.iloc[::-1]  # reverse so the highest weight lands at the top
        ax.barh(range(len(df)), df["权重"].astype(float), color="#8172b3")
        ax.set_yticks(range(len(df)))
        ax.set_yticklabels(df["词"])
        ax.set_xlabel("TF-IDF 权重")
        ax.set_title(f"《{keyword}》热词 Top {len(df)}")
        fig.tight_layout()
        return fig
    except Exception:
        plt.close(fig)
        raise


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


__all__ = [
    "volume_chart",
    "top_posts",
    "engagement_distribution",
    "platform_comparison",
    "engagement_growth",
    "sentiment_breakdown",
    "sentiment_trend",
    "keyword_bar",
]
