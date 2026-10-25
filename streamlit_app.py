"""Streamlit UI for the live MCP vs Code Mode support-ticket comparison."""

import asyncio
import io
import sys
from contextlib import redirect_stdout
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from live_demo import (
    DEFAULT_QUERY,
    MODEL,
    ROOT,
    SERVER,
    close_groq_client,
    mcp_tools_as_groq,
    run_code_mode,
    run_mcp_style,
)


st.set_page_config(
    page_title="MCP vs Code Mode",
    page_icon="🔎",
    layout="wide",
)

st.markdown(
    """
    <style>
    .block-container { padding-top: 2.2rem; padding-bottom: 3rem; }
    [data-testid="stMetric"] {
        background: #f6f8fc;
        border: 1px solid #e7eaf1;
        padding: 1rem 1.1rem;
        border-radius: 12px;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


async def execute_comparison(query: str) -> dict[str, Any]:
    """Run both live paths against the configured Groq model and MySQL DB."""
    try:
        params = StdioServerParameters(
            command=sys.executable,
            args=[str(SERVER)],
            cwd=str(ROOT),
        )
        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await asyncio.wait_for(session.initialize(), timeout=20)
                tools = await session.list_tools()
                tool_defs = mcp_tools_as_groq(tools.tools)
                mcp_answer, mcp_stats = await run_mcp_style(session, tool_defs, query)
                code_answer, code_stats = await run_code_mode(session, query)
        return {
            "mcp_answer": mcp_answer,
            "mcp_stats": mcp_stats,
            "code_answer": code_answer,
            "code_stats": code_stats,
        }
    finally:
        await close_groq_client()


def total_tokens(stats: dict[str, int]) -> int:
    return stats["prompt_tokens"] + stats["completion_tokens"]


def markdown_table_to_dataframe(response: str) -> pd.DataFrame | None:
    """Parse the ticket markdown table so Streamlit can render it in a scrollable grid."""
    table_lines = [
        line.strip()
        for line in response.splitlines()
        if line.strip().startswith("|") and line.strip().endswith("|")
    ]
    if len(table_lines) < 2:
        return None

    def cells(line: str) -> list[str]:
        values: list[str] = []
        current: list[str] = []
        escaped = False
        for character in line[1:-1]:
            if escaped:
                current.append(character)
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == "|":
                values.append("".join(current).strip())
                current = []
            else:
                current.append(character)
        values.append("".join(current).strip())
        return values

    headers = cells(table_lines[0])
    rows = []
    for line in table_lines[1:]:
        values = cells(line)
        if values and all(value and set(value) <= {"-", ":"} for value in values):
            continue
        if len(values) == len(headers):
            rows.append(values)
    return pd.DataFrame(rows, columns=headers) if rows else None


def show_response(response: str) -> None:
    """Display query tables in bounded, horizontally scrollable panels."""
    frame = markdown_table_to_dataframe(response)
    if frame is None:
        st.markdown(response)
        return

    # Code Mode may return several compact result groups (for example, a
    # tier/priority summary plus a top-customer ranking). Render each group
    # separately and pivot the summary to match the MCP path's presentation.
    if "result_group" in frame.columns:
        group_names = frame["result_group"].fillna("").astype(str).str.lower()
        group_kind = group_names.map(
            lambda name: (
                "summary" if "summary" in name
                else "top_customers" if "top" in name and "customer" in name
                else name
            )
        )
        summary = frame[group_kind.eq("summary")].copy()
        top_customers = frame[group_kind.eq("top_customers")].copy()
        rendered = False

        if not summary.empty and {"customer_tier", "priority", "count"}.issubset(summary.columns):
            summary["count"] = pd.to_numeric(summary["count"], errors="coerce")
            summary["_tier_order"] = summary["customer_tier"].astype(str)
            summary["_priority_order"] = pd.Categorical(
                summary["priority"].astype(str), categories=["low", "medium", "high", "urgent"], ordered=True
            )
            summary = summary.sort_values(["_tier_order", "_priority_order"])
            summary_view = summary[["customer_tier", "priority", "count"]].rename(
                columns={"customer_tier": "Customer Tier", "priority": "Priority", "count": "Ticket Count"}
            )
            st.caption(f"Query results ({len(summary_view)} rows)")
            st.dataframe(
                summary_view,
                hide_index=True,
                use_container_width=True,
                height=min(44 * (len(summary_view) + 1) + 3, 420),
            )
            rendered = True

        if not top_customers.empty:
            columns = [name for name in ("customer", "urgent_count") if name in top_customers.columns]
            if columns:
                top_customers = top_customers[columns].rename(
                    columns={"customer": "Customer", "urgent_count": "Urgent Tickets"}
                )
                st.caption("Customers with the most urgent tickets")
                st.dataframe(
                    top_customers,
                    hide_index=True,
                    use_container_width=True,
                    height=min(44 * (len(top_customers) + 1) + 3, 240),
                )
                rendered = True

        known_groups = {"summary", "top_customers"}
        for group in frame.loc[~group_kind.isin(known_groups), "result_group"].dropna().unique():
            st.caption(str(group).replace("_", " ").title())
            st.dataframe(
                frame.loc[group_names.eq(str(group).lower())].drop(columns=["result_group"]),
                hide_index=True,
                use_container_width=True,
                height=min(44 * (len(frame.loc[group_names.eq(str(group).lower())]) + 1) + 3, 420),
            )
            rendered = True
        if rendered:
            return

    st.caption(f"Query results ({len(frame)} rows)")
    st.dataframe(frame, hide_index=True, use_container_width=True, height=360)


def exception_details(error: BaseException) -> list[str]:
    """Flatten TaskGroup/ExceptionGroup wrappers to show their real causes."""
    if isinstance(error, BaseExceptionGroup):
        details: list[str] = []
        for child in error.exceptions:
            details.extend(exception_details(child))
        return details
    return [f"{type(error).__name__}: {error}"]


def main() -> None:
    st.title("MCP vs Code Mode")
    st.caption(
        f"Ask the support-ticket database a question and compare both workflows. "
        f"Model: `{MODEL}` · Usage is reported from Groq API responses."
    )
    st.caption(
        "Code Mode runs model-written JavaScript in a local QuickJS child process. Its only host access is a "
        "read-only MCP query bridge; Groq and MySQL credentials stay in the local app/server processes."
    )

    with st.form("ticket_query_form"):
        query = st.text_area(
            "Your question",
            value=DEFAULT_QUERY,
            height=100,
            placeholder="For example: Show unresolved urgent tickets about security or data loss",
        )
        submitted = st.form_submit_button("Run comparison", type="primary", use_container_width=True)

    if submitted:
        if not query.strip():
            st.warning("Enter a question about the support tickets.")
            return
        for key in ("comparison_result", "comparison_trace", "comparison_query"):
            st.session_state.pop(key, None)
        trace = io.StringIO()
        try:
            with st.spinner("Running MCP and Code Mode with Groq…"):
                with redirect_stdout(trace):
                    result = asyncio.run(execute_comparison(query.strip()))
            st.session_state["comparison_result"] = result
            st.session_state["comparison_trace"] = trace.getvalue()
            st.session_state["comparison_query"] = query.strip()
        except Exception as error:
            details = exception_details(error)
            st.error(f"Could not complete the comparison: {details[0]}")
            detail_text = " ".join(details).lower()
            if "tokens per minute" in detail_text or "request too large" in detail_text:
                st.info(
                    "Groq rejected this request because its prompt exceeded the account's current token-per-minute limit. "
                    "For summaries, use counts/groupings instead of asking Groq to read every raw ticket row."
                )
            elif "failed to parse tool call arguments as json" in detail_text:
                st.info(
                    "Groq returned malformed MCP tool-call arguments before the database query ran. "
                    "The app retries this once with simpler SQL instructions; if it happens again, try the same question once more."
                )
            elif "syntaxerror" in detail_text or "typeerror" in detail_text or "sandbox" in detail_text or "syntax repair" in detail_text:
                st.info(
                    "This is a Code Mode JavaScript execution issue, not a MySQL setup error. The app now retries a generated "
                    "workflow when QuickJS reports a syntax or type error; inspect the workflow trace if it still fails."
                )
            elif "did not provide executable javascript" in detail_text:
                st.info(
                    "Groq returned no runnable JavaScript on either attempt. Open the workflow trace below to see "
                    "both responses and their finish reasons."
                )
            else:
                st.info(
                    "Check that the project `.env` has valid local Groq/MySQL settings and that the MySQL service is running. "
                    "For the SLA example, confirm the database was initialized with `init_db.py`."
                )
            if trace.getvalue():
                st.session_state["comparison_trace"] = trace.getvalue()
            with st.expander("Technical error details", expanded=True):
                st.code("\n".join(details), language="text")
            with st.expander("Workflow trace captured before the failure", expanded=False):
                if trace.getvalue():
                    st.code(trace.getvalue(), language="text")
                else:
                    st.warning(
                        "No trace was captured. The Streamlit process may still be running an older version; "
                        "stop it and start it again to load the current code."
                    )

    result = st.session_state.get("comparison_result")
    if not result:
        st.info("Enter a question above to see both answers and compare token usage.")
        return

    st.caption(f"Query: {st.session_state.get('comparison_query', '')}")
    mcp_column, code_column = st.columns(2, gap="large")
    with mcp_column:
        st.subheader("MCP response")
        show_response(result["mcp_answer"])
    with code_column:
        st.subheader("Code Mode response")
        show_response(result["code_answer"])

    mcp_stats = result["mcp_stats"]
    code_stats = result["code_stats"]
    mcp_total = total_tokens(mcp_stats)
    code_total = total_tokens(code_stats)
    difference = mcp_total - code_total
    percent = (difference / mcp_total * 100) if mcp_total else 0.0

    st.divider()
    st.subheader("Token usage and savings")
    usage_a, usage_b, usage_c = st.columns(3)
    usage_a.metric("MCP tokens", f"{mcp_total:,}")
    usage_b.metric("Code Mode tokens", f"{code_total:,}")
    if difference > 0:
        usage_c.metric("Tokens saved with Code Mode", f"{difference:,}", f"{percent:.1f}% fewer")
    elif difference < 0:
        usage_c.metric("Code Mode used more", f"{abs(difference):,}", f"{abs(percent):.1f}% more")
    else:
        usage_c.metric("Token difference", "0", "Same usage")

    usage_table = pd.DataFrame(
        {
            "Metric": ["Prompt tokens", "Completion tokens", "Total tokens", "Groq API requests", "MCP tool calls", "SQL queries", "Query rows fetched", "Query-result characters sent to Groq"],
            "MCP": [
                mcp_stats["prompt_tokens"], mcp_stats["completion_tokens"], mcp_total,
                mcp_stats["llm_requests"], mcp_stats["mcp_tool_calls"], mcp_stats["sql_queries"],
                mcp_stats["query_result_rows"], mcp_stats["query_result_chars_sent_to_groq"],
            ],
            "Code Mode": [
                code_stats["prompt_tokens"], code_stats["completion_tokens"], code_total,
                code_stats["llm_requests"], code_stats["mcp_tool_calls"], code_stats["sql_queries"],
                code_stats["query_result_rows"], code_stats["query_result_chars_sent_to_groq"],
            ],
        }
    )
    st.dataframe(usage_table, hide_index=True, use_container_width=True)
    st.caption(
        "Token totals are prompt + completion tokens reported by Groq. Both paths use the same MCP server and SQL result. "
        "The MCP workflow sends query results to Groq for its final answer; Code Mode keeps those rows in the sandbox. "
        "The character count is a payload-size indicator, not a token count. Results vary by model and query."
    )

    with st.expander("Workflow and MCP tool-call trace"):
        st.code(st.session_state.get("comparison_trace", "No trace captured."), language="text")


if __name__ == "__main__":
    main()
