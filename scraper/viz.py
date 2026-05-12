"""Plot helpers for quick visual exploration of scraped data.

Lazy-imports matplotlib so the scraper itself doesn't depend on it at import-time.
Each function returns a `matplotlib.figure.Figure`; callers can `.savefig()`, modify
axes, or just display in a notebook.

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
    df = posts if answers.empty else (posts._append(answers) if not posts.empty else answers)
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


def _truncate(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 1] + "…"


__all__ = [
    "volume_chart",
    "top_posts",
    "engagement_distribution",
    "platform_comparison",
    "engagement_growth",
]
