import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openmarker import server, tablet, workflow


class SetupTests(unittest.TestCase):
    """Authorising this computer from the browser, where there is no terminal to type into."""

    def setUp(self):
        folder = tempfile.TemporaryDirectory(dir=Path(__file__).parent)
        self.addCleanup(folder.cleanup)
        self.home = Path(folder.name)
        self.data = self.home / "openmarker"
        self.calls = []       # every ssh that was started: (arguments, environment)
        self.refuse = ""      # what ssh complains of, when the password step should fail
        for target, name, replacement in (
            (Path, "home", lambda: self.home),
            (subprocess, "run", self.fake_run),
            (workflow, "reachable", lambda host, timeout=0.7: True),
            (server.shutil, "which", lambda name: name),
            (server, "_seen", (None, None)),
        ):
            patcher = patch.object(target, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def fake_run(self, command, **options):
        """Stands in for ssh-keygen and ssh."""
        self.calls.append((command, options.get("env")))
        if command[0] == "ssh-keygen":
            key = Path(command[command.index("-f") + 1])
            key.write_text("private", encoding="utf-8")
            key.with_suffix(".pub").write_text("ssh-ed25519 AAAA openmarker\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0)
        if "PubkeyAuthentication=no" in command:  # the step that uses the password
            if self.refuse:
                return subprocess.CompletedProcess(command, 255, b"", self.refuse.encode())
            (self.data / "known_hosts").write_text("the tablet now", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, b"", b"")
        answer = b"ok\n" if command[-1] == "echo ok" else b"paper-tablet\n"
        return subprocess.CompletedProcess(command, 0, answer, b"")

    def test_password_from_the_browser_reaches_ssh_without_a_terminal(self):
        self.assertEqual(server.setup({"password": "s3cret", "host": ""}), {"ok": True})
        command, env = next(call for call in self.calls if "PubkeyAuthentication=no" in call[0])
        self.assertEqual(env[tablet.ASKPASS], "s3cret")
        self.assertEqual(env["SSH_ASKPASS_REQUIRE"], "force")
        self.assertNotIn("s3cret", " ".join(command))  # never on a command line, where other programs could read it
        self.assertIn(f"root@{tablet.USB_HOST}", command)
        self.assertTrue(tablet.is_set_up())
        self.assertEqual(server._seen, (tablet.USB_HOST, "USB"))
        self.assertEqual(workflow.load_config().get("tablet_name"), "paper-tablet")

    def test_wifi_address_is_remembered(self):
        server.setup({"password": "s3cret", "host": "192.168.1.50"})
        self.assertEqual(workflow.load_config().get("wifi_host"), "192.168.1.50")

    def test_wrong_password_leaves_nothing_behind(self):
        self.refuse = "root@10.11.99.1: Permission denied (publickey,password)."
        with self.assertRaisesRegex(server.UserError, "did not accept that password"):
            server.setup({"password": "nope"})
        self.assertFalse(tablet.is_set_up())
        self.assertEqual(list(self.data.glob("id_ed25519*")), [])

    def test_failed_repeat_keeps_the_existing_setup(self):
        server.setup({"password": "s3cret"})
        (self.data / "known_hosts").write_text("the tablet before", encoding="utf-8")
        self.refuse = "Permission denied"
        with self.assertRaises(server.UserError):
            server.setup({"password": "nope"})
        self.assertTrue(tablet.is_set_up())
        self.assertEqual((self.data / "known_hosts").read_text(encoding="utf-8"), "the tablet before")

    def test_repeat_accepts_a_tablet_that_was_reset(self):
        server.setup({"password": "s3cret"})
        (self.data / "known_hosts").write_text("the tablet before", encoding="utf-8")
        server.setup({"password": "s3cret"})
        self.assertEqual((self.data / "known_hosts").read_text(encoding="utf-8"), "the tablet now")

    def test_rejects_what_cannot_be_a_password_or_an_address(self):
        for body in ({}, {"password": ""}, {"password": 5}, {"password": "x" * 201},
                     {"password": "x", "host": "-oProxyCommand=calc"}, {"password": "x", "host": "a b"}):
            with self.assertRaises(server.UserError, msg=body):
                server.setup(body)
        self.assertEqual(self.calls, [])

    def test_says_so_when_the_tablet_is_not_there(self):
        with patch.object(workflow, "reachable", lambda host, timeout=0.7: False):
            with self.assertRaisesRegex(server.UserError, "USB"):
                server.setup({"password": "x"})
            with self.assertRaisesRegex(server.UserError, "Nothing answered"):
                server.setup({"password": "x", "host": "192.168.1.50"})
        self.assertEqual(self.calls, [])


class AskpassTests(unittest.TestCase):
    def test_answers_only_when_ssh_asks(self):
        with patch.dict(os.environ, clear=False):
            os.environ.pop(tablet.ASKPASS, None)
            self.assertFalse(tablet.answer_askpass())
        written = []
        with patch.dict(os.environ, {tablet.ASKPASS: "s3cret"}), patch.object(os, "write", lambda fd, data: written.append((fd, data))):
            self.assertTrue(tablet.answer_askpass())
        self.assertEqual(written, [(1, b"s3cret\n")])


if __name__ == "__main__":
    unittest.main()
