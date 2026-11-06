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


def gauge(pct: float, caption: str) -> None:
    pct = max(0.0, min(100.0, pct))
    color = PASS_COLOR if pct >= 80 else WARN_COLOR if pct >= 50 else FAIL_COLOR
    df = pd.DataFrame({"part": ["value", "rest"], "v": [pct, 100 - pct], "c": [color, BORDER]})
    arc = alt.Chart(df).mark_arc(innerRadius=62, outerRadius=80, cornerRadius=4).encode(
        theta=alt.Theta("v:Q", stack=True), color=alt.Color("c:N", scale=None), order=alt.Order("part:N"))
    text = alt.Chart(pd.DataFrame({"t": [f"{pct:.0f}%"]})).mark_text(
        fontSize=28, fontWeight="bold", color=color).encode(text="t:N")
    st.altair_chart((arc + text).properties(height=190, width=190), width="content")
    st.caption(caption)


def histogram(name: str, scores: list[float], color: str) -> None:
    bins = [f"{i / 10:.1f}-{(i + 1) / 10:.1f}" for i in range(10)]
    counts = [0] * 10
    for s in scores:
        counts[min(int(s * 10), 9)] += 1
    total = max(len(scores), 1)
    df = pd.DataFrame({"bin": bins, "share": [c / total for c in counts], "count": counts})
    chart = alt.Chart(df).mark_bar(color=color, cornerRadiusTopLeft=2, cornerRadiusTopRight=2).encode(
        x=alt.X("bin:N", sort=bins, title=None, axis=alt.Axis(labelAngle=0)),
        y=alt.Y("share:Q", title=None, scale=alt.Scale(domain=[0, 1]), axis=alt.Axis(format="%")),
        tooltip=["bin", "count"]).properties(height=200)
    st.markdown(f"**{name}**  \n<span class='nv-meta'>Score distribution across {len(scores)} result(s)</span>",
                unsafe_allow_html=True)
    st.altair_chart(chart, width="stretch")


def score_strip(points: list[tuple[str, float, bool]]) -> None:
    """One dot per test case at its average score."""
    if not points:
        st.caption("No judge scores in this run.")
        return
    df = pd.DataFrame(points, columns=["case", "score", "passed"])
    df["status"] = df["passed"].map({True: "Passing", False: "Failing"})
    chart = alt.Chart(df).mark_circle(size=110, opacity=0.9).encode(
        x=alt.X("score:Q", scale=alt.Scale(domain=[0, 1]), title=None),
        y=alt.Y("jitter:Q", title=None, axis=None, scale=alt.Scale(domain=[-1, 1])),
        color=alt.Color("status:N", scale=alt.Scale(domain=["Passing", "Failing"], range=[PASS_COLOR, FAIL_COLOR]),
                        legend=alt.Legend(orient="top", title=None)),
        tooltip=["case", alt.Tooltip("score:Q", format=".2f"), "status"],
    ).transform_calculate(jitter="(random() - 0.5) * 1.2").properties(height=140)
    st.altair_chart(chart, width="stretch")


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
