"""Schema and read-only SQL tools backed by the shared MySQL ticket database."""

import json

from mcp.server.fastmcp import FastMCP

from ticket_database import TICKET_SCHEMA, execute_ticket_query


mcp = FastMCP("support-ticket-demo")


@mcp.tool()
def get_ticket_schema() -> str:
    """Return the ticket table schema and the allowed query scope."""
    return TICKET_SCHEMA


@mcp.tool()
def run_ticket_query(sql: str) -> str:
    """Run one read-only MySQL SELECT across the whitelisted ticket, customer, and SLA tables."""
    return json.dumps(execute_ticket_query(sql), ensure_ascii=False)


if __name__ == "__main__":
    mcp.run()
