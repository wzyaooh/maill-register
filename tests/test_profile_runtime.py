import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from core.profile_runtime import (
    BrowserObservation,
    ProfileConflictError,
    ProfileRuntime,
    ProfileUnavailableError,
    ProfileBusyError,
    ProxyUnavailableError,
    build_identity,
    classify_browser_observation,
    derive_overall_status,
    proxy_binding,
)


class ProfileRuntimeTests(unittest.TestCase):
    def test_browser_observation_mapping_and_attributes_stay_synchronised(self):
        observation = BrowserObservation(
            authenticated=True,
            code="authenticated",
            identity_confidence="bound",
        )

        observation.update(
            authenticated=False,
            code="cleanup_failed",
            cleanup_status="failed",
        )

        self.assertIs(observation["authenticated"], observation.authenticated)
        self.assertEqual(observation["code"], observation.code)
        self.assertEqual(observation["cleanup_status"], observation.cleanup_status)
        self.assertEqual(observation.status, "cleanup_failed")

        observation["code"] = "login_required"
        self.assertEqual(observation.code, "login_required")
        self.assertEqual(observation.status, "login_required")

    def test_browser_observation_attribute_mutations_update_mapping(self):
        observation = BrowserObservation(
            authenticated=True,
            code="authenticated",
            cleanup_status="completed",
        )

        observation.authenticated = False
        observation.code = "cleanup_failed"
        observation.cleanup_status = "failed"

        self.assertIs(observation["authenticated"], False)
        self.assertEqual(observation["code"], "cleanup_failed")
        self.assertEqual(observation["cleanup_status"], "failed")
        self.assertEqual(observation.status, "cleanup_failed")

        del observation.code
        self.assertNotIn("code", observation)

    def test_provision_uses_random_contained_profile_and_secure_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")

            self.assertRegex(handle.profile_id, r"^[0-9a-f-]{32,36}$")
            self.assertTrue(str(handle.path).startswith(str(Path(directory).resolve())))
            self.assertEqual(handle.path.parent.name, "profiles")
            self.assertEqual(handle.path.stat().st_mode & 0o777, 0o700)
            self.assertEqual(handle.manifest_path.stat().st_mode & 0o777, 0o600)
            manifest = runtime.load(handle)
            self.assertEqual(manifest["state"], "provisioning")
            self.assertEqual(manifest["engine"], "playwright")
            self.assertEqual(manifest["email"], "user@example.test")
            self.assertNotIn("password", json.dumps(manifest).lower())

    def test_provision_rejects_secret_identity_extensions(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            identity = build_identity("profile-secrets", "playwright")
            identity.update({"api_key": "manifest-api-token", "otp": "246810"})
            with self.assertRaises(ProfileConflictError):
                runtime.provision("user@example.test", "playwright", identity=identity)

    def test_identity_is_stable_and_engine_specific(self):
        first = build_identity("profile-1", "playwright")
        second = build_identity("profile-1", "playwright")
        other = build_identity("profile-2", "playwright")
        selenium = build_identity("profile-1", "selenium")
        self.assertEqual(first, second)
        self.assertNotEqual(first, other)
        self.assertEqual(selenium, build_identity("profile-1", "selenium"))
        self.assertIn("user_agent", first)
        self.assertIn("timezone_id", first)

    def test_proxy_binding_contains_only_non_secret_endpoint_hash(self):
        bound = proxy_binding("user:secret@example.test:443")
        same_endpoint = proxy_binding("other:password@example.test:443")
        self.assertEqual(bound["endpoint_hash"], same_endpoint["endpoint_hash"])
        self.assertNotIn("secret", json.dumps(bound))
        self.assertNotIn("password", json.dumps(bound))

    def test_lifecycle_bind_ready_and_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "selenium")
            runtime.bind(handle, "user@example.test", {"channel": "chrome", "major_version": "120"})
            self.assertEqual(runtime.load(handle)["state"], "bound")
            runtime.mark_ready(handle)
            self.assertEqual(runtime.load(handle)["state"], "ready")
            with self.assertRaises(ProfileConflictError) as raised:
                runtime.resolve(profile_id=handle.profile_id, expected_engine="playwright")
            self.assertEqual(raised.exception.code, "profile_conflict")
            with self.assertRaises(ProfileConflictError):
                runtime.resolve(profile_id=handle.profile_id, expected_email="other@example.test")

    def test_bind_without_runtime_info_preserves_provisioning_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            runtime.bind(handle, "user@example.test")
            self.assertEqual(runtime.load(handle)["browser"]["channel"], "chrome")

    def test_empty_runtime_observation_does_not_replace_recorded_channel(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            runtime.bind(handle, "user@example.test", {
                "channel": "chrome", "major_version": "120",
            })
            before = runtime.load(handle)["browser"]

            runtime.record_runtime(handle, {})

            self.assertEqual(runtime.load(handle)["browser"], before)

    def test_lifecycle_does_not_rebind_or_record_runtime_on_retired_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            runtime.bind(handle, "user@example.test")
            runtime.mark_ready(handle)
            runtime._write_update(handle, state="retired")
            with self.assertRaises(ProfileConflictError):
                runtime.bind(handle, "user@example.test")
            with self.assertRaises(ProfileConflictError):
                runtime.record_runtime(handle, {"channel": "chrome", "major_version": "120"})

    def test_invalid_or_missing_manifest_is_unavailable(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            handle.manifest_path.write_text("{broken", encoding="utf-8")
            with self.assertRaises(ProfileUnavailableError) as raised:
                runtime.resolve(profile_id=handle.profile_id)
            self.assertEqual(raised.exception.code, "profile_unavailable")

    def test_manifest_type_and_symlink_validation_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            manifest = runtime.load(handle)
            manifest["email"] = None
            runtime._atomic_json(handle.manifest_path, manifest)
            with self.assertRaises(ProfileUnavailableError):
                runtime.resolve(profile_id=handle.profile_id)

            # Restore a valid manifest, then ensure a symlink cannot be used as
            # the manifest file to escape the profile directory.
            manifest["email"] = "user@example.test"
            runtime._atomic_json(handle.manifest_path, manifest)
            target = Path(outside) / "manifest.json"
            target.write_text(json.dumps(manifest), encoding="utf-8")
            handle.manifest_path.unlink()
            handle.manifest_path.symlink_to(target)
            with self.assertRaises(ProfileUnavailableError):
                runtime.resolve(profile_id=handle.profile_id)

    def test_manifest_schema_rejects_unsupported_versions_and_secret_fields(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            manifest = runtime.load(handle)
            manifest["schema_version"] = 99
            runtime._atomic_json(handle.manifest_path, manifest)
            with self.assertRaises(ProfileUnavailableError):
                runtime.resolve(profile_id=handle.profile_id)

            manifest["schema_version"] = runtime.schema_version
            manifest["password"] = "must-not-be-persisted"
            runtime._atomic_json(handle.manifest_path, manifest)
            with self.assertRaises(ProfileUnavailableError):
                runtime.resolve(profile_id=handle.profile_id)

    def test_manifest_schema_rejects_secret_key_casing_and_separator_variants(self):
        variants = (
            "APIKey", "verificationCode", "CookieValue", "session_id",
        )
        for key in variants:
            with self.subTest(key=key), tempfile.TemporaryDirectory() as directory:
                runtime = ProfileRuntime(directory)
                handle = runtime.provision("user@example.test", "playwright")
                manifest = runtime.load(handle)
                manifest[key] = "must-not-be-persisted"
                runtime._atomic_json(handle.manifest_path, manifest)

                with self.assertRaises(ProfileUnavailableError):
                    runtime.resolve(profile_id=handle.profile_id)

    def test_load_and_lease_reject_a_handle_outside_this_runtime(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            foreign = ProfileRuntime(outside).provision("foreign@example.test", "playwright")
            with self.assertRaises(ProfileConflictError):
                runtime.load(foreign)
            with self.assertRaises(ProfileConflictError):
                runtime.lease(foreign, "health")

    def test_lease_rejects_a_replaced_lock_symlink(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            target = Path(outside) / "foreign.lock"
            target.touch()
            handle.lock_path.unlink()
            handle.lock_path.symlink_to(target)
            with self.assertRaises(ProfileConflictError):
                with runtime.lease(handle, "health"):
                    pass

    def test_lease_detects_profile_directory_replacement_after_acquisition(self):
        """The lease must stay tied to the directory it validated."""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            with runtime.lease(handle, "warm") as lease:
                moved = Path(directory) / "moved-profile"
                handle.path.rename(moved)
                handle.path.symlink_to(Path(outside), target_is_directory=True)
                with self.assertRaises(ProfileConflictError):
                    lease.assert_stable()

    def test_lease_release_reports_unverified_when_close_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            lease = runtime.lease(handle, "warm")
            lease.acquire()
            original_close = lease._close_stream

            def failed_close():
                lease._held = True
                lease._release_verified = False
                return False

            lease._close_stream = failed_close
            self.assertFalse(lease.release())
            self.assertTrue(lease._held)
            self.assertFalse(lease._release_verified)
            lease._close_stream = original_close
            self.assertTrue(lease.release())

    def test_manifest_rejects_incomplete_persisted_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            manifest = runtime.load(handle)
            manifest["identity"] = {}
            runtime._atomic_json(handle.manifest_path, manifest)
            with self.assertRaises(ProfileUnavailableError):
                runtime.resolve(profile_id=handle.profile_id)

    def test_broken_profile_symlink_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            runtime = ProfileRuntime(directory)
            broken = runtime.profiles_root / "broken-profile"
            broken.symlink_to(Path(outside) / "removed", target_is_directory=True)

            with self.assertRaises(ProfileConflictError):
                runtime.resolve(profile_id="broken-profile")

    def test_lease_excludes_second_process(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            script = textwrap.dedent(
                """
                import sys
                from core.profile_runtime import ProfileRuntime, ProfileBusyError
                runtime = ProfileRuntime(sys.argv[1])
                handle = runtime.resolve(profile_id=sys.argv[2])
                try:
                    with runtime.lease(handle, 'probe'):
                        print('acquired', flush=True)
                        input()
                except ProfileBusyError:
                    print('busy', flush=True)
                """
            )
            child = subprocess.Popen(
                [sys.executable, "-c", script, directory, handle.profile_id],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
            )
            self.assertEqual(child.stdout.readline().strip(), "acquired")
            try:
                with self.assertRaises(ProfileBusyError):
                    with runtime.lease(handle, "health"):
                        pass
            finally:
                child.communicate("close\n", timeout=5)
            self.assertEqual(child.returncode, 0)

    def test_status_derivation_keeps_busy_distinct_from_locked(self):
        # A bare adapter boolean is not proof of a Google session.  The
        # classifier must see a trusted origin plus both an identity-bound
        # cookie and an explicit application shell before returning
        # authenticated.
        self.assertEqual(classify_browser_observation({"authenticated": True}), "login_required")
        self.assertEqual(classify_browser_observation({"challenge": True}), "challenge")
        self.assertEqual(classify_browser_observation({"profile_busy": True}), "profile_busy")
        self.assertEqual(classify_browser_observation({"code": "profile_conflict"}), "profile_conflict")
        self.assertEqual(classify_browser_observation({"code": "account_mismatch",
                                                       "application_signal": True}),
                         "account_mismatch")
        self.assertEqual(classify_browser_observation({"observed_email": "other@example.test"}, "user@example.test"), "account_mismatch")
        self.assertEqual(derive_overall_status("profile_busy", "active"), "degraded")
        self.assertEqual(derive_overall_status("authenticated", "active"), "active")
        self.assertEqual(derive_overall_status("challenge", "active"), "degraded")
        self.assertEqual(derive_overall_status("authenticated", "password_changed"), "password_changed")
        self.assertEqual(derive_overall_status("account_mismatch", "locked"), "degraded")
        self.assertEqual(derive_overall_status("profile_conflict", "locked"), "degraded")
        self.assertEqual(derive_overall_status("not_configured", "locked"), "locked")
        self.assertEqual(derive_overall_status("identity_unavailable", "active"), "degraded")
        self.assertEqual(
            classify_browser_observation({
                "code": "authenticated", "observed_email": "other@example.test",
            }, "user@example.test"),
            "account_mismatch",
        )

    def test_browser_observation_requires_trusted_origin_and_identity_evidence(self):
        self.assertEqual(
            classify_browser_observation({
                "authenticated": True,
                "origin": "https://mail.google.com",
                "application_signal": True,
            }, "user@example.test"),
            "login_required",
        )
        self.assertEqual(
            classify_browser_observation({
                "authenticated": True,
                "origin": "http://127.0.0.1",
                "observed_email": "user@example.test",
                "application_signal": True,
            }, "user@example.test"),
            "login_required",
        )
        self.assertEqual(
            classify_browser_observation({
                "authenticated": True,
                "origin": "https://mail.google.com/mail/u/0/#inbox",
                "observed_email": "user@example.test",
                "cookies": [{
                    "name": "SID", "value": "live", "domain": ".google.com",
                    "secure": True, "expires": 4102444800,
                }],
                "application_shell": True,
            }, "user@example.test"),
            "authenticated",
        )

    def test_browser_observation_negative_signals_override_positive_flags(self):
        observation = {
            "origin": "https://mail.google.com",
            "observed_email": "user@example.test",
            "identity_confidence": "verified",
            "application_signal": True,
            "authenticated": True,
            "challenge": True,
        }
        self.assertEqual(
            classify_browser_observation(observation, "user@example.test"),
            "challenge",
        )

    def test_browser_observation_does_not_accept_forged_cookie_name_or_confidence(self):
        self.assertEqual(
            classify_browser_observation({
                "origin": "https://mail.google.com",
                "auth_cookie": True,
                "auth_cookie_names": ["SID"],
                "identity_confidence": "verified",
                "application_signal": True,
            }, "user@example.test"),
            "login_required",
        )

    def test_session_auth_rejects_manifest_bound_to_a_different_account(self):
        from core.profile_runtime import classify_session_auth
        facts = classify_session_auth(
            text="Inbox Compose",
            origin="https://mail.google.com",
            expected_email="user@example.test",
            manifest={
                "email": "other@example.test",
                "identity_state": "native",
            },
            application_shell=True,
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "account_mismatch")

    def test_session_auth_does_not_trust_generic_text_or_cookie_name(self):
        facts = __import__("core.profile_runtime", fromlist=["classify_session_auth"]).classify_session_auth(
            text="Inbox Compose Primary",
            cookies=[{"name": "SID", "value": "stale"}],
            origin="https://mail.google.com",
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "login_required")

    def test_session_auth_requires_https_trusted_origin(self):
        from core.profile_runtime import classify_session_auth
        facts = classify_session_auth(
            text="Inbox Compose",
            cookies=[{
                "name": "SID", "value": "valid", "domain": ".google.com",
                "secure": True, "expires": 4102444800,
            }],
            origin="http://127.0.0.1",
            observed_email="user@example.test",
            expected_email="user@example.test",
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "login_required")

    def test_session_auth_requires_valid_cookie_and_shell_with_identity(self):
        from core.profile_runtime import classify_session_auth
        cookie_facts = classify_session_auth(
            text="Welcome",
            cookies=[{
                "name": "SID", "value": "valid", "domain": ".google.com",
                "secure": True, "expires": 4102444800,
            }],
            origin="https://mail.google.com",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            application_shell=True,
        )
        self.assertTrue(cookie_facts["authenticated"])
        shell_facts = classify_session_auth(
            text="Inbox Compose",
            cookies=[],
            origin="https://mail.google.com",
            observed_email="user@example.test",
            expected_email="user@example.test",
            application_shell=True,
        )
        self.assertFalse(shell_facts["authenticated"])
        self.assertEqual(shell_facts["status"], "login_required")

    def test_session_auth_normalizes_full_trusted_page_url(self):
        from core.profile_runtime import classify_session_auth
        facts = classify_session_auth(
            text="Welcome",
            cookies=[{
                "name": "SID", "value": "valid", "domain": ".google.com",
                "secure": True, "expires": 4102444800,
            }],
            origin="https://mail.google.com/mail/u/0/#inbox",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            application_shell=True,
        )
        self.assertTrue(facts["authenticated"])
        self.assertTrue(facts["trusted_origin"])

    def test_session_auth_challenge_and_expired_cookie_win_over_positive_evidence(self):
        from core.profile_runtime import classify_session_auth
        challenge = classify_session_auth(
            text="Inbox Verify it's you",
            cookies=[{
                "name": "SID", "value": "valid", "domain": ".google.com",
                "secure": True, "expires": 4102444800,
            }],
            origin="https://mail.google.com",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
        )
        self.assertEqual(challenge["status"], "challenge")
        expired = classify_session_auth(
            text="Welcome",
            cookies=[{
                "name": "SID", "value": "expired", "domain": ".google.com",
                "secure": True, "expires": 1,
            }],
            origin="https://mail.google.com",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
        )
        self.assertFalse(expired["authenticated"])

    def test_unbound_profile_rejects_caller_supplied_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            with self.assertRaises(ProfileConflictError) as raised:
                runtime.validate_proxy(runtime.load(handle), "proxy.example.test:8443")
            self.assertEqual(raised.exception.code, "profile_conflict")

    def test_bound_chromium_profile_rejects_a_chrome_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            runtime.bind(handle, "user@example.test", {
                "channel": "chromium", "major_version": "120",
            })
            runtime.mark_ready(handle)
            with self.assertRaisesRegex(Exception, "channel") as raised:
                runtime.record_runtime(handle, {
                    "channel": "chrome", "major_version": "120",
                })
            self.assertEqual(raised.exception.code, "runtime_mismatch")

    def test_invalid_nonempty_proxy_is_not_treated_as_direct_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            with self.assertRaises(ProxyUnavailableError) as raised:
                runtime.provision("user@example.test", "playwright", proxy="not-a-proxy")
            self.assertEqual(raised.exception.code, "proxy_unavailable")

    def test_kernel_login_and_warm_delegates_to_existing_session_helpers(self):
        from unittest.mock import AsyncMock, patch
        from core.profile_runtime import BrowserProfileKernel
        import core.account_warmer as account_warmer
        with tempfile.TemporaryDirectory() as directory:
            runtime = ProfileRuntime(directory)
            handle = runtime.provision("user@example.test", "playwright")
            runtime.bind(handle, "user@example.test")
            runtime.mark_ready(handle)
            expected = {"success": True, "browser_status": "authenticated"}
            # Patch the already-loaded module object.  Several adapter tests
            # intentionally swap optional modules in sys.modules; resolving a
            # dotted patch target during that sequence can load a second
            # account_warmer module and miss the helper imported by the kernel.
            with patch.object(account_warmer, "_warm_playwright_session",
                              new=AsyncMock(return_value=expected)) as warm:
                result = __import__("asyncio").run(
                    BrowserProfileKernel(runtime).login_and_warm(
                        handle, "user@example.test", "secret", 0
                    )
                )
            self.assertEqual(result, expected)
            warm.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
