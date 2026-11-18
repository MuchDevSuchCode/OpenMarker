"""The tablet's own settings.

The tablet's app keeps its settings in one file, in the `[General]` part of
it, as `Name=value` lines. A few of them are known here by name and shown
with a proper control; the rest can be shown as they are stored ("Advanced").
The file also holds the tablet's passwords and sign-in tokens: those are
never read out of this module, and cannot be changed through it.

The app reads the file only when it starts, and may rewrite it while it
runs, so a change is made with the app stopped: stop, check the file is
still what was read, replace it, start. The previous file is kept beside it
as `xochitl.conf.openmarker-backup`.

The device name is the tablet's network name (`hostname`), which is not in
that file.
"""
from __future__ import annotations

import hashlib
import re

from . import tablet

CONF = "/home/root/.config/remarkable/xochitl.conf"
NAME = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?")
LINE = re.compile(r"([A-Za-z][A-Za-z0-9_.-]{0,80})=(.*)")
SECRET = re.compile(r"passw|passcode|token|secret|psk|credential|apikey|(?:^|_)pin$", re.IGNORECASE)
LONGEST = 200

# key -> (label, kind, explanation). Kinds: "minutes" and "hours" are stored in milliseconds, "bool" as true/false.
KNOWN: dict[str, tuple[str, str, str]] = {
    "IdleSuspendDelay": ("Sleep after", "minutes", "How long the tablet waits, untouched, before it goes to sleep."),
    "SuspendPowerOffDelay": ("Power off after", "hours", "How long the tablet sleeps before it switches itself off."),
    "RightHanded": ("Right-handed", "bool", "Off for left-handed use, which mirrors the toolbar."),
    "InputLocale": ("Keyboard language", "text", "As the tablet writes it, such as en_US."),
    "WebInterfaceEnabled": ("USB web interface", "bool", "OpenMarker sends documents through it, which avoids restarting the tablet's app."),
    "wifion": ("Wi-Fi", "bool", "Whether the tablet's Wi-Fi is switched on."),
}
UNIT = {"minutes": 60_000, "hours": 3_600_000}
LIMIT = {"minutes": 24 * 60, "hours": 24 * 365}


class ConfigError(ValueError):
    """A problem to show to the person as-is."""


def _general(text: str) -> dict[str, tuple[int, str]]:
    """The plain settings of the [General] part: key -> (line number, value as stored)."""
    found: dict[str, tuple[int, str]] = {}
    section = ""
    for number, line in enumerate(text.splitlines()):
        line = line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
        elif section == "General" and (m := LINE.fullmatch(line)):
            found[m[1]] = (number, m[2])
    return found


def _shown(key: str, value: str) -> bool:
    """Whether a setting may leave the tablet: not a secret, and not something Qt has packed up."""
    return not SECRET.search(key) and not value.startswith(("@", '"@')) and len(value) <= LONGEST


def _for_person(kind: str, stored: str):
    """A stored value as the page shows it, or None if it is not what the kind expects."""
    if kind == "bool":
        return {"true": True, "false": False}.get(stored.strip().lower())
    if kind in UNIT:
        return round(int(stored) / UNIT[kind], 2) if re.fullmatch(r"\d{1,12}", stored.strip()) else None
    return stored


def _to_store(key: str, value: object) -> str:
    """What to write in the file for a value the page sent."""
    label, kind, _ = KNOWN.get(key, (key, "raw", ""))
    if kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError(f"{label} must be on or off.")
        return "true" if value else "false"
    if kind in UNIT:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= LIMIT[kind]:
            raise ConfigError(f"{label} must be a number of {kind} from 0 to {LIMIT[kind]:,}.")
        return str(round(value * UNIT[kind]))
    if not isinstance(value, str) or len(value) > LONGEST or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ConfigError(f"{label} must be one line of text, at most {LONGEST} characters.")
    if value != value.strip() or value.startswith("@") or (kind == "text" and not re.fullmatch(r"[A-Za-z0-9_.@-]{1,40}", value)):
        raise ConfigError(f"{label} can't be set to that.")
    return value


def _read(host: str) -> str:
    return tablet._ssh(host, f"cat {CONF}").decode("utf-8", "replace")


def read(host: str, advanced: bool = False) -> dict:
    """The tablet's settings, for the page. Secrets are left out; `advanced` adds the settings not known by name."""
    general = _general(_read(host))
    settings, other, hidden = [], [], 0
    for key, (label, kind, text) in KNOWN.items():
        if key in general and (value := _for_person(kind, general[key][1])) is not None:
            settings.append({"key": key, "label": label, "kind": kind, "help": text, "value": value})
    known = {s["key"] for s in settings}
    for key, (_, value) in general.items():
        if key in known:
            continue
        if not _shown(key, value):
            hidden += 1
        elif advanced:
            other.append({"key": key, "value": value})
    return {"name": tablet.hostname(host), "settings": settings, "other": other, "hidden": hidden, "advanced": advanced}


def edited(text: str, values: dict, advanced: bool = False) -> str:
    """The file with these settings changed. Only settings that are already in it, and are not secrets, can be."""
    if not isinstance(values, dict) or len(values) > 200:
        raise ConfigError("Those settings can't be saved.")
    general = _general(text)
    lines = text.splitlines(keepends=True)
    for key, value in values.items():
        if not isinstance(key, str) or key not in general or not _shown(key, general[key][1]):
            raise ConfigError(f"The tablet has no setting called {str(key)[:80]} that can be changed here.")
        if key not in KNOWN and not advanced:
            raise ConfigError("Switch on Show advanced settings to change that setting.")
        number, stored = general[key]
        if key in KNOWN and _for_person(KNOWN[key][1], stored) is None:
            raise ConfigError(f"{KNOWN[key][0]} is stored in a way OpenMarker does not know, so it was left alone.")
        new = _to_store(key, value)
        if new != stored:
            line = lines[number]
            ending = line[len(line.rstrip("\r\n")):]
            lines[number] = f"{line[: line.index('=') + 1]}{new}{ending}"
    return "".join(lines)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def save(host: str, values: dict, advanced: bool = False) -> bool:
    """Change settings on the tablet. Returns whether anything was different (and the tablet's app restarted)."""
    before = tablet._ssh(host, f"cat {CONF}")
    after = edited(before.decode("utf-8", "surrogateescape"), values, advanced).encode("utf-8", "surrogateescape")
    if after == before:
        return False
    # One command, so that the tablet's app is started again even if the connection is lost half way.
    # The new file replaces the old one only if the old one is still what was read and the new one arrived whole.
    script = (
        f"F={CONF}; systemctl stop xochitl; R=changed; "
        f"if [ \"$(sha256sum < $F | cut -d' ' -f1)\" = {_digest(before)} ]; then R=broken; "
        f"cp -p $F $F.new && cat > $F.new && [ \"$(sha256sum < $F.new | cut -d' ' -f1)\" = {_digest(after)} ] "
        f"&& cp -p $F $F.openmarker-backup && mv $F.new $F && R=saved; fi; "
        f"rm -f $F.new; systemctl start xochitl; echo $R"
    )
    answer = tablet._ssh(host, script, stdin=after).decode("utf-8", "replace").strip().splitlines()
    result = answer[-1] if answer else ""
    if result == "changed":
        raise ConfigError("The tablet changed its settings at the same moment. Nothing was saved; try again.")
    if result != "saved":
        raise ConfigError("The settings could not be written on the tablet. Nothing was changed.")
    return True


def checked_name(name: object) -> str:
    if not isinstance(name, str) or not NAME.fullmatch(name.strip()):
        raise ConfigError("A device name has 1 to 63 letters, digits and hyphens, and does not begin or end with a hyphen.")
    return name.strip()


def rename(host: str, name: object) -> str:
    """Give the tablet a new network name."""
    name = checked_name(name)
    script = (
        f"if command -v hostnamectl >/dev/null 2>&1; then hostnamectl set-hostname {name}; "
        f"else echo {name} > /etc/hostname && hostname {name}; fi; "
        f"systemctl try-restart avahi-daemon >/dev/null 2>&1; hostname"
    )
    if tablet._ssh(host, script).decode("utf-8", "replace").strip() != name:
        raise ConfigError("The tablet did not take the new name.")
    return name
