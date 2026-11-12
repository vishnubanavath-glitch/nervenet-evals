"""NerveNet evaluation dashboard (local, Streamlit).

    cd "E:\\BSS\\nervenet ai\\nervenet-evals"
    uv run streamlit run dashboard/app.py

Reads results/ and datasets/ from disk. Everything stays on this machine.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))

import data as D  # noqa: E402
import ui  # noqa: E402

st.set_page_config(page_title="NerveNet Evals", page_icon=":material/fact_check:", layout="wide")
ui.inject_css()


# ---------------------------------------------------------------------------
# Cached loading
# ---------------------------------------------------------------------------

@st.cache_data(ttl=5, show_spinner=False)
def runs_cached() -> list[D.Run]:
    return D.list_runs()


@st.cache_data(show_spinner=False)
def cases_cached(path: str, mtime: float) -> dict:
    run = next((r for r in D.list_runs() if str(r.path) == path), None)
    return D.load_cases(run) if run else {}


def run_cases(run: D.Run) -> dict:
    mtime = max((p.stat().st_mtime for p in run.path.glob("*.json")), default=0)
    return cases_cached(str(run.path), mtime)


def select_run(run: D.Run) -> None:
    st.session_state["run_path"] = str(run.path)


def current_run(runs: list[D.Run]) -> D.Run | None:
    path = st.session_state.get("run_path")
    return next((r for r in runs if str(r.path) == path), runs[0] if runs else None)


# ---------------------------------------------------------------------------
# Page: Runs
# ---------------------------------------------------------------------------

def run_headline(run: D.Run) -> tuple[str, str]:
    """(pass rate text, cost text) for the runs list."""
    s = run.summary or {}
    if run.kind == "chat":
        cases = run_cases(run)
        if not cases:
            return "-", "-"
        statuses = [D.chat_case_status(c) for c in cases.values()]
        rate = f"{100 * statuses.count('Passed') / len(statuses):.0f}% ({statuses.count('Passed')}/{len(statuses)})"
        cost = s.get("chat_cost_usd", 0) + s.get("judge_cost_usd", 0)
        return rate, f"${cost:.2f}" if s else "-"
    if run.kind == "tools":
        c = s.get("counts") or {}
        total = sum(c.values()) - c.get("SKIP", 0)
        return (f"{100 * c.get('PASS', 0) / total:.0f}% ({c.get('PASS', 0)}/{total})" if total else "-"), "$0.00"
    if run.kind == "schema-context":
        steps = (s.get("steps") or {}).values()
        judged = sum(x.get("columns_judged", 0) for x in steps)
        passed = sum(x.get("columns_passed", 0) for x in steps)
        cost = sum((x.get("judge_cost_usd") or 0) + (x.get("haiku_cost_usd") or 0) for x in steps)
        return (f"{100 * passed / judged:.0f}% ({passed}/{judged} columns)" if judged else "checks only"), f"${cost:.2f}"
    if run.kind == "smoke":
        return ("checks pass" if s.get("checks_passed") else "checks failed"), "-"
    return "-", "-"


def page_runs() -> None:
    ui.page_header("Evaluation", "Every run saved on this computer. Select a run to open its overview and test cases.",
                   accent="runs")
    runs = runs_cached()
    if not runs:
        st.info("No runs yet. Start one from **Run evaluation**.")
        return
    latest = {k: next((r for r in runs if r.kind == k), None) for k in ("chat", "tools", "schema-context")}
    k1, k2, k3, k4 = st.columns(4)
    with k1:
        ui.card("Runs saved", str(len(runs)), f"{len({r.dataset for r in runs})} dataset(s)")
    for col, kind in ((k2, "chat"), (k3, "tools"), (k4, "schema-context")):
        with col:
            r = latest[kind]
            rate, cost = run_headline(r) if r else ("-", "-")
            pct = re.match(r"(\d+)%", rate)
            ui.card(f"Latest {D.KIND_LABEL[kind].lower()}", rate.split(" ")[0],
                    (r.started.strftime("%d %b, %H:%M") + f" · {cost}") if r and r.started else "no run yet",
                    bad=bool(pct) and int(pct.group(1)) < 50)
    ui.section("All runs")
    c1, c2 = st.columns(2)
    kinds = c1.multiselect("Type", list(D.KIND_LABEL.values()), default=[D.KIND_LABEL[k] for k in ("chat", "tools", "schema-context")])
    datasets = c2.multiselect("Dataset", sorted({r.dataset for r in runs}), default=sorted({r.dataset for r in runs}))
    shown = [r for r in runs if D.KIND_LABEL.get(r.kind) in kinds and r.dataset in datasets]
    rows = []
    for r in shown:
        rate, cost = run_headline(r)
        rows.append({"Started": r.started.strftime("%d %b %Y %H:%M") if r.started else "", "Type": D.KIND_LABEL[r.kind],
                     "Dataset": r.dataset, "Pass rate": rate, "Cost": cost, "Run ID": r.run_id})
    df = pd.DataFrame(rows)
    event = st.dataframe(df, hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key="runs_table")
    sel = event.selection.rows if event and event.selection else []
    if sel:
        chosen = shown[sel[0]]
        if st.button(f"Open {chosen.label}", type="primary"):
            select_run(chosen)
            st.switch_page(PAGE_RUN)
    else:
        st.caption("Select a row, then open it.")


# ---------------------------------------------------------------------------
# Page: Run details
# ---------------------------------------------------------------------------

def page_run() -> None:
    runs = runs_cached()
    if not runs:
        st.info("No runs yet.")
        return
    ui.page_header("Run", "Overview of one evaluation run and every test case in it.", accent="details")
    run = current_run(runs)
    labels = [r.label for r in runs]
    idx = labels.index(run.label) if run in runs else 0
    choice = st.selectbox("Run", labels, index=idx, label_visibility="collapsed")
    run = runs[labels.index(choice)]
    select_run(run)

    menu, main = st.columns([1.3, 8.7], gap="medium")
    view = st.session_state.get("run_view", "Overview")
    with menu:
        if st.button("Back", icon=":material/arrow_back:", type="tertiary", key="run_back"):
            st.switch_page(PAGE_RUNS)
        for name, icon in (("Overview", ":material/grid_view:"), ("Test Cases", ":material/science:")):
            if st.button(name, icon=icon, type="primary" if view == name else "tertiary", width="stretch",
                         key=f"run_menu_{name}"):
                st.session_state["run_view"] = name
                st.rerun()
    with main:
        if view == "Overview":
            {"chat": chat_overview, "tools": tools_overview, "schema-context": schema_overview,
             "smoke": smoke_view}[run.kind](run)
        else:
            {"chat": chat_cases, "tools": tools_cases, "schema-context": schema_cases,
             "smoke": smoke_view}[run.kind](run)


# ---- chat -----------------------------------------------------------------------------------

def chat_overview(run: D.Run) -> None:
    cases = run_cases(run)
    if not cases:
        st.warning("This run has no saved test cases (it may have been interrupted).")
        return
    s = run.summary or {}
    statuses = {cid: D.chat_case_status(c) for cid, c in cases.items()}
    passed = sum(1 for v in statuses.values() if v == "Passed")
    duration = sum(t.get("duration_ms", 0) for c in cases.values() for t in c.get("turns", [])) / 1000
    ui.section("Overview")
    ui.meta_line([("ID", run.run_id), ("Dataset", run.dataset), ("Duration", f"{duration / 60:.1f} min of chat"),
                  ("Chat cost", f"${s.get('chat_cost_usd', 0):.2f}"), ("Evaluation cost", f"${s.get('judge_cost_usd', 0):.2f}"),
                  ("Judge", D.judge_model_name())])
    st.write("")

    scores = D.chat_metric_scores(cases)
    names = [m for m in D.TURN_METRICS + D.CONV_METRICS if m in scores]
    left, mid, right = st.columns([2.2, 4.3, 4.5], gap="medium")
    with left:
        with st.container(border=True):
            ui.gauge(100 * passed / len(cases), f"{passed} of {len(cases)} test cases passing")
    with mid:
        with st.container(border=True):
            ui.caption_label("Test case scores")
            ui.score_strip([(cid, D.case_avg_score(c), statuses[cid] == "Passed")
                            for cid, c in cases.items() if D.case_avg_score(c) is not None])
    with right:
        with st.container(border=True):
            ui.metrics_panel([{"name": D.METRIC_LABEL.get(m, m),
                               "avg": sum(v for _, v, _ in scores[m]) / len(scores[m]),
                               "passed": sum(1 for *_, ok in scores[m] if ok),
                               "failed": sum(1 for *_, ok in scores[m] if not ok)} for m in names],
                             key=run.run_id)

    ui.section("Code checks")
    turns = [t for c in cases.values() for t in c.get("turns", [])]
    data_turns = [t for t in turns if t.get("match") != "none"]
    data_ok = sum(1 for t in data_turns if t.get("checks") and t["checks"][0].get("passed"))
    checks_ok = sum(1 for t in turns if all(c.get("passed") for c in t.get("checks", [])))
    gov_ok = sum(1 for t in turns if all(g.get("passed") for g in t.get("governance", [])))
    c1, c2, c3 = st.columns(3)
    with c1:
        ui.card("Correct data shown", f"{data_ok}/{len(data_turns)}", "result table matches the answer key",
                bad=data_ok < len(data_turns))
    with c2:
        ui.card("All code checks", f"{checks_ok}/{len(turns)}", "data, model, tools, explore-before-query",
                bad=checks_ok < len(turns))
    with c3:
        ui.card("Governance clean", f"{gov_ok}/{len(turns)}", "no real names / sensitive values reached the AI",
                bad=gov_ok < len(turns))

    if scores:
        ui.section("Eval metrics")
        ui.eval_metrics(chat_metric_frame(cases, statuses), ["Case type", "Status", "Data check"], key=run.run_id)


def chat_metric_frame(cases: dict, statuses: dict) -> pd.DataFrame:
    """One row per judge result, with the keys the Eval metrics charts can group by."""
    rows = []
    for cid, rec in cases.items():
        kind = rec.get("kind", "")
        for t in rec.get("turns", []):
            data = t["checks"][0]["passed"] if t.get("match") != "none" and t.get("checks") else None
            for m, v in (t.get("judge") or {}).items():
                if v and v.get("score") is not None:
                    rows.append({"metric": D.METRIC_LABEL.get(m, m), "score": v["score"], "passed": bool(D.metric_ok(v)),
                                 "case": t["name"], "Case type": kind, "Status": statuses[cid],
                                 "Data check": "-" if data is None else ("PASS" if data else "FAIL")})
        for m, v in (rec.get("conversation_judge") or {}).items():
            if v and v.get("score") is not None:
                rows.append({"metric": D.METRIC_LABEL.get(m, m), "score": v["score"], "passed": bool(D.metric_ok(v)),
                             "case": cid, "Case type": kind, "Status": statuses[cid], "Data check": "-"})
    order = [D.METRIC_LABEL[m] for m in D.TURN_METRICS + D.CONV_METRICS]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["_o"] = df["metric"].map(lambda x: order.index(x) if x in order else 99)
        df = df.sort_values("_o").drop(columns="_o")
    return df


def chat_cases(run: D.Run) -> None:
    cases = run_cases(run)
    if not cases:
        st.warning("No saved test cases in this run.")
        return
    statuses = {cid: D.chat_case_status(c) for cid, c in cases.items()}
    n_pass = sum(1 for v in statuses.values() if v == "Passed")
    c1, c2 = st.columns([2, 3])
    flt = c1.segmented_control("Show", ["All", "Passed", "Failed"], default="All", key=f"flt_{run.run_id}")
    with c2:
        ui.pass_summary(n_pass, len(cases))

    def avg(rec, metric):
        vals = [t["judge"][metric]["score"] for t in rec.get("turns", [])
                if (t.get("judge") or {}).get(metric, {}).get("score") is not None]
        conv = (rec.get("conversation_judge") or {}).get(metric) or {}
        if conv.get("score") is not None:
            vals.append(conv["score"])
        return round(sum(vals) / len(vals), 2) if vals else None

    ids = [cid for cid in cases if flt in (None, "All") or (flt == "Passed") == (statuses[cid] == "Passed")]
    rows = []
    for cid in ids:
        rec = cases[cid]
        turns = rec.get("turns", [])
        data = [t["checks"][0]["passed"] for t in turns if t.get("match") != "none" and t.get("checks")]
        rows.append({
            "Name": cid, "Status": ("✅ " if statuses[cid] == "Passed" else "❌ ") + statuses[cid],
            "Type": rec.get("kind", ""), "First turn": turns[0]["question"] if turns else rec.get("error", ""),
            "Turns": len(turns), "Data": "-" if not data else ("PASS" if all(data) else "FAIL"),
            **{D.METRIC_LABEL[m]: avg(rec, m) for m in D.TURN_METRICS + D.CONV_METRICS},
        })
    event = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=f"cases_{run.run_id}_{flt}",
                         column_config={D.METRIC_LABEL[m]: st.column_config.NumberColumn(format="%.2f")
                                        for m in D.TURN_METRICS + D.CONV_METRICS})
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("Click a row to open the test case details.")
        return
    open_details("chat", run, ids, sel[0], key=f"cases_{run.run_id}_{flt}")


def chat_case_details(rec: dict, status: str) -> None:
    st.markdown(f"### Test case {rec['id']} &nbsp; {ui.status_badge(status)}", unsafe_allow_html=True)
    if rec.get("notes"):
        st.caption(f"Golden note: {rec['notes']}")
    if rec.get("error"):
        st.error(rec["error"])
        return
    turns = rec.get("turns", [])
    left, right = st.columns([5, 7], gap="large")
    with left:
        st.markdown("**Conversation**")
        for t in turns:
            ui.chat_bubble("user", t["question"])
            ui.chat_bubble("assistant", t.get("answer_raw") or t.get("answer") or "")
            tables = (t.get("turn_detail") or {}).get("ui_tables") or []
            for tb in tables:
                rows = tb.get("rows") or []
                if rows:
                    with st.expander(f"Table shown to the user · {tb.get('title') or ''} ({len(rows)} rows)"):
                        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    with right:
        t_metrics, t_checks, t_remarks, t_expected, t_trace = st.tabs(
            ["Metrics", "Checks", "Remarks", "Expected", "Trace"])
        multi = len(turns) > 1
        with t_metrics:
            conv = rec.get("conversation_judge") or {}
            if conv:
                st.markdown("**Whole conversation**")
                for m, v in conv.items():
                    metric_block(m, v)
            for t in turns:
                if multi:
                    st.markdown(f"**{t['name']}** · {t['question']}")
                if not t.get("judge"):
                    st.caption("Not judged.")
                for m, v in (t.get("judge") or {}).items():
                    metric_block(m, v)
        with t_checks:
            for t in turns:
                if multi:
                    st.markdown(f"**{t['name']}**")
                rows = [{"Check": c["name"], "Result": "PASS" if c["passed"] else "FAIL", "Detail": c.get("detail", "")}
                        for c in t.get("checks", [])]
                rows += [{"Check": f"governance: {g['name']}", "Result": "PASS" if g["passed"] else "FAIL",
                          "Detail": g.get("detail", "")} for g in t.get("governance", [])]
                st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        with t_remarks:
            for t in turns:
                if multi:
                    st.markdown(f"**{t['name']}** · {t['question']}")
                remarks_block(t.get("remarks"))
        with t_expected:
            for t in turns:
                if multi:
                    st.markdown(f"**{t['name']}** · {t['question']}")
                if t.get("match") == "none":
                    st.markdown("**Required behaviour**")
                    st.info(" ".join((t.get("expected_behavior") or "").split()))
                else:
                    exp = t.get("expected") or {}
                    st.markdown(f"Correct result from the data · match: `{t.get('match')}`")
                    if exp.get("rows") is not None:
                        st.dataframe(pd.DataFrame(exp["rows"], columns=exp.get("columns")), hide_index=True,
                                     width="stretch")
        with t_trace:
            for t in turns:
                if multi:
                    st.markdown(f"**{t['name']}**")
                trace_block(t)


def metric_block(name: str, v: dict) -> None:
    ok = D.metric_ok(v)
    status = "Errored" if v.get("error") else ("Success" if ok else "Failed")
    with st.expander(f"{D.METRIC_LABEL.get(name, name)}  ·  {status}  ·  {ui.fmt_score(v.get('score'))}",
                     expanded=True):
        def field(label: str, value: str) -> str:
            return (f"<div style='color:{ui.MUTED};font-size:0.82rem'>{label}</div>"
                    f"<div style='color:{ui.TEXT};font-size:1rem;margin-bottom:12px'>{value}</div>")
        c1, c2 = st.columns(2)
        c1.markdown(field("Status", ui.status_badge(status)) + field("Score", ui.fmt_score(v.get("score"))),
                    unsafe_allow_html=True)
        c2.markdown(field("Evaluation Model", f"{D.judge_model_name()} (Anthropic)")
                    + field("Threshold", f"{D.THRESHOLD}"), unsafe_allow_html=True)
        st.markdown(f"<div style='color:{ui.MUTED};font-size:0.82rem'>Reason</div>", unsafe_allow_html=True)
        st.write(v.get("reason") or "-")
        if v.get("error"):
            st.error(v["error"])


def remarks_block(r: dict | None) -> None:
    if not r:
        st.caption("No remarks for this answer (run without remarks).")
        return
    if r.get("error"):
        st.error(r["error"])
        return
    for title, key, kind in (("What's good", "whats_good", "pass"), ("What's bad", "whats_bad", "fail"),
                             ("What can be improved", "improvements", "warn")):
        items = r.get(key) or []
        st.markdown(ui.badge(title, kind), unsafe_allow_html=True)
        if items:
            st.markdown("\n".join(f"- {x}" for x in items))
        else:
            st.caption("none")


def trace_block(t: dict) -> None:
    trace = (t.get("turn_detail") or {}).get("trace") or {}
    usage = t.get("usage") or {}
    ui.meta_line([("Models", ", ".join(trace.get("models_used", [])) or "-"),
                  ("LLM calls", str(usage.get("llm_calls", "-"))),
                  ("Tokens in", f"{usage.get('total_input_tokens', 0):,}"),
                  ("from cache", f"{usage.get('cache_read_input_tokens', 0):,}"),
                  ("Tokens out", f"{usage.get('output_tokens', 0):,}"),
                  ("Cost", f"${t.get('chat_cost_usd', 0):.3f}"), ("Time", f"{t.get('duration_ms', 0) / 1000:.1f}s")])
    tools = trace.get("tool_calls") or []
    if tools:
        st.markdown("**Tools called (in order)**")
        st.dataframe(pd.DataFrame([{
            "Tool": tc["name"], "Error": tc.get("is_error"), "ms": tc.get("duration_ms"),
            "Arguments": json.dumps({k: v for k, v in (tc.get("arguments") or {}).items()
                                     if k not in ("tenant_id", "user_id", "organization_id")}, ensure_ascii=False)[:300],
        } for tc in tools]), hide_index=True, width="stretch")
    for sql in t.get("sql") or []:
        st.code(sql, language="sql")
    calls = trace.get("llm_calls") or []
    if calls:
        with st.expander(f"History sent to the AI ({len(calls[0]['request']['messages'])} message(s) in the first call)"):
            for m in calls[0]["request"]["messages"]:
                content = m.get("content")
                text = content if isinstance(content, str) else " ".join(
                    b.get("text", f"[{b.get('type')}]") for b in content if isinstance(b, dict))
                st.markdown(f"**{m.get('role')}**: {text[:1500]}")
        with st.expander("System prompt"):
            sp = calls[0]["request"].get("system")
            st.text(sp if isinstance(sp, str) else json.dumps(sp, ensure_ascii=False, indent=1)[:20000])
        with st.expander("What the AI received from each tool"):
            for tc in tools:
                st.markdown(f"**{tc['name']}**")
                seen = tc.get("result_seen_by_model")
                st.code((seen if isinstance(seen, str) else json.dumps(seen, ensure_ascii=False))[:6000], language="json")


# ---- tools ----------------------------------------------------------------------------------

def tools_overview(run: D.Run) -> None:
    s = run.summary or {}
    c = s.get("counts") or {}
    total = sum(c.values()) - c.get("SKIP", 0)
    ui.section("Overview")
    ui.meta_line([("ID", run.run_id), ("Dataset", run.dataset), ("Cost", "$0.00 (no AI)")])
    left, right = st.columns([2, 5])
    with left:
        ui.gauge(100 * c.get("PASS", 0) / total if total else 0, f"{c.get('PASS', 0)} of {total} tests passing")
    with right:
        rows = [{"Area": a, **{k: v.get(k, 0) for k in ("PASS", "FAIL", "ERROR", "SKIP")}}
                for a, v in (s.get("by_area") or {}).items()]
        st.markdown("**By area**")
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        if s.get("failed"):
            st.markdown("**Failed:** " + ", ".join(s["failed"]))


def tools_cases(run: D.Run) -> None:
    cases = run_cases(run)
    flt = st.segmented_control("Show", ["All", "PASS", "FAIL", "SKIP"], default="All", key=f"tflt_{run.run_id}")
    ids = [k for k, v in cases.items() if flt in (None, "All") or v.get("status") == flt]
    rows = [{"ID": k, "Status": cases[k].get("status"), "Area": cases[k].get("area"), "Test": cases[k].get("title"),
             "Expect": cases[k].get("expect"), "Detail": cases[k].get("detail")} for k in ids]
    event = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=f"tcases_{run.run_id}_{flt}")
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("Click a row to open the test details.")
        return
    open_details("tools", run, ids, sel[0], key=f"tcases_{run.run_id}_{flt}")


def tools_case_details(r: dict) -> None:
    st.markdown(f"### {r['id']} · {r.get('title')} &nbsp; {ui.status_badge(r.get('status', ''))}", unsafe_allow_html=True)
    st.write(r.get("detail"))
    c1, c2 = st.columns(2)
    with c1:
        st.markdown("**Sent to the tool (masked)**")
        st.json(r.get("sent") or {})
        if r.get("reference_sql"):
            st.markdown("**Answer-key SQL (real names, run on the CSV)**")
            st.code(r["reference_sql"], language="sql")
        if r.get("expected"):
            st.markdown("**Expected result**")
            st.dataframe(pd.DataFrame(r["expected"]["rows"], columns=r["expected"]["columns"]), hide_index=True)
    with c2:
        st.markdown("**Tool returned**")
        st.json(r.get("received") or {}, expanded=1)
        if r.get("local_only"):
            st.markdown("**Details (local only)**")
            st.json(r["local_only"])


# ---- test case details panel (right side, prev / next) --------------------------------------

def open_details(kind: str, run: D.Run, ids: list[str], index: int, key: str, extra: dict | None = None) -> None:
    """Open the details panel when a new row is selected; a button re-opens it for the same row."""
    st.session_state["tc"] = {"kind": kind, "run": str(run.path), "ids": ids, "extra": extra or {}}
    if st.session_state.get(f"opened_{key}") != index:
        st.session_state[f"opened_{key}"] = index
        st.session_state["tc_idx"] = index
        test_case_panel()
    elif st.button("Open details", icon=":material/open_in_new:", key=f"reopen_{key}"):
        st.session_state["tc_idx"] = index
        test_case_panel()


@st.dialog("Test Case Details", width=1300, position="right")
def test_case_panel() -> None:
    tc = st.session_state.get("tc") or {}
    ids, idx = tc.get("ids") or [], st.session_state.get("tc_idx", 0)
    run = next((r for r in runs_cached() if str(r.path) == tc.get("run")), None)
    if not run or not ids:
        st.info("Nothing selected.")
        return
    idx = max(0, min(idx, len(ids) - 1))
    c1, c2, c3 = st.columns([1, 1, 7])
    if c1.button("prev", icon=":material/arrow_back:", disabled=idx == 0, key="tc_prev"):
        st.session_state["tc_idx"] = idx - 1
        st.rerun(scope="fragment")
    if c2.button("next", icon=":material/arrow_forward:", icon_position="right", disabled=idx >= len(ids) - 1,
                 key="tc_next"):
        st.session_state["tc_idx"] = idx + 1
        st.rerun(scope="fragment")
    c3.caption(f"{idx + 1} of {len(ids)}")
    cases = run_cases(run)
    if tc["kind"] == "chat":
        rec = cases[ids[idx]]
        chat_case_details(rec, D.chat_case_status(rec))
    elif tc["kind"] == "tools":
        tools_case_details(cases[ids[idx]])
    else:
        rec = cases[tc["extra"]["step"]]
        profile = (D._read_json(run.path / "profile.json") or {}).get("columns", {})
        schema_column_details(ids[idx], rec.get("suggestions") or {}, rec.get("column_scores") or {}, profile)


# ---- schema context -------------------------------------------------------------------------

def schema_overview(run: D.Run) -> None:
    s = run.summary or {}
    ui.section("Overview")
    ui.meta_line([("ID", run.run_id), ("Dataset", run.dataset), ("Judge", D.judge_model_name())])
    cases = run_cases(run)
    for step, info in (s.get("steps") or {}).items():
        st.markdown(f"#### {'Column suggestions' if step == 'column_settings' else 'Whole draft context'}")
        if info.get("error"):
            st.error(info["error"])
            continue
        left, mid, right = st.columns([2, 4, 4])
        judged, passed = info.get("columns_judged", 0), info.get("columns_passed", 0)
        with left:
            if judged:
                ui.gauge(100 * passed / judged, f"{passed} of {judged} column descriptions pass")
            else:
                st.caption("Not judged (code checks only).")
        with mid:
            st.markdown("**Code checks**")
            for name, v in (info.get("checks") or {}).items():
                st.markdown(f"{ui.status_badge('PASS' if v['passed'] else 'FAIL')} **{name}** "
                            f"<span class='nv-meta'>{v.get('detail', '')}</span>", unsafe_allow_html=True)
            ui.meta_line([("Haiku cost", f"${info.get('haiku_cost_usd') or 0:.4f}"),
                          ("Judge cost", f"${info.get('judge_cost_usd') or 0:.2f}")])
        with right:
            scores = [v["score"] for v in ((cases.get(step) or {}).get("column_scores") or {}).values()
                      if v.get("score") is not None]
            if scores:
                ui.histogram("Column description accuracy", scores, ui.METRIC_COLORS[2])
        ts = info.get("table_score")
        if ts:
            st.markdown(f"**Table context accuracy:** {ui.fmt_score(ts.get('score'))} "
                        f"{ui.status_badge('PASS' if ts.get('passed') else 'FAIL')}", unsafe_allow_html=True)
            st.write(ts.get("reason"))


def schema_cases(run: D.Run) -> None:
    cases = run_cases(run)
    steps = [k for k in ("column_settings", "schema_context") if k in cases]
    if not steps:
        st.info("No saved steps in this run.")
        return
    step = st.segmented_control("Step", steps, default=steps[0], key=f"step_{run.run_id}",
                                format_func=lambda k: "Column suggestions" if k == "column_settings" else "Whole draft context")
    rec = cases[step or steps[0]]
    sugg, scores = rec.get("suggestions") or {}, rec.get("column_scores") or {}
    profile = (cases.get("profile") or D._read_json(run.path / "profile.json") or {}).get("columns", {})
    cols = list(sugg)
    rows = [{"Column": c, "Label": sugg[c].get("label"), "Role": sugg[c].get("role"),
             "Sensitivity": sugg[c].get("sensitivity"), "Score": (scores.get(c) or {}).get("score"),
             "Status": "-" if c not in scores else ("✅ Pass" if scores[c].get("passed") else "❌ Fail")} for c in cols]
    event = st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch", on_select="rerun",
                         selection_mode="single-row", key=f"scols_{run.run_id}_{step}",
                         column_config={"Score": st.column_config.NumberColumn(format="%.2f")})
    sel = event.selection.rows if event and event.selection else []
    if not sel:
        st.caption("Click a column to open what the AI wrote, the facts from the data and the judge's reason.")
        return
    open_details("schema-context", run, cols, sel[0], key=f"scols_{run.run_id}_{step}",
                 extra={"step": step or steps[0]})


def schema_column_details(c: str, sugg: dict, scores: dict, profile: dict) -> None:
    st.markdown(f"### {c}")
    l, r = st.columns(2)
    with l:
        st.markdown("**What the AI (Haiku) wrote**")
        st.json(sugg[c])
    with r:
        if c in scores:
            metric_block("Column description accuracy", scores[c])
        if c in profile:
            from evals.profile import column_facts_text
            st.markdown("**Facts measured from the data**")
            st.write(column_facts_text(c, profile[c]))


def smoke_view(run: D.Run) -> None:
    st.json(run.summary or {})
    for k, v in run_cases(run).items():
        with st.expander(k):
            st.json(v, expanded=1)


# ---------------------------------------------------------------------------
# Page: Golden datasets
# ---------------------------------------------------------------------------

def real_sql(text: str) -> str:
    """Tools-golden placeholders -> real names (for the answer key), without the app's catalog."""
    text = re.sub(r"\{c:([^}]+)\}", lambda m: f'"{m.group(1)}"', text)
    return re.sub(r"\{t:([^}]+)\}", lambda m: m.group(1), text)


@st.cache_data(show_spinner="Calculating expected answers from the CSV...")
def expected_answers(dataset: str, sqls: tuple[str, ...], csv_mtime: float) -> dict[str, dict]:
    from evals.config import load_dataset
    from evals.tools_eval import Reference

    ref = Reference(load_dataset(dataset))
    out = {}
    for sql in sqls:
        try:
            cols, rows = ref.run(" ".join(sql.split()))
            out[sql] = {"columns": cols, "rows": rows}
        except Exception as exc:
            out[sql] = {"error": str(exc)}
    return out


def validate_golden(kind: str, text: str) -> list[str]:
    """Check one golden file (chat.yaml or tools.yaml)."""
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return [f"Not valid YAML: {exc}"]
    cases = (doc or {}).get("cases")
    if not isinstance(cases, list) or not cases:
        return ["The file needs a top-level 'cases:' list with at least one case."]
    return validate_section(kind, cases)


def validate_section(kind: str, cases: list) -> list[str]:
    problems, seen = [], set()
    for i, c in enumerate(cases, 1):
        cid = c.get("id") if isinstance(c, dict) else None
        if not cid:
            problems.append(f"Case #{i} has no id.")
            continue
        if cid in seen:
            problems.append(f"{cid}: id used twice.")
        seen.add(cid)
        if kind == "chat":
            turns = c.get("turns")
            if not isinstance(turns, list) or not turns:
                problems.append(f"{cid}: needs a 'turns' list.")
                continue
            for n, t in enumerate(turns, 1):
                if not t.get("question"):
                    problems.append(f"{cid} turn {n}: missing question.")
                match = t.get("match", "rows" if t.get("expected_sql") else "none")
                if match not in ("rows", "contains", "none"):
                    problems.append(f"{cid} turn {n}: match must be rows, contains or none.")
                if match != "none" and not t.get("expected_sql"):
                    problems.append(f"{cid} turn {n}: needs expected_sql (or match: none + expected_behavior).")
                if match == "none" and not t.get("expected_behavior"):
                    problems.append(f"{cid} turn {n}: a behaviour case needs expected_behavior.")
        else:
            if not any(k in c for k in ("sql", "raw_sql", "raw_sql_template", "tool")):
                problems.append(f"{cid}: needs sql, raw_sql, raw_sql_template or tool.")
            allowed = {"match_reference", "rejected", "row_limit", "capped", "masked", "masked_headers",
                       "kpi_matches", "not_listed"}
            if c.get("expect") not in allowed:
                problems.append(f"{cid}: expect must be one of {sorted(allowed)}.")
    return problems


def page_goldens() -> None:
    ui.page_header("Golden", "The questions and tests each evaluation uses. Expected answers are calculated from the "
                   "dataset's CSV.", accent="datasets")
    datasets = D.list_datasets()
    if not datasets:
        st.info("No datasets under datasets/.")
        return
    dataset = st.selectbox("Dataset", datasets)
    from evals.config import load_dataset
    ds = load_dataset(dataset)
    csv = Path(ds.reference_file) if ds.reference_file else None
    st.markdown(f"<span class='nv-meta'>Source <b>{ds.source_id}</b> in tenant <b>{ds.tenant_slug}</b> · "
                f"answer key <b>{csv}</b></span>", unsafe_allow_html=True)
    tab_chat, tab_tools = st.tabs(["Chat questions (chat.yaml)", "Tools tests (tools.yaml)"])
    for kind, tab in (("chat", tab_chat), ("tools", tab_tools)):
        with tab:
            if not D.golden_path(dataset, kind).exists():
                st.info(f"No {kind}.yaml yet for this dataset.")
                continue
            golden_list(dataset, kind, csv)
            golden_file_editor(dataset, kind)


def golden_list(dataset: str, kind: str, csv: Path | None) -> None:
    path = D.golden_path(dataset, kind)
    doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    cases = doc.get("cases") or []
    rows = []
    sqls: list[str] = []
    for c in cases:
        if kind == "chat":
            turns = c.get("turns") or []
            rows.append({"ID": c.get("id"), "Turns": len(turns),
                         "Questions": "  →  ".join(t.get("question", "") for t in turns),
                         "Match": ", ".join(t.get("match", "rows" if t.get("expected_sql") else "none") for t in turns),
                         "Notes": c.get("notes", "")})
            sqls += [t["expected_sql"] for t in turns if t.get("expected_sql")]
        else:
            rows.append({"ID": c.get("id"), "Area": c.get("area"), "Test": c.get("title"), "Expect": c.get("expect")})
            if c.get("expect") in ("match_reference", "kpi_matches") and c.get("sql"):
                sqls.append(real_sql(c["sql"]))
    st.markdown(f"**{len(cases)} {'question case(s)' if kind == 'chat' else 'test(s)'}** in `{path.name}`")
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")

    if sqls and csv and csv.exists() and st.toggle("Show expected answers (calculated from the CSV)",
                                                     key=f"exp_{dataset}_{kind}"):
        answers = expected_answers(dataset, tuple(sqls), csv.stat().st_mtime)
        for c in cases:
            items = ([(t.get("question"), t.get("expected_sql"), t.get("expected_behavior")) for t in c.get("turns", [])]
                     if kind == "chat" else [(c.get("title"), real_sql(c["sql"]) if c.get("sql") and
                                              c.get("expect") in ("match_reference", "kpi_matches") else None, None)])
            with st.expander(f"{c.get('id')} · {items[0][0] if items else ''}"):
                if c.get("notes"):
                    st.caption(f"Note to confirm: {c['notes']}")
                for q, sql, behaviour in items:
                    if len(items) > 1:
                        st.markdown(f"**{q}**")
                    if sql:
                        a = answers.get(sql) or {}
                        if a.get("error"):
                            st.error(a["error"])
                        else:
                            st.dataframe(pd.DataFrame(a["rows"], columns=a["columns"]), hide_index=True)
                        st.code(" ".join(sql.split()), language="sql")
                    elif behaviour:
                        st.info(" ".join(behaviour.split()))
                    else:
                        st.caption(f"Expectation: {c.get('expect')}")

def golden_file_editor(dataset: str, kind: str) -> None:
    path = D.golden_path(dataset, kind)
    text = path.read_text(encoding="utf-8")
    with st.expander(f"Edit {path.name}"):
        st.caption("Edit the YAML, press Validate, then Save.")
        new = st.text_area("YAML", text, height=520, key=f"yaml_{dataset}_{kind}", label_visibility="collapsed")
        b1, b2, _ = st.columns([1, 1, 6])
        if b1.button("Validate", key=f"val_{dataset}_{kind}"):
            problems = validate_golden(kind, new)
            if problems:
                st.error("\n".join(f"- {p}" for p in problems))
            else:
                st.success("Looks good.")
        if b2.button("Save", type="primary", key=f"save_{dataset}_{kind}"):
            problems = validate_golden(kind, new)
            if problems:
                st.error("Not saved:\n" + "\n".join(f"- {p}" for p in problems))
            elif new == text:
                st.info("No changes.")
            else:
                path.write_text(new, encoding="utf-8")
                st.cache_data.clear()
                st.success("Saved.")
                st.rerun()


# ---------------------------------------------------------------------------
# Page: Run evaluation
# ---------------------------------------------------------------------------

@st.cache_resource
def job_registry() -> dict:
    return {}


def job_running(job: dict) -> bool:
    return bool(job.get("proc")) and job["proc"].poll() is None


def start_job(cmd: list[str], label: str) -> None:
    logs = D.RESULTS_DIR / "_dashboard_logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{datetime.now():%Y%m%d-%H%M%S}.log"
    env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1"}
    fh = open(log_path, "w", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=D.EVALS_DIR, stdout=fh, stderr=subprocess.STDOUT, env=env)
    job_registry().update(proc=proc, log=log_path, label=label, cmd=cmd, started=time.time(), fh=fh)


def golden_ids(dataset: str, kind: str) -> list[str]:
    path = D.golden_path(dataset, kind)
    if not path.exists():
        return []
    return [c.get("id") for c in (yaml.safe_load(path.read_text(encoding="utf-8")) or {}).get("cases", []) if c.get("id")]


def page_launch() -> None:
    ui.page_header("Run", "Choose what to evaluate, check the estimated cost, then start. The log updates live.",
                   accent="evaluation")
    job = job_registry()
    running = job_running(job)

    datasets = D.list_datasets()
    if not datasets:
        st.info("No datasets under datasets/.")
        return
    c1, c2 = st.columns(2)
    dataset = c1.selectbox("Dataset", datasets, disabled=running)
    what = c2.selectbox("What to evaluate", ["Everything", "Chat responses", "Tools", "Schema context", "Setup check"],
                        disabled=running)

    py = sys.executable
    scripts = D.EVALS_DIR / "scripts"
    cmd: list[str] = []
    estimate = ""
    if what == "Everything":
        judge = st.toggle("Opus judge (scores + remarks)", value=True, disabled=running)
        cmd = [py, str(scripts / "run_all.py"), "--dataset", dataset] + ([] if judge else ["--no-judge"])
        estimate = "about $8.50 and 25 minutes" if judge else "about $1.50 and 15 minutes"
    elif what == "Chat responses":
        ids = golden_ids(dataset, "chat")
        pick = st.multiselect("Questions (leave empty for all)", ids, disabled=running)
        o1, o2 = st.columns(2)
        judge = o1.toggle("Opus judge (scores)", value=True, disabled=running)
        remarks = o2.toggle("Remarks (good / bad / improve)", value=True, disabled=running or not judge)
        n = len(pick) or len(ids)
        cmd = [py, str(scripts / "eval_chat.py"), "--dataset", dataset]
        cmd += ["--only", ",".join(pick)] if pick else []
        cmd += [] if judge else ["--no-judge"]
        cmd += [] if (remarks and judge) else ["--no-remarks"]
        per = 0.05 + (0.15 if judge else 0) + (0.015 if judge and remarks else 0)
        estimate = f"about ${n * per:.2f} and {max(1, round(n * 0.5))} minutes for {n} question(s)"
    elif what == "Tools":
        ids = golden_ids(dataset, "tools")
        pick = st.multiselect("Tests (leave empty for all)", ids, disabled=running)
        cmd = [py, str(scripts / "eval_tools.py"), "--dataset", dataset] + (["--only", ",".join(pick)] if pick else [])
        estimate = "free (no AI), under a minute"
    elif what == "Schema context":
        step = st.radio("Step", ["both", "columns", "context"], horizontal=True, disabled=running,
                        format_func={"both": "Both steps", "columns": "Column suggestions", "context": "Whole draft"}.get)
        judge = st.toggle("Opus judge", value=True, disabled=running)
        cmd = [py, str(scripts / "eval_schema_context.py"), "--dataset", dataset, "--step", step]
        cmd += [] if judge else ["--no-judge"]
        estimate = "about $1.50 and 5 minutes" if judge else "about $0.02 and 2 minutes"
    else:
        cmd = [py, str(scripts / "check_setup.py"), "--dataset", dataset, "--judge"]
        estimate = "about $0.01, a few seconds"

    st.markdown(f"<span class='nv-meta'>Estimated cost: <b>{estimate}</b></span>", unsafe_allow_html=True)
    b1, b2, _ = st.columns([1, 1, 5])
    if b1.button("Start", type="primary", disabled=running):
        start_job(cmd, f"{what} · {dataset}")
        st.rerun()
    if b2.button("Stop", disabled=not running):
        job["proc"].terminate()
        st.warning("Stopping...")

    if job.get("log"):
        st.divider()
        live_log()


@st.fragment(run_every="2s")
def live_log() -> None:
    job = job_registry()
    running = job_running(job)
    elapsed = time.time() - job.get("started", time.time())
    if running:
        st.markdown(f"{ui.badge('RUNNING', 'warn')} **{job['label']}** · {elapsed / 60:.1f} min", unsafe_allow_html=True)
    else:
        code = job["proc"].returncode if job.get("proc") else None
        if job.get("fh") and not job["fh"].closed:
            job["fh"].close()
        st.markdown(f"{ui.badge('FINISHED' if code == 0 else 'FINISHED WITH FAILURES', 'pass' if code == 0 else 'fail')} "
                    f"**{job['label']}**", unsafe_allow_html=True)
    try:
        lines = Path(job["log"]).read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        lines = []
    # The app prints large metadata blocks during chat runs; keep the log readable.
    keep = [ln for ln in lines if ln.strip() and not ln.startswith(("  \"", "  {", "  }", "  ]", "    "))]
    st.code("\n".join(keep[-60:]) or "(starting...)", language="text")
    if not running:
        st.caption(f"Full log: {job['log']}")
        if st.button("Open results", key="open_results"):
            runs = D.list_runs()
            if runs:
                select_run(runs[0])
                st.switch_page(PAGE_RUN)


# ---------------------------------------------------------------------------
# Navigation
# ---------------------------------------------------------------------------

# NV_DASH_START_PAGE (runs | run | goldens | evaluate) picks the first page; used by automated tests.
_START = os.getenv("NV_DASH_START_PAGE", "runs")
PAGE_RUNS = st.Page(page_runs, title="Runs", icon=":material/history:", default=_START == "runs")
PAGE_RUN = st.Page(page_run, title="Run details", icon=":material/insights:", url_path="run", default=_START == "run")
PAGE_GOLDENS = st.Page(page_goldens, title="Golden datasets", icon=":material/dataset:", url_path="goldens",
                       default=_START == "goldens")
PAGE_LAUNCH = st.Page(page_launch, title="Run evaluation", icon=":material/play_circle:", url_path="evaluate",
                      default=_START == "evaluate")

st.logo(str(Path(__file__).resolve().parent / "assets" / "logo.svg"), size="large")
st.navigation([PAGE_RUNS, PAGE_RUN, PAGE_GOLDENS, PAGE_LAUNCH], position="top").run()
