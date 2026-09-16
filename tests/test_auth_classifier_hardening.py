import unittest
from unittest.mock import patch

from core.profile_runtime import (
    BrowserObservation,
    classify_browser_observation,
    classify_session_auth,
)


def _valid_cookie():
    return [{
        "name": "SID",
        "value": "live-session",
        "domain": ".google.com",
        "secure": True,
        "expires": 4102444800,
    }]


class AuthenticationClassifierHardeningTests(unittest.TestCase):
    def test_reconstructed_identity_requires_a_fresh_matching_observed_email(self):
        manifest = {
            "identity_state": "identity_reconstructed",
            "identity_verified": True,
            "identity_verified_email": "user@example.test",
            "email": "user@example.test",
            "profile_id": "profile-reconstructed",
            "engine": "playwright",
            "state": "ready",
        }
        missing = classify_session_auth(
            text="ordinary body", cookies=_valid_cookie(),
            expected_email="user@example.test", manifest=manifest,
            origin="https://mail.google.com", application_shell=True,
        )
        mismatched = classify_session_auth(
            text="ordinary body", cookies=_valid_cookie(),
            observed_email="other@example.test",
            expected_email="user@example.test", manifest=manifest,
            origin="https://mail.google.com", application_shell=True,
        )
        self.assertFalse(missing["authenticated"])
        self.assertEqual(missing["status"], "account_mismatch")
        self.assertFalse(mismatched["authenticated"])
        self.assertEqual(mismatched["status"], "account_mismatch")

    def test_reconstructed_identity_on_signin_page_remains_login_required(self):
        facts = classify_session_auth(
            text="Sign in to Gmail Email or phone",
            cookies=[],
            expected_email="user@example.test",
            manifest={
                "identity_state": "identity_reconstructed",
                "identity_verified": False,
                "email": "user@example.test",
                "profile_id": "profile-reconstructed-signin",
                "state": "ready",
            },
            origin="https://accounts.google.com/signin",
            application_shell=False,
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "login_required")

    def test_playwright_and_selenium_protocol_inputs_have_identical_classifier_results(self):
        common = {
            "text": "ordinary body",
            "cookies": _valid_cookie(),
            "observed_email": "user@example.test",
            "expected_email": "user@example.test",
            "manifest": {
                "identity_state": "identity_reconstructed",
                "identity_verified": True,
                "identity_verified_email": "user@example.test",
                "email": "user@example.test",
                "profile_id": "profile-parity",
                "engine": "playwright",
                "state": "ready",
            },
            "origin": "https://mail.google.com/mail/u/0/#inbox",
            "application_shell": True,
        }
        playwright = classify_session_auth(**common)
        selenium = classify_session_auth(**dict(common))
        self.assertEqual(playwright, selenium)
    def test_observed_account_labels_are_normalized_before_identity_matching(self):
        facts = classify_session_auth(
            text="ordinary body",
            cookies=_valid_cookie(),
            observed_email="Google Account: user@example.test",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )
        self.assertTrue(facts["authenticated"])
        self.assertEqual(facts["observed_email"], "user@example.test")

        self.assertEqual(
            classify_browser_observation(
                {
                    "origin": "https://mail.google.com",
                    "cookies": _valid_cookie(),
                    "observed_email": "Google Account: user@example.test",
                    "application_shell": True,
                },
                "user@example.test",
            ),
            "authenticated",
        )

    def test_placeholder_or_control_character_cookie_values_are_not_auth_evidence(self):
        for value in ("placeholder", "fixture-session", "dummy", "live\nforged"):
            with self.subTest(value=value):
                facts = classify_session_auth(
                    text="ordinary body",
                    cookies=[{
                        "name": "SID", "value": value, "domain": ".google.com",
                        "secure": True, "expires": 4102444800,
                    }],
                    expected_email="user@example.test",
                    manifest={"identity_state": "native"},
                    origin="https://mail.google.com",
                    application_shell=True,
                )
                self.assertFalse(facts["authenticated"])

    def test_malformed_trusted_origins_fail_closed(self):
        for origin in (
            "https://user:password@mail.google.com",
            "https://mail.google.com:443",
            "https://mail.google.com.",
            "https://mail.google.com.evil.test",
            "https://mail.google.com/%0aevil",
        ):
            with self.subTest(origin=origin):
                facts = classify_session_auth(
                    text="ordinary body",
                    cookies=_valid_cookie(),
                    expected_email="user@example.test",
                    manifest={"identity_state": "native"},
                    origin=origin,
                    application_shell=True,
                )
                self.assertFalse(facts["trusted_origin"])
                self.assertFalse(facts["authenticated"])

    def test_ordinary_body_sign_in_and_challenge_words_do_not_override_session(self):
        facts = classify_session_auth(
            text="Help article: sign in to read the challenge documentation.",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )
        self.assertFalse(facts["signals"]["login_required"])
        self.assertFalse(facts["signals"]["challenge"])
        self.assertTrue(facts["authenticated"])

    def test_native_session_requires_cookie_and_explicit_shell(self):
        cookie_only = classify_session_auth(
            text="Welcome",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com/mail/u/0/#inbox",
        )
        shell_only = classify_session_auth(
            text="Inbox Compose",
            cookies=[],
            observed_email="user@example.test",
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )
        complete = classify_session_auth(
            text="untrusted body text",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )

        self.assertFalse(cookie_only["authenticated"])
        self.assertEqual(cookie_only["status"], "login_required")
        self.assertFalse(shell_only["authenticated"])
        self.assertEqual(shell_only["status"], "login_required")
        self.assertTrue(complete["authenticated"])

    def test_body_words_do_not_become_an_application_shell(self):
        facts = classify_session_auth(
            text="Inbox Compose Primary",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
        )

        self.assertFalse(facts["authenticated"])
        self.assertFalse(facts["application_shell"])
        self.assertEqual(facts["status"], "login_required")

    def test_browser_mapping_ignores_weak_signal_and_forged_cookie_flag(self):
        weak = {
            "authenticated": True,
            "origin": "https://mail.google.com",
            "observed_email": "user@example.test",
            "identity_confidence": "verified",
            "auth_cookie": True,
            "application_signal": True,
            "application_shell": True,
        }

        self.assertEqual(
            classify_browser_observation(weak, "user@example.test"),
            "login_required",
        )

    def test_browser_observation_requires_both_protocol_facts_after_projection(self):
        observation = BrowserObservation(
            _expected_email="user@example.test",
            _trusted_session=True,
            origin="https://mail.google.com",
            observed_email="user@example.test",
            authenticated=True,
            auth_cookie=True,
            application_shell=True,
        )

        self.assertEqual(observation.status, "authenticated")
        observation["application_shell"] = False
        self.assertEqual(observation.status, "login_required")

    def test_cookie_domain_must_match_the_final_google_origin_host(self):
        for domain in ("accounts.google.com", "evil.google.com", "google.com.evil"):
            with self.subTest(domain=domain):
                facts = classify_session_auth(
                    text="ordinary application text",
                    cookies=[{
                        "name": "SID", "value": "live", "domain": domain,
                        "secure": True, "expires": 4102444800,
                    }],
                    expected_email="user@example.test",
                    manifest={"identity_state": "native"},
                    origin="https://mail.google.com/mail/u/0/#inbox",
                    application_shell=True,
                )
                self.assertFalse(facts["authenticated"])
                self.assertEqual(facts["status"], "login_required")

        host_cookie = classify_session_auth(
            text="ordinary application text",
            cookies=[{
                "name": "SID", "value": "live", "domain": "mail.google.com",
                "secure": True, "expires": 4102444800,
            }],
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )
        self.assertTrue(host_cookie["authenticated"])

    def test_expired_or_nonfinite_cookie_expiry_is_rejected(self):
        for expiry in (1, float("nan"), float("inf")):
            with self.subTest(expiry=expiry), patch(
                "core.profile_runtime.time.time", return_value=100
            ):
                facts = classify_session_auth(
                    text="ordinary application text",
                    cookies=[{
                        "name": "SID", "value": "live", "domain": ".google.com",
                        "secure": True, "expires": expiry,
                    }],
                    expected_email="user@example.test",
                    manifest={"identity_state": "native"},
                    origin="https://mail.google.com",
                    application_shell=True,
                )
                self.assertFalse(facts["authenticated"])
                self.assertEqual(facts["status"], "login_required")

    def test_generic_challenge_word_does_not_override_valid_session_facts(self):
        facts = classify_session_auth(
            text="Inbox Compose Primary challenge documentation",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://mail.google.com",
            application_shell=True,
        )

        self.assertFalse(facts["signals"]["challenge"])
        self.assertTrue(facts["authenticated"])

    def test_redirect_to_another_google_origin_cannot_reuse_mail_cookie(self):
        facts = classify_session_auth(
            text="Inbox Compose Primary",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={"identity_state": "native"},
            origin="https://accounts.google.com/signin",
            application_shell=True,
        )

        self.assertFalse(facts["authenticated"])
        self.assertFalse(facts["trusted_origin"])
        self.assertEqual(facts["status"], "login_required")

    def test_durable_native_manifest_requires_a_valid_matching_email(self):
        base = {
            "identity_state": "native",
            "profile_id": "profile-1",
            "engine": "playwright",
            "state": "ready",
        }
        for email in (None, "not-an-email", "other@example.test"):
            with self.subTest(email=email):
                manifest = dict(base)
                if email is not None:
                    manifest["email"] = email
                facts = classify_session_auth(
                    text="ordinary body",
                    cookies=_valid_cookie(),
                    expected_email="user@example.test",
                    manifest=manifest,
                    origin="https://mail.google.com",
                    application_shell=True,
                )
                self.assertFalse(facts["authenticated"])
                self.assertEqual(facts["status"], "account_mismatch")

    def test_durable_native_manifest_does_not_bind_without_observed_identity_when_email_is_invalid(self):
        facts = classify_session_auth(
            text="ordinary body",
            cookies=_valid_cookie(),
            expected_email="user@example.test",
            manifest={
                "identity_state": "native",
                "profile_id": "profile-2",
                "engine": "selenium",
                "state": "ready",
                "email": "",
            },
            origin="https://mail.google.com",
            application_shell=True,
        )
        self.assertFalse(facts["authenticated"])
        self.assertEqual(facts["status"], "account_mismatch")


if __name__ == "__main__":
    unittest.main()
