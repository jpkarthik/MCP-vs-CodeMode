"""Shared MySQL access used by the MCP server and the Code Mode workflow."""

import os
import re
from pathlib import Path
from typing import Any

import mysql.connector
from dotenv import load_dotenv
from sqlglot import exp, parse


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

DB_NAME = os.getenv("MYSQL_DATABASE", "mcp_codemode_demo")
DB_HOST = os.getenv("MYSQL_HOST", "127.0.0.1")
DB_PORT = int(os.getenv("MYSQL_PORT", "3306"))
DB_USER = os.getenv("MYSQL_USER", "root")
DB_PASSWORD = os.getenv("MYSQL_PASSWORD", "")

if not re.fullmatch(r"[A-Za-z0-9_]+", DB_NAME):
    raise ValueError("MYSQL_DATABASE may contain only letters, digits, and underscores.")


SEED_TICKETS = [
    ("T-1041", "Northwind", "billing", "high", "open", "frustrated", "I was charged twice. Please fix this today."),
    ("T-1042", "Contoso", "how-to", "low", "open", "neutral", "Where can I change my profile photo?"),
    ("T-1043", "Fabrikam", "outage", "urgent", "open", "angry", "Our whole team cannot sign in."),
    ("T-1044", "Adventure Works", "billing", "medium", "pending", "neutral", "Can you send me the invoice again?"),
    ("T-1045", "Tailspin", "data-loss", "urgent", "open", "worried", "A folder disappeared after the sync."),
    ("T-1046", "Northwind", "how-to", "low", "closed", "positive", "Thanks, that worked."),
    ("T-1047", "Litware", "outage", "high", "open", "frustrated", "Reports are timing out for everyone."),
    ("T-1048", "Contoso", "billing", "low", "open", "neutral", "How do I update our card?"),
    ("T-1049", "Wingtip", "security", "urgent", "open", "concerned", "I saw a login from a country I don't recognize."),
    ("T-1050", "Fabrikam", "how-to", "medium", "pending", "neutral", "How do I invite another admin?"),
]

# Illustrative demo account classifications, not real customer contracts.
SEED_CUSTOMERS = [
    ("Northwind", "enterprise"),
    ("Contoso", "standard"),
    ("Fabrikam", "enterprise"),
    ("Adventure Works", "standard"),
    ("Tailspin", "enterprise"),
    ("Litware", "standard"),
    ("Wingtip", "enterprise"),
]

# Illustrative first-response targets by account tier and ticket priority.
SEED_SLA_POLICIES = [
    ("enterprise", "urgent", 1),
    ("enterprise", "high", 4),
    ("enterprise", "medium", 24),
    ("enterprise", "low", 48),
    ("standard", "urgent", 2),
    ("standard", "high", 8),
    ("standard", "medium", 48),
    ("standard", "low", 72),
]

# Add-on examples make the SLA query deterministic without changing the
# timestamps or contents of the user's existing sample tickets.
SLA_DEMO_TICKETS = [
    ("T-1051", "Northwind", "billing", "high", "open", "frustrated", "Enterprise high-priority ticket, created two days ago.", 2880),
    ("T-1052", "Fabrikam", "outage", "urgent", "open", "angry", "Enterprise urgent ticket, created twenty minutes ago.", 20),
    ("T-1053", "Wingtip", "security", "urgent", "open", "concerned", "Enterprise security ticket, created four hours ago.", 240),
    ("T-1054", "Tailspin", "data-loss", "urgent", "open", "worried", "Enterprise data-loss ticket, created forty-five minutes ago.", 45),
    ("T-1055", "Fabrikam", "outage", "high", "pending", "angry", "Enterprise high-priority ticket, created ten hours ago.", 600),
]

# Repeatable demo-only workload for illustrating local aggregation. IDs are
# namespaced, and INSERT IGNORE makes initialization safe to rerun.
SYNTHETIC_TICKET_COUNT = 500
SYNTHETIC_PRIORITIES = ("low", "medium", "high", "urgent")
SYNTHETIC_SENTIMENTS = ("neutral", "positive", "frustrated", "angry", "worried", "concerned")
SYNTHETIC_TOPICS = ("billing", "how-to", "outage", "data-loss", "security")
SYNTHETIC_MESSAGES = (
    "Please review this issue and let me know what happens next.",
    "The problem is still affecting our team.",
    "I followed the instructions, but the issue continues.",
    "Could someone check this when possible?",
    "This is blocking our work and needs attention.",
    "The latest update seems to have fixed the problem, thanks.",
)


def synthetic_customers() -> list[tuple[str, str]]:
    """Return deterministic demo-only accounts for the synthetic tickets."""
    return [
        (f"Demo {'Enterprise' if index <= 10 else 'Standard'} {index:02d}", "enterprise" if index <= 10 else "standard")
        for index in range(1, 21)
    ]


def synthetic_tickets() -> list[tuple[str, str, str, str, str, str, str, int]]:
    """Build varied, deterministic rows without using any real customer data."""
    accounts = synthetic_customers()
    rows = []
    for index in range(1, SYNTHETIC_TICKET_COUNT + 1):
        customer, _tier = accounts[(index - 1) % len(accounts)]
        topic = SYNTHETIC_TOPICS[(index * 7) % len(SYNTHETIC_TOPICS)]
        priority = SYNTHETIC_PRIORITIES[(index * 5) % len(SYNTHETIC_PRIORITIES)]
        status_slot = (index * 7) % 10
        status = "open" if status_slot < 6 else "pending" if status_slot < 8 else "closed"
        sentiment = SYNTHETIC_SENTIMENTS[(index * 11) % len(SYNTHETIC_SENTIMENTS)]
        message = SYNTHETIC_MESSAGES[(index * 13) % len(SYNTHETIC_MESSAGES)]
        age_days = 1 + ((index * 17) % 60)
        rows.append((f"SYN-{index:06d}", customer, topic, priority, status, sentiment, message, age_days))
    return rows


def _connect(database: bool = True):
    config: dict[str, Any] = {
        "host": DB_HOST,
        "port": DB_PORT,
        "user": DB_USER,
        "password": DB_PASSWORD,
        "connection_timeout": 8,
    }
    if database:
        config["database"] = DB_NAME
    return mysql.connector.connect(**config)


def initialize_database() -> int:
    """Create demo tables and insert missing ticket, account, and SLA samples."""
    admin = _connect(database=False)
    try:
        cursor = admin.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{DB_NAME}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        cursor.close()
    finally:
        admin.close()

    connection = _connect()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS tickets (
                ticket_id VARCHAR(20) PRIMARY KEY,
                customer VARCHAR(120) NOT NULL,
                topic VARCHAR(40) NOT NULL,
                priority VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL,
                sentiment VARCHAR(40) NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                INDEX idx_tickets_status_priority (status, priority),
                INDEX idx_tickets_topic (topic)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS customers (
                customer VARCHAR(120) PRIMARY KEY,
                customer_tier VARCHAR(20) NOT NULL,
                INDEX idx_customers_tier (customer_tier)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
        cursor.execute(
            """CREATE TABLE IF NOT EXISTS sla_policies (
                customer_tier VARCHAR(20) NOT NULL,
                priority VARCHAR(20) NOT NULL,
                response_hours INT NOT NULL,
                PRIMARY KEY (customer_tier, priority)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci"""
        )
        cursor.executemany(
            """INSERT IGNORE INTO tickets
                (ticket_id, customer, topic, priority, status, sentiment, message)
                VALUES (%s, %s, %s, %s, %s, %s, %s)""",
            SEED_TICKETS,
        )
        inserted = max(cursor.rowcount, 0)
        cursor.executemany(
            "INSERT IGNORE INTO customers (customer, customer_tier) VALUES (%s, %s)",
            [*SEED_CUSTOMERS, *synthetic_customers()],
        )
        cursor.executemany(
            """INSERT IGNORE INTO sla_policies
                (customer_tier, priority, response_hours) VALUES (%s, %s, %s)""",
            SEED_SLA_POLICIES,
        )
        cursor.executemany(
            """INSERT IGNORE INTO tickets
                (ticket_id, customer, topic, priority, status, sentiment, message, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        DATE_SUB(CURRENT_TIMESTAMP, INTERVAL %s MINUTE))""",
            SLA_DEMO_TICKETS,
        )
        inserted += max(cursor.rowcount, 0)
        cursor.executemany(
            """INSERT IGNORE INTO tickets
                (ticket_id, customer, topic, priority, status, sentiment, message, created_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s,
                        DATE_SUB(CURRENT_TIMESTAMP, INTERVAL %s DAY))""",
            synthetic_tickets(),
        )
        inserted += max(cursor.rowcount, 0)
        connection.commit()
        cursor.close()
        return inserted
    finally:
        connection.close()


TICKET_SCHEMA = """Tables available for support-ticket questions:

Table: tickets (one row per ticket)
- ticket_id VARCHAR(20) PRIMARY KEY
- customer VARCHAR(120) (joins to customers.customer)
- topic VARCHAR(40) (examples: billing, how-to, outage, data-loss, security)
- priority VARCHAR(20) (low, medium, high, urgent)
- status VARCHAR(20) (open, pending, closed)
- sentiment VARCHAR(40) (neutral, positive, frustrated, angry, worried, concerned)
- message TEXT
- created_at TIMESTAMP

- customer_tier VARCHAR(20) and response_hours INT are in the related tables below.

Table: customers (one row per customer)
- customer VARCHAR(120) PRIMARY KEY
- customer_tier VARCHAR(20) (enterprise or standard)

Table: sla_policies (one row per customer tier and priority)
- customer_tier VARCHAR(20)
- priority VARCHAR(20)
- response_hours INT (illustrative first-response target)
- primary key: (customer_tier, priority)

Relationships: tickets.customer = customers.customer; customers.customer_tier = sla_policies.customer_tier; tickets.priority = sla_policies.priority.

Use only these three tables. Use explicit INNER JOIN ... ON clauses for related data; do not guess customer tiers or SLA targets. Interpret the user's natural-language meaning and preserve every requested condition. Priority is ordered low < medium < high < urgent; "high-priority" handling includes high and urgent. "Need to handle" means actionable, so include open and pending tickets and exclude closed tickets. Sentiment values are a closed set: neutral means neither positive nor negative (for example, "neither good nor bad" or "neither satisfied nor dissatisfied"); positive means satisfied/good; frustrated, angry, worried, and concerned are adverse feedback. Map natural-language descriptions to stored categories by meaning, not by searching for unstored synonyms. "Exceeded response SLA" means the ticket is unresolved and its created_at is earlier than NOW() minus the matching response_hours; join the customer and policy tables to evaluate this. These SLA values and customer tiers are illustrative demo data, not real contract terms.

For ticket-list queries (not counts or other aggregates), select these same comparison columns in both paths: ticket_id, customer, topic, priority, status, sentiment, customer_tier, response_hours, created_at. This keeps results comparable. For a top-N ticket list with no ranking criterion, use ORDER BY created_at DESC (newest first) before LIMIT, consistently in both paths. Qualify columns with table aliases when joins make names ambiguous. If ordering is helpful, sort using a simple column and ASC/DESC; do not use FIELD() or CASE expressions for custom ordering."""

MAX_SQL_ROWS = 1_000
ALLOWED_COLUMNS = {
    "ticket_id", "customer", "topic", "priority", "status", "sentiment", "message", "created_at",
    "customer_tier", "response_hours",
}
ALLOWED_SQL_FUNCTIONS = {
    "COUNT", "SUM", "AVG", "MIN", "MAX", "LOWER", "UPPER", "COALESCE",
    "ROUND", "ABS", "AND", "OR", "NOT", "FIELD", "NOW", "DATE_SUB", "TIMESTAMPDIFF",
}


def validate_ticket_select(sql: str) -> str:
    """Parse model-generated SQL and constrain it to a single read-only tickets SELECT."""
    if not sql or len(sql) > 10_000:
        raise ValueError("The generated SQL is empty or too long.")
    statements = [statement for statement in parse(sql, read="mysql") if statement is not None]
    if len(statements) != 1 or not isinstance(statements[0], exp.Select):
        raise ValueError("Only one read-only SELECT statement is allowed.")

    statement = statements[0]
    if statement.args.get("with") or statement.args.get("locks") or statement.args.get("into"):
        raise ValueError("CTEs, INTO clauses, and locking clauses are not allowed in the demo query.")
    if any(isinstance(node, exp.Subquery) for node in statement.walk()):
        raise ValueError("Subqueries are not allowed in the demo query.")

    allowed_tables = {"tickets", "customers", "sla_policies"}
    tables = list(statement.find_all(exp.Table))
    if not tables or any(table.name.lower() not in allowed_tables or table.db not in (None, "", DB_NAME) for table in tables):
        raise ValueError("The query may read only from tickets, customers, and sla_policies in the demo database.")
    joins = statement.args.get("joins") or []
    if len(joins) > 2 or any(
        not join.args.get("on") or join.args.get("using") or join.args.get("side")
        or join.args.get("kind") not in (None, "INNER") or join.args.get("method")
        for join in joins
    ):
        raise ValueError("Use at most two simple INNER JOINs with explicit ON conditions.")
    for column in statement.find_all(exp.Column):
        if column.name != "*" and column.name.lower() not in ALLOWED_COLUMNS:
            raise ValueError(f"Column {column.name!r} is not part of the tickets schema.")
    for function in statement.find_all(exp.Func):
        # CASE is an expression, not a callable SQL function. sqlglot's
        # Func.name can resolve to a CASE branch literal (for example URGENT).
        if isinstance(function, exp.Case):
            continue
        # For aggregate nodes such as COUNT(*), sqlglot's `.name` can be the
        # argument (`*`) rather than the SQL function identifier.
        function_name = function.sql_name().upper()
        if function_name not in ALLOWED_SQL_FUNCTIONS:
            raise ValueError(f"SQL function {function_name!r} is not allowed.")

    limit = statement.args.get("limit")
    if limit is None:
        statement = statement.limit(MAX_SQL_ROWS)
    else:
        limit_value = limit.expression
        if not isinstance(limit_value, exp.Literal) or not limit_value.is_int:
            raise ValueError("Use a numeric LIMIT in the query.")
        if int(limit_value.this) > MAX_SQL_ROWS:
            statement.set("limit", exp.Limit(expression=exp.Literal.number(MAX_SQL_ROWS)))
    return statement.sql(dialect="mysql")


def execute_ticket_query(sql: str) -> list[dict[str, str]]:
    """Execute a validated, read-only model-generated query against tickets."""
    safe_sql = validate_ticket_select(sql)
    connection = _connect()
    try:
        connection.start_transaction(readonly=True)
        cursor = connection.cursor(dictionary=True)
        cursor.execute(safe_sql)
        rows = cursor.fetchall()
        connection.rollback()
        return [{key: str(value) for key, value in row.items()} for row in rows]
    finally:
        connection.close()
