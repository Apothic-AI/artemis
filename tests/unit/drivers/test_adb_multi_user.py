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

"""Multi-user primitives of the AndroidAdbDriver: enumeration, switching, scoping."""

import re
from unittest.mock import MagicMock

import pytest

from artemis.drivers.android.adb_driver import (
    AndroidAdbDriver,
    UserSwitchRestrictedError,
    parse_current_user,
    parse_user_list,
    scope_command_for_user,
)
from artemis.drivers.base import AndroidUserInfo, FLAG_GUEST, FLAG_MANAGED_PROFILE

_PM_LIST_USERS = (
    "Users:\n"
    "\tUserInfo{0:Owner:13} running\n"
    "\tUserInfo{10:Test User:110}\n"
    "\tUserInfo{11:Guest:14} running\n"
)


class FakeShellDevice:
    """Stands in for adbutils' AdbDevice: records shell commands, answers from a table."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}
        self.commands: list[str] = []

    def shell(self, cmd, timeout=None):
        self.commands.append(cmd)
        for pattern, out in self.responses.items():
            if re.fullmatch(pattern, cmd, flags=re.DOTALL):
                return out
        return ""


@pytest.fixture
def driver():
    d = AndroidAdbDriver(device_id="emulator-5554", adb_client=MagicMock())
    return d


def _set_device(d: AndroidAdbDriver, device: FakeShellDevice) -> None:
    d._device = device  # the cached property seam


# --- Pure parsers ---------------------------------------------------------------------


def test_parse_user_list_reads_ids_names_flags_and_running():
    users = parse_user_list(_PM_LIST_USERS)
    assert users == [
        (0, "Owner", 0x13, True),
        (10, "Test User", 0x110, False),
        (11, "Guest", 0x14, True),
    ]


def test_parse_user_list_ignores_unrelated_lines():
    assert parse_user_list("Users:\n  (no users)") == []


def test_parse_current_user_extracts_first_integer():
    assert parse_current_user("0\n") == 0
    assert parse_current_user("Current user: 3") == 3
    assert parse_current_user("") is None


def test_scope_command_for_user_positions():
    assert scope_command_for_user("am broadcast -n X", 10) == "am broadcast --user 10 -n X"
    assert scope_command_for_user("am start -a MAIN", 2) == "am start --user 2 -a MAIN"
    assert (
        scope_command_for_user("settings get secure key", 10) == "settings --user 10 get secure key"
    )
    assert scope_command_for_user("ls -la", 10) == "ls -la"
    assert scope_command_for_user("am broadcast -n X", None) == "am broadcast -n X"


# --- Driver primitives ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_users_marks_current_user(driver):
    device = FakeShellDevice(
        {
            r"pm list users": _PM_LIST_USERS,
            r"am get-current-user": "10\n",
        }
    )
    _set_device(driver, device)

    users = await driver.list_users()

    assert [u.user_id for u in users] == [0, 10, 11]
    current = [u for u in users if u.is_current]
    assert [u.user_id for u in current] == [10]
    assert [u.user_id for u in users if u.is_running] == [0, 11]


@pytest.mark.asyncio
async def test_list_users_falls_back_to_dumpsys(driver):
    device = FakeShellDevice(
        {
            r"pm list users": "",
            r"dumpsys user": "Users:\n  UserInfo{0:Owner:13}\n",
            r"am get-current-user": "0\n",
        }
    )
    _set_device(driver, device)

    users = await driver.list_users()
    assert [(u.user_id, u.name) for u in users] == [(0, "Owner")]


@pytest.mark.asyncio
async def test_list_users_raises_without_parseable_output(driver):
    _set_device(driver, FakeShellDevice())
    with pytest.raises(RuntimeError, match="user list"):
        await driver.list_users()


@pytest.mark.asyncio
async def test_get_current_user(driver):
    _set_device(driver, FakeShellDevice({r"am get-current-user": "0\n"}))
    assert await driver.get_current_user() == 0


@pytest.mark.asyncio
async def test_get_current_user_raises_on_garbage(driver):
    _set_device(driver, FakeShellDevice({r"am get-current-user": "error: no users\n"}))
    with pytest.raises(RuntimeError, match="get-current-user"):
        await driver.get_current_user()


@pytest.mark.asyncio
async def test_switch_user_accepts_the_command(driver):
    device = FakeShellDevice({r"am switch-user 10": "Success\n"})
    _set_device(driver, device)
    assert await driver.switch_user(10) is True
    assert device.commands == ["am switch-user 10"]


@pytest.mark.asyncio
async def test_switch_user_returns_false_on_other_refusal(driver):
    _set_device(driver, FakeShellDevice({r"am switch-user 10": "Error: user not found\n"}))
    assert await driver.switch_user(10) is False


@pytest.mark.asyncio
async def test_switch_user_raises_on_firmware_restriction(driver):
    for output in (
        "Error: java.lang.SecurityException: You need MANAGE_USERS permission",
        "Error: SecurityException: switch user not allowed",
        "Cannot switch user on this device",
    ):
        _set_device(driver, FakeShellDevice({r"am switch-user 10": output}))
        with pytest.raises(UserSwitchRestrictedError):
            await driver.switch_user(10)


@pytest.mark.asyncio
async def test_switch_user_success_is_never_mistaken_for_restriction(driver):
    _set_device(driver, FakeShellDevice({r"am switch-user 10": "Switched successfully\n"}))
    assert await driver.switch_user(10) is True


# --- Per-user plumbing ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_shell_scopes_user_aware_commands(driver):
    device = FakeShellDevice()
    _set_device(driver, device)

    await driver.execute_shell("am broadcast -a X", user_id=10)
    await driver.execute_shell("settings get secure key", user_id=10)
    await driver.execute_shell("ls", user_id=10)
    await driver.execute_shell("am broadcast -a X")

    assert device.commands == [
        "am broadcast --user 10 -a X",
        "settings --user 10 get secure key",
        "ls",
        "am broadcast -a X",
    ]


@pytest.mark.asyncio
async def test_launch_app_targets_profile_with_am_start(driver):
    device = FakeShellDevice()
    _set_device(driver, device)

    assert await driver.launch_app("com.example.app", user_id=10) is True
    assert device.commands == [
        "am start --user 10 -a android.intent.action.MAIN"
        " -c android.intent.category.LAUNCHER -p com.example.app"
    ]

    assert await driver.launch_app("com.example.app") is True
    assert device.commands[-1] == "monkey -p com.example.app -c android.intent.category.LAUNCHER 1"


# --- AndroidUserInfo model -------------------------------------------------------------


def test_user_info_flags_drive_kind():
    assert AndroidUserInfo(user_id=0, name="O", flags=0x0001).is_admin
    assert AndroidUserInfo(user_id=11, name="Guest", flags=FLAG_GUEST).is_guest
    assert AndroidUserInfo(user_id=10, name="W", flags=FLAG_MANAGED_PROFILE).is_managed_profile
    assert AndroidUserInfo(user_id=12, name="S", flags=0x0040).kind == "secondary"
