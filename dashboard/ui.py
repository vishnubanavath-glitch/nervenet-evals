"""Shared look and small components for the dashboard."""
from __future__ import annotations

import html
import re

import altair as alt
import pandas as pd
import streamlit as st

# Palette: #000814 page · #14213D panels · #000000 sidebar/bubbles · #FCA311 accent · #E5E5E5 text.
BG = "#000814"
PANEL = "#14213D"
BLACK = "#000000"
BORDER = "#22345F"
ACCENT = "#FCA311"
TEXT = "#E5E5E5"
MUTED = "#8D99AE"
PASS_COLOR = "#34D399"
FAIL_COLOR = "#F87171"
WARN_COLOR = ACCENT
METRIC_COLORS = [ACCENT, "#4EA8DE", PASS_COLOR, "#A78BFA", "#F4A261", "#90E0EF"]

CSS = f"""
<style>
.stApp {{background:{BG};}}
.block-container {{padding-top: 1.6rem; max-width: 1400px;}}
section[data-testid="stSidebar"], div[data-testid="stSidebarCollapsedControl"] {{display:none;}}
header[data-testid="stHeader"] {{background:{BLACK}; border-bottom:1px solid {BORDER};}}
.stAppDeployButton {{display:none;}}
.block-container {{padding-top: 4.2rem !important;}}
.nv-head {{margin:0.2rem 0 1.1rem 0; padding-bottom:0.9rem; border-bottom:1px solid {BORDER};}}
.nv-head h1 {{font-size:1.85rem; font-weight:700; margin:0; padding:0; color:{TEXT};}}
.nv-head h1 span {{color:{ACCENT};}}
.nv-head p {{margin:0.25rem 0 0 0; color:{MUTED}; font-size:0.95rem;}}
.nv-section {{font-size:1.05rem; font-weight:650; color:{TEXT}; margin:1.3rem 0 0.6rem 0;
  padding-left:10px; border-left:3px solid {ACCENT};}}
h1, h2, h3, h4 {{color:{TEXT};}}
div[data-testid="stVerticalBlockBorderWrapper"] {{background:{PANEL}; border-color:{BORDER} !important;}}
div[data-testid="stExpander"] details {{background:{PANEL}; border:1px solid {BORDER}; border-radius:10px;}}
button[data-baseweb="tab"] p {{font-size:0.95rem;}}
button[data-baseweb="tab"][aria-selected="true"] p {{color:{ACCENT};}}
div[data-testid="stMetricValue"] {{color:{ACCENT};}}
.nv-badge {{display:inline-block; padding:2px 10px; border-radius:6px; font-size:0.78rem; font-weight:600;}}
.nv-pass {{background:rgba(52,211,153,.14); color:{PASS_COLOR}; border:1px solid rgba(52,211,153,.4);}}
.nv-fail {{background:rgba(248,113,113,.14); color:{FAIL_COLOR}; border:1px solid rgba(248,113,113,.4);}}
.nv-warn {{background:rgba(252,163,17,.14); color:{ACCENT}; border:1px solid rgba(252,163,17,.45);}}
.nv-mute {{background:rgba(141,153,174,.12); color:{MUTED}; border:1px solid rgba(141,153,174,.35);}}
.nv-card {{background:linear-gradient(160deg, {PANEL} 0%, #0B1630 100%); border:1px solid {BORDER};
  border-radius:14px; padding:16px 18px; height:100%; box-shadow:0 4px 18px rgba(0,0,0,.35);
  transition:border-color .15s ease, transform .15s ease;}}
.nv-card:hover {{border-color:{ACCENT}; transform:translateY(-1px);}}
.nv-card h4 {{margin:0 0 4px 0; font-size:0.9rem; color:{TEXT}; font-weight:600;}}
.nv-card .big {{font-size:1.7rem; font-weight:700; color:{ACCENT}; line-height:1.2;}}
.nv-card .big.bad {{color:{FAIL_COLOR};}}
.nv-card .sub {{font-size:0.8rem; color:{MUTED};}}
.nv-meta {{color:{MUTED}; font-size:0.85rem;}}
.nv-meta b {{color:{TEXT};}}
.nv-bubble {{border-radius:12px; padding:10px 14px; margin:6px 0 2px 0; border:1px solid {BORDER};}}
.nv-user {{background:{PANEL}; margin-right:25%;}}
.nv-assistant {{background:{BLACK}; margin-left:8%;}}
.nv-role {{font-size:0.75rem; color:{MUTED}; font-weight:600; margin-bottom:8px;}}
.nv-role.right {{text-align:right;}}
.nv-list li {{margin-bottom:4px;}}
</style>
"""


def page_header(title: str, subtitle: str = "", accent: str = "") -> None:
    acc = f" <span>{html.escape(accent)}</span>" if accent else ""
    sub = f"<p>{html.escape(subtitle)}</p>" if subtitle else ""
    st.markdown(f'<div class="nv-head"><h1>{html.escape(title)}{acc}</h1>{sub}</div>', unsafe_allow_html=True)


def section(title: str) -> None:
    st.markdown(f'<div class="nv-section">{html.escape(title)}</div>', unsafe_allow_html=True)


def inject_css() -> None:
    st.markdown(CSS, unsafe_allow_html=True)


def badge(text: str, kind: str = "mute") -> str:
    cls = {"pass": "nv-pass", "fail": "nv-fail", "warn": "nv-warn"}.get(kind, "nv-mute")
    return f'<span class="nv-badge {cls}">{html.escape(text)}</span>'


def status_badge(status: str) -> str:
    kind = {"Passed": "pass", "PASS": "pass", "Success": "pass", "Failed": "fail", "FAIL": "fail",
            "Errored": "fail", "ERROR": "fail", "SKIP": "mute", "Skipped": "mute"}.get(status, "mute")
    return badge(status, kind)


def card(title: str, big: str, sub: str = "", bad: bool = False) -> None:
    st.markdown(f'<div class="nv-card"><h4>{html.escape(title)}</h4>'
                f'<div class="big{" bad" if bad else ""}">{html.escape(big)}</div>'
                f'<div class="sub">{html.escape(sub)}</div></div>', unsafe_allow_html=True)


def meta_line(items: list[tuple[str, str]]) -> None:
    st.markdown('<div class="nv-meta">' + " &nbsp;·&nbsp; ".join(
        f"{html.escape(k)} <b>{html.escape(v)}</b>" for k, v in items) + "</div>", unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Charts (modelled on the evaluation-platform screenshots)
# ---------------------------------------------------------------------------

METRIC_COLOR = {
    "Correctness": ACCENT,
    "Answer Relevancy": "#4EA8DE",
    "Faithfulness": "#2EC4B6",
    "Knowledge Retention": "#A35BFF",
    "Conversation Completeness": "#FFE01A",
    "Column description accuracy": "#4EA8DE",
    "Table context accuracy": ACCENT,
}
GRID = "#1B2A4D"
_BINS = [f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)]


def metric_color(name: str, i: int = 0) -> str:
    return METRIC_COLOR.get(name, METRIC_COLORS[i % len(METRIC_COLORS)])


def _style(chart):
    # configure() replaces the whole config, so it must come before the configure_* calls.
    return (chart.configure(background="transparent")
            .configure_view(stroke=None)
            .configure_axis(labelColor=MUTED, titleColor=MUTED, gridColor=GRID, domainColor=BORDER,
                            tickColor=BORDER, labelFontSize=11, labelPadding=6)
            .configure_legend(labelColor=TEXT, symbolType="circle", labelFontSize=12))


def caption_label(text: str) -> None:
    """Small upper-case panel label, e.g. TEST CASE SCORES."""
    st.markdown(f"<div style='font-size:0.75rem;letter-spacing:.06em;color:{MUTED};font-weight:600;"
                f"text-transform:uppercase;margin-bottom:4px'>{html.escape(text)}</div>", unsafe_allow_html=True)


def gauge(pct: float, caption: str) -> None:
    """Half-circle pass-rate gauge with the percentage in the middle."""
    import math
    pct = max(0.0, min(100.0, pct))
    color = PASS_COLOR if pct >= 80 else WARN_COLOR if pct >= 50 else FAIL_COLOR
    w, h = 240, 150
    cx, cy = w / 2, h - 18
    track = pd.DataFrame([{"t0": -math.pi / 2, "t1": math.pi / 2}])
    value = pd.DataFrame([{"t0": -math.pi / 2, "t1": -math.pi / 2 + math.pi * pct / 100}])

    def arc(df, col):
        return alt.Chart(df).mark_arc(innerRadius=86, outerRadius=100, cornerRadius=8).encode(
            theta=alt.Theta("t0:Q", scale=None), theta2="t1:Q", x=alt.value(cx), y=alt.value(cy),
            color=alt.value(col))

    text = alt.Chart(pd.DataFrame({"t": [f"{pct:.0f}%"]})).mark_text(
        fontSize=34, fontWeight="bold", color=color, baseline="bottom").encode(
        text="t:N", x=alt.value(cx), y=alt.value(cy - 6))
    layers = [arc(track, BORDER)] + ([arc(value, color)] if pct > 0 else []) + [text]
    st.altair_chart(_style(alt.layer(*layers).properties(width=w, height=h)), width="content")
    st.markdown(f"<div style='text-align:center;width:{w}px;color:{MUTED};font-size:0.85rem;margin-top:-8px'>"
                f"{html.escape(caption)}</div>", unsafe_allow_html=True)


def score_strip(points: list[tuple[str, float, bool]]) -> None:
    """TEST CASE SCORES: one dot per test case on a 0-1 line, Passing / Failing legend at the top right."""
    if not points:
        st.caption("No judge scores in this run.")
        return
    df = pd.DataFrame(points, columns=["case", "score", "passed"])
    df["status"] = df["passed"].map({True: "Passing", False: "Failing"})
    mid = 52
    scale = alt.Scale(domain=[0, 1], nice=False)
    axis = alt.Axis(values=[i / 10 for i in range(11)], format="~g", grid=False, domain=False, ticks=False,
                    labelPadding=10)
    line = alt.Chart(pd.DataFrame({"a": [0.0], "b": [1.0]})).mark_rule(color=BORDER, strokeWidth=1).encode(
        x=alt.X("a:Q", scale=scale, axis=axis, title=None), x2="b:Q", y=alt.value(mid))
    dots = alt.Chart(df).mark_circle(size=95, opacity=0.95).encode(
        x=alt.X("score:Q", scale=scale, title=None, axis=axis),
        y=alt.value(mid),
        color=alt.Color("status:N", scale=alt.Scale(domain=["Passing", "Failing"], range=[PASS_COLOR, FAIL_COLOR]),
                        legend=alt.Legend(orient="top-right", direction="horizontal", title=None)),
        tooltip=["case", alt.Tooltip("score:Q", format=".2f"), "status"])
    st.altair_chart(_style(alt.layer(line, dots).properties(height=66)), width="stretch")


def metrics_panel(stats: list[dict], key: str, per_page: int = 2) -> None:
    """FAILING METRICS: two metrics at a time (name, average score, passed / failed) with a pager."""
    if not stats:
        st.caption("This run was not judged (code checks only).")
        return
    stats = sorted(stats, key=lambda s: (s["failed"] == 0, s["avg"]))  # failing ones first
    pages = (len(stats) + per_page - 1) // per_page
    page = min(st.session_state.get(f"mp_{key}", 0), pages - 1)
    head, prev, pos, nxt = st.columns([6, 1, 1.2, 1])
    with head:
        caption_label("Failing metrics")
    if prev.button("‹", key=f"mp_prev_{key}", type="tertiary", disabled=page == 0):
        st.session_state[f"mp_{key}"] = page - 1
        st.rerun()
    pos.markdown(f"<div style='text-align:center;color:{TEXT};font-size:0.85rem;padding-top:6px'>"
                 f"{page + 1}/{pages}</div>", unsafe_allow_html=True)
    if nxt.button("›", key=f"mp_next_{key}", type="tertiary", disabled=page >= pages - 1):
        st.session_state[f"mp_{key}"] = page + 1
        st.rerun()
    cols = st.columns(per_page)
    for col, s in zip(cols, stats[page * per_page:(page + 1) * per_page]):
        good = s["avg"] >= 0.5
        col.markdown(
            f"<div style='padding:4px 2px'><div style='color:{TEXT};font-weight:650'>{html.escape(s['name'])}</div>"
            f"<div style='color:{PASS_COLOR if good else FAIL_COLOR};font-size:2rem;font-weight:700;"
            f"line-height:1.3'>{s['avg']:.2f}</div><div style='color:{MUTED};font-size:0.85rem'>"
            f"{s['passed']} passed · {s['failed']} failed</div></div>", unsafe_allow_html=True)


def _card_title(title: str, subtitle: str) -> None:
    st.markdown(f"<div style='font-weight:650;color:{TEXT}'>{html.escape(title)}</div>"
                f"<div style='color:{MUTED};font-size:0.82rem;margin-bottom:6px'>{html.escape(subtitle)}</div>",
                unsafe_allow_html=True)


def histogram(name: str, scores: list[float], color: str) -> None:
    """Score distribution card: share of results per 0.1 bin, thin marks for empty bins."""
    counts = [0] * 10
    for s in scores:
        counts[min(int(s * 10), 9)] += 1
    total = max(len(scores), 1)
    df = pd.DataFrame({"bin": _BINS, "share": [c / total for c in counts], "count": counts})
    x = alt.X("bin:N", sort=_BINS, title=None, scale=alt.Scale(paddingInner=0.25),
              axis=alt.Axis(labelAngle=0, values=_BINS[::2], domain=False, ticks=False))
    y_axis = alt.Axis(values=[0, 0.25, 0.5, 0.75, 1], domain=False, ticks=False, grid=True,
                      labelExpr="datum.value == 0 ? '0' : format(datum.value, '.2f')")
    y = alt.Y("share:Q", title=None, scale=alt.Scale(domain=[0, 1]), axis=y_axis)
    floor = alt.Chart(df).mark_bar(color=color).encode(
        x=x, y=alt.Y("lo:Q", scale=alt.Scale(domain=[0, 1]), axis=y_axis, title=None), y2="hi:Q"
    ).transform_calculate(lo="0", hi="0.012")
    bars = alt.Chart(df[df["count"] > 0]).mark_bar(color=color, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=x, y=y, tooltip=[alt.Tooltip("bin:N", title="Score"), alt.Tooltip("count:Q", title="Results")])
    with st.container(border=True):
        _card_title(name, "Score distribution across eval results.")
        st.altair_chart(_style(alt.layer(floor, bars).properties(height=230)), width="stretch")


def _value_chart(df: pd.DataFrame, title: str, color: str, how: str, group: str | None) -> None:
    """Average / Median view: one bar per group value (or a single bar), 0-1 scale, value on top."""
    d = df.copy()
    d["group"] = d[group].astype(str) if group else "All results"
    agg = d.groupby("group")["score"]
    out = (agg.mean() if how == "Average" else agg.median()).reset_index(name="value")
    x = alt.X("group:N", title=None, axis=alt.Axis(labelAngle=0, domain=False, ticks=False))
    y = alt.Y("value:Q", title=None, scale=alt.Scale(domain=[0, 1]),
              axis=alt.Axis(values=[0, 0.25, 0.5, 0.75, 1], format=".2f", domain=False, ticks=False))
    bars = alt.Chart(out).mark_bar(color=color, cornerRadiusTopLeft=3, cornerRadiusTopRight=3, size=46).encode(
        x=x, y=y, tooltip=["group", alt.Tooltip("value:Q", format=".2f")])
    labels = alt.Chart(out).mark_text(color=TEXT, dy=-8, fontWeight="bold").encode(
        x=x, y=y, text=alt.Text("value:Q", format=".2f"))
    with st.container(border=True):
        _card_title(title, f"{how} score{' by ' + group if group else ''}.")
        st.altair_chart(_style(alt.layer(bars, labels).properties(height=230)), width="stretch")


def _breakdown_chart(df: pd.DataFrame, title: str, group: str | None) -> None:
    """Breakdown view: passing / failing results per group value (or per score range)."""
    d = df.copy()
    d["status"] = d["passed"].map({True: "Passing", False: "Failing"})
    if group:
        d["x"] = d[group].astype(str)
        sort = sorted(d["x"].unique())
    else:
        d["x"] = d["score"].apply(lambda s: _BINS[min(int(s * 10), 9)])
        sort = _BINS
    chart = alt.Chart(d).mark_bar(cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("x:N", sort=sort, title=None, axis=alt.Axis(labelAngle=0, domain=False, ticks=False)),
        y=alt.Y("count():Q", title=None, axis=alt.Axis(tickMinStep=1, domain=False, ticks=False)),
        color=alt.Color("status:N", scale=alt.Scale(domain=["Passing", "Failing"], range=[PASS_COLOR, FAIL_COLOR]),
                        legend=alt.Legend(orient="top", direction="horizontal", title=None)),
        tooltip=["x", "status", "count()"])
    with st.container(border=True):
        _card_title(title, "Passing and failing results " + (f"by {group}." if group else "per score range."))
        st.altair_chart(_style(chart.properties(height=230)), width="stretch")


def eval_metrics(df: pd.DataFrame, group_keys: list[str], key: str) -> None:
    """Eval metrics section: Histogram / Average / Median / Breakdown, Value and Group by key."""
    if df.empty:
        st.caption("No judge scores in this run.")
        return
    c1, c2, c3 = st.columns([4, 3, 3])
    view = c1.segmented_control("View", ["Histogram", "Average", "Median", "Breakdown"], default="Histogram",
                                key=f"ev_{key}", label_visibility="collapsed") or "Histogram"
    group = c3.selectbox("Group by key", ["No group by"] + group_keys, key=f"eg_{key}")
    group = None if group == "No group by" else group
    values = ["All values"] + (sorted(df[group].astype(str).unique()) if group else [])
    value = c2.selectbox("Value", values, key=f"evv_{key}_{group}", disabled=group is None,
                         help="Choose a Group by key first" if group is None else None)
    chart_group = group
    if group and value != "All values":
        df = df[df[group].astype(str) == value]
        chart_group = None
    metrics = list(dict.fromkeys(df["metric"]))
    for i in range(0, len(metrics), 2):
        cols = st.columns(2)
        for j, (col, m) in enumerate(zip(cols, metrics[i:i + 2])):
            sub = df[df["metric"] == m]
            with col:
                if view == "Histogram":
                    histogram(m, sub["score"].tolist(), metric_color(m, i + j))
                elif view in ("Average", "Median"):
                    _value_chart(sub, m, metric_color(m, i + j), view, chart_group)
                else:
                    _breakdown_chart(sub, m, chart_group)


def pass_summary(n_pass: int, total: int, noun: str = "test cases") -> None:
    """PASSED x% | a/b and FAILED y% | c/b, as filled badges."""
    total = max(total, 1)

    def chip(label: str, bg: str, n: int) -> str:
        return (f"<span style='background:{bg};color:{BG};padding:6px 14px;border-radius:6px;font-weight:700;"
                f"font-size:0.8rem;letter-spacing:.04em'>{label}</span>"
                f"<span style='color:{TEXT};font-weight:650;margin:0 22px 0 10px'>{100 * n / total:.2f}% "
                f"&nbsp;|&nbsp; {n}/{total} {noun}</span>")
    st.markdown("<div style='text-align:right;padding:6px 0'>" + chip("PASSED", PASS_COLOR, n_pass)
                + chip("FAILED", FAIL_COLOR, total - n_pass) + "</div>", unsafe_allow_html=True)


_UI_BLOCK = re.compile(r"```(table|chart|followups|questions)\b(.*?)```", re.DOTALL)


def chat_bubble(role: str, text: str) -> None:
    if role == "user":
        st.markdown(f'<div class="nv-bubble nv-user">{html.escape(text)}</div><div class="nv-role">User</div>',
                    unsafe_allow_html=True)
    else:
        with st.container():
            st.markdown('<div class="nv-role right">Assistant</div>', unsafe_allow_html=True)
            # The app's UI blocks (table/chart placeholders, follow-up buttons) are shown as small notes.
            shown = _UI_BLOCK.sub(lambda m: f"\n\n_[{m.group(1)} block]_\n\n", text or "(no answer)")
            st.markdown(shown)


def fmt_score(v) -> str:
    return "-" if v is None else f"{v:.2f}"
