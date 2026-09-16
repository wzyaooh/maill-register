import asyncio
import json
import os
import sqlite3
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.sms_orders import (
    ORDER_STATE_ACTIVE,
    ORDER_STATE_AWAITING_CODE,
    ORDER_STATE_CANCEL_PENDING,
    ORDER_STATE_CODE_RECEIVED,
    ORDER_STATE_COMPLETED,
    SmsOrderStore,
)
from services import sms_manager


class SmsManagerPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.directory.name, "database.db")
        self.store = SmsOrderStore(self.db_path)
        self.config = types.SimpleNamespace(
            FIVESIM_API_KEY="test-key", SMS_ACTIVATE_API_KEY="",
            ONLINESIM_API_KEY="", GETSMS_API_KEY="",
        )
        self.config_patch = patch.object(sms_manager, "Config", self.config)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.addCleanup(self.directory.cleanup)

    def test_allocation_is_persisted_before_phone_is_returned(self):
        async def provider_allocate():
            return {"phone": "+14155551234", "id": "provider-1"}

        with patch.object(sms_manager, "_get_5sim_phone", provider_allocate), \
             patch.object(sms_manager, "SmsOrderStore", return_value=self.store):
            result = asyncio.run(sms_manager.get_phone_from_any_service(
                job_id="job-1", attempt_id="attempt-1",
            ))

        self.assertEqual(result["service"], "5sim")
        self.assertEqual(result["id"], "provider-1")
        self.assertTrue(result["local_order_id"])
        order = self.store.get_order(result["local_order_id"])
        self.assertEqual(order["state"], ORDER_STATE_ACTIVE)
        self.assertEqual(order["job_id"], "job-1")
        self.assertEqual(order["attempt_id"], "attempt-1")
        self.assertTrue(order["phone_label"].endswith("1234"))

    def test_allocation_rejects_disabled_persistence_before_provider_call(self):
        provider = AsyncMock(side_effect=AssertionError("provider must not be called"))
        with patch.object(sms_manager, "_get_5sim_phone", provider):
            with self.assertRaises(ValueError):
                asyncio.run(sms_manager.get_phone_from_any_service(
                    order_store=False, job_id="job-1", attempt_id="attempt-1",
                ))
        provider.assert_not_awaited()

    def test_allocation_persistence_cancellation_compensates_provider_order(self):
        async def provider_allocate():
            return {"phone": "+14155551235", "id": "provider-persist-cancel"}

        class CancelledStore:
            def create_order(self, *_args, **_kwargs):
                raise asyncio.CancelledError()

        provider_cancel = AsyncMock(return_value=None)
        with patch.object(sms_manager, "_get_5sim_phone", provider_allocate), \
             patch.object(sms_manager, "SmsOrderStore", return_value=CancelledStore()), \
             patch.object(sms_manager, "_cancel_5sim_order", provider_cancel):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.get_phone_from_any_service(
                    job_id="job-cancel", attempt_id="attempt-cancel",
                ))

        provider_cancel.assert_awaited_once_with("provider-persist-cancel")

    def test_poll_records_code_received_without_persisting_otp(self):
        order = self.store.create_order("5sim", "provider-2", "+14155550002")
        self.store.activate(order["order_id"])

        async def provider_poll(_order_id, _wait):
            return "246810"

        with patch.object(sms_manager, "_poll_5sim_code", provider_poll):
            code = asyncio.run(sms_manager.get_code_from_service(
                "5sim", "provider-2", wait_time=1,
                order_store=self.store,
            ))

        self.assertEqual(code, "246810")
        saved = self.store.get_order(order["order_id"])
        self.assertEqual(saved["state"], ORDER_STATE_CODE_RECEIVED)
        raw = json.dumps(sqlite3.connect(self.db_path).execute(
            "SELECT * FROM sms_orders"
        ).fetchone())
        self.assertNotIn("246810", raw)

    def test_cancelled_poll_records_compensation_before_propagating(self):
        order = self.store.create_order("5sim", "provider-poll-cancelled", "+14155550024")
        self.store.activate(order["order_id"])

        async def cancelled(_order_id, _wait):
            raise asyncio.CancelledError()

        with patch.object(sms_manager, "_poll_5sim_code", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.get_code_from_service(
                    "5sim", "provider-poll-cancelled", order_store=self.store,
                ))

        saved = self.store.get_order(order["order_id"])
        self.assertEqual(saved["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(saved["last_error_code"], "provider_timeout")

    def test_cancel_success_is_idempotent_and_provider_failure_is_durable(self):
        order = self.store.create_order("5sim", "provider-3", "+14155550003")
        self.store.activate(order["order_id"])

        async def provider_cancel(_order_id):
            return None

        with patch.object(sms_manager, "_cancel_5sim_order", provider_cancel):
            first = asyncio.run(sms_manager.cancel_order(
                "5sim", "provider-3", order_store=self.store,
            ))
            second = asyncio.run(sms_manager.cancel_order(
                "5sim", "provider-3", order_store=self.store,
            ))
        self.assertTrue(first["ok"])
        self.assertTrue(second["ok"])
        self.assertEqual(self.store.get_order(order["order_id"])["state"], "cancelled")

        pending = self.store.create_order("5sim", "provider-4", "+14155550004")
        self.store.activate(pending["order_id"])

        async def provider_failure(_order_id):
            raise RuntimeError("provider secret response")

        with patch.object(sms_manager, "_cancel_5sim_order", provider_failure):
            with self.assertRaises(sms_manager.SmsProviderError):
                asyncio.run(sms_manager.cancel_order(
                    "5sim", "provider-4", order_store=self.store,
                ))
        failed = self.store.get_order(pending["order_id"])
        self.assertEqual(failed["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(failed["last_error_code"], "provider_error")

    def test_false_provider_cancel_result_is_durable_failure(self):
        order = self.store.create_order("5sim", "provider-false-cancel", "+14155550026")
        self.store.activate(order["order_id"])

        async def provider_cancel(_order_id):
            return False

        with patch.object(sms_manager, "_cancel_5sim_order", provider_cancel):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.cancel_order(
                    "5sim", "provider-false-cancel", order_store=self.store,
                    backoff_seconds=0,
                ))

        self.assertEqual(raised.exception.error_code, "provider_rejected")
        self.assertEqual(
            self.store.get_order(order["order_id"])["state"],
            ORDER_STATE_CANCEL_PENDING,
        )

    def test_false_provider_finish_result_is_durable_failure(self):
        order = self.store.create_order("5sim", "provider-false-finish", "+14155550027")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])

        async def provider_finish(_order_id):
            return {"success": False}

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.finish_order(
                    "5sim", "provider-false-finish", order_store=self.store,
                    backoff_seconds=0,
                ))

        self.assertEqual(raised.exception.error_code, "provider_rejected")
        self.assertEqual(
            self.store.get_order(order["order_id"])["state"],
            "finish_pending",
        )

    def test_finish_local_finalize_failure_keeps_finish_operation_code(self):
        order = self.store.create_order("5sim", "provider-local-finalize-failure", "+14155550028")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])

        async def provider_finish(_order_id):
            return None

        original_record = self.store.record_compensation

        def fail_finalize(*args, **kwargs):
            raise OSError("local sqlite unavailable")

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish), \
             patch.object(self.store, "record_compensation", side_effect=fail_finalize):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.finish_order(
                    "5sim", "provider-local-finalize-failure", order_store=self.store,
                ))

        self.assertEqual(raised.exception.operation, "finish")
        self.assertEqual(raised.exception.error_code, "provider_error")
        # The provider was already told to finish; no caller may reinterpret
        # this local persistence failure as permission to cancel the order.
        self.assertEqual(self.store.get_order(order["order_id"])["state"], "finish_pending")

    def test_cancelled_provider_call_releases_compensation_claim(self):
        order = self.store.create_order("5sim", "provider-cancelled", "+14155550015")
        self.store.activate(order["order_id"])

        async def provider_cancel(_order_id):
            raise asyncio.CancelledError()

        with patch.object(sms_manager, "_cancel_5sim_order", provider_cancel):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.cancel_order(
                    "5sim", "provider-cancelled", order_store=self.store,
                    max_attempts=3, backoff_seconds=0,
                ))

        saved = self.store.get_order(order["order_id"])
        self.assertEqual(saved["state"], ORDER_STATE_CANCEL_PENDING)
        self.assertEqual(saved["compensation_claim_token"], "")
        self.assertEqual(saved["compensation_attempts"], 1)

    def test_cancelled_finish_call_releases_compensation_claim(self):
        order = self.store.create_order("5sim", "provider-finish-cancelled", "+14155550016")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])

        async def provider_finish(_order_id):
            raise asyncio.CancelledError()

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.finish_order(
                    "5sim", "provider-finish-cancelled", order_store=self.store,
                    max_attempts=3, backoff_seconds=0,
                ))

        saved = self.store.get_order(order["order_id"])
        self.assertEqual(saved["state"], "finish_pending")
        self.assertEqual(saved["compensation_claim_token"], "")
        self.assertEqual(saved["compensation_attempts"], 1)
        self.assertEqual(saved["last_error_code"], "provider_timeout")

    def test_provider_timeout_and_rejection_keep_machine_error_codes(self):
        cancel = self.store.create_order("5sim", "provider-timeout", "+14155550017")
        self.store.activate(cancel["order_id"])

        async def timeout(_order_id):
            raise asyncio.TimeoutError()

        with patch.object(sms_manager, "_cancel_5sim_order", timeout):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.cancel_order(
                    "5sim", "provider-timeout", order_store=self.store,
                    max_attempts=2, backoff_seconds=0,
                ))
        self.assertEqual(raised.exception.error_code, "provider_timeout")
        self.assertEqual(
            self.store.get_order(cancel["order_id"])["last_error_code"],
            "provider_timeout",
        )

        finish = self.store.create_order("5sim", "provider-rejection", "+14155550018")
        self.store.activate(finish["order_id"])
        self.store.mark_awaiting_code(finish["order_id"])
        self.store.mark_code_received(finish["order_id"])

        async def rejection(_order_id):
            raise RuntimeError("provider rejected secret payload")

        with patch.object(sms_manager, "_finish_5sim_order", rejection):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.finish_order(
                    "5sim", "provider-rejection", order_store=self.store,
                    max_attempts=2, backoff_seconds=0,
                ))
        self.assertEqual(raised.exception.error_code, "provider_error")
        self.assertEqual(
            self.store.get_order(finish["order_id"])["last_error_code"],
            "provider_error",
        )

    def test_provider_http_failure_is_not_treated_as_success(self):
        class Response:
            status = 503

            async def text(self):
                return "provider-secret-error"

        class Request:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Session:
            def __init__(self):
                self.request = Request()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def get(self, *_args, **_kwargs):
                return self.request

        with patch.object(sms_manager.aiohttp, "ClientSession", Session):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager._cancel_5sim_order("provider-http-failure"))
        self.assertEqual(raised.exception.error_code, "provider_rejected")

    def test_provider_error_body_is_not_treated_as_success(self):
        class Response:
            status = 200

            async def text(self):
                return "ERROR_SQL_PROVIDER_SECRET"

        class Request:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def get(self, *_args, **_kwargs):
                return Request()

        with patch.object(sms_manager.aiohttp, "ClientSession", Session):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager._finish_5sim_order("provider-body-failure"))
        self.assertEqual(raised.exception.error_code, "provider_rejected")

    def test_nested_json_failure_body_is_not_treated_as_success(self):
        class Response:
            status = 200

            async def text(self):
                return '{"ok":true,"data":{"success":false,"error":"denied"}}'

        class Request:
            async def __aenter__(self):
                return Response()

            async def __aexit__(self, *_args):
                return False

        class Session:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            def get(self, *_args, **_kwargs):
                return Request()

        with patch.object(sms_manager.aiohttp, "ClientSession", Session):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager._finish_5sim_order("provider-nested-body-failure"))
        self.assertEqual(raised.exception.error_code, "provider_rejected")

    def test_json_only_provider_failure_body_is_not_treated_as_success(self):
        """Adapters must inspect structured JSON even when text() is absent."""
        class Response:
            status = 200

            async def json(self):
                return {"data": {"errors": [{"code": "denied"}]}}

        with self.assertRaises(sms_manager.SmsProviderError) as raised:
            asyncio.run(sms_manager._validated_provider_response(
                Response(), "5sim", "finish"
            ))
        self.assertEqual(raised.exception.error_code, "provider_rejected")

    def test_finish_success_closes_order_without_exposing_provider_payload(self):
        order = self.store.create_order("5sim", "provider-5", "+14155550005")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])

        async def provider_finish(_order_id):
            return {"secret": "provider-payload"}

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish):
            result = asyncio.run(sms_manager.finish_order(
                "5sim", "provider-5", order_store=self.store,
            ))
        self.assertTrue(result["ok"])
        self.assertEqual(self.store.get_order(order["order_id"])["state"], ORDER_STATE_COMPLETED)
        self.assertNotIn("provider-payload", json.dumps(self.store.get_order(order["order_id"])))

    def test_finish_marks_order_pending_before_calling_provider(self):
        order = self.store.create_order("5sim", "provider-finish-pending", "+14155550008")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])
        observed_states = []

        async def provider_finish(_order_id):
            observed_states.append(self.store.get_order(order["order_id"])["state"])

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish):
            result = asyncio.run(sms_manager.finish_order(
                "5sim", "provider-finish-pending", order_store=self.store,
            ))

        self.assertTrue(result["ok"])
        self.assertEqual(observed_states, ["finish_pending"])

    def test_reconcile_finishes_code_received_order_after_process_restart(self):
        order = self.store.create_order("5sim", "provider-finish-restart", "+14155550009")
        self.store.activate(order["order_id"])
        self.store.mark_awaiting_code(order["order_id"])
        self.store.mark_code_received(order["order_id"])
        provider_finish = AsyncMock(return_value=None)

        with patch.object(sms_manager, "_finish_5sim_order", provider_finish):
            result = asyncio.run(sms_manager.reconcile_expired_orders(
                order_store=self.store, max_attempts=2, backoff_seconds=0,
            ))

        self.assertEqual(result["completed"], 1)
        self.assertEqual(self.store.get_order(order["order_id"])["state"], ORDER_STATE_COMPLETED)
        provider_finish.assert_awaited_once_with("provider-finish-restart")

    def test_reconcile_expired_orders_retries_cancel_with_bounded_attempts(self):
        order = self.store.create_order("5sim", "provider-6", "+14155550006")
        old = "2000-01-01T00:00:00+00:00"
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET lease_expires_at=?, next_action_at=? WHERE order_id=?",
                (old, old, order["order_id"]),
            )
        calls = []

        async def flaky_cancel(provider_order_id):
            calls.append(provider_order_id)
            if len(calls) == 1:
                raise RuntimeError("provider payload")

        with patch.object(sms_manager, "_cancel_5sim_order", flaky_cancel):
            first = asyncio.run(sms_manager.reconcile_expired_orders(
                order_store=self.store, max_attempts=2, backoff_seconds=0,
            ))
            self.assertEqual(first["cancelled"], 0)
            self.assertEqual(first["failed"], 1)
            second = asyncio.run(sms_manager.reconcile_expired_orders(
                order_store=self.store, max_attempts=2, backoff_seconds=0,
            ))

        self.assertEqual(second["cancelled"], 1)
        self.assertEqual(calls, ["provider-6", "provider-6"])
        self.assertEqual(self.store.get_order(order["order_id"])["state"], "cancelled")

    def test_reconcile_cancellation_releases_claim_for_a_later_worker(self):
        order = self.store.create_order("5sim", "provider-reconcile-cancelled", "+14155550019")
        old = "2000-01-01T00:00:00+00:00"
        with self.store.connect() as conn:
            conn.execute(
                "UPDATE sms_orders SET lease_expires_at=?, next_action_at=? WHERE order_id=?",
                (old, old, order["order_id"]),
            )

        async def cancelled(_order_id):
            raise asyncio.CancelledError()

        with patch.object(sms_manager, "_cancel_5sim_order", cancelled):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=self.store, max_attempts=3, backoff_seconds=0,
                ))

        saved = self.store.get_order(order["order_id"])
        self.assertEqual(saved["state"], "cancel_pending")
        self.assertEqual(saved["compensation_claim_token"], "")
        self.assertEqual(saved["compensation_attempts"], 1)

    def test_poll_code_is_not_returned_when_local_success_persistence_fails(self):
        order = self.store.create_order("5sim", "provider-poll-persist-failure", "+14155550033")
        self.store.activate(order["order_id"])

        async def provider_poll(_order_id, _wait):
            return "246810"

        with patch.object(sms_manager, "_poll_5sim_code", provider_poll), \
             patch.object(self.store, "mark_code_received", side_effect=OSError("db down")) as mark_code, \
             patch.object(self.store, "request_cancel", return_value=order) as request_cancel:
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.get_code_from_service(
                    "5sim", "provider-poll-persist-failure", wait_time=1,
                    order_store=self.store,
                ))

        self.assertEqual(raised.exception.error_code, "provider_error")
        mark_code.assert_called_once_with(order["order_id"])
        request_cancel.assert_called_once()

    def test_poll_cancellation_persistence_failure_is_not_swallowed(self):
        order = self.store.create_order("5sim", "provider-poll-cancel-persist", "+14155550034")
        self.store.activate(order["order_id"])

        async def provider_poll(_order_id, _wait):
            raise asyncio.CancelledError()

        with patch.object(sms_manager, "_poll_5sim_code", provider_poll), \
             patch.object(self.store, "mark_polled", side_effect=OSError("db down")):
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(sms_manager.get_code_from_service(
                    "5sim", "provider-poll-cancel-persist", order_store=self.store,
                ))

        # A failed local write must not make the order look completed; the
        # order is moved to the durable cancellation queue for reconciliation.
        self.assertEqual(
            self.store.get_order(order["order_id"])["state"], "cancel_pending"
        )

    def test_allocation_persistence_and_provider_cleanup_failure_is_terminal(self):
        async def provider_allocate():
            return {"phone": "+14155551236", "id": "provider-persist-hard-failure"}

        async def provider_cancel(_provider_order_id):
            raise RuntimeError("provider unavailable")

        class BrokenStore:
            def create_order(self, *_args, **_kwargs):
                raise OSError("database unavailable")

        with patch.object(sms_manager, "_get_5sim_phone", provider_allocate), \
             patch.object(sms_manager, "SmsOrderStore", return_value=BrokenStore()), \
             patch.object(sms_manager, "_cancel_5sim_order", provider_cancel):
            with self.assertRaises(sms_manager.SmsProviderError) as raised:
                asyncio.run(sms_manager.get_phone_from_any_service(
                    job_id="job-persist-failure", attempt_id="attempt-persist-failure",
                ))

        self.assertEqual(raised.exception.operation, "allocate")
        self.assertEqual(raised.exception.error_code, "provider_error")


if __name__ == "__main__":
    unittest.main()
