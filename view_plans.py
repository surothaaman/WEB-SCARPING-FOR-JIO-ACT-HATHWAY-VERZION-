import sqlite3
from database import DB_PATH

# Prevent accidentally creating a new empty database.
if not DB_PATH.exists():
    raise FileNotFoundError("Run the scraper to create plans.db first.")

connection = sqlite3.connect(DB_PATH)
connection.row_factory = sqlite3.Row

try:
    plans = connection.execute("""
        SELECT
            id,
            provider,
            displayed_price,
            validity,
            data_and_speed,
            scraped_at
        FROM plans
        ORDER BY id
    """).fetchall()

    print(f"Total saved plans: {len(plans)}")

    for plan in plans:
        print(f"\nPlan ID: {plan['id']}")
        print("Provider:", plan["provider"])
        print("Displayed price:", plan["displayed_price"])
        print("Validity:", plan["validity"])
        print("Data and speed:", plan["data_and_speed"])
        print("Scraped at (UTC):", plan["scraped_at"])

finally:
    connection.close()