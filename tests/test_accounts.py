import http.client
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from openmarker import accounts, paths, server, workflow

PASSWORD = "correct horse battery"
TUNNEL = {"Host": "example.ngrok.app", "X-Forwarded-For": "203.0.113.9", "X-Forwarded-Proto": "https"}
TUNNEL_ORIGIN = "https://example.ngrok.app"


class AccountsCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        for target, name, replacement in (
            (Path, "home", lambda: self.home),
            (accounts, "_cache", None),
            (accounts, "_wrong", {}),
            (accounts, "_recent", []),
            (accounts, "SCRYPT", {"n": 2**4, "r": 8, "p": 1, "dklen": 32}),  # fast, for tests
            (server, "_works", {}),
            (server, "_background", lambda folder: None),
        ):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(paths.use, None)


class AccountTests(AccountsCase):
    def test_first_account_keeps_the_data_that_was_here_and_others_get_their_own(self):
        first = accounts.create("Ada", PASSWORD)
        second = accounts.create("Grace Hopper", PASSWORD)
        self.assertTrue(first["admin"] and first["first"])
        self.assertFalse(second["admin"] or second["first"])
        self.assertEqual(accounts.folder(first), self.home / "openmarker")
        self.assertEqual(accounts.folder(second), self.home / "openmarker" / "accounts" / second["id"])
        paths.use(accounts.folder(second))
        self.assertEqual(workflow.config_path().parent, accounts.folder(second))

    def test_password_is_not_kept_and_is_checked(self):
        accounts.create("Ada", PASSWORD)
        self.assertNotIn(PASSWORD, (self.home / "openmarker" / "accounts.json").read_text(encoding="utf-8"))
        self.assertEqual(accounts.check("ada", PASSWORD)["name"], "Ada")  # names ignore case
        self.assertIsNone(accounts.check("Ada", PASSWORD + "x"))
        self.assertIsNone(accounts.check("Nobody", PASSWORD))
        self.assertIsNone(accounts.check(None, 5))

    def test_rejects_weak_passwords_bad_names_and_duplicates(self):
        for name, password in (("Ada", "short"), ("", PASSWORD), ("../x", PASSWORD), ("a" * 41, PASSWORD), (5, PASSWORD)):
            with self.assertRaises(accounts.AccountError, msg=(name, password)):
                accounts.create(name, password)
        accounts.create("Ada", PASSWORD)
        with self.assertRaisesRegex(accounts.AccountError, "already"):
            accounts.create("ADA", PASSWORD)

    def test_wrong_passwords_make_a_stranger_wait_but_not_this_computer(self):
        accounts.create("Ada", PASSWORD)
        for _ in range(accounts.TRIES):
            self.assertIsNone(accounts.check("Ada", "wrong password"))
        with self.assertRaises(accounts.Locked):
            accounts.check("Ada", PASSWORD)
        self.assertIsNotNone(accounts.check("Ada", PASSWORD, trusted=True))
        self.assertIsNotNone(accounts.check("Ada", PASSWORD))  # a right password clears the count

    def test_sessions_end_on_sign_out_expiry_and_password_change(self):
        ada = accounts.create("Ada", PASSWORD)
        one, two = accounts.sign_in(ada["id"]), accounts.sign_in(ada["id"])
        self.assertEqual(accounts.signed_in(one)["id"], ada["id"])
        self.assertNotIn(one, (self.home / "openmarker" / "accounts.json").read_text(encoding="utf-8"))
        self.assertIsNone(accounts.signed_in("made up"))
        accounts.sign_out(one)
        self.assertIsNone(accounts.signed_in(one))
        accounts.set_password(ada["id"], PASSWORD + "!")
        self.assertIsNone(accounts.signed_in(two))
        three = accounts.sign_in(ada["id"])
        with patch.object(accounts.time, "time", lambda: 4e9):
            self.assertIsNone(accounts.signed_in(three))

    def test_first_account_is_removed_last_and_that_turns_accounts_off(self):
        ada, grace = accounts.create("Ada", PASSWORD), accounts.create("Grace", PASSWORD)
        with self.assertRaises(accounts.AccountError):
            accounts.remove(ada["id"])
        accounts.remove(grace["id"])
        self.assertTrue(accounts.folder(grace).is_dir())  # their data is left alone
        accounts.remove(ada["id"])
        self.assertFalse(accounts.enabled())

    def test_a_damaged_file_never_reads_as_no_accounts(self):
        accounts.create("Ada", PASSWORD)
        (self.home / "openmarker" / "accounts.json").write_text("{", encoding="utf-8")
        with self.assertRaises(accounts.AccountError):
            accounts.enabled()


class RequestTests(AccountsCase):
    def setUp(self):
        super().setUp()
        self.httpd = server.Server(("127.0.0.1", 0), server.Handler)
        self.port = self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)

    def ask(self, path, body=None, cookie="", headers=None, raw=None):
        """One request. Returns (status, JSON or text, the cookie the reply set)."""
        local = {"Host": f"127.0.0.1:{self.port}"}
        send = dict(local if headers is None else headers)
        if cookie:
            send["Cookie"] = cookie
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            send.setdefault("Origin", f"http://{send['Host']}")
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        self.addCleanup(connection.close)
        connection.putrequest("POST" if data is not None else "GET", path, skip_host=True)
        for name, value in send.items():
            connection.putheader(name, value)
        if data is not None:
            connection.putheader("Content-Length", str(len(data)))
        connection.endheaders(data)
        reply = connection.getresponse()
        text = reply.read().decode()
        try:
            answer = json.loads(text)
        except ValueError:
            answer = text
        return reply.status, answer, (reply.getheader("Set-Cookie") or "").split(";")[0], reply.getheader("Set-Cookie") or ""

    def first_account(self):
        status, answer, cookie, _ = self.ask("/api/account-add", {"name": "Ada", "password": PASSWORD})
        self.assertEqual((status, answer), (200, {"ok": True}))
        return cookie

    def test_without_accounts_only_this_computer_is_served(self):
        self.assertEqual(self.ask("/api/status")[0], 200)
        self.assertEqual(self.ask("/api/status", headers=TUNNEL)[0], 403)
        self.assertEqual(self.ask("/", headers=TUNNEL)[0], 403)
        self.assertEqual(self.ask("/api/status", headers={"Host": "evil.example"})[0], 403)
        rewritten = {"Host": f"localhost:{self.port}", "X-Forwarded-For": "203.0.113.9"}  # a tunnel that rewrites Host
        self.assertEqual(self.ask("/api/status", headers=rewritten)[0], 403)
        # ...so a stranger cannot make the first account either.
        status, _, cookie, _ = self.ask("/api/account-add", {"name": "Eve", "password": PASSWORD}, headers={**TUNNEL, "Origin": TUNNEL_ORIGIN})
        self.assertEqual((status, cookie), (403, ""))
        self.assertFalse(accounts.enabled())

    def test_with_accounts_everything_needs_a_sign_in(self):
        cookie = self.first_account()
        self.assertEqual(self.ask("/api/status")[:2], (401, {"error": "sign-in", "app": "openmarker"}))
        self.assertEqual(self.ask("/api/library", headers=TUNNEL)[0], 401)
        self.assertEqual(self.ask("/api/config", {"auto_sync": True})[0], 401)
        self.assertEqual(self.ask("/api/status", cookie="openmarker_%d=made-up" % self.port)[0], 401)
        self.assertEqual(self.ask("/")[0], 200)  # the page itself, which asks for the sign-in
        self.assertEqual(self.ask("/api/session")[1], {"accounts": True, "signed_in": False, "here": True})
        self.assertEqual(self.ask("/api/status", cookie=cookie)[0], 200)
        who = self.ask("/api/session", cookie=cookie)[1]
        self.assertEqual((who["name"], who["admin"], who["signed_in"]), ("Ada", True, True))

    def test_sign_in_through_a_tunnel(self):
        self.first_account()
        tunnel = {**TUNNEL, "Origin": TUNNEL_ORIGIN}
        status, answer, cookie, _ = self.ask("/api/sign-in", {"name": "Ada", "password": "not the password"}, headers=tunnel)
        self.assertEqual((answer["ok"], cookie), (False, ""))
        status, answer, cookie, full = self.ask("/api/sign-in", {"name": "Ada", "password": PASSWORD}, headers=tunnel)
        self.assertEqual(answer, {"ok": True})
        self.assertIn("HttpOnly", full)
        self.assertIn("Secure", full)
        self.assertIn("SameSite=Lax", full)
        self.assertEqual(self.ask("/api/status", cookie=cookie, headers=TUNNEL)[0], 200)
        self.assertFalse(self.ask("/api/session", cookie=cookie, headers=TUNNEL)[1]["here"])
        self.ask("/api/sign-out", {}, cookie=cookie, headers=tunnel)
        self.assertEqual(self.ask("/api/status", cookie=cookie, headers=TUNNEL)[0], 401)

    def test_another_website_cannot_ask_in_the_browsers_name(self):
        cookie = self.first_account()
        evil = {"Host": f"127.0.0.1:{self.port}", "Origin": "https://evil.example"}
        self.assertEqual(self.ask("/api/config", {"auto_sync": True}, cookie=cookie, headers=evil)[0], 403)
        self.assertEqual(self.ask("/api/status", cookie=cookie, headers={"Host": f"127.0.0.1:{self.port}", "Sec-Fetch-Site": "cross-site"})[0], 403)
        self.assertEqual(self.ask("/api/sign-in", raw=b'{"name":"Ada","password":"x"}', headers=TUNNEL)[0], 403)  # no Origin, not local

    def test_strangers_cannot_send_large_requests_or_quit(self):
        self.first_account()
        tunnel = {**TUNNEL, "Origin": TUNNEL_ORIGIN}
        self.assertEqual(self.ask("/api/sign-in", raw=b"x" * 5000, headers=tunnel)[0], 413)
        self.assertEqual(self.ask("/api/quit", {}, headers=tunnel)[0], 401)
        self.assertEqual(self.ask("/api/quit", {})[0], 401)  # a page on this computer, not signed in

    def test_each_account_has_its_own_settings_and_rights(self):
        ada = self.first_account()
        self.assertEqual(self.ask("/api/account-add", {"name": "Grace", "password": PASSWORD}, cookie=ada)[1], {"ok": True})
        grace = self.ask("/api/sign-in", {"name": "Grace", "password": PASSWORD})[2]
        self.assertEqual(self.ask("/api/config", {"advanced": True, "calendar": "https://example.org/g.ics"}, cookie=grace)[1], {"ok": True})
        self.assertEqual(self.ask("/api/config", cookie=grace)[1]["calendar"], "https://example.org/g.ics")
        self.assertEqual(self.ask("/api/config", cookie=ada)[1]["calendar"], "")
        self.assertNotEqual(self.ask("/api/config", cookie=grace)[1]["folder"], self.ask("/api/config", cookie=ada)[1]["folder"])
        self.assertEqual(self.ask("/api/status", cookie=grace)[1]["source"], "mirror")  # never the owner's desktop cache
        # What only an administrator may do.
        self.assertEqual([p["name"] for p in self.ask("/api/accounts", cookie=grace)[1]["accounts"]], ["Grace"])
        self.assertEqual([p["name"] for p in self.ask("/api/accounts", cookie=ada)[1]["accounts"]], ["Ada", "Grace"])
        for action, body in (("account-add", {"name": "Eve", "password": PASSWORD}), ("quit", {}),
                             ("config", {"markdown_dir": "C:/Windows"}),
                             ("account-remove", {"id": accounts.everyone()[0]["id"]}),
                             ("account-password", {"id": accounts.everyone()[0]["id"], "password": PASSWORD + "2"})):
            answer = self.ask(f"/api/{action}", body, cookie=grace)[1]
            self.assertFalse(answer["ok"], action)
            self.assertIn("administrator", answer["error"], action)
        self.assertEqual(len(accounts.everyone()), 2)

    def test_changing_your_password_needs_the_current_one_and_keeps_this_browser_in(self):
        ada = self.first_account()
        other = self.ask("/api/sign-in", {"name": "Ada", "password": PASSWORD})[2]
        self.assertFalse(self.ask("/api/account-password", {"current": "wrong", "password": PASSWORD + "2"}, cookie=ada)[1]["ok"])
        status, answer, fresh, _ = self.ask("/api/account-password", {"current": PASSWORD, "password": PASSWORD + "2"}, cookie=ada)
        self.assertEqual(answer, {"ok": True})
        self.assertEqual(self.ask("/api/status", cookie=fresh)[0], 200)
        self.assertEqual(self.ask("/api/status", cookie=other)[0], 401)
        self.assertEqual(self.ask("/api/status", cookie=ada)[0], 401)

    def test_removing_the_last_account_turns_accounts_off(self):
        ada = self.first_account()
        answer = self.ask("/api/account-remove", {"id": accounts.everyone()[0]["id"]}, cookie=ada)[1]
        self.assertTrue(answer["ok"])
        self.assertEqual(self.ask("/api/status")[0], 200)
        self.assertEqual(self.ask("/api/status", headers=TUNNEL)[0], 403)

    def test_a_running_copy_is_recognised_while_it_wants_a_sign_in(self):
        self.assertTrue(server.running(self.port))
        self.first_account()
        self.assertTrue(server.running(self.port))


if __name__ == "__main__":
    unittest.main()
