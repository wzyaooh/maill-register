import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from core.sms_orders import (
    ORDER_STATE_ACTIVE,
    ORDER_STATE_AWAITING_CODE,
    ORDER_STATE_CANCELLED,
    ORDER_STATE_CANCEL_PENDING,
    ORDER_STATE_CODE_RECEIVED,
    ORDER_STATE_COMPENSATION_FAILED,
    ORDER_STATE_COMPLETED,
    ORDER_STATE_EXPIRED,
    ORDER_STATE_ALLOCATING,
    SmsOrderStore,
)


class SmsOrderStoreTests(unittest.TestCase):
    def test_excluded_orders_cannot_be_reclaimed_in_the_same_pass(self):
        first = self.store.create_order("5sim", "exclude-first")
        second = self.store.create_order("5sim", "exclude-second")
        self.store.request_cancel(first["order_id"])
        self.store.request_cancel(second["order_id"])
        claimed = self.store.claim_due_compensation(
            limit=1, exclude_order_ids=[first["order_id"]],
        )
        self.assertEqual([row["order_id"] for row in claimed], [second["order_id"]])

    def test_claim_check_requires_current_token_action_and_unexpired_lease(self):
        order = self.store.create_order("5sim", "check-claim")
        self.store.request_cancel(order["order_id"])
        claimed = self.store.claim_due_compensation(limit=1)[0]
        token = claimed["compensation_claim_token"]
        self.assertTrue(self.store.assert_compensation_claim(
            order["order_id"], claim_token=token, action="cancel",
        ))
        for token_value, action, now in (
            ("wrong-token", "cancel", None), (token, "finish", None),
            (token, "cancel", claimed["compensation_claimed_until"]),
        ):
            with self.subTest(token=token_value, action=action, now=now):
                with self.assertRaises(ValueError):
                    self.store.assert_compensation_claim(
                        order["order_id"], claim_token=token_value, action=action, now=now,
                    )
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = self.directory.name + "/database.db"
        self.store = SmsOrderStore(self.db_path)
        self.addCleanup(self.directory.cleanup)

    def test_order_lifecycle_is_durable_and_phone_is_redacted(self):
        order = self.store.create_order(
            provider="5sim", provider_order_id="provider-123",
            phone="+14155551234", job_id="job-1", attempt_id="attempt-1",
            metadata={"safe": "value", "otp": "246810", "api_key": "secret"},
        )
        self.assertEqual(order["state"], ORDER_STATE_ALLOCATING)
        self.assertEqual(order["job_id"], "job-1")
        self.assertEqual(order["attempt_id"], "attempt-1")
        self.assertNotEqual(order["phone_label"], "+14155551234")
        self.assertTrue(order["phone_label"].endswith("1234"))

        self.assertEqual(self.store.activate(order["order_id"])["state"], ORDER_STATE_ACTIVE)
        self.assertEqual(
            self.store.mark_awaiting_code(order["order_id"])["state"],
            ORDER_STATE_AWAITING_CODE,
        )
        self.assertEqual(
            self.store.mark_code_received(order["order_id"])["state"],
            ORDER_STATE_CODE_RECEIVED,
        )
        completed = self.store.complete(order["order_id"])
        self.assertEqual(completed["state"], ORDER_STATE_COMPLETED)

        reopened = SmsOrderStore(self.db_path)
        self.assertEqual(reopened.get_order(order["order_id"])["state"], ORDER_STATE_COMPLETED)
        raw = sqlite3.connect(self.db_path).execute(
            "SELECT phone_label, metadata FROM sms_orders"
        ).fetchone()
        raw_text = json.dumps(raw)
        self.assertNotIn("+14155551234", raw_text)
        self.assertNotIn("246810", raw_text)
        self.assertNotIn("secret", raw_text)

    def test_repeated_transitions_are_idempotent(self):
        order = self.store.create_order("sms_activate", "order-1", "+8613800138000")
        self.assertEqual(self.store.activate(order["order_id"])["state"], ORDER_STATE_ACTIVE)
        self.assertEqual(self.store.activate(order["order_id"])["state"], ORDER_STATE_ACTIVE)
        self.assertEqual(
            self.store.request_cancel(order["order_id"], error_code="timeout")["state"],
            ORDER_STATE_CANCEL_PENDING,
        )
        self.assertEqual(
            self.store.request_cancel(order["order_id"], error_code="timeout")["state"],
            ORDER_STATE_CANCEL_PENDING,
        )
        self.assertEqual(
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                provider_status="CANCELLED",
            )["state"],
            ORDER_STATE_CANCELLED,
        )

    def test_provider_status_is_finite_at_every_persistence_boundary(self):
        order = self.store.create_order("5sim", "status-boundary", "+14155550012")
        self.store.activate(order["order_id"])
        completed = self.store.complete(
            order["order_id"], provider_status="provider-secret-status"
        )
        self.assertEqual(completed["last_provider_status"], "")

        pending = self.store.create_order("5sim", "status-boundary-2", "+14155550013")
        self.store.request_cancel(pending["order_id"])
        claimed = self.store.claim_due_compensation(limit=1)[0]
        saved = self.store.record_compensation(
            pending["order_id"], action="cancel", success=True,
            provider_status="provider-secret-status",
            claim_token=claimed["compensation_claim_token"],
        )
        self.assertEqual(saved["last_provider_status"], "ok")

    def test_expired_claim_can_be_reclaimed_and_old_worker_is_fenced(self):
        order = self.store.create_order("5sim", "claim-expiry", "+14155550014")
        self.store.request_cancel(order["order_id"])
        first = self.store.claim_due_compensation(limit=1, lease_seconds=1)[0]
        future = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()
        second = self.store.claim_due_compensation(
            limit=1, lease_seconds=60, now=future
        )[0]
        self.assertNotEqual(
            first["compensation_claim_token"], second["compensation_claim_token"]
        )
        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                claim_token=first["compensation_claim_token"],
            )
        self.assertEqual(
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                provider_status="CANCELLED",
            )["state"],
            ORDER_STATE_CANCELLED,
        )

    def test_expired_orders_are_claimed_once_and_compensation_is_bounded(self):
        order = self.store.create_order(
            "getsms", "order-expired", "+12025550199", lease_seconds=1,
        )
        old = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET lease_expires_at=?, next_action_at=?, updated_at=? WHERE order_id=?",
                (old, old, old, order["order_id"]),
            )

        claimed = self.store.claim_expired(limit=10)
        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0]["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(self.store.claim_expired(limit=10), [])

        failed = self.store.record_compensation(
            order["order_id"], action="cancel", success=False,
            error_code="provider_timeout", max_attempts=2, backoff_seconds=0,
        )
        self.assertEqual(failed["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(failed["compensation_attempts"], 1)
        exhausted = self.store.record_compensation(
            order["order_id"], action="cancel", success=False,
            error_code="provider_timeout", max_attempts=2, backoff_seconds=0,
        )
        self.assertEqual(exhausted["state"], ORDER_STATE_COMPENSATION_FAILED)
        self.assertEqual(exhausted["compensation_attempts"], 2)

    def test_duplicate_provider_order_is_returned_without_new_row(self):
        first = self.store.create_order("5sim", "same-id", "+14155550000")
        second = self.store.create_order("5sim", "same-id", "+14155550000")
        self.assertEqual(first["order_id"], second["order_id"])
        self.assertEqual(len(self.store.list_orders()), 1)

    def test_due_compensation_is_claimed_by_only_one_worker(self):
        order = self.store.create_order("5sim", "provider-claim-once", "+14155550010")
        self.store.request_cancel(order["order_id"], error_code="sms_timeout")

        def claim_once():
            return self.store.claim_due_compensation(limit=1, lease_seconds=60)

        with ThreadPoolExecutor(max_workers=2) as pool:
            first, second = pool.map(lambda _index: claim_once(), (1, 2))

        self.assertEqual(len(first) + len(second), 1)
        claimed = (first or second)[0]
        self.assertTrue(claimed["compensation_claim_token"])
        self.assertTrue(claimed["compensation_claimed_until"])

    def test_existing_order_schema_is_migrated_for_compensation_claims(self):
        with self.store.connect() as conn:
            conn.execute("ALTER TABLE sms_orders RENAME TO sms_orders_legacy")
            conn.execute(
                """CREATE TABLE sms_orders (
                    order_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    provider_order_id TEXT NOT NULL,
                    phone_label TEXT NOT NULL DEFAULT '',
                    job_id TEXT NOT NULL DEFAULT '',
                    attempt_id TEXT NOT NULL DEFAULT '',
                    state TEXT NOT NULL,
                    pending_action TEXT NOT NULL DEFAULT '',
                    lease_expires_at TEXT,
                    allocated_at TEXT NOT NULL,
                    activated_at TEXT,
                    last_polled_at TEXT,
                    poll_count INTEGER NOT NULL DEFAULT 0,
                    code_received_at TEXT,
                    completed_at TEXT,
                    cancelled_at TEXT,
                    next_action_at TEXT,
                    compensation_attempts INTEGER NOT NULL DEFAULT 0,
                    last_error_code TEXT NOT NULL DEFAULT '',
                    last_provider_status TEXT NOT NULL DEFAULT '',
                    metadata TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(provider, provider_order_id)
                )"""
            )
        migrated = SmsOrderStore(self.db_path)
        columns = {
            row[1] for row in migrated.connect().execute("PRAGMA table_info(sms_orders)")
        }
        self.assertIn("compensation_claim_token", columns)
        self.assertIn("compensation_claimed_until", columns)

    def test_minimal_legacy_order_schema_is_migrated_before_indexes_are_created(self):
        path = os.path.join(self.directory.name, "minimal-legacy.db")
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE sms_orders (order_id TEXT, provider TEXT, "
                "provider_order_id TEXT)"
            )
            conn.execute(
                "INSERT INTO sms_orders(order_id, provider, provider_order_id) "
                "VALUES ('legacy-1', '5sim', 'provider-legacy')"
            )

        migrated = SmsOrderStore(path)
        saved = migrated.get_order("legacy-1")
        self.assertEqual(saved["state"], "allocating")
        self.assertEqual(saved["pending_action"], "cancel")
        self.assertIn("compensation_claim_token", saved)
        self.assertEqual(
            migrated.create_order("5sim", "provider-new", "+14155550025")["state"],
            "allocating",
        )

    def test_without_rowid_legacy_schema_is_migrated_without_startup_failure(self):
        path = os.path.join(self.directory.name, "without-rowid.db")
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE TABLE sms_orders ("
                "order_id TEXT PRIMARY KEY, provider TEXT, provider_order_id TEXT"
                ") WITHOUT ROWID"
            )
            conn.execute(
                "INSERT INTO sms_orders(order_id, provider, provider_order_id) "
                "VALUES ('legacy-without-rowid', '5sim', 'provider-without-rowid')"
            )

        migrated = SmsOrderStore(path)
        saved = migrated.get_order("legacy-without-rowid")
        self.assertEqual(saved["state"], ORDER_STATE_ALLOCATING)
        self.assertEqual(
            migrated.create_order("5sim", "provider-without-rowid-new")["state"],
            ORDER_STATE_ALLOCATING,
        )

    def test_restart_scrubs_invalid_legacy_provider_status(self):
        order = self.store.create_order("5sim", "legacy-invalid-status", "+14155550020")
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET last_provider_status=? WHERE order_id=?",
                ("provider-secret-status", order["order_id"]),
            )

        reopened = SmsOrderStore(self.db_path)
        saved = reopened.get_order(order["order_id"])
        self.assertEqual(saved["last_provider_status"], "")

    def test_stale_compensation_worker_cannot_record_over_a_new_claim(self):
        order = self.store.create_order("5sim", "provider-claim-fence", "+14155550011")
        self.store.request_cancel(order["order_id"], error_code="sms_timeout")
        first = self.store.claim_due_compensation(limit=1, lease_seconds=1)[0]
        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                claim_token="wrong-token", provider_status="ok",
            )
        self.assertEqual(
            self.store.get_order(order["order_id"])["compensation_claim_token"],
            first["compensation_claim_token"],
        )

    def test_tokenless_callback_from_another_store_cannot_override_active_claim(self):
        order = self.store.create_order("5sim", "provider-cross-store-fence", "+14155550021")
        self.store.request_cancel(order["order_id"], error_code="sms_timeout")
        claimed = self.store.claim_due_compensation(limit=1, lease_seconds=60)[0]
        other_store = SmsOrderStore(self.db_path)

        with self.assertRaises(ValueError):
            other_store.record_compensation(
                order["order_id"], action="cancel", success=True,
                provider_status="ok",
            )

        current = self.store.get_order(order["order_id"])
        self.assertEqual(current["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(
            current["compensation_claim_token"], claimed["compensation_claim_token"]
        )

    def test_unclaimed_tokenless_callback_from_another_store_is_rejected(self):
        order = self.store.create_order("5sim", "provider-unclaimed-cross-store", "+14155550024")
        self.store.request_cancel(order["order_id"])
        other_store = SmsOrderStore(self.db_path)

        with self.assertRaises(ValueError):
            other_store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code="provider_error",
            )

        current = self.store.get_order(order["order_id"])
        self.assertEqual(current["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(current["compensation_attempts"], 0)

    def test_invalid_compensation_timestamp_is_rejected_without_mutation(self):
        order = self.store.create_order("5sim", "provider-invalid-time", "+14155550022")
        self.store.request_cancel(order["order_id"])

        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], action="cancel", success=False,
                error_code="provider_timeout", now="not-a-timestamp",
            )

        current = self.store.get_order(order["order_id"])
        self.assertEqual(current["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(current["compensation_attempts"], 0)

    def test_invalid_claim_timestamp_is_reclaimed_and_old_token_is_fenced(self):
        order = self.store.create_order("5sim", "provider-invalid-claim", "+14155550023")
        self.store.request_cancel(order["order_id"])
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET compensation_claim_token=?, "
                "compensation_claimed_until=? WHERE order_id=?",
                ("old-token", "not-a-timestamp", order["order_id"]),
            )

        reclaimed = self.store.claim_due_compensation(limit=1, lease_seconds=60)
        self.assertEqual(len(reclaimed), 1)
        self.assertNotEqual(reclaimed[0]["compensation_claim_token"], "old-token")
        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                claim_token="old-token",
            )

    def test_compensation_claim_cannot_apply_a_different_action(self):
        order = self.store.create_order("5sim", "action-fence", "+14155550019")
        self.store.activate(order["order_id"])
        self.store.mark_code_received(order["order_id"])
        self.store.request_finish(order["order_id"])
        claimed = self.store.claim_due_compensation(
            order_id=order["order_id"], action="finish"
        )[0]

        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], action="cancel", success=True,
                claim_token=claimed["compensation_claim_token"],
            )

        current = self.store.get_order(order["order_id"])
        self.assertEqual(current["state"], "finish_pending")
        self.assertEqual(current["pending_action"], "finish")

    def test_state_transition_rejects_malformed_timestamp_without_mutation(self):
        order = self.store.create_order("5sim", "invalid-state-time", "+14155550029")

        with self.assertRaises(ValueError):
            self.store._set_state(
                order["order_id"], (ORDER_STATE_ALLOCATING,), ORDER_STATE_ACTIVE,
                now="not-a-timestamp",
            )

        current = self.store.get_order(order["order_id"])
        self.assertEqual(current["state"], ORDER_STATE_ALLOCATING)

    def test_lifecycle_numeric_parameters_reject_implicit_coercion(self):
        with self.assertRaises(ValueError):
            self.store.create_order("5sim", "strict-lease-float", lease_seconds=1.5)
        with self.assertRaises(ValueError):
            self.store.create_order("5sim", "strict-lease-string", lease_seconds="1")
        with self.assertRaises(ValueError):
            self.store.list_orders(limit=1.5)
        with self.assertRaises(ValueError):
            self.store.list_due_compensation(limit="1")
        with self.assertRaises(ValueError):
            self.store.expire_due(limit=1.5)
        with self.assertRaises(ValueError):
            self.store.claim_expired(limit="1")
        with self.assertRaises(ValueError):
            self.store.claim_due_compensation(limit=1.5)
        with self.assertRaises(ValueError):
            self.store.claim_due_compensation(lease_seconds="1")

        order = self.store.create_order("5sim", "strict-record", "+14155550030")
        self.store.request_cancel(order["order_id"])
        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], success=False, max_attempts="2",
            )
        with self.assertRaises(ValueError):
            self.store.record_compensation(
                order["order_id"], success=False, backoff_seconds=1.5,
            )

    def test_order_identity_fields_reject_control_characters(self):
        with self.assertRaises(ValueError):
            self.store.create_order("5sim", "provider\n-id")
        with self.assertRaises(ValueError):
            self.store.create_order("5sim", "provider-id", job_id="job\n1")
        with self.assertRaises(ValueError):
            self.store.create_order("5sim", "provider-id-2", attempt_id="attempt\t1")

    def test_legacy_counter_and_metadata_values_are_rewritten_on_restart(self):
        order = self.store.create_order(
            "5sim", "legacy-sanitize", "+14155550031",
            metadata={"safe": "value"},
        )
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET poll_count=?, compensation_attempts=?, metadata=? "
                "WHERE order_id=?",
                (
                    "not-a-number", "1.5",
                    json.dumps({"safe": "value", "api_key": "legacy-secret", "otp": "123456"}),
                    order["order_id"],
                ),
            )

        reopened = SmsOrderStore(self.db_path)
        saved = reopened.get_order(order["order_id"])
        self.assertEqual(saved["poll_count"], 0)
        self.assertEqual(saved["compensation_attempts"], 0)
        self.assertEqual(saved["metadata"], {"safe": "value"})
        raw = sqlite3.connect(self.db_path).execute(
            "SELECT poll_count, compensation_attempts, metadata FROM sms_orders "
            "WHERE order_id=?", (order["order_id"],)
        ).fetchone()
        self.assertEqual(raw[0], 0)
        self.assertEqual(raw[1], 0)
        self.assertNotIn("legacy-secret", raw[2])
        self.assertNotIn("123456", raw[2])

    def test_malformed_runtime_compensation_counter_is_recovered(self):
        order = self.store.create_order("5sim", "runtime-counter", "+14155550032")
        self.store.request_cancel(order["order_id"])
        claimed = self.store.claim_due_compensation(order_id=order["order_id"])[0]
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET compensation_attempts=? WHERE order_id=?",
                ("corrupt", order["order_id"]),
            )

        saved = self.store.record_compensation(
            order["order_id"], success=False, max_attempts=2,
            claim_token=claimed["compensation_claim_token"],
        )
        self.assertEqual(saved["compensation_attempts"], 1)

    def test_database_manager_exposes_the_same_durable_order_store(self):
        from core.database import DatabaseManager

        database = DatabaseManager(self.db_path)
        order = database.sms_orders.create_order("5sim", "db-order", "+14155550007")
        reopened = DatabaseManager(self.db_path)
        self.assertEqual(
            reopened.sms_orders.get_order(order["order_id"])["provider_order_id"],
            "db-order",
        )


if __name__ == "__main__":
    unittest.main()
