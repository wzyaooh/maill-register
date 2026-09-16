import asyncio
import io
import json
import logging
import os
import signal
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from collections import Counter
from datetime import datetime, timedelta, timezone

from core.sms_orders import SmsOrderStore, ORDER_STATE_COMPENSATION_FAILED
from services import sms_manager


class SmsCompensationTests(unittest.TestCase):
    def test_slow_batch_claims_orders_when_ready_and_never_repeats_acknowledgements(self):
        from pathlib import Path
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        base_time = datetime.now(timezone.utc)

        class Clock(datetime):
            elapsed = 0

            @classmethod
            def now(cls, tz=None):
                value = base_time + timedelta(seconds=cls.elapsed)
                return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

        calls = []

        async def cancel(provider_order_id):
            calls.append(provider_order_id)
            Clock.elapsed += 10
            return {"ok": True}

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(str(Path(directory) / "database.db"))
            with patch("core.sms_orders.datetime", Clock), patch.object(
                sms_manager, "_provider_functions", return_value={"cancel": cancel}
            ):
                for number in range(7):
                    order = ledger.sms_orders.create_order("5sim", "slow-" + str(number))
                    ledger.sms_orders.request_cancel(order["order_id"])
                first = run_sms_compensation_job(ledger=ledger, limit=7)
                self.assertEqual(first, {"claimed": 7, "cancelled": 7, "completed": 0, "failed": 0})
                self.assertFalse(any(
                    order["state"] == "cancel_pending" for order in ledger.sms_orders.list_orders()
                ))
                run_sms_compensation_job(ledger=ledger, limit=7)
                self.assertEqual(Counter(calls), {"slow-" + str(number): 1 for number in range(7)})

    def test_lost_inflight_claim_cannot_be_reported_as_a_successful_pass(self):
        from pathlib import Path
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(str(Path(directory) / "database.db"))
            order = ledger.sms_orders.create_order("5sim", "claim-lost")
            ledger.sms_orders.request_cancel(order["order_id"])
            calls = []

            async def cancel(provider_order_id):
                calls.append(provider_order_id)
                with ledger.sms_orders.connect() as conn:
                    conn.execute(
                        "UPDATE sms_orders SET compensation_claim_token='new-owner' WHERE order_id=?",
                        (order["order_id"],),
                    )
                return {"ok": True}

            with patch.object(sms_manager, "_provider_functions", return_value={"cancel": cancel}):
                result = run_sms_compensation_job(ledger=ledger, limit=1)
            self.assertEqual(result.get("error_code"), "compensation_claim_lost")
            self.assertEqual(ledger.list_jobs()[0]["state"], "failed")
            self.assertEqual(calls, ["claim-lost"])
            saved = ledger.sms_orders.get_order(order["order_id"])
            self.assertEqual(saved["state"], "cancel_pending")
            self.assertEqual(saved["compensation_claim_token"], "new-owner")

    def test_claim_lost_before_request_does_not_call_the_provider(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(directory + "/database.db")
            order = store.create_order("5sim", "lost-before-request")
            store.request_cancel(order["order_id"])
            original_claim = store.claim_due_compensation
            calls = []

            def claim(**kwargs):
                claimed = original_claim(**kwargs)
                with store.connect() as conn:
                    conn.execute(
                        "UPDATE sms_orders SET compensation_claim_token='new-owner' WHERE order_id=?",
                        (order["order_id"],),
                    )
                return claimed

            async def cancel(provider_order_id):
                calls.append(provider_order_id)
                return {"ok": True}

            with patch.object(store, "claim_due_compensation", side_effect=claim), patch.object(
                sms_manager, "_provider_functions", return_value={"cancel": cancel}
            ):
                result = asyncio.run(sms_manager.reconcile_expired_orders(order_store=store, limit=1))
            self.assertEqual(calls, [])
            self.assertEqual(result.get("error_code"), "compensation_claim_lost")

    def test_unresponsive_provider_has_a_bounded_request_and_durable_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(directory + "/database.db")
            order = store.create_order("5sim", "unresponsive-provider")
            store.request_cancel(order["order_id"])

            async def cancel(_provider_order_id):
                await asyncio.Event().wait()

            with patch.object(sms_manager, "_provider_functions", return_value={"cancel": cancel}):
                result = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, limit=1, provider_timeout=0.01,
                ))
            self.assertEqual(result.get("error_code"), "provider_timeout")
            self.assertEqual(result["failed"], 1)
            saved = store.get_order(order["order_id"])
            self.assertEqual(saved["state"], "cancel_pending")
            self.assertEqual(saved["compensation_attempts"], 1)
            self.assertEqual(saved["compensation_claim_token"], "")
            self.assertEqual(store.list_due_compensation(), [])

    def test_pass_time_budget_does_not_claim_unstarted_orders(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(directory + "/database.db")
            for number in range(3):
                order = store.create_order("5sim", "budget-" + str(number))
                store.request_cancel(order["order_id"])
            elapsed = [0]
            calls = []

            async def cancel(provider_order_id):
                calls.append(provider_order_id)
                elapsed[0] += 10
                return {"ok": True}

            with patch.object(sms_manager, "_provider_functions", return_value={"cancel": cancel}), patch(
                "services.sms_manager.monotonic", side_effect=lambda: elapsed[0], create=True,
            ):
                result = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, limit=3, time_budget_seconds=15,
                ))
            self.assertEqual(result, {"claimed": 2, "cancelled": 2, "completed": 0, "failed": 0})
            pending = [row for row in store.list_orders() if row["state"] == "cancel_pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["compensation_claim_token"], "")

    def test_shutdown_stops_before_claiming_another_order(self):
        import threading

        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(directory + "/database.db")
            for number in range(2):
                order = store.create_order("5sim", "shutdown-" + str(number))
                store.request_cancel(order["order_id"])
            stop = threading.Event()

            async def cancel(_provider_order_id):
                stop.set()
                return {"ok": True}

            with patch.object(sms_manager, "_provider_functions", return_value={"cancel": cancel}):
                result = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, limit=2, cancel_event=stop,
                ))
            self.assertEqual(result, {"claimed": 1, "cancelled": 1, "completed": 0, "failed": 0})
            pending = [row for row in store.list_orders() if row["state"] == "cancel_pending"]
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["compensation_claim_token"], "")

    def test_failed_orders_are_not_reclaimed_twice_in_the_same_zero_backoff_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(directory + "/database.db")
            for number in range(2):
                order = store.create_order("5sim", "failed-" + str(number))
                store.request_cancel(order["order_id"])
            calls = []

            async def cancel(provider_order_id):
                calls.append(provider_order_id)
                return {"ok": False}

            with patch.object(sms_manager, "_provider_functions", return_value={"cancel": cancel}):
                result = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, limit=10, backoff_seconds=0,
                ))
            self.assertEqual(result["failed"], 2)
            self.assertEqual(Counter(calls), {"failed-0": 1, "failed-1": 1})
            self.assertEqual([row["compensation_attempts"] for row in store.list_orders()], [1, 1])

    def _run_worker_main(self, worker, task, task_directory, ledger_path):
        old_argv = sys.argv
        old_stdout, old_stderr = sys.stdout, sys.stderr
        old_signal_handler = signal.getsignal(signal.SIGTERM)
        old_secret_provider = worker._worker_secret_provider
        root_logger = logging.getLogger()
        old_handlers = root_logger.handlers[:]
        old_log_level = root_logger.level
        try:
            sys.argv = ["worker", task["id"]]
            sys.stdout = io.StringIO()
            sys.stderr = io.StringIO()
            with patch.dict(os.environ, {
                "WEB_TASK_DIRECTORY": str(task_directory),
                "LEDGER_DB_PATH": str(ledger_path),
                "WEB_PARENT_PID": str(os.getpid()),
            }, clear=False):
                worker.main()
            return sys.stdout.getvalue() + sys.stderr.getvalue()
        finally:
            sys.argv = old_argv
            sys.stdout, sys.stderr = old_stdout, old_stderr
            signal.signal(signal.SIGTERM, old_signal_handler)
            worker._worker_secret_provider = old_secret_provider
            for handler in root_logger.handlers[:]:
                root_logger.removeHandler(handler)
                handler.close()
            root_logger.handlers = old_handlers
            root_logger.setLevel(old_log_level)

    def test_scheduler_async_cancellation_closes_job_attempt_and_order_claim(self):
        from pathlib import Path
        from core.job_ledger import JobLedger
        from web.tasks import TaskManager

        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(TaskManager)
            manager.root = Path(directory)
            ledger = JobLedger(str(manager.root / "data" / "database.db"))
            order = ledger.sms_orders.create_order(
                "5sim", "scheduler-cancel", "+14155550101"
            )
            ledger.sms_orders.request_cancel(order["order_id"])
            with patch.object(sms_manager, "_cancel_5sim_order", new=AsyncMock(
                side_effect=asyncio.CancelledError()
            )), self.assertRaises(asyncio.CancelledError):
                manager.run_sms_compensation_once()

            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "cancelled")
            self.assertEqual(job["terminal_error_code"], "cancelled")
            attempts = ledger.list_attempts(job["job_id"])
            self.assertEqual([item["state"] for item in attempts], ["cancelled"])
            saved = ledger.sms_orders.get_order(order["order_id"])
            self.assertEqual(saved["state"], "cancel_pending")
            self.assertEqual(saved["compensation_claim_token"], "")

    def test_compensation_action_async_cancellation_closes_job(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(sms_manager, "reconcile_expired_orders", new=AsyncMock(
                     side_effect=asyncio.CancelledError()
                 )), self.assertRaises(asyncio.CancelledError):
                execute("compensation", {}, Mock())

            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "cancelled")
            self.assertEqual([item["state"] for item in ledger.list_attempts(job["job_id"])],
                             ["cancelled"])

    def test_startup_async_cancellation_closes_task_job_and_attempt(self):
        from pathlib import Path
        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            task_directory = Path(directory) / "data" / "web"
            db_path = task_directory.parent / "database.db"
            store = TaskStore(task_directory)
            task = store.create("validate", {})
            with patch.object(sms_manager, "reconcile_expired_orders", new=AsyncMock(
                side_effect=asyncio.CancelledError()
            )), patch.object(worker, "_invoke_execute", return_value={"success": True}):
                self._run_worker_main(worker, task, task_directory, db_path)

            self.assertEqual(store.get(task["id"])["status"], "cancelled")
            ledger = JobLedger(str(db_path))
            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "cancelled")
            self.assertEqual([item["state"] for item in ledger.list_attempts(job["job_id"])],
                             ["cancelled"])

    def test_compensation_backoff_does_not_turn_empty_retry_into_success(self):
        from core.job_ledger import JobLedger
        from web.worker import execute, run_sms_compensation_job

        for entrypoint in ("scheduler", "worker-action"):
            with self.subTest(entrypoint=entrypoint), tempfile.TemporaryDirectory() as directory:
                db_path = os.path.join(directory, "database.db")
                ledger = JobLedger(db_path)
                order = ledger.sms_orders.create_order(
                    "5sim", "deferred-cancel", "+14155550102"
                )
                ledger.sms_orders.request_cancel(order["order_id"])
                provider = AsyncMock(side_effect=TimeoutError())
                with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                     patch.object(sms_manager, "_cancel_5sim_order", provider):
                    if entrypoint == "scheduler":
                        result = run_sms_compensation_job(
                            ledger=ledger, backoff_seconds=3600
                        )
                    else:
                        result = execute(
                            "compensation", {"backoff_seconds": 3600}, Mock()
                        )

                job = ledger.list_jobs()[0]
                self.assertEqual(job["state"], "failed")
                self.assertEqual(job["terminal_error_code"], "provider_timeout")
                self.assertEqual(result["error_code"], "provider_timeout")
                self.assertEqual(
                    {key: result[key] for key in ("claimed", "cancelled", "completed", "failed")},
                    {"claimed": 1, "cancelled": 0, "completed": 0, "failed": 1},
                )
                attempts = ledger.list_attempts(job["job_id"])
                self.assertEqual(len(attempts), 1)
                self.assertTrue(all(item["state"] == "failed" for item in attempts))
                provider.assert_awaited_once()
                saved = ledger.sms_orders.get_order(order["order_id"])
                self.assertEqual(saved["state"], "cancel_pending")
                self.assertIsNotNone(saved["next_action_at"])

    def test_real_compensation_exhaustion_stops_job_retry(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            order = ledger.sms_orders.create_order(
                "5sim", "exhausted-cancel", "+14155550103"
            )
            ledger.sms_orders.request_cancel(order["order_id"])
            provider = AsyncMock(side_effect=TimeoutError())
            with patch.object(sms_manager, "_cancel_5sim_order", provider):
                result = run_sms_compensation_job(ledger=ledger, max_attempts=1)

            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "failed")
            self.assertEqual(result["error_code"], "compensation_failed")
            self.assertEqual(len(ledger.list_attempts(job["job_id"])), 1)
            self.assertEqual(ledger.sms_orders.get_order(order["order_id"])["state"],
                             ORDER_STATE_COMPENSATION_FAILED)
            provider.assert_awaited_once()

    def test_successful_bounded_pass_does_not_retry_unprocessed_backlog(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            orders = [ledger.sms_orders.create_order(
                "5sim", "bounded-cancel-%s" % index, "+14155550105"
            ) for index in range(4)]
            for order in orders:
                ledger.sms_orders.request_cancel(order["order_id"])
            provider = AsyncMock(return_value=None)
            with patch.object(sms_manager, "_cancel_5sim_order", provider):
                result = run_sms_compensation_job(ledger=ledger, limit=1)

            self.assertEqual(result, {
                "claimed": 1, "cancelled": 1, "completed": 0, "failed": 0,
            })
            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "succeeded")
            self.assertEqual([item["state"] for item in ledger.list_attempts(job["job_id"])],
                             ["succeeded"])
            self.assertEqual(len(ledger.sms_orders.list_orders(states=("cancel_pending",))), 3)
            provider.assert_awaited_once()

    def test_historical_exhausted_order_does_not_fail_new_successful_pass(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            exhausted = ledger.sms_orders.create_order(
                "5sim", "historical-exhausted", "+14155550106"
            )
            ledger.sms_orders.request_cancel(exhausted["order_id"])
            claimed = ledger.sms_orders.claim_due_compensation()[0]
            ledger.sms_orders.record_compensation(
                exhausted["order_id"], action="cancel", success=False,
                error_code="provider_timeout", max_attempts=1,
                claim_token=claimed["compensation_claim_token"],
            )
            pending = ledger.sms_orders.create_order(
                "5sim", "new-successful-cancel", "+14155550107"
            )
            ledger.sms_orders.request_cancel(pending["order_id"])
            provider = AsyncMock(return_value=None)
            with patch.object(sms_manager, "_cancel_5sim_order", provider):
                result = run_sms_compensation_job(ledger=ledger)

            self.assertEqual(result, {
                "claimed": 1, "cancelled": 1, "completed": 0, "failed": 0,
            })
            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "succeeded")
            self.assertEqual(ledger.sms_orders.get_order(exhausted["order_id"])["state"],
                             ORDER_STATE_COMPENSATION_FAILED)
            provider.assert_awaited_once_with("new-successful-cancel")

    def test_compensation_reentry_preserves_live_attempt_owner(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            job = ledger.create_job("compensation", job_id="shared-live-compensation")
            attempt = ledger.start_attempt(
                job["job_id"], subject="sms-compensation", worker_id="other-owner"
            )
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch("web.worker.reconcile_sms_orders_once") as reconciler:
                result = execute("compensation", {}, Mock(), job_id=job["job_id"])

            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "compensation_claimed")
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "running")
            saved = ledger.get_attempt(attempt["attempt_id"])
            self.assertEqual(saved["state"], "running")
            self.assertEqual(saved["worker_id"], "other-owner")
            reconciler.assert_not_called()

    def test_compensation_reentry_cannot_finalize_foreign_job_after_owner_finishes(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            job = ledger.create_job("compensation", job_id="transitioning-compensation")
            attempt = ledger.start_attempt(
                job["job_id"], subject="sms-compensation", worker_id="other-owner"
            )
            original_get_latest = JobLedger.get_latest_attempt
            transitioned = []

            def finish_after_first_observation(instance, *args, **kwargs):
                observed = original_get_latest(instance, *args, **kwargs)
                if observed and observed["attempt_id"] == attempt["attempt_id"] and not transitioned:
                    transitioned.append(observed["attempt_id"])
                    ledger.finish_attempt(
                        attempt["attempt_id"], outcome="succeeded", worker_id="other-owner"
                    )
                return observed

            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(JobLedger, "get_latest_attempt", new=finish_after_first_observation), \
                 patch("web.worker.reconcile_sms_orders_once") as reconciler:
                result = execute("compensation", {}, Mock(), job_id=job["job_id"])

            self.assertFalse(result["success"])
            self.assertEqual(result["error_code"], "compensation_claimed")
            self.assertEqual(transitioned, [attempt["attempt_id"]])
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "running")
            saved = ledger.get_attempt(attempt["attempt_id"])
            self.assertEqual(saved["state"], "succeeded")
            self.assertEqual(saved["worker_id"], "other-owner")
            reconciler.assert_not_called()

    def test_compensation_rejects_truthy_nonboolean_success_without_succeeded_attempt(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        for value in ("false", 1, [], None):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                ledger = JobLedger(os.path.join(directory, "database.db"))
                malformed = {
                    "claimed": 0, "cancelled": 0, "completed": 0, "failed": 0,
                    "success": value,
                }
                with patch("web.worker.reconcile_sms_orders_once", return_value=malformed):
                    result = run_sms_compensation_job(ledger=ledger)

                self.assertEqual(result.get("error_code"), "invalid_reconciliation_result")
                job = ledger.list_jobs()[0]
                self.assertEqual(job["state"], "failed")
                self.assertEqual([item["state"] for item in ledger.list_attempts(job["job_id"])],
                                 ["failed"])

    def test_compensation_rejects_boolean_result_without_succeeded_attempt(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            with patch("web.worker.reconcile_sms_orders_once", return_value=True):
                result = run_sms_compensation_job(ledger=ledger)

            self.assertEqual(result["error_code"], "invalid_reconciliation_result")
            job = ledger.list_jobs()[0]
            self.assertEqual([item["state"] for item in ledger.list_attempts(job["job_id"])],
                             ["failed"])

    def test_failed_policy_job_does_not_replay_committed_attempt_as_success(self):
        from core.job_ledger import JobLedger
        from core.retry_engine import RetryEngine
        from web.worker import execute

        class CommittedMalformedPolicy(RetryEngine):
            def record_attempt(self, *args, **kwargs):
                result = super().record_attempt(*args, **kwargs)
                result["retry_decision"] = "invalid"
                return result

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            job = ledger.create_job("compensation", job_id="committed-policy-failure")
            retry = CommittedMalformedPolicy(ledger=ledger, job_id=job["job_id"])
            reconciler = Mock(return_value={
                "claimed": 2, "cancelled": 2, "completed": 0, "failed": 0,
            })
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch("web.worker._operation_retry_engine", return_value=retry), \
                 patch("web.worker.reconcile_sms_orders_once", reconciler):
                first = execute("compensation", {}, Mock(), job_id=job["job_id"])
                replay = execute("compensation", {}, Mock(), job_id=job["job_id"])

            self.assertFalse(first["success"])
            self.assertEqual(replay, first)
            self.assertEqual(ledger.get_job(job["job_id"])["state"], "failed")
            self.assertEqual(ledger.list_attempts(job["job_id"])[0]["state"], "succeeded")
            reconciler.assert_called_once()

    def test_real_provider_rejection_preserves_terminal_job_policy(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            order = ledger.sms_orders.create_order(
                "5sim", "rejected-cancel", "+14155550104"
            )
            ledger.sms_orders.request_cancel(order["order_id"])
            provider = AsyncMock(return_value={"success": False})
            with patch.object(sms_manager, "_cancel_5sim_order", provider):
                result = run_sms_compensation_job(ledger=ledger)

            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "failed")
            self.assertEqual(result["error_code"], "provider_rejected")
            self.assertEqual(len(ledger.list_attempts(job["job_id"])), 1)
            provider.assert_awaited_once()

    def test_compensation_restart_reuses_completed_counters(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            reconciler = AsyncMock(return_value={
                "claimed": 3, "cancelled": 2, "completed": 1, "failed": 0,
            })
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(sms_manager, "reconcile_expired_orders", reconciler):
                execute("compensation", {}, Mock(), job_id="completed-compensation")
                result = execute(
                    "compensation", {}, Mock(), job_id="completed-compensation"
                )

            self.assertEqual(
                {key: result[key] for key in ("claimed", "cancelled", "completed", "failed")},
                {"claimed": 3, "cancelled": 2, "completed": 1, "failed": 0},
            )
            attempts = JobLedger(db_path).list_attempts("completed-compensation")
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["result_metadata"]["cancelled"], 2)
            reconciler.assert_awaited_once()

    def test_scheduler_protocol_failure_returns_only_complete_counters_and_code(self):
        from core.job_ledger import JobLedger
        from web.worker import run_sms_compensation_job

        with tempfile.TemporaryDirectory() as directory:
            ledger = JobLedger(os.path.join(directory, "database.db"))
            with patch("web.worker.reconcile_sms_orders_once", side_effect=RuntimeError(
                "unlabelled-provider-response-payload"
            )):
                result = run_sms_compensation_job(ledger=ledger)

            self.assertEqual(result, {
                "claimed": 0, "cancelled": 0, "completed": 0, "failed": 1,
                "error_code": "invalid_reconciliation_result",
            })
            job = ledger.list_jobs()[0]
            self.assertEqual(job["state"], "failed")
            self.assertNotIn("unlabelled-provider-response-payload",
                             json.dumps(job) + json.dumps(ledger.list_attempts(job["job_id"])))

    def test_compensation_job_runs_reconcile_and_closes_ledger(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            reconciler = AsyncMock(return_value={
                "claimed": 2, "cancelled": 2, "completed": 0, "failed": 0,
            })
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(sms_manager, "reconcile_expired_orders", reconciler):
                result = execute(
                    "compensation", {"limit": 10}, Mock(),
                    job_id="compensation-job",
                )

            self.assertEqual(result["cancelled"], 2)
            self.assertEqual(result["job_id"], "compensation-job")
            self.assertEqual(
                ledger.get_job("compensation-job")["state"], "succeeded"
            )
            reconciler.assert_awaited_once()
            call_kwargs = reconciler.await_args.kwargs
            self.assertEqual(call_kwargs["order_store"].db_path, ledger.sms_orders.db_path)
            self.assertEqual(call_kwargs["limit"], 10)

    def test_compensation_job_is_failed_when_provider_rows_remain_failed(self):
        from core.job_ledger import JobLedger
        from web.worker import execute

        with tempfile.TemporaryDirectory() as directory:
            db_path = os.path.join(directory, "database.db")
            ledger = JobLedger(db_path)
            with patch.dict(os.environ, {"LEDGER_DB_PATH": db_path}), \
                 patch.object(sms_manager, "reconcile_expired_orders", new=AsyncMock(
                     return_value={
                         "claimed": 1, "cancelled": 0, "completed": 0, "failed": 1,
                     }
                 )):
                result = execute(
                    "compensation", {}, Mock(), job_id="compensation-failed",
                )

            self.assertEqual(result["failed"], 1)
            self.assertEqual(
                ledger.get_job("compensation-failed")["state"], "failed"
            )

    def test_startup_hook_invokes_reconcile_once_and_is_best_effort(self):
        from web.worker import reconcile_sms_orders_once

        reconciler = AsyncMock(return_value={
            "claimed": 0, "cancelled": 0, "completed": 0, "failed": 0,
        })
        with patch.object(sms_manager, "reconcile_expired_orders", reconciler):
            result = reconcile_sms_orders_once(limit=7)

        self.assertEqual(result["failed"], 0)
        reconciler.assert_awaited_once()
        self.assertEqual(reconciler.await_args.kwargs["limit"], 7)

        failing = AsyncMock(side_effect=RuntimeError("provider payload"))
        with patch.object(sms_manager, "reconcile_expired_orders", failing):
            result = reconcile_sms_orders_once(limit=7)
        self.assertEqual(result["error_code"], "reconciliation_failed")

    def test_reconcile_projection_preserves_a_finite_provider_error_code(self):
        from web.worker import reconcile_sms_orders_once

        reconciler = AsyncMock(return_value={
            "claimed": 1, "cancelled": 0, "completed": 0, "failed": 1,
            "error_code": "provider_timeout",
        })
        with patch.object(sms_manager, "reconcile_expired_orders", reconciler):
            result = reconcile_sms_orders_once(limit=1)

        self.assertEqual(result["error_code"], "provider_timeout")

    def test_reconcile_projection_rejects_malformed_provider_counters(self):
        from web.worker import reconcile_sms_orders_once

        malformed = AsyncMock(return_value={
            "claimed": 1, "cancelled": 0, "completed": 0,
            "failed": "1",
        })
        with patch.object(sms_manager, "reconcile_expired_orders", malformed):
            result = reconcile_sms_orders_once(limit=1)

        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["error_code"], "invalid_reconciliation_result")

    def test_reconcile_projection_rejects_missing_provider_counters(self):
        from web.worker import reconcile_sms_orders_once

        complete = {
            "claimed": 1, "cancelled": 0, "completed": 0, "failed": 0,
        }
        for missing in complete:
            with self.subTest(missing=missing):
                malformed = dict(complete)
                malformed.pop(missing)
                reconciler = AsyncMock(return_value=malformed)
                with patch.object(
                    sms_manager, "reconcile_expired_orders", reconciler
                ):
                    result = reconcile_sms_orders_once(limit=1)

                self.assertEqual(result["failed"], 1)
                self.assertEqual(
                    result["error_code"], "invalid_reconciliation_result"
                )

    def test_exhausted_provider_compensation_is_terminal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = SmsOrderStore(os.path.join(directory, "database.db"))
            order = store.create_order("5sim", "provider-expire", "+14155550007")
            store.request_cancel(order["order_id"], error_code="sms_timeout")

            async def always_fails(_provider_order_id):
                raise RuntimeError("provider payload")

            with patch.object(sms_manager, "_cancel_5sim_order", always_fails):
                first = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, max_attempts=2, backoff_seconds=0,
                ))
                second = asyncio.run(sms_manager.reconcile_expired_orders(
                    order_store=store, max_attempts=2, backoff_seconds=0,
                ))

            self.assertEqual(first["failed"], 1)
            self.assertEqual(second["failed"], 1)
            self.assertEqual(
                store.get_order(order["order_id"])["state"],
                ORDER_STATE_COMPENSATION_FAILED,
            )

    def test_task_manager_exposes_internal_scheduler_hook(self):
        from pathlib import Path
        from web.tasks import TaskManager
        from core.job_ledger import JobLedger

        with tempfile.TemporaryDirectory() as directory:
            manager = object.__new__(TaskManager)
            manager.root = Path(directory)
            db_path = manager.root / "data" / "database.db"
            expected = {"claimed": 1, "cancelled": 1, "completed": 0, "failed": 0}
            with patch("web.worker.reconcile_sms_orders_once", return_value=expected) as hook:
                result = manager.run_sms_compensation_once(limit=5)

            self.assertEqual(result, expected)
            hook.assert_called_once()
            self.assertEqual(hook.call_args.kwargs["limit"], 5)
            self.assertEqual(hook.call_args.kwargs["order_store"].db_path, str(db_path))

            ledger = JobLedger(str(db_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "succeeded")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "succeeded")
            self.assertEqual(attempts[0]["strategy"], "compensation")

    def test_worker_startup_reconciliation_uses_durable_compensation_job(self):
        """Startup recovery must use the same durable compensation boundary."""
        from pathlib import Path

        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            task_directory = runtime / "data" / "web"
            task_store = TaskStore(task_directory)
            task = task_store.create("validate", {})
            ledger_path = runtime / "data" / "database.db"
            expected = {
                "claimed": 1,
                "cancelled": 1,
                "completed": 0,
                "failed": 0,
            }
            with patch.object(worker, "_invoke_execute", return_value={"success": True}), \
                 patch.object(worker, "recover_stale_attempts", return_value=0), \
                 patch.object(worker, "reconcile_sms_orders_once", return_value=expected), \
                 patch.object(worker, "run_sms_compensation_job", wraps=worker.run_sms_compensation_job) as startup_hook:
                self._run_worker_main(worker, task, task_directory, ledger_path)

            ledger = JobLedger(str(ledger_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "succeeded")
            self.assertEqual(jobs[0]["metadata"].get("source"),
                             "startup-reconciliation")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "succeeded")
            self.assertEqual(attempts[0]["strategy"], "compensation")
            self.assertFalse(any(item["state"] == "running" for item in attempts))
            self.assertNotIn("provider payload", repr(jobs[0]))
            self.assertNotIn("provider payload", repr(attempts[0]))
            startup_hook.assert_called_once()
            self.assertEqual(
                startup_hook.call_args.kwargs["source"],
                "startup-reconciliation",
            )

    def test_startup_compensation_provider_error_is_projected_and_terminal(self):
        """Startup provider failures persist only finite codes and no running rows."""
        from pathlib import Path

        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            task_directory = runtime / "data" / "web"
            task_store = TaskStore(task_directory)
            task = task_store.create("validate", {})
            ledger_path = runtime / "data" / "database.db"
            expected = {
                "claimed": 0,
                "cancelled": 0,
                "completed": 0,
                "failed": 1,
                "error_code": "reconciliation_failed",
            }
            with patch.object(worker, "_invoke_execute", return_value={"success": True}), \
                 patch.object(worker, "recover_stale_attempts", return_value=0), \
                 patch.object(worker, "reconcile_sms_orders_once", return_value=expected), \
                 patch.object(worker, "run_sms_compensation_job", wraps=worker.run_sms_compensation_job) as startup_hook:
                self._run_worker_main(worker, task, task_directory, ledger_path)

            ledger = JobLedger(str(ledger_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "failed")
            self.assertEqual(jobs[0]["terminal_error_code"],
                             "reconciliation_failed")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertGreaterEqual(len(attempts), 1)
            self.assertTrue(all(item["state"] == "failed" for item in attempts))
            self.assertFalse(any(item["state"] == "running" for item in attempts))
            self.assertNotIn("provider payload", repr(jobs[0]))
            self.assertNotIn("provider payload", repr(attempts[0]))
            startup_hook.assert_called_once()
            self.assertEqual(
                startup_hook.call_args.kwargs["source"],
                "startup-reconciliation",
            )

    def test_startup_compensation_cancellation_closes_job_and_attempt(self):
        """Cancellation during startup compensation must not leave a running attempt."""
        from pathlib import Path

        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory)
            task_directory = runtime / "data" / "web"
            task_store = TaskStore(task_directory)
            task = task_store.create("validate", {})
            ledger_path = runtime / "data" / "database.db"
            with patch.object(worker, "_invoke_execute", return_value={"success": True}), \
                 patch.object(worker, "recover_stale_attempts", return_value=0), \
                 patch.object(worker, "reconcile_sms_orders_once", side_effect=KeyboardInterrupt), \
                 patch.object(worker, "run_sms_compensation_job", wraps=worker.run_sms_compensation_job) as startup_hook:
                self._run_worker_main(worker, task, task_directory, ledger_path)

            ledger = JobLedger(str(ledger_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "cancelled")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertEqual(len(attempts), 1)
            self.assertEqual(attempts[0]["state"], "cancelled")
            self.assertFalse(any(item["state"] == "running" for item in attempts))
            startup_hook.assert_called_once()
            self.assertEqual(
                startup_hook.call_args.kwargs["source"],
                "startup-reconciliation",
            )

    def test_startup_reconciliation_retries_transient_provider_failure(self):
        from pathlib import Path

        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            task_directory = Path(directory) / "data" / "web"
            ledger_path = task_directory.parent / "database.db"
            store = TaskStore(task_directory)
            task = store.create("validate", {})
            provider_payload = "unlabelled-provider-api-key-otp-payload"
            transient = {
                "claimed": 1, "cancelled": 0, "completed": 0, "failed": 1,
                "error_code": "provider_timeout", "message": provider_payload,
                "payload": {"credential": provider_payload},
            }
            successful = {
                "claimed": 1, "cancelled": 1, "completed": 0, "failed": 0,
                "payload": provider_payload,
            }
            reconciler = AsyncMock(side_effect=[transient, successful])
            execute = Mock(return_value={"success": True})
            with patch.object(sms_manager, "reconcile_expired_orders", reconciler), \
                 patch.object(worker, "_invoke_execute", execute):
                output = self._run_worker_main(
                    worker, task, task_directory, ledger_path
                )

            ledger = JobLedger(str(ledger_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "succeeded")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertEqual([item["state"] for item in attempts],
                             ["failed", "succeeded"])
            self.assertEqual(attempts[0]["error_code"], "provider_timeout")
            self.assertEqual(reconciler.await_count, 2)
            self.assertFalse(any(item["state"] == "running" for item in attempts))
            self.assertNotIn(provider_payload, json.dumps(jobs + attempts))
            self.assertNotIn(provider_payload, output)
            execute.assert_called_once()

    def test_startup_reconciliation_terminal_provider_failure_does_not_retry(self):
        from pathlib import Path

        from core.job_ledger import JobLedger
        from web.tasks import TaskStore
        import web.worker as worker

        with tempfile.TemporaryDirectory() as directory:
            task_directory = Path(directory) / "data" / "web"
            ledger_path = task_directory.parent / "database.db"
            store = TaskStore(task_directory)
            task = store.create("validate", {})
            provider_payload = "terminal-provider-payload-with-otp"
            reconciler = AsyncMock(return_value={
                "claimed": 1, "cancelled": 0, "completed": 0, "failed": 1,
                "error_code": "compensation_failed",
                "message": provider_payload, "response": provider_payload,
            })
            execute = Mock(return_value={"success": True})
            with patch.object(sms_manager, "reconcile_expired_orders", reconciler), \
                 patch.object(worker, "_invoke_execute", execute):
                output = self._run_worker_main(
                    worker, task, task_directory, ledger_path
                )

            ledger = JobLedger(str(ledger_path))
            jobs = [item for item in ledger.list_jobs()
                    if item.get("kind") == "compensation"]
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]["state"], "failed")
            self.assertEqual(jobs[0]["terminal_error_code"],
                             "compensation_failed")
            attempts = ledger.list_attempts(jobs[0]["job_id"])
            self.assertEqual([item["state"] for item in attempts], ["failed"])
            self.assertEqual(attempts[0]["retry_decision"], "stop")
            reconciler.assert_awaited_once()
            self.assertNotIn(provider_payload, json.dumps(jobs + attempts))
            self.assertNotIn(provider_payload, output)
            execute.assert_called_once()


if __name__ == "__main__":
    unittest.main()
