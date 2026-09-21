# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Per-user helper provisioning: settings --user scoping, token broadcast, session keying."""

from __future__ import annotations

from pathlib import Path
import subprocess

import pytest

from artemis.runtime import helper_manager as hm
from artemis.runtime.helper_manager import (
    AccessibilityHelperManager,
    BundledHelper,
    HelperSession,
)

SERIAL = "pixel-1"


class FakeAdb:
    """Scripted adb with per-user secure-settings storage."""

    def __init__(self):
        self.calls: list[list[str]] = []
        self.installed: dict[str, int | None] = {SERIAL: 2}
        #: (serial, user_id) -> enabled_accessibility_services value.
        self.services: dict[tuple[str, int], str] = {(SERIAL, 0): ""}
        self.transport: dict[str, str] = {SERIAL: "3"}
        self.forwards: list[tuple[str, int, int]] = []
        self.next_port = 40000
        self.tokens: dict[tuple[str, int], str] = {}

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        out, code = "", 0
        if args[:2] == ["devices", "-l"]:
            lines = ["List of devices attached"]
            for serial, tid in self.transport.items():
                lines.append(f"{serial}  device product:p model:m device:d transport_id:{tid}")
            out = "\n".join(lines) + "\n"
        elif args[:2] == ["forward", "--list"]:
            out = "".join(f"{s} tcp:{local} tcp:{r}\n" for s, local, r in self.forwards)
        elif args[0] == "-s":
            serial, rest = args[1], args[2:]
            if rest[:3] == ["shell", "dumpsys", "package"]:
                out = f"Package [{rest[3]}]\n    versionCode={self.installed.get(serial)}\n"
            elif rest[:2] == ["shell", "settings"]:
                settings_args = rest[2:]
                user = 0
                if len(settings_args) >= 2 and settings_args[0] == "--user":
                    user = int(settings_args[1])
                    settings_args = settings_args[2:]
                op = settings_args[0] if settings_args else ""
                key = settings_args[2] if len(settings_args) > 2 else ""
                if op == "get":
                    out = (
                        (self.services.get((serial, user), "null")) + "\n"
                        if key == "enabled_accessibility_services"
                        else "1\n"
                    )
                elif op == "put":
                    if key == "enabled_accessibility_services":
                        value = settings_args[3] if len(settings_args) > 3 else ""
                        self.services[(serial, user)] = "" if value == "null" else value
            elif rest[:3] == ["shell", "am", "broadcast"]:
                user = _broadcast_user(rest[3:])
                self.tokens[(serial, user)] = rest[-1]
                out = "Broadcast completed: result=0\n"
            elif rest[:3] == ["shell", "am", "start"]:
                pass
            elif rest[:2] == ["shell", "ps"]:
                out = "PID ARGS"  # no UiAutomation holders
            elif rest[0] == "install":
                out = "Success\n"
            elif rest[:2] == ["forward", "--no-rebind"]:
                port = self.next_port
                self.next_port += 1
                self.forwards.append((serial, port, int(rest[3].split(":")[1])))
                out = f"{port}\n"
            elif rest[:2] == ["forward", "--remove"]:
                port = int(rest[2].split(":")[1])
                self.forwards = [f for f in self.forwards if not (f[0] == serial and f[1] == port)]
            else:
                raise AssertionError(f"unexpected adb call: {args}")
        else:
            raise AssertionError(f"unexpected adb call: {args}")
        return subprocess.CompletedProcess(args, code, out, "")


def _split_user_and_key(tokens: list[str]) -> tuple[int, str]:
    """Reads ``[--user N] key`` off a settings get/put argv tail."""
    if tokens[:1] == ["--user"]:
        return int(tokens[1]), tokens[2]
    return 0, tokens[0]


def _broadcast_user(argv_after_shell: list[str]) -> int:
    """The ``--user`` value of an ``am broadcast`` argv, or 0."""
    if "--user" in argv_after_shell:
        return int(argv_after_shell[argv_after_shell.index("--user") + 1])
    return 0


class FakePing:
    """Answers on host ports whose forward targets a device with the service enabled."""

    def __init__(self, adb: FakeAdb, current_user: dict[str, int]):
        self.adb = adb
        self.current_user = current_user

    def __call__(self, port: int):
        for serial, local, remote in self.adb.forwards:
            if local != port or remote != hm.DEVICE_PORT:
                continue
            user = self.current_user[serial]
            if self.adb.installed.get(serial) is None:
                return None
            if hm.SERVICE_NAME not in (self.adb.services.get((serial, user), "") or ""):
                return None
            return {
                "success": True,
                "version_code": self.adb.installed[serial],
                "version_name": "1.1.0",
                "protocol_version": 2,
                "auth_required": True,
                "token_set": (serial, user) in self.adb.tokens,
            }
        return None


@pytest.fixture
def env(tmp_path: Path, bundled: BundledHelper):
    adb = FakeAdb()
    current_user = {SERIAL: 0}
    manager = AccessibilityHelperManager(
        run_adb=adb,
        ping=FakePing(adb, current_user),
        bundled=bundled,
        sleep=lambda _s: None,
        token_path=tmp_path / "token" / "session.token",
        auto_install=lambda: True,
    )
    manager.current_user = current_user  # test handle: the device's foreground user
    return adb, manager


@pytest.fixture
def bundled(tmp_path: Path) -> BundledHelper:
    apk = tmp_path / "helper.apk"
    apk.write_bytes(b"apk")
    return BundledHelper(apk_path=apk, version_code=2, version_name="1.1.0", sha256="x")


# --- Per-user settings argv -----------------------------------------------------------


def test_settings_argv_omits_the_flag_for_user_zero():
    assert hm._settings_argv("get", "k") == ["shell", "settings", "get", "secure", "k"]
    assert hm._settings_argv("put", "k", 0, value="v") == [
        "shell",
        "settings",
        "put",
        "secure",
        "k",
        "v",
    ]


def test_settings_argv_scopes_non_default_users():
    assert hm._settings_argv("get", "k", 10) == [
        "shell",
        "settings",
        "--user",
        "10",
        "get",
        "secure",
        "k",
    ]
    assert hm._settings_argv("put", "k", 10, value="v") == [
        "shell",
        "settings",
        "--user",
        "10",
        "put",
        "secure",
        "k",
        "v",
    ]


def test_is_service_enabled_queries_the_profile(env):
    adb, manager = env
    adb.services[(SERIAL, 10)] = hm.SERVICE_NAME
    assert manager.is_service_enabled(SERIAL, 10) is True
    assert manager.is_service_enabled(SERIAL) is False
    scoped = [c for c in adb.calls if "--user" in c and c[c.index("--user") + 1] == "10"]
    assert scoped, "expected a --user 10 scoped settings query"


def test_enable_service_writes_per_user_settings(env):
    adb, manager = env
    assert manager._enable_service(SERIAL, 10) is True
    assert adb.services[(SERIAL, 10)] == hm.SERVICE_NAME
    puts = [c for c in adb.calls if c[:4] == ["-s", SERIAL, "shell", "settings"] and "--user" in c]
    assert any(c[c.index("--user") + 1] == "10" for c in puts)
    # User 0's setting is untouched by the per-profile write.
    assert adb.services[(SERIAL, 0)] == ""


def test_revive_service_scopes_the_rebind(env):
    adb, manager = env
    adb.services[(SERIAL, 10)] = hm.SERVICE_NAME
    manager._revive_service(SERIAL, 10)
    assert adb.services[(SERIAL, 10)] == hm.SERVICE_NAME


def test_push_token_scopes_the_broadcast(env):
    adb, manager = env
    assert manager.push_token(SERIAL, 10) is True
    assert (SERIAL, 10) in adb.tokens
    assert (SERIAL, 0) not in adb.tokens
    broadcast = next(c for c in adb.calls if "broadcast" in c)
    assert broadcast[broadcast.index("--user") + 1] == "10"


# --- Session keying by user -----------------------------------------------------------


def test_attach_records_the_profile_and_stays_cached(env):
    adb, manager = env
    # The device's foreground user is already profile 10 (the helper only runs
    # in the foreground user's context, so the tunnel answers for that profile).
    manager.current_user[SERIAL] = 10
    session = manager.attach(SERIAL, user_id=10)
    assert session.user_id == 10
    again = manager.attach(SERIAL, user_id=10)
    assert again is session


def test_attach_rebuilds_when_the_requested_profile_changes(env):
    adb, manager = env
    first = manager.attach(SERIAL, user_id=0)
    manager.current_user[SERIAL] = 10
    second = manager.attach(SERIAL, user_id=10)
    assert second.user_id == 10
    assert second is not first
    # The rebuilt session serves the new profile's enabled setting.
    assert adb.services[(SERIAL, 10)] == hm.SERVICE_NAME


def test_switch_session_drops_and_rebuilds_for_the_new_profile(env):
    adb, manager = env
    first = manager.attach(SERIAL, user_id=0)
    old_port = first.local_port

    second = manager.switch_session(SERIAL, 10)

    assert isinstance(second, HelperSession)
    assert second.user_id == 10
    assert second.local_port != old_port or second is not first
    assert (SERIAL, 10) in adb.tokens
    # The old tunnel's forward was dropped with the old profile's session.
    assert (SERIAL, old_port, hm.DEVICE_PORT) not in adb.forwards


def test_attach_provisions_the_new_profile_service(env):
    adb, manager = env
    manager.current_user[SERIAL] = 10
    session = manager.switch_session(SERIAL, 10)
    assert session.user_id == 10
    assert adb.services[(SERIAL, 10)] == hm.SERVICE_NAME
