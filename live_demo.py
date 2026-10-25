"""Compare Groq token usage for MCP tool calling and a Code Mode workflow."""

import asyncio
import getpass
import json
import os
import re
import sys
import traceback
from pathlib import Path
from typing import Any

from groq import AsyncGroq
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from dotenv import load_dotenv
from local_code_sandbox import execute_code


ROOT = Path(__file__).resolve().parent
SERVER = ROOT / "mcp_server.py"
load_dotenv(ROOT / ".env")
MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b")
_groq_client: AsyncGroq | None = None
SYSTEM = (
    "You answer questions about customer-support tickets. Use the schema and the user's natural-language request "
    "to select or summarize the matching ticket records. After receiving query results, answer from those results, "
    "preserve requested filters, and do not invent recommendations. Prefer a compact table or concise aggregate."
)
DEFAULT_QUERY = (
    "Triage the open support queue. Show urgent/high-priority, security-related, "
    "or data-loss tickets with a short recommended next action."
)


async def close_groq_client() -> None:
    """Close the per-run async HTTP client so UI reruns can use a new event loop."""
    global _groq_client
    if _groq_client is not None:
        await _groq_client.close()
        _groq_client = None


def format_query_rows(rows: list[Any]) -> str:
    """Render flat results and multiple named result groups as one table."""
    if not rows:
        return "No tickets matched your request."
    normalized: list[dict[str, Any]] = []
    for index, row in enumerate(rows):
        if isinstance(row, list):
            group = "Summary" if index == 0 else f"Result group {index + 1}"
            for item in row:
                if isinstance(item, dict):
                    normalized.append({"result_group": group, **item})
                else:
                    normalized.append({"result_group": group, "value": item})
        elif isinstance(row, dict):
            normalized.append(row)
        else:
            normalized.append({"value": row})
    if not normalized:
        return "No tickets matched your request."
    rows = normalized
    columns = list(dict.fromkeys(column for row in rows for column in row))
    header = "| " + " | ".join(columns) + " |"
    separator = "| " + " | ".join(["---"] * len(columns)) + " |"
    body = [
        "| " + " | ".join(str(row.get(column, "")).replace("|", "\\|").replace("\n", " ") for column in columns) + " |"
        for row in rows
    ]
    return f"Query results ({len(rows)} rows):\n\n" + "\n".join([header, separator, *body])


async def groq_chat(
    messages: list[dict[str, Any]],
    tools: list[dict[str, Any]],
    tool_choice: Any = "auto",
) -> dict[str, Any]:
    global _groq_client
    key = os.getenv("GROQ_API_KEY")
    if not key:
        raise RuntimeError("GROQ_API_KEY is not set. Add it to .env and run again.")
    if _groq_client is None:
        _groq_client = AsyncGroq(api_key=key)
    payload = {
        "model": MODEL,
        "messages": messages,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice
        payload["parallel_tool_calls"] = False
    try:
        completion = await _groq_client.chat.completions.create(**payload)
        return completion.model_dump()
    except Exception as error:
        status_code = getattr(error, "status_code", None)
        response = getattr(error, "response", None)
        detail = response.text if response is not None else str(error)
        if response is not None:
            ray_id = response.headers.get("cf-ray")
            if ray_id:
                detail += f"\nCloudflare Ray ID: {ray_id}"
        if "1010" in detail:
            detail += "\nCloudflare 1010 is an access block at Groq's edge, not an MCP/query error. If it persists with the Groq SDK, contact Groq support and include the Ray ID."
        if status_code:
            raise RuntimeError(f"Groq API returned HTTP {status_code}: {detail}") from error
        raise RuntimeError(f"Groq request failed: {detail}") from error


def add_usage(stats: dict[str, int], response: dict[str, Any]) -> None:
    usage = response.get("usage") or {}
    stats["prompt_tokens"] += int(usage.get("prompt_tokens", 0))
    stats["completion_tokens"] += int(usage.get("completion_tokens", 0))
    stats["llm_requests"] += 1


def assistant_message_for_history(message: dict[str, Any]) -> dict[str, Any]:
    """Keep only chat-completion fields Groq accepts on a follow-up request."""
    clean: dict[str, Any] = {"role": "assistant"}
    if message.get("content") is not None:
        clean["content"] = message["content"]
    tool_calls = message.get("tool_calls") or []
    if tool_calls:
        clean["tool_calls"] = [
            {
                "id": call["id"],
                "type": "function",
                "function": {
                    "name": call["function"]["name"],
                    "arguments": call["function"].get("arguments") or "{}",
                },
            }
            for call in tool_calls
        ]
    return clean


def mcp_tools_as_groq(tools: list[Any]) -> list[dict[str, Any]]:
    return [{
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description or "",
            "parameters": tool.inputSchema,
        },
    } for tool in tools]


def tool_text(result: Any) -> str:
    # The MCP demo tools return their payload as JSON text content.
    return "\n".join(block.text for block in result.content if getattr(block, "text", None))


async def call_mcp(session: ClientSession, name: str, arguments: dict[str, Any], stats: dict[str, int]) -> str:
    result = await session.call_tool(name, arguments)
    stats["mcp_tool_calls"] += 1
    if name == "run_ticket_query":
        stats["sql_queries"] += 1
    if result.isError:
        raise RuntimeError(f"MCP tool {name} failed: {tool_text(result)}")
    return tool_text(result)


async def run_mcp_style(session: ClientSession, tool_defs: list[dict[str, Any]], query: str) -> tuple[str, dict[str, int]]:
    stats = {"prompt_tokens": 0, "completion_tokens": 0, "llm_requests": 0, "mcp_tool_calls": 0, "sql_queries": 0,
             "query_result_rows": 0, "query_result_chars": 0, "query_result_chars_sent_to_groq": 0}
    query_executed = False
    malformed_tool_retry_used = False
    query_rows: list[dict[str, str]] = []
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content": query + " First call get_ticket_schema. Then use run_ticket_query with a single-line MySQL SELECT. For a request asking for counts, summaries, groupings, or rankings, use a compact GROUP BY query with COUNT and only the requested grouping columns. For top-N aggregate results, return the grouping keys and counts so you can rank them in the final answer; order by the aggregate expression, not its alias. For top-N ticket-list requests without an explicit sort criterion, order by created_at DESC (newest first) before LIMIT so the result is deterministic. Do not use subqueries, CTEs, JSON functions, or nested SELECTs. For ticket-list requests, select exactly ticket_id, customer, topic, priority, status, sentiment, customer_tier, response_hours, created_at. Preserve all user-requested filters."},
    ]
    print("\n[MCP path] Asking Groq to call MCP tools; watch this trace:")
    for _ in range(10):
        try:
            response = await groq_chat(messages, tool_defs)
        except RuntimeError as error:
            if "failed to parse tool call arguments as json" not in str(error).lower() or malformed_tool_retry_used:
                raise
            malformed_tool_retry_used = True
            print("  Groq returned malformed MCP tool-call JSON; retrying once with stricter SQL formatting instructions.")
            messages.append({
                "role": "user",
                "content": "Your last tool-call arguments were not valid JSON. Retry with a valid run_ticket_query tool call. Put the entire SQL statement on one line. Use ordinary SELECT/JOIN/WHERE/GROUP BY only as needed; do not use subqueries, CTEs, or JSON functions. For count or summary requests, use COUNT with GROUP BY so the query result stays compact.",
            })
            continue
        add_usage(stats, response)
        assistant_message = response["choices"][0]["message"]
        tool_calls = assistant_message.get("tool_calls") or []
        if not tool_calls:
            if not query_executed:
                messages.append(assistant_message_for_history(assistant_message))
                messages.append({"role": "user", "content": "Use run_ticket_query to execute a schema-grounded SELECT before answering. Preserve the user's full intent and all necessary filters."})
                continue
            content = assistant_message.get("content") or ""
            return (content.strip() or format_query_rows(query_rows)), stats
        messages.append(assistant_message_for_history(assistant_message))
        for call in tool_calls:
            name = call["function"]["name"]
            args = json.loads(call["function"].get("arguments") or "{}")
            print(f"  Groq requested MCP tool: {name}({json.dumps(args)})")
            tool_succeeded = True
            try:
                output = await call_mcp(session, name, args, stats)
            except RuntimeError as error:
                tool_succeeded = False
                output = f"Query/tool error: {error}. Correct the SQL and retry, preserving every filter needed to answer the user's request. Do not silently broaden or narrow the requested results."
                print(f"  {output}")
            print(f"  MCP returned {len(output)} characters")
            if name == "run_ticket_query" and tool_succeeded:
                query_executed = True
                stats["query_result_chars_sent_to_groq"] += len(output)
                try:
                    parsed_rows = json.loads(output)
                    if isinstance(parsed_rows, list):
                        query_rows = parsed_rows
                        stats["query_result_rows"] += len(parsed_rows)
                        stats["query_result_chars"] += len(output)
                except json.JSONDecodeError:
                    pass
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": output})
    raise RuntimeError("MCP path exceeded 10 model/tool turns; stop rather than report incomplete usage.")


async def run_code_mode(session: ClientSession, query: str) -> tuple[str, dict[str, int]]:
    stats = {"prompt_tokens": 0, "completion_tokens": 0, "llm_requests": 0, "mcp_tool_calls": 0, "sql_queries": 0,
             "query_result_rows": 0, "query_result_chars": 0, "query_result_chars_sent_to_groq": 0}
    # The local agent retrieves domain schema through MCP. Groq writes source
    # text; QuickJS executes it locally and reaches MCP only through RPC.
    schema = await call_mcp(session, "get_ticket_schema", {}, stats)
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": (
                SYSTEM
                + "\n\nYou are writing code, not calling tools directly. Output only valid synchronous JavaScript, with no prose and no markdown fences. "
                "Use conservative ES5 syntax only: var declarations, ordinary function expressions, arrays, objects, loops, and if statements. "
                "Do not use arrow functions, template strings, destructuring, optional chaining, async/await, or newer syntax. "
                "Return exactly one complete IIFE expression. Assign its final answer to a variable and explicitly `return` that variable; "
                "the returned value must always be a JSON-serializable flat array of objects (return [] when there are no matches). "
                "If the request asks for multiple sections, add a `result_group` field to each object instead of returning nested arrays. "
                "Call the provided mcp.call RPC method with 'run_ticket_query' and an argument object {sql: '...'} containing a read-only MySQL SELECT. "
                "The query result is a JSON string. Parse it with JSON.parse and do filtering, grouping, or aggregation in JavaScript "
                "when the request asks for a summary; for summary/count/group/ranking requests, fetch flat rows with simple SELECT/JOIN/WHERE, "
                "avoid SQL aggregation and subqueries, then return only compact computed results. Do not invent anything. "
                "For top-N ticket lists with no ranking criterion, query newest first using ORDER BY created_at DESC before LIMIT. "
                "For ticket-list output, include exactly ticket_id, customer, topic, priority, status, sentiment, customer_tier, response_hours, and created_at. "
                "Example structure (replace the query and computation to match the user's request): (function(){ var raw = mcp.call('run_ticket_query', {sql: 'SELECT ...'}); var rows = JSON.parse(raw); var answer = rows; return answer; })() "
                "The local sandbox has no filesystem, environment, network, or database credentials.\n\n"
                "Database schema returned by the MCP server:\n"
                + schema
            ),
        },
        {
            "role": "user",
            "content": query + " Write code that answers the complete natural-language request. Translate semantic wording to the stored values described by the schema; do not add filters the request did not imply.",
        },
    ]
    # Groq returns source as plain text so multiline JS is not embedded in
    # function-call JSON arguments (which this Groq/model combination rejected).
    def extract_code(response: dict[str, Any]) -> str:
        message = response["choices"][0]["message"]
        for call in message.get("tool_calls") or []:
            if call.get("function", {}).get("name") == "execute_code":
                args = json.loads(call["function"].get("arguments") or "{}")
                candidate = args.get("code", "").strip()
                if candidate:
                    return candidate
        content = message.get("content") or ""
        if content.lstrip().startswith("{"):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and isinstance(parsed.get("code"), str):
                    content = parsed["code"]
            except json.JSONDecodeError:
                pass
        fenced = re.search(r"```(?:javascript|js)?\s*(.*?)```", content, flags=re.IGNORECASE | re.DOTALL)
        if fenced:
            return fenced.group(1).strip()
        start = content.find("(() =>")
        if start >= 0:
            return content[start:].strip()
        # Models sometimes omit the IIFE wrapper and return a plain script in
        # message content. Preserve that code so the sandbox can execute it as
        # a function body (and reject it there if it is still invalid).
        if "mcp.call" in content:
            starts = [index for marker in ("const ", "let ", "var ", "return ", "mcp.call(", "function ", "(function") if (index := content.find(marker)) >= 0]
            return content[min(starts):].strip() if starts else content
        return ""

    response = await groq_chat(messages, [])
    add_usage(stats, response)
    code = extract_code(response)
    if not code or "mcp.call" not in code:
        first_message = response["choices"][0]["message"]
        first_content = first_message.get("content") or ""
        print("  Groq's first response did not contain executable JavaScript:")
        print(f"  finish_reason={response['choices'][0].get('finish_reason')!r}; content_chars={len(first_content)}")
        print("  " + (first_content[:4_000].replace("\n", "\n  ") or "<empty response>"))
        if first_message.get("tool_calls"):
            print("  tool_calls=" + json.dumps(first_message["tool_calls"], ensure_ascii=False)[:2_000])
        retry_messages = messages + [
            {"role": "assistant", "content": first_content or "I did not provide executable code."},
            {
                "role": "user",
                "content": (
                    "Your last response did not contain executable JavaScript. Try once more. Output only one complete "
                    "JavaScript IIFE expression, with no prose or markdown. It must call mcp.call('run_ticket_query', {sql: '...'}), "
                    "parse that JSON string, and return an array of compact results that answers my request. "
                    "For count/group/ranking requests, fetch flat rows and aggregate them in JavaScript. "
                    "Use only the read-only tables and columns in the supplied schema."
                ),
            },
        ]
        retry_response = await groq_chat(retry_messages, [])
        add_usage(stats, retry_response)
        code = extract_code(retry_response)
        retry_message = retry_response["choices"][0]["message"]
        if not code or "mcp.call" not in code:
            retry_content = retry_message.get("content") or ""
            print("  Groq's regeneration response also lacked executable JavaScript:")
            print(f"  finish_reason={retry_response['choices'][0].get('finish_reason')!r}; content_chars={len(retry_content)}")
            print("  " + (retry_content[:4_000].replace("\n", "\n  ") or "<empty response>"))
            if retry_message.get("tool_calls"):
                print("  tool_calls=" + json.dumps(retry_message["tool_calls"], ensure_ascii=False)[:2_000])
    if not code or "mcp.call" not in code:
        raise RuntimeError(
            "Groq did not provide executable JavaScript after one regeneration attempt. "
            "Inspect the workflow trace for both model responses and finish reasons."
        )
    print("\n[Code Mode path] Groq wrote JavaScript for the local QuickJS sandbox:")
    print("  " + code.replace("\n", "\n  "))

    async def mcp_rpc(name: str, arguments: dict[str, Any]) -> str:
        output = await call_mcp(session, name, arguments, stats)
        if name == "run_ticket_query":
            stats["query_result_chars"] += len(output)
            try:
                query_rows = json.loads(output)
                if isinstance(query_rows, list):
                    stats["query_result_rows"] += len(query_rows)
            except json.JSONDecodeError:
                pass
        return output

    for repair_attempt in range(3):
        try:
            rows = await execute_code(code, mcp_rpc)
            break
        except RuntimeError as error:
            # Generated JavaScript can fail either while parsing or while
            # running (for example, calling a value that is not a function).
            # Allow at most two counted repair calls for either failure.
            error_text = str(error)
            if not any(kind in error_text for kind in ("SyntaxError", "TypeError")) or repair_attempt == 2:
                raise
            print(f"  QuickJS rejected generated JavaScript at runtime; requesting repair {repair_attempt + 1}/2.")
            print("  Failed code: " + code.replace("\n", "\n  "))
            repair_messages = messages + [
                {
                    "role": "assistant",
                    "content": "The JavaScript below failed in the QuickJS sandbox. Repair the specific error while preserving the user's query intent and SQL filters. Return one complete synchronous JavaScript IIFE expression using conservative ES5 syntax only (var, ordinary functions, arrays, objects, loops, and if statements). Do not use arrow functions, template strings, destructuring, optional chaining, async/await, or newer syntax. Use only mcp.call('run_ticket_query', {sql: '...'}), parse its JSON string result, and return an array. No prose or markdown.\n\n"
                    + code,
                },
                {"role": "user", "content": f"QuickJS execution error: {error}. Return corrected executable JavaScript."},
            ]
            repair_response = await groq_chat(repair_messages, [])
            add_usage(stats, repair_response)
            repaired_code = extract_code(repair_response)
            if not repaired_code or "mcp.call" not in repaired_code:
                raise RuntimeError("Groq did not return an executable workflow during the syntax repair.") from error
            code = repaired_code
    else:
        raise RuntimeError("Code Mode did not complete the sandbox workflow.")
    if not isinstance(rows, list):
        raise RuntimeError("Sandbox code must return a list of ticket rows.")
    print(f"  QuickJS child process called MCP through the RPC bridge and returned {len(rows)} result item(s)")
    print(f"  Query-result characters forwarded to Groq: {stats['query_result_chars_sent_to_groq']}")
    print("  Ticket rows stayed local; Groq received the code-generation request, not the query result")
    return format_query_rows(rows), stats


def print_comparison(mcp_stats: dict[str, int], code_stats: dict[str, int]) -> None:
    print("\nACTUAL GROQ USAGE (sum across every request in each path)")
    print(f"{'Metric':<30} {'MCP path':>12} {'Code Mode':>12}")
    print("-" * 56)
    for label, key in [
        ("Prompt tokens", "prompt_tokens"),
        ("Completion tokens", "completion_tokens"),
        ("Total tokens", None),
        ("Groq API requests", "llm_requests"),
        ("MCP tool calls", "mcp_tool_calls"),
        ("SQL queries", "sql_queries"),
        ("Query rows fetched", "query_result_rows"),
        ("Query-result characters sent to Groq", "query_result_chars_sent_to_groq"),
    ]:
        left = mcp_stats["prompt_tokens"] + mcp_stats["completion_tokens"] if key is None else mcp_stats[key]
        right = code_stats["prompt_tokens"] + code_stats["completion_tokens"] if key is None else code_stats[key]
        print(f"{label:<30} {left:>12} {right:>12}")
    mcp_total = mcp_stats["prompt_tokens"] + mcp_stats["completion_tokens"]
    code_total = code_stats["prompt_tokens"] + code_stats["completion_tokens"]
    if mcp_total:
        saved = mcp_total - code_total
        print(f"\nToken difference: {saved:+d} tokens ({saved / mcp_total * 100:+.1f}% vs MCP path)")
    print("Token counts come from Groq's usage fields. Results vary by model and response wording.")


async def async_main() -> None:
    if not os.getenv("GROQ_API_KEY"):
        os.environ["GROQ_API_KEY"] = getpass.getpass("Groq API key (input hidden): ").strip()
    if not os.getenv("GROQ_API_KEY"):
        raise RuntimeError("No Groq API key was entered.")
    query = input(f"\nAsk about the support tickets (Enter for default: {DEFAULT_QUERY})\n> ").strip()
    if not query:
        query = DEFAULT_QUERY
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(ROOT))
    print("Starting local MCP server ...", flush=True)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            print("Connecting to MCP server ...", flush=True)
            await asyncio.wait_for(session.initialize(), timeout=20)
            print("MCP handshake complete.", flush=True)
            available = await session.list_tools()
            tool_defs = mcp_tools_as_groq(available.tools)
            print(f"Model: {MODEL}\nReal Groq requests; local MCP server over stdio.", flush=True)
            mcp_answer, mcp_stats = await run_mcp_style(session, tool_defs, query)
            print("\nMCP path answer:\n" + mcp_answer)
            code_answer, code_stats = await run_code_mode(session, query)
            print("\nCode Mode answer:\n" + code_answer)
            print_comparison(mcp_stats, code_stats)


async def check_mcp() -> None:
    """Check the local MCP server without making a Groq API request."""
    params = StdioServerParameters(command=sys.executable, args=[str(SERVER)], cwd=str(ROOT))
    print(f"Starting local MCP server with {sys.executable} ...", flush=True)
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            print("Connecting to MCP server ...", flush=True)
            await asyncio.wait_for(session.initialize(), timeout=20)
            print("MCP handshake complete.", flush=True)
            tools = await session.list_tools()
            print("MCP connection succeeded. Tools:", flush=True)
            for tool in tools.tools:
                print(f"- {tool.name}: {tool.description}")
            stats = {"mcp_tool_calls": 0, "sql_queries": 0}
            schema = await call_mcp(session, "get_ticket_schema", {}, stats)
            print(f"MCP schema tool returned {len(schema)} characters.")
            rows = await call_mcp(
                session,
                "run_ticket_query",
                {"sql": "SELECT ticket_id, status FROM tickets ORDER BY ticket_id LIMIT 3"},
                stats,
            )
            print(f"Sample query result: {rows}")


if __name__ == "__main__":
    try:
        if "--check-mcp" in sys.argv:
            asyncio.run(check_mcp())
        else:
            asyncio.run(async_main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except Exception as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        raise SystemExit(1)
