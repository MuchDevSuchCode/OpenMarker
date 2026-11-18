import hashlib
import unittest
from unittest.mock import patch

from openmarker import tablet, tabletconfig

CONF = (
    "[General]\r\n"
    "DeveloperPassword=hunter2\r\n"
    "IdleSuspendDelay=1200000\r\n"
    "SuspendPowerOffDelay=43200000\r\n"
    "RightHanded=true\r\n"
    "InputLocale=en_US\r\n"
    "devicetoken=abc.def\r\n"
    "Shapes=@Variant(\\0\\0\\0\\x7f)\r\n"
    "PageNumbering=2\r\n"
    "\r\n"
    "[wifinetworks]\r\n"
    "Home=@Variant(secret)\r\n"
    "IdleSuspendDelay=5\r\n"
)


class TabletConfigTests(unittest.TestCase):
    def setUp(self):
        self.conf = CONF.encode()
        self.commands = []
        self.result = "saved"
        for name, replacement in (("_ssh", self.ssh), ("hostname", lambda host: "paper-tablet")):
            patcher = patch.object(tablet, name, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)

    def ssh(self, host, command, stdin=None, use_key=True):
        self.commands.append((command, stdin))
        if command.startswith("cat "):
            return self.conf
        if "hostnamectl" in command:
            return (command.split("set-hostname ")[1].split(";")[0] + "\n").encode()
        return f"{self.result}\n".encode()

    def test_shows_known_settings_and_never_the_secrets(self):
        shown = tabletconfig.read("host")
        self.assertEqual(shown["name"], "paper-tablet")
        self.assertEqual({s["key"]: s["value"] for s in shown["settings"]},
                         {"IdleSuspendDelay": 20, "SuspendPowerOffDelay": 12, "RightHanded": True, "InputLocale": "en_US"})
        self.assertEqual(shown["other"], [])  # the rest only with Advanced
        advanced = tabletconfig.read("host", advanced=True)
        self.assertEqual(advanced["other"], [{"key": "PageNumbering", "value": "2"}])
        self.assertEqual(advanced["hidden"], 3)
        for secret in ("hunter2", "abc.def", "Variant", "Home"):
            self.assertNotIn(secret, repr(advanced))

    def test_changes_only_the_asked_lines(self):
        new = tabletconfig.edited(CONF, {"IdleSuspendDelay": 45, "RightHanded": False, "PageNumbering": "3"}, advanced=True)
        expected = (CONF.replace("IdleSuspendDelay=1200000", "IdleSuspendDelay=2700000")
                    .replace("RightHanded=true", "RightHanded=false").replace("PageNumbering=2", "PageNumbering=3"))
        self.assertEqual(new, expected)  # line endings, other parts and the secrets are untouched
        self.assertEqual(tabletconfig.edited(CONF, {"IdleSuspendDelay": 20}), CONF)

    def test_refuses_what_must_not_be_changed(self):
        for values, advanced in (
            ({"PageNumbering": "3"}, False),            # not known by name: needs Advanced
            ({"DeveloperPassword": "x"}, True),         # a secret
            ({"Shapes": "x"}, True),                    # packed data
            ({"Home": "x"}, True),                      # another part of the file
            ({"NewKey": "x"}, True),                    # not in the file
            ({"IdleSuspendDelay": -1}, True),
            ({"IdleSuspendDelay": "20"}, True),
            ({"RightHanded": "true"}, True),
            ({"InputLocale": "en_US\nDeveloperPassword=x"}, True),
            ({"PageNumbering": "2\r\n[General]"}, True),
            ({"PageNumbering": "@Variant(x)"}, True),
            ([], True),
        ):
            with self.assertRaises(tabletconfig.ConfigError, msg=values):
                tabletconfig.edited(CONF, values, advanced)

    def test_save_replaces_the_file_only_if_it_is_still_what_was_read(self):
        self.assertTrue(tabletconfig.save("host", {"IdleSuspendDelay": 45}))
        script, sent = self.commands[-1]
        self.assertEqual(sent, CONF.replace("IdleSuspendDelay=1200000", "IdleSuspendDelay=2700000").encode())
        self.assertIn(hashlib.sha256(self.conf).hexdigest(), script)
        self.assertIn(hashlib.sha256(sent).hexdigest(), script)
        self.assertLess(script.index("systemctl stop xochitl"), script.index("mv "))
        self.assertTrue(script.rstrip().endswith("systemctl start xochitl; echo $R"))  # started again whatever happened
        self.assertNotIn("hunter2", script)

    def test_save_does_nothing_when_nothing_differs(self):
        self.assertFalse(tabletconfig.save("host", {"IdleSuspendDelay": 20}))
        self.assertEqual(len(self.commands), 1)  # only the read: the tablet's app is not restarted

    def test_save_reports_a_file_that_changed_meanwhile(self):
        self.result = "changed"
        with self.assertRaisesRegex(tabletconfig.ConfigError, "same moment"):
            tabletconfig.save("host", {"IdleSuspendDelay": 45})
        self.result = "broken"
        with self.assertRaisesRegex(tabletconfig.ConfigError, "Nothing was changed"):
            tabletconfig.save("host", {"IdleSuspendDelay": 45})

    def test_rename_takes_only_a_network_name(self):
        self.assertEqual(tabletconfig.rename("host", " study-tablet "), "study-tablet")
        for name in ("", "-a", "a-", "a b", "a;reboot", "$(reboot)", "a" * 64, None, "tablet.local"):
            with self.assertRaises(tabletconfig.ConfigError, msg=name):
                tabletconfig.rename("host", name)
        self.assertEqual(sum("hostnamectl" in command for command, _ in self.commands), 1)


if __name__ == "__main__":
    unittest.main()
