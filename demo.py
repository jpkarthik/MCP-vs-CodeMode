"""Small, dependency-free MCP vs Code Mode showcase.

This is an illustrative simulation, not a benchmark of a live MCP server.
Both paths read the same mock ticket data. The difference is how much work
and intermediate data must pass through the model-facing interface.
"""

from dataclasses import dataclass
from typing import Iterable


@dataclass(frozen=True)
class Ticket:
    id: str
    customer: str
    topic: str
    priority: str
    status: str
    sentiment: str
    latest_message: str


TICKETS = [
    Ticket("T-1041", "Northwind", "billing", "high", "open", "frustrated", "I was charged twice. Please fix this today."),
    Ticket("T-1042", "Contoso", "how-to", "low", "open", "neutral", "Where can I change my profile photo?"),
    Ticket("T-1043", "Fabrikam", "outage", "urgent", "open", "angry", "Our whole team cannot sign in."),
    Ticket("T-1044", "Adventure Works", "billing", "medium", "pending", "neutral", "Can you send me the invoice again?"),
    Ticket("T-1045", "Tailspin", "data-loss", "urgent", "open", "worried", "A folder disappeared after the sync."),
    Ticket("T-1046", "Northwind", "how-to", "low", "closed", "positive", "Thanks, that worked."),
    Ticket("T-1047", "Litware", "outage", "high", "open", "frustrated", "Reports are timing out for everyone."),
    Ticket("T-1048", "Contoso", "billing", "low", "open", "neutral", "How do I update our card?"),
    Ticket("T-1049", "Wingtip", "security", "urgent", "open", "concerned", "I saw a login from a country I don't recognize."),
    Ticket("T-1050", "Fabrikam", "how-to", "medium", "pending", "neutral", "How do I invite another admin?"),
]


def list_open_tickets() -> list[str]:
    """Mock MCP tool: return IDs for every open ticket."""
    return [ticket.id for ticket in TICKETS if ticket.status == "open"]


def get_ticket_details(ticket_id: str) -> Ticket:
    """Mock MCP tool: fetch one ticket's details."""
    return next(ticket for ticket in TICKETS if ticket.id == ticket_id)


def make_triage_row(ticket: Ticket) -> dict[str, str]:
    return {
        "id": ticket.id,
        "customer": ticket.customer,
        "topic": ticket.topic,
        "priority": ticket.priority,
        "next_step": next_step(ticket),
    }


def next_step(ticket: Ticket) -> str:
    if ticket.topic == "security":
        return "Escalate to security now"
    if ticket.topic == "data-loss":
        return "Escalate to data recovery"
    if ticket.topic == "outage":
        return "Link to incident and notify on-call"
    if ticket.topic == "billing" and ticket.priority in {"high", "urgent"}:
        return "Check duplicate charge and contact customer"
    return "Reply using the relevant help article"


def mcp_style_triage() -> tuple[list[dict[str, str]], dict[str, int]]:
    """Model-visible tool-call style: list, then inspect each ticket in turn."""
    calls = {"list_open_tickets": 1, "get_ticket_details": 0}
    visible_payloads = list_open_tickets()
    calls["get_ticket_details"] += len(visible_payloads)
    detailed_tickets = [get_ticket_details(ticket_id) for ticket_id in visible_payloads]
    # In this style, the model receives the list result and each details result.
    visible_chars = sum(len(repr(ticket_id)) for ticket_id in visible_payloads) + sum(len(repr(ticket)) for ticket in detailed_tickets)
    actionable = [ticket for ticket in detailed_tickets if ticket.priority in {"high", "urgent"} or ticket.topic in {"security", "data-loss"}]
    result = sorted(
        (make_triage_row(ticket) for ticket in actionable),
        key=lambda row: (row["priority"] != "urgent", row["priority"] != "high", row["id"]),
    )
    return result, {
        "model_visible_tool_calls": sum(calls.values()),
        "backend_tool_operations": sum(calls.values()),
        "intermediate_chars_to_model": visible_chars,
    }


def code_mode_triage() -> tuple[list[dict[str, str]], dict[str, int]]:
    """Code Mode style: run the filtering and summarizing logic beside tools."""
    calls = {"list_open_tickets": 1, "get_ticket_details": 0}
    ticket_ids = list_open_tickets()
    calls["get_ticket_details"] += len(ticket_ids)
    tickets = [get_ticket_details(ticket_id) for ticket_id in ticket_ids]
    # Here the code filters and reduces the records before returning to the model.
    actionable = [ticket for ticket in tickets if ticket.priority in {"high", "urgent"} or ticket.topic in {"security", "data-loss"}]
    rows = [make_triage_row(ticket) for ticket in actionable]
    rows.sort(key=lambda row: (row["priority"] != "urgent", row["priority"] != "high", row["id"]))
    return rows, {
        "model_visible_tool_calls": 1,
        "backend_tool_operations": sum(calls.values()),
        "intermediate_chars_to_model": len(repr(rows)),
    }


def print_table(rows: Iterable[dict[str, str]]) -> None:
    print(f"{'ID':<8} {'CUSTOMER':<18} {'TOPIC':<12} {'PRIORITY':<9} NEXT STEP")
    print("-" * 96)
    for row in rows:
        print(f"{row['id']:<8} {row['customer']:<18} {row['topic']:<12} {row['priority']:<9} {row['next_step']}")


def main() -> None:
    mcp_rows, mcp_stats = mcp_style_triage()
    code_rows, code_stats = code_mode_triage()

    print("SUPPORT QUEUE TRIAGE — illustrative simulation\n")
    print("MCP-style result: the model inspects each open ticket")
    print_table(mcp_rows)
    print("\nCode Mode result: code filters and summarizes before returning")
    print_table(code_rows)
    print("\nCOMPARISON (same mock data; not a live performance benchmark)")
    print(f"{'Metric':<34} {'MCP-style':>12} {'Code Mode':>12}")
    print("-" * 60)
    for label, key in [
        ("Model-visible tool calls", "model_visible_tool_calls"),
        ("Backend tool operations", "backend_tool_operations"),
        ("Intermediate chars returned", "intermediate_chars_to_model"),
    ]:
        print(f"{label:<34} {mcp_stats[key]:>12} {code_stats[key]:>12}")
    print("\nThe triage lists should match. Code Mode keeps routine records and intermediate results")
    print("inside the execution, then returns only the actionable summary to the model.")


if __name__ == "__main__":
    main()
