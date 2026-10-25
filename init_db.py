"""Initialize the demo MySQL database and seed ticket, customer, and SLA samples."""

from ticket_database import DB_HOST, DB_NAME, DB_PORT, DB_USER, SYNTHETIC_TICKET_COUNT, initialize_database


if __name__ == "__main__":
    inserted = initialize_database()
    print(f"Database {DB_NAME!r} initialized on {DB_HOST}:{DB_PORT} as {DB_USER!r}.")
    print(f"Inserted {inserted} sample ticket rows (existing tickets were left unchanged).")
    print(f"The database includes up to {SYNTHETIC_TICKET_COUNT} deterministic synthetic tickets with SYN-* IDs.")
    print("Customer tiers and illustrative SLA policies are available for joined-query demos.")
