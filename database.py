import sqlite3
import json
import re
from pathlib import Path

# Keep the database beside this Python file.
DB_PATH = Path(__file__).resolve().parent / "plans.db"


def create_tables():
    connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        connection.execute("""
            CREATE TABLE IF NOT EXISTS plans (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                displayed_price TEXT NOT NULL,
                validity TEXT NOT NULL,
                data_and_speed TEXT NOT NULL,
                source_url TEXT NOT NULL,
                scraped_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS provider_plans (
                id INTEGER PRIMARY KEY, provider TEXT NOT NULL, city TEXT NOT NULL,
                advertised_price_paise INTEGER NOT NULL, speed_mbps REAL NOT NULL,
                billing_period TEXT, conditions_json TEXT NOT NULL,
                source_url TEXT NOT NULL, scraped_at TEXT NOT NULL
            )
        """)

        connection.execute("""
            CREATE TABLE IF NOT EXISTS address_checks (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                address TEXT NOT NULL,
                normalized_address TEXT NOT NULL,
                status TEXT NOT NULL,
                service_available INTEGER,
                currency TEXT NOT NULL DEFAULT 'USD',
                message TEXT,
                source_url TEXT,
                checked_at TEXT NOT NULL,
                elapsed_seconds REAL,
                result_json TEXT NOT NULL
            )
        """)
        connection.execute("""
            CREATE INDEX IF NOT EXISTS idx_address_checks_lookup
            ON address_checks(provider, normalized_address, checked_at DESC)
        """)
        connection.execute("""
            CREATE TABLE IF NOT EXISTS address_check_plans (
                id INTEGER PRIMARY KEY,
                check_id INTEGER NOT NULL REFERENCES address_checks(id) ON DELETE CASCADE,
                name TEXT NOT NULL,
                monthly_price REAL,
                speed TEXT,
                speed_mbps REAL,
                billing_period TEXT,
                address_confirmed INTEGER NOT NULL DEFAULT 0,
                raw_text TEXT
            )
        """)
        connection.commit()
        print(f"Database ready: {DB_PATH}")

    finally:
        connection.close()


def normalize_address(value):
    return " ".join(str(value or "").casefold().split())


def save_address_check(result):
    """Persist one address lookup and its candidate offers as a single transaction."""
    address = str(result.get("address", "")).strip()
    if not address:
        raise ValueError("Address result is missing its address.")
    status = str(result.get("status", "incomplete"))
    available = result.get("service_available")
    available_db = None if available is None else int(bool(available))
    checked_at = result.get("checked_at")
    if not checked_at:
        from datetime import datetime, timezone
        checked_at = datetime.now(timezone.utc).isoformat()

    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        with connection:
            cursor = connection.execute("""
                INSERT INTO address_checks (
                    provider, address, normalized_address, status,
                    service_available, currency, message, source_url,
                    checked_at, elapsed_seconds, result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                result.get("provider", "Verizon"), address, normalize_address(address),
                status, available_db, result.get("currency", "USD"),
                result.get("message", ""), result.get("source_url", ""), checked_at,
                result.get("elapsed_seconds"), json.dumps(result, ensure_ascii=False),
            ))
            check_id = cursor.lastrowid
            plan_rows = []
            for plan in result.get("plans", []):
                price = plan.get("monthly_price_usd")
                speed = str(plan.get("speed", ""))
                match = re.search(r"([\d.]+)\s*(Gbps|Mbps)", speed, re.I)
                speed_mbps = None
                if match:
                    speed_mbps = float(match.group(1)) * (1000 if match.group(2).lower() == "gbps" else 1)
                plan_rows.append((
                    check_id, str(plan.get("name", "Verizon home internet offer")),
                    float(price) if price is not None else None, speed or None, speed_mbps,
                    plan.get("billing_period"), int(bool(plan.get("address_confirmed", False))),
                    str(plan.get("raw_text", "")),
                ))
            if plan_rows:
                connection.executemany("""
                    INSERT INTO address_check_plans (
                        check_id, name, monthly_price, speed, speed_mbps,
                        billing_period, address_confirmed, raw_text
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """, plan_rows)
        return check_id
    finally:
        connection.close()


def get_latest_address_check(address):
    """Return the most recent saved check for an address, including its plans."""
    connection = sqlite3.connect(DB_PATH, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        row = connection.execute("""
            SELECT * FROM address_checks
            WHERE provider = 'Verizon' AND normalized_address = ?
            ORDER BY id DESC LIMIT 1
        """, (normalize_address(address),)).fetchone()
        if row is None:
            return None
        stored = json.loads(row["result_json"])
        result = {**stored, **dict(row)}
        result.pop("normalized_address", None)
        result.pop("result_json", None)
        result["service_available"] = None if result["service_available"] is None else bool(result["service_available"])
        result["plans"] = [dict(plan) for plan in connection.execute("""
            SELECT name, monthly_price, speed, speed_mbps, billing_period,
                   address_confirmed, raw_text
            FROM address_check_plans WHERE check_id = ? ORDER BY id
        """, (result["id"],))]
        details_by_name = {p.get("name"): p for p in stored.get("plans", [])}
        result["plans"] = [{**details_by_name.get(p["name"], {}), **p} for p in result["plans"]]
        for plan in result["plans"]:
            plan["address_confirmed"] = bool(plan["address_confirmed"])
        return result
    finally:
        connection.close()
def save_plans(provider, plans, source_url):
    from datetime import datetime, timezone

    # Avoid replacing saved data with an empty or incomplete result.
    if not plans:
        raise ValueError("No plans were captured. Existing data was kept.")

    required_fields = ("price", "validity", "data_and_speed")

    for plan in plans:
        if any(not plan.get(field) for field in required_fields):
            raise ValueError("A plan has missing fields. Existing data was kept.")

    scraped_at = datetime.now(timezone.utc).isoformat()

    rows = [
        (
            provider,
            plan["price"],
            plan["validity"],
            plan["data_and_speed"],
            source_url,
            scraped_at,
        )
        for plan in plans
    ]

    connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        # Both operations succeed together or are rolled back together.
        with connection:
            connection.execute(
                "DELETE FROM plans WHERE provider = ? AND source_url = ?",
                (provider, source_url),
            )

            connection.executemany("""
                INSERT INTO plans (
                    provider,
                    displayed_price,
                    validity,
                    data_and_speed,
                    source_url,
                    scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?)
            """, rows)

        print(f"Saved {len(rows)} plans for {provider}.")

    finally:
        connection.close()

def save_provider_plans(provider, city, plans, source_url):
    import json
    from datetime import datetime, timezone
    from decimal import Decimal

    if not plans:
        raise ValueError("No plans to save. Existing data was kept.")

    scraped_at = datetime.now(timezone.utc).isoformat()
    rows = []

    for plan in plans:
        # Store money as integer paise, avoiding floating-point rounding.
        price = Decimal(plan["advertised_price_inr"])
        price_paise = price * 100
        speed = float(plan["speed_mbps"])

        if (
            not price.is_finite()
            or price < 0
            or price_paise != price_paise.to_integral_value()
            or not 0 < speed < float("inf")
        ):
            raise ValueError("Invalid price or speed. Existing data was kept.")

        rows.append((
            provider,
            city,
            int(price_paise),
            speed,
            plan["billing_period"],
            json.dumps(plan["conditions"], ensure_ascii=False),
            source_url,
            scraped_at,
        ))

    connection = sqlite3.connect(DB_PATH, timeout=30)

    try:
        with connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS provider_plans (
                    id INTEGER PRIMARY KEY,
                    provider TEXT NOT NULL,
                    city TEXT NOT NULL,
                    advertised_price_paise INTEGER NOT NULL,
                    speed_mbps REAL NOT NULL,
                    billing_period TEXT,
                    conditions_json TEXT NOT NULL,
                    source_url TEXT NOT NULL,
                    scraped_at TEXT NOT NULL
                )
            """)

            connection.execute("""
                DELETE FROM provider_plans
                WHERE provider = ? AND city = ? AND source_url = ?
            """, (provider, city, source_url))

            connection.executemany("""
                INSERT INTO provider_plans (
                    provider,
                    city,
                    advertised_price_paise,
                    speed_mbps,
                    billing_period,
                    conditions_json,
                    source_url,
                    scraped_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)

        print(f"Saved {len(rows)} plans for {provider} in {city}.")

    finally:
        connection.close()

if __name__ == "__main__":
    create_tables()
