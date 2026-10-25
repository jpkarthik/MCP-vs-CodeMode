# MCP vs Code Mode token showcase

For the full architecture, code walkthrough, setup, and result interpretation, see [PROJECT_DOCUMENTATION.md](PROJECT_DOCUMENTATION.md). Copy `.env.example` to `.env` and configure your local Groq and MySQL credentials before running the live demo.

This support-ticket demo compares two workflows against the same MySQL database:

- `demo.py` is the original no-network illustration.
- `live_demo.py` makes real Groq API requests and reports actual prompt and completion token usage.

Both paths use the same local MCP server and MySQL database. In the MCP path, Groq sees the MCP tools, requests the schema and query, then receives query results for its final response. In the Code Mode path, Groq returns JavaScript source as plain text; the app executes it in a separate QuickJS process with no inherited Groq/MySQL credentials, filesystem API, or network API. Its only host capability is an allowlisted RPC call to the read-only MCP query tool. Ticket rows return to the local sandbox for filtering/aggregation and display without being sent back to Groq. SQLGlot validates each statement; the database layer allows one read-only SELECT across the demo's three whitelisted tables, with a 1,000-row cap.

This local Code Mode demonstration uses model-written JavaScript, a QuickJS child process, and an RPC bridge to MCP. It is not Cloudflare's Dynamic Worker sandbox. The child process runs under the same Windows user account, so this is a restricted local runtime for a demo, not a hardened OS security boundary. Do not expose the app, MCP server, or MySQL port publicly.

## Set up MySQL and run (Windows PowerShell)

Use a virtual environment so the demo's packages don't change dependencies used by other Python projects. From the screenshot, your Anaconda interpreter appears to be `D:\Anaconda\envs\AIML\python.exe`:

```powershell
cd D:\000_MCPvsCodeMode
& "D:\Anaconda\envs\AIML\python.exe" -m venv .venv
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
```

Open `.env` and set `MYSQL_PASSWORD` to the password for your local MySQL user. Defaults are host `127.0.0.1`, port `3306`, user `root`, and database `mcp_codemode_demo`; update them if your setup differs. Also set `GROQ_API_KEY` there. The `.env` file is excluded by `.gitignore` and loaded automatically. The MySQL account needs permission to create a database and table.

Initialize the database and seed sample ticket, customer-tier, and SLA rows:

```powershell
& ".\.venv\Scripts\python.exe" init_db.py
```

The setup creates `tickets`, `customers`, and `sla_policies` if they do not exist. It inserts only missing sample IDs and leaves existing rows untouched. SLA targets and customer tiers are illustrative demo values, not real contracts. Five additional tickets have timestamps relative to initialization so the SLA query has both breached and non-breached examples. Initialization also adds 500 deterministic, clearly labeled synthetic tickets (`SYN-*`) and 20 demo-only customers, inserted idempotently. No real customer data is used for these rows. The read-only query tool permits up to 1,000 rows for this larger workload.

Run the comparison:

```powershell
& ".\.venv\Scripts\python.exe" live_demo.py
```

At startup, the program asks what you want to know about the tickets. The same query goes through both paths. Press Enter to use the sample triage query.

The default Groq model is `openai/gpt-oss-120b`. If it is unavailable for your account, set `GROQ_MODEL` in PowerShell to another supported tool-calling model.

Example multi-condition query:

```text
Show unresolved urgent or high-priority tickets for enterprise customers that have exceeded their response SLA.
```

The model should join tickets to customer tiers and SLA policies, keep only open or pending high/urgent enterprise tickets, and compare each ticket's age with that policy's response target.

Example bulk-processing query for comparing row forwarding with local Code Mode processing:

```text
Across all open and pending tickets, summarize the count by customer tier, priority, and sentiment. Include up to three example ticket IDs for each group.
```

The MCP path sends the retrieved rows to Groq for the final summary. Code Mode can retrieve the same rows through MCP, calculate groups and example IDs inside QuickJS, and return only the compact summary. The UI reports actual Groq tokens, query rows fetched, and the characters of query results forwarded to Groq. Re-run `init_db.py` to add the synthetic rows to an existing local database; `INSERT IGNORE` avoids duplicates.

## Run the Streamlit UI

Install the updated requirements if you already created the virtual environment, then start the app:

```powershell
& ".\.venv\Scripts\python.exe" -m pip install -r requirements.txt
& ".\.venv\Scripts\python.exe" -m streamlit run streamlit_app.py
```

The page shows the MCP and Code Mode answers side by side, actual Groq prompt/completion token counts, total token difference, request and query counts, and an expandable workflow/MCP trace. The `.env` file is loaded from the project folder as it is for the command-line demo.

## What it compares

**MCP path:** Groq sees `get_ticket_schema` and `run_ticket_query` as MCP tools. It reads the schema, constructs SQL from the user's natural-language question, and asks the MCP server to execute it.

**Code Mode path:** Local Python fetches the schema through MCP, asks Groq for one JavaScript workflow, and executes that code in QuickJS. The sandbox calls MCP's `run_ticket_query` through a restricted RPC bridge. The database password remains in the local MCP server process; the sandbox cannot read it. Returned rows stay local and are formatted without another Groq request.

The summary reports Groq prompt/completion tokens and request counts, MCP tool calls, and SQL query counts. The Code Mode path calls the same MCP server through local RPC; its returned ticket rows are not added to a later Groq prompt. Token usage varies by model and response wording. The live version makes real Groq API calls, which may count against Groq limits or billing. Only the user question, database schema, and generated code are sent to Groq; credentials and query-result rows are not.

If the MCP connection needs a local check before calling Groq, run:

```powershell
& ".\.venv\Scripts\python.exe" live_demo.py --check-mcp
```

`requirements.txt` includes the official MCP Python SDK, Groq Python SDK, MySQL Connector/Python, and SQLGlot for parsing generated SQL.
