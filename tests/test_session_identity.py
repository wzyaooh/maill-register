"""Versioned browser-session identity provider contract."""

import json
import unittest

from core import session_identity


IDENTITY_ENDPOINT = session_identity.IDENTITY_ENDPOINT
AccountSessionRecord = session_identity.AccountSessionRecord
IdentityObservation = session_identity.IdentityObservation
SessionIdentityProof = session_identity.SessionIdentityProof
parse_google_accounts_v1 = session_identity.parse_google_accounts_v1
parse_gmail_session_slot = session_identity.parse_gmail_session_slot
resolve_session_identity = session_identity.resolve_session_identity


def _body(accounts, *, prefix=True, **extra):
    value = {"accounts": accounts}
    value.update(extra)
    encoded = json.dumps(value, separators=(",", ":")).encode("utf-8")
    return (b")]}\'\n" if prefix else b"") + encoded


def _account(slot=0, email="User@Example.test", valid_session=True, **extra):
    value = {"slot": slot, "email": email, "valid_session": valid_session}
    value.update(extra)
    return value


class SessionIdentityProviderTests(unittest.TestCase):
    def test_constants_are_fixed_and_not_caller_selectable(self):
        self.assertEqual(
            session_identity.IDENTITY_PROVIDER, "google_browser_accounts_v1"
        )
        self.assertEqual(session_identity.IDENTITY_PROVIDER_VERSION, 1)
        self.assertEqual(
            IDENTITY_ENDPOINT,
            "https://accounts.google.com/ListAccounts"
            "?gpsia=1&source=ChromiumBrowser&json=standard",
        )
        self.assertEqual(session_identity.MAX_RESPONSE_BYTES, 262144)
        self.assertEqual(session_identity.MAX_RECORDS, 32)
        self.assertEqual(session_identity.NAVIGATION_TIMEOUT_MS, 15000)

    def test_v1_parser_normalizes_email_and_strips_exact_xssi_prefix(self):
        records = parse_google_accounts_v1(
            _body([_account()]),
            "https://accounts.google.com/ListAccounts",
        )
        self.assertEqual(
            records, (AccountSessionRecord(0, "user@example.test", True),)
        )

    def test_parser_accepts_only_exact_fixed_provider_endpoint_path(self):
        for url in (
            IDENTITY_ENDPOINT,
            "https://accounts.google.com/ListAccounts?gpsia=1&source=ChromiumBrowser&json=standard",
        ):
            with self.subTest(url=url):
                self.assertEqual(
                    len(parse_google_accounts_v1(_body([_account()]), url)), 1
                )

    def test_parser_rejects_malformed_protocol_without_echoing_body(self):
        cases = {
            "missing xssi": b'{"accounts":[]}',
            "malformed json": b")]}'\n{\"accounts\":",
            "missing accounts": b")]}'\n{\"other\":[]}",
            "accounts object": _body({}),
            "slot string": _body([_account(slot="0")]),
            "negative slot": _body([_account(slot=-1)]),
            "boolean slot": _body([_account(slot=True)]),
            "email non-string": _body([_account(email=123)]),
            "session non-boolean": _body([_account(valid_session=1)]),
            "unknown account key": _body([_account(display_name="user")]),
            "unknown top-level key": _body([_account()], unexpected=True),
        }
        for label, body in cases.items():
            with self.subTest(label=label):
                with self.assertRaises(session_identity.IdentityProtocolError) as raised:
                    parse_google_accounts_v1(
                        body, "https://accounts.google.com/ListAccounts"
                    )
                self.assertEqual(raised.exception.code, "identity_unavailable")
                self.assertNotIn(body[:80].decode("utf-8", "replace"), repr(raised.exception))

    def test_parser_rejects_duplicate_slots_and_duplicate_valid_emails(self):
        duplicate_slot = _body([
            _account(slot=0, email="a@example.test"),
            _account(slot=0, email="b@example.test"),
        ])
        duplicate_email = _body([
            _account(slot=0, email="A@example.test"),
            _account(slot=1, email="a@example.test"),
        ])
        for body in (duplicate_slot, duplicate_email):
            with self.assertRaises(session_identity.IdentityProtocolError) as raised:
                parse_google_accounts_v1(
                    body, "https://accounts.google.com/ListAccounts"
                )
            self.assertEqual(raised.exception.code, "identity_unavailable")

    def test_parser_bounds_records_bytes_and_json_depth(self):
        too_many = _body([_account(slot=i, email="u%d@example.test" % i) for i in range(33)])
        too_large = b")]}'\n" + b'{"accounts":[]}' + b" " * session_identity.MAX_RESPONSE_BYTES
        deeply_nested = b")]}'\n" + (
            b'{"accounts":[{"slot":0,"email":"a@example.test",'
            b'"valid_session":true,"extra":'
            + b"[" * 20
            + b"0"
            + b"]" * 20
            + b"}]}"
        )
        for body in (too_many, too_large, deeply_nested):
            with self.subTest(size=len(body)):
                with self.assertRaises(session_identity.IdentityProtocolError) as raised:
                    parse_google_accounts_v1(
                        body, "https://accounts.google.com/ListAccounts"
                    )
                self.assertEqual(raised.exception.code, "identity_unavailable")

    def test_provider_login_redirect_is_login_required(self):
        with self.assertRaises(session_identity.IdentityProtocolError) as raised:
            parse_google_accounts_v1(
                b"ignored", "https://accounts.google.com/signin"
            )
        self.assertEqual(raised.exception.status, "login_required")
        self.assertEqual(raised.exception.code, "login_required")

    def test_provider_non_allowed_origin_or_path_is_unavailable(self):
        for url in (
            "http://accounts.google.com/ListAccounts",
            "https://accounts.google.com:443/ListAccounts",
            "https://user:password@accounts.google.com/ListAccounts",
            "https://accounts.google.com./ListAccounts",
            "https://accounts.google.com.evil.test/ListAccounts",
            "https://accounts.google.com/Other",
            "https://evil.test/ListAccounts",
        ):
            with self.subTest(url=url):
                with self.assertRaises(session_identity.IdentityProtocolError) as raised:
                    parse_google_accounts_v1(_body([_account()]), url)
                self.assertEqual(raised.exception.status, "identity_unavailable")
                self.assertEqual(raised.exception.code, "identity_unavailable")

    def test_gmail_slot_parser_accepts_only_unambiguous_path_slot(self):
        self.assertEqual(
            parse_gmail_session_slot("https://mail.google.com/mail/u/0/#inbox"), 0
        )
        self.assertEqual(
            parse_gmail_session_slot("https://mail.google.com/mail/u/12/"), 12
        )
        for url in (
            "https://mail.google.com/",
            "https://mail.google.com/mail/u/-1/#inbox",
            "https://mail.google.com/mail/u/0",
            "https://mail.google.com/mail/u/0/?email=a@example.test",
            "http://mail.google.com/mail/u/0/",
            "https://mail.google.com:443/mail/u/0/",
            "https://user:password@mail.google.com/mail/u/0/",
            "https://mail.google.com./mail/u/0/",
            "https://mail.google.com.evil.test/mail/u/0/",
        ):
            with self.subTest(url=url):
                self.assertIsNone(parse_gmail_session_slot(url))


class SessionIdentityResolutionTests(unittest.TestCase):
    def _records(self):
        return (
            AccountSessionRecord(0, "a@example.test", True),
            AccountSessionRecord(1, "b@example.test", True),
        )

    def test_resolver_requires_unique_slot_and_all_expected_emails(self):
        result = resolve_session_identity(
            (AccountSessionRecord(0, "User@Example.test", True),),
            session_slot=0,
            expected_email="user@example.test",
            manifest_email="USER@example.test",
            final_origin="https://mail.google.com",
        )
        self.assertEqual(result.status, "authenticated")
        self.assertEqual(result.error_code, "")
        self.assertIsInstance(result.proof, SessionIdentityProof)
        self.assertEqual(result.proof.session_slot, 0)
        self.assertEqual(result.proof.observed_email, "user@example.test")
        self.assertEqual(result.proof.final_origin, "https://mail.google.com")
        self.assertEqual(result.proof.provider, "google_browser_accounts_v1")

    def test_slot_mismatch_and_page_account_a_cannot_bind_session_b(self):
        result = resolve_session_identity(
            self._records(),
            session_slot=1,
            expected_email="a@example.test",
            manifest_email="a@example.test",
            final_origin="https://mail.google.com",
        )
        self.assertEqual(result.status, "account_mismatch")
        self.assertEqual(result.error_code, "account_mismatch")
        self.assertIsNone(result.proof)

    def test_ambiguous_slot_is_unavailable_instead_of_using_array_order(self):
        result = resolve_session_identity(
            (AccountSessionRecord(0, "a@example.test", True),),
            session_slot=None,
            expected_email="a@example.test",
            manifest_email="a@example.test",
            final_origin="https://mail.google.com",
        )
        self.assertEqual(result.status, "identity_unavailable")
        self.assertEqual(result.error_code, "identity_unavailable")
        self.assertIsNone(result.proof)

    def test_resolver_rejects_missing_invalid_or_inactive_identity_facts(self):
        cases = (
            {"session_slot": 9, "expected_email": "a@example.test", "manifest_email": "a@example.test"},
            {"session_slot": 0, "expected_email": None, "manifest_email": "a@example.test"},
            {"session_slot": 0, "expected_email": "a@example.test", "manifest_email": None},
            {"session_slot": 0, "expected_email": "a@example.test", "manifest_email": "b@example.test"},
        )
        for values in cases:
            with self.subTest(values=values):
                result = resolve_session_identity(
                    self._records(), final_origin="https://mail.google.com", **values
                )
                self.assertIn(
                    result.status, ("identity_unavailable", "account_mismatch")
                )
                self.assertIsNone(result.proof)

        inactive = resolve_session_identity(
            (AccountSessionRecord(0, "a@example.test", False),),
            session_slot=0,
            expected_email="a@example.test",
            manifest_email="a@example.test",
            final_origin="https://mail.google.com",
        )
        self.assertEqual(inactive.status, "identity_unavailable")
        self.assertIsNone(inactive.proof)

    def test_resolver_rejects_application_origin_and_full_url_slot_conflicts(self):
        for origin in (
            "http://mail.google.com",
            "https://mail.google.com:443",
            "https://user:password@mail.google.com",
            "https://mail.google.com.",
            "https://mail.google.com.evil.test",
            "https://evil.test",
        ):
            with self.subTest(origin=origin):
                result = resolve_session_identity(
                    self._records(), session_slot=0,
                    expected_email="a@example.test",
                    manifest_email="a@example.test", final_origin=origin,
                )
                self.assertEqual(result.status, "identity_unavailable")
                self.assertIsNone(result.proof)

        result = resolve_session_identity(
            self._records(), session_slot=0,
            expected_email="a@example.test", manifest_email="a@example.test",
            final_origin="https://mail.google.com/mail/u/1/#inbox",
        )
        self.assertEqual(result.status, "account_mismatch")
        self.assertIsNone(result.proof)

    def test_observations_and_proofs_are_immutable_and_cannot_be_forged(self):
        result = resolve_session_identity(
            (AccountSessionRecord(0, "a@example.test", True),),
            session_slot=0, expected_email="a@example.test",
            manifest_email="a@example.test", final_origin="https://mail.google.com",
        )
        with self.assertRaises((AttributeError, TypeError)):
            result.proof.observed_email = "attacker@example.test"
        with self.assertRaises((AttributeError, TypeError)):
            result.status = "authenticated"
        forged = SessionIdentityProof(
            "google_browser_accounts_v1", 1, 0, "a@example.test",
            "https://mail.google.com", "2026-01-01T00:00:00+00:00",
        )
        self.assertIsNone(getattr(forged, "_evidence_marker", None))
        self.assertIsNot(getattr(result.proof, "_evidence_marker", None), None)
        self.assertNotIn('"accounts"', repr(result))


if __name__ == "__main__":
    unittest.main()
