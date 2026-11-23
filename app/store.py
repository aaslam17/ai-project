"""Persistent SQLite store for customer orders, refund receipts, tickets, and memory notes.

Provides deterministic seed data so automated evaluators, integration tests, and
interactive sessions work out of the box while persisting state across turns.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.pii_redaction import get_pii_redactor

_SEED_ORDERS: tuple[dict[str, Any], ...] = (
    {
        "order_id": "ORD-1001",
        "customer_tier": "gold",
        "fulfillment_status": "DELIVERED",
        "order_date_iso": "2026-11-12",
        "days_since_delivery": 8,
        "total_amount_usd": 42.50,
        "items": [
            {
                "sku": "SKU-MOUSE-01",
                "name": "Wireless Ergonomic Mouse",
                "category": "electronics",
                "quantity": 1,
                "unit_price_usd": 42.50,
            }
        ],
    },
    {
        "order_id": "ORD-1002",
        "customer_tier": "enterprise",
        "fulfillment_status": "DELIVERED",
        "order_date_iso": "2026-11-15",
        "days_since_delivery": 5,
        "total_amount_usd": 349.00,
        "items": [
            {
                "sku": "SKU-MON-27",
                "name": "27-Inch 4K IPS Monitor",
                "category": "electronics",
                "quantity": 1,
                "unit_price_usd": 349.00,
            }
        ],
    },
    {
        "order_id": "ORD-1003",
        "customer_tier": "standard",
        "fulfillment_status": "DELIVERED",
        "order_date_iso": "2026-10-05",
        "days_since_delivery": 45,
        "total_amount_usd": 100.00,
        "items": [
            {
                "sku": "SKU-GC-100",
                "name": "Digital Store Gift Card ($100)",
                "category": "digital_gift_card",
                "quantity": 1,
                "unit_price_usd": 100.00,
            }
        ],
    },
    {
        "order_id": "ORD-1004",
        "customer_tier": "standard",
        "fulfillment_status": "DELIVERED",
        "order_date_iso": "2026-10-10",
        "days_since_delivery": 40,
        "total_amount_usd": 85.00,
        "items": [
            {
                "sku": "SKU-JACKET-M",
                "name": "All-Weather Commuter Jacket",
                "category": "apparel",
                "quantity": 1,
                "unit_price_usd": 85.00,
            }
        ],
    },
    {
        "order_id": "ORD-1005",
        "customer_tier": "gold",
        "fulfillment_status": "IN_TRANSIT",
        "order_date_iso": "2026-11-20",
        "days_since_delivery": None,
        "total_amount_usd": 64.00,
        "items": [
            {
                "sku": "SKU-STAND-AL",
                "name": "Aluminum Laptop Stand",
                "category": "home_office",
                "quantity": 1,
                "unit_price_usd": 64.00,
            }
        ],
    },
    {
        "order_id": "ORD-1006",
        "customer_tier": "gold",
        "fulfillment_status": "DELIVERED",
        "order_date_iso": "2026-11-17",
        "days_since_delivery": 6,
        "total_amount_usd": 35.00,
        "items": [
            {
                "sku": "SKU-HUB-USBC",
                "name": "7-in-1 USB-C Travel Hub",
                "category": "electronics",
                "quantity": 1,
                "unit_price_usd": 35.00,
            }
        ],
    },
)


class OrderGuardStore:
    """Thread-safe SQLite store backing orders, refunds, escalations, and memory."""

    def __init__(self, db_path: str | None = None) -> None:
        resolved_path = db_path or get_settings().order_store_db_path
        self._db_path = resolved_path
        if resolved_path != ":memory:":
            Path(resolved_path).parent.mkdir(parents=True, exist_ok=True)
        self._initialize_schema_and_seed()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _initialize_schema_and_seed(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS orders (
                    order_id TEXT PRIMARY KEY,
                    customer_tier TEXT NOT NULL,
                    fulfillment_status TEXT NOT NULL,
                    order_date_iso TEXT NOT NULL,
                    days_since_delivery INTEGER,
                    total_amount_usd REAL NOT NULL,
                    items_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS refund_ledger (
                    refund_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    refunded_amount_usd REAL NOT NULL,
                    reason TEXT NOT NULL,
                    signature_digest TEXT NOT NULL,
                    created_at_iso TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS escalation_tickets (
                    ticket_id TEXT PRIMARY KEY,
                    order_id TEXT NOT NULL,
                    priority TEXT NOT NULL,
                    issue_summary TEXT NOT NULL,
                    sla_hours INTEGER NOT NULL,
                    created_at_iso TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS memory_notes (
                    note_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    scrubbed_summary TEXT NOT NULL,
                    created_at_iso TEXT NOT NULL
                );
                """
            )
            for order in _SEED_ORDERS:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO orders (
                        order_id, customer_tier, fulfillment_status,
                        order_date_iso, days_since_delivery, total_amount_usd, items_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        order["order_id"],
                        order["customer_tier"],
                        order["fulfillment_status"],
                        order["order_date_iso"],
                        order["days_since_delivery"],
                        order["total_amount_usd"],
                        json.dumps(order["items"]),
                    ),
                )
            conn.commit()

    def get_order(self, order_id: str) -> dict[str, Any] | None:
        """Retrieves an order record by `order_id`, or None if not found."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM orders WHERE order_id = ?", (order_id,)
            ).fetchone()
            if row is None:
                return None
            return {
                "order_id": row["order_id"],
                "customer_tier": row["customer_tier"],
                "fulfillment_status": row["fulfillment_status"],
                "order_date_iso": row["order_date_iso"],
                "days_since_delivery": row["days_since_delivery"],
                "total_amount_usd": float(row["total_amount_usd"]),
                "items": json.loads(row["items_json"]),
            }

    def get_existing_refund(self, order_id: str) -> dict[str, Any] | None:
        """Returns any previously issued refund receipt for `order_id`."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM refund_ledger WHERE order_id = ? ORDER BY created_at_iso DESC LIMIT 1",
                (order_id,),
            ).fetchone()
            if row is None:
                return None
            return dict(row)

    def record_refund(
        self, *, order_id: str, refund_amount_usd: float, reason: str
    ) -> dict[str, Any]:
        """Atomically records a refund receipt and marks the order `REFUNDED`."""
        refund_id = f"RFND-{uuid.uuid4().hex[:8].upper()}"
        created_at = datetime.now(UTC).isoformat()
        signing_key = get_settings().get_refund_signing_key().encode("utf-8")
        payload = f"{refund_id}:{order_id}:{refund_amount_usd:.2f}:{reason}".encode()
        digest = hmac.new(signing_key, payload, hashlib.sha256).hexdigest()[:16]

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO refund_ledger (
                    refund_id, order_id, refunded_amount_usd, reason,
                    signature_digest, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (refund_id, order_id, refund_amount_usd, reason, digest, created_at),
            )
            conn.execute(
                "UPDATE orders SET fulfillment_status = 'REFUNDED' WHERE order_id = ?",
                (order_id,),
            )
            conn.commit()

        return {
            "refund_id": refund_id,
            "order_id": order_id,
            "refunded_amount_usd": refund_amount_usd,
            "signature_digest": digest,
            "created_at_iso": created_at,
        }

    def create_ticket(
        self, *, order_id: str, priority: str, issue_summary: str
    ) -> dict[str, Any]:
        """Persists a PII-scrubbed escalation ticket."""
        ticket_id = f"ESC-{uuid.uuid4().hex[:8].upper()}"
        sla_map = {"P1": 2, "P2": 8, "P3": 24}
        sla_hours = sla_map.get(priority, 24)
        scrubbed_summary = get_pii_redactor().redact_text(issue_summary)
        created_at = datetime.now(UTC).isoformat()

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO escalation_tickets (
                    ticket_id, order_id, priority, issue_summary, sla_hours, created_at_iso
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    ticket_id,
                    order_id,
                    priority,
                    scrubbed_summary,
                    sla_hours,
                    created_at,
                ),
            )
            conn.commit()

        return {
            "ticket_id": ticket_id,
            "order_id": order_id,
            "priority": priority,
            "sla_hours": sla_hours,
            "issue_summary": scrubbed_summary,
        }

    def save_memory_note(
        self, *, user_id: str, session_id: str, summary: str
    ) -> str:
        """Persists a PII-redacted conversational memory note."""
        note_id = f"MEM-{uuid.uuid4().hex[:8].upper()}"
        scrubbed = get_pii_redactor().redact_text(summary)
        created_at = datetime.now(UTC).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO memory_notes (
                    note_id, user_id, session_id, scrubbed_summary, created_at_iso
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (note_id, user_id, session_id, scrubbed, created_at),
            )
            conn.commit()
        return note_id

    def list_memory_notes(self, user_id: str, limit: int = 5) -> list[str]:
        """Retrieves recent PII-scrubbed memory notes for `user_id`."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT scrubbed_summary FROM memory_notes
                WHERE user_id = ?
                ORDER BY created_at_iso DESC
                LIMIT ?
                """,
                (user_id, limit),
            ).fetchall()
            return [str(r["scrubbed_summary"]) for r in rows]


_DEFAULT_STORE: OrderGuardStore | None = None


def get_order_store() -> OrderGuardStore:
    """Returns the process-wide OrderGuardStore."""
    global _DEFAULT_STORE
    if _DEFAULT_STORE is None:
        _DEFAULT_STORE = OrderGuardStore()
    return _DEFAULT_STORE


def reset_order_store(db_path: str | None = None) -> OrderGuardStore:
    """Resets the process-wide OrderGuardStore (used by isolated unit tests)."""
    global _DEFAULT_STORE
    _DEFAULT_STORE = OrderGuardStore(db_path=db_path)
    return _DEFAULT_STORE
