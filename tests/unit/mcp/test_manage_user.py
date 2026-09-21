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

"""The manage_user tool: spec schema, executor translation, and switch guard rails."""

import pytest

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.drivers.base import AndroidUserInfo, FLAG_ADMIN, FLAG_GUEST, FLAG_MANAGED_PROFILE
from artemis.mcp.action_manifest import OPTIONAL_ACTIONS, available_device_actions
from artemis.mcp.action_specs import (
    ACTION_SPECS,
    OPERATOR_SHELL_ORDER,
    make_wire_handler,
    operator_shell_tool,
    tool_declaration,
    wire_dialects,
)
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.adb import AdbActuator
from artemis.drivers.android.adb_driver import UserSwitchRestrictedError


def _user(user_id: int, name: str, flags: int, current: bool = False) -> AndroidUserInfo:
    return AndroidUserInfo(user_id=user_id, name=name, flags=flags, is_current=current)


class FakeUserDriver:
    """Scripted driver: user table, current user, switch outcomes, trust dump."""

    def __init__(self, users: list[AndroidUserInfo], current: int = 0):
        self.users = users
        self.current = current
        self.switch_calls: list[int] = []
        self.shell_commands: list[str] = []
        self.switch_outcome: Exception | bool = True
        #: Bumped to the target id after `switch_user` when the human completes
        #: a manual switch; polls then confirm.
        self.manual_switch_after_attempt = False
        self.device_id = "emulator-5554"

    async def list_users(self) -> list[AndroidUserInfo]:
        return self.users

    async def get_current_user(self) -> int:
        return self.current

    async def switch_user(self, user_id: int) -> bool:
        self.switch_calls.append(user_id)
        if isinstance(self.switch_outcome, Exception):
            if self.manual_switch_after_attempt:
                # The firmware refused, but the human completes the switch on
                # the device screen while the poll window runs.
                self.current = user_id
            raise self.switch_outcome
        if not self.switch_outcome:
            return False
        # An accepted switch changes the foreground user (the settle poll
        # confirms against this).
        self.current = user_id
        return True

    async def execute_shell(self, command: str, timeout_seconds: float = 15.0, user_id=None):
        self.shell_commands.append(command)
        return self.trust_dump

    trust_dump = 'User "0":\n    deviceLocked=false\nUser "10":\n    deviceLocked=false\n'


class FakeHelperManager:
    def __init__(self):
        self.switched: list[tuple[str, int]] = []

    def switch_session(self, serial: str, user_id: int):
        self.switched.append((serial, user_id))
        return object()


def _actuator(driver: FakeUserDriver, helper: FakeHelperManager | None = None) -> AdbActuator:
    ctx = ArtemisContext(
        trace_id="test",
        device=DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id=driver.device_id,
        ),
    )
    controller = type("C", (), {})()
    controller.driver = driver
    controller.get_ui_elements = pytest.importorskip("types").SimpleNamespace  # replaced below
    import asyncio as _asyncio

    async def _elements():
        return [{"text": "settings"}]

    controller.get_ui_elements = _elements
    return AdbActuator(ctx, controller=controller, helper_manager=helper or FakeHelperManager())


USERS = [
    _user(0, "Owner", FLAG_ADMIN, current=True),
    _user(10, "Test", 0x0040),
    _user(11, "Guest", FLAG_GUEST),
    _user(12, "Work", FLAG_MANAGED_PROFILE),
]


# --- Spec & manifest -------------------------------------------------------------------


def test_manage_user_is_an_optional_action():
    assert "manage_user" in OPTIONAL_ACTIONS
    assert "manage_user" in ACTION_SPECS


def test_manage_user_operator_shell_schema():
    shell = operator_shell_tool("manage_user")
    assert "Switch" in shell.description or "switch" in shell.description
    schema = shell.args_schema.model_json_schema()
    props = schema["properties"]
    assert set(props) == {"action", "user_id"}
    assert props["action"]["enum"] == ["switch", "list", "current"]
    assert "user_id" not in schema.get("required", [])


def test_manage_user_declaration_projects_the_operator_shell():
    decl = tool_declaration("manage_user")
    assert decl.parameters["required"] == ["action"]
    assert decl.parameters["properties"]["action"]["enum"] == ["switch", "list", "current"]


def test_manage_user_wire_dialect_binds_the_actuator():
    spec = ACTION_SPECS["manage_user"]
    assert spec.wire is not None
    assert {p.name for p in spec.wire.params} == {"action", "user_id"}
    assert spec.name in {s.name for s in wire_dialects()}


def test_manage_user_in_operator_shell_order():
    assert OPERATOR_SHELL_ORDER.index("manage_user") == OPERATOR_SHELL_ORDER.index("manage_app") + 1


@pytest.mark.asyncio
async def test_wire_handler_invokes_manage_user():
    calls = []

    class _A:
        async def manage_user(self, action, user_id=None):
            calls.append((action, user_id))
            from artemis.mcp.action_types import ActionResult

            return ActionResult.success("manage_user", "ok")

    spec = ACTION_SPECS["manage_user"]

    def _wrap(res):
        return res

    def _wrap_exc(action, exc):
        raise exc

    handler = make_wire_handler(spec, _A(), _wrap, _wrap_exc)
    await handler(action="switch", user_id="10")
    assert calls == [("switch", "10")]


def test_available_device_actions_include_manage_user_for_full_backends():
    class _Full:
        def capabilities(self):
            from artemis.mcp.action_manifest import DEVICE_ACTIONS

            return DEVICE_ACTIONS

    assert "manage_user" in available_device_actions(_Full())


# --- Actuator: list / current / invalid -----------------------------------------------


@pytest.mark.asyncio
async def test_list_formats_profiles():
    actuator = _actuator(FakeUserDriver(USERS))
    res = await actuator.manage_user("list")
    assert res.ok
    assert "ID 0 'Owner'" in res.message
    assert "admin" in res.message
    assert "ID 11 'Guest'" in res.message and "guest" in res.message
    assert "ID 12 'Work'" in res.message and "managed profile" in res.message


@pytest.mark.asyncio
async def test_current_reports_active_profile():
    driver = FakeUserDriver(USERS, current=10)
    actuator = _actuator(driver)
    res = await actuator.manage_user("current")
    assert res.ok
    assert "ID 10" in res.message and "Test" in res.message


@pytest.mark.asyncio
async def test_invalid_action_is_rejected():
    actuator = _actuator(FakeUserDriver(USERS))
    res = await actuator.manage_user("restart")
    assert not res.ok
    assert res.code is ActionCode.INVALID_ARGS


# --- Actuator: switch guard rails -----------------------------------------------------


@pytest.mark.asyncio
async def test_switch_requires_user_id():
    actuator = _actuator(FakeUserDriver(USERS))
    res = await actuator.manage_user("switch")
    assert not res.ok
    assert res.code is ActionCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_switch_rejects_unknown_profile():
    actuator = _actuator(FakeUserDriver(USERS))
    res = await actuator.manage_user("switch", 99)
    assert not res.ok
    assert res.code is ActionCode.TARGET_NOT_FOUND


@pytest.mark.asyncio
async def test_switch_rejects_managed_profile():
    actuator = _actuator(FakeUserDriver(USERS))
    res = await actuator.manage_user("switch", 12)
    assert not res.ok
    assert res.code is ActionCode.INVALID_ARGS
    assert "work profile" in res.message.lower()


@pytest.mark.asyncio
async def test_switch_is_blocked_by_a_locked_target_profile():
    driver = FakeUserDriver(USERS)
    driver.trust_dump = 'User "0":\n    deviceLocked=false\nUser "10":\n    deviceLocked=true\n'
    actuator = _actuator(driver)
    res = await actuator.manage_user("switch", 10)
    assert not res.ok
    assert res.code is ActionCode.BLOCKED
    assert "Unlock manually" in res.message
    assert driver.switch_calls == []


@pytest.mark.asyncio
async def test_switch_polls_until_the_foreground_user_matches(monkeypatch):
    async def _instant_sleep(_s, *a, **k):
        return None

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    driver = FakeUserDriver(USERS, current=0)
    helper = FakeHelperManager()
    actuator = _actuator(driver, helper)

    res = await actuator.manage_user("switch", 10)

    assert res.ok
    assert driver.switch_calls == [10]
    assert driver.current == 10
    assert helper.switched == [("emulator-5554", 10)]
    assert actuator.ctx.device.current_user_id == 10
    assert "Switched device user to profile 'Test' (ID 10)" in res.message


@pytest.mark.asyncio
async def test_switch_returns_blocked_when_firmware_restricts(monkeypatch):
    async def _instant_sleep(_s, *a, **k):
        return None

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    driver = FakeUserDriver(USERS, current=0)
    driver.switch_outcome = UserSwitchRestrictedError(
        "Error: java.lang.SecurityException: needs MANAGE_USERS"
    )
    actuator = _actuator(driver)
    actuator.MANUAL_SWITCH_CONFIRM_TIMEOUT_SECONDS = 0.0

    res = await actuator.manage_user("switch", 10)

    assert not res.ok
    assert res.code is ActionCode.BLOCKED
    assert "manually on the device" in res.message


@pytest.mark.asyncio
async def test_switch_resumes_when_manual_switch_is_confirmed(monkeypatch):
    """A BLOCKED pause recovers automatically once the human switches by hand."""

    async def _instant_sleep(_s, *a, **k):
        return None

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    driver = FakeUserDriver(USERS, current=0)
    driver.switch_outcome = UserSwitchRestrictedError("SecurityException")
    driver.manual_switch_after_attempt = True
    helper = FakeHelperManager()
    actuator = _actuator(driver, helper)
    actuator.MANUAL_SWITCH_CONFIRM_TIMEOUT_SECONDS = 5.0
    actuator.SWITCH_POLL_INTERVAL_SECONDS = 0.0

    res = await actuator.manage_user("switch", 10)

    assert res.ok
    assert "manually" in res.message.lower()
    assert helper.switched == [("emulator-5554", 10)]


@pytest.mark.asyncio
async def test_switch_is_a_noop_when_already_current(monkeypatch):
    driver = FakeUserDriver(USERS, current=10)
    helper = FakeHelperManager()
    actuator = _actuator(driver, helper)

    res = await actuator.manage_user("switch", 10)

    assert res.ok
    assert driver.switch_calls == []  # no am switch-user issued
    assert helper.switched == [("emulator-5554", 10)]


@pytest.mark.asyncio
async def test_switch_into_guest_carries_the_ephemeral_note(monkeypatch):
    async def _instant_sleep(_s, *a, **k):
        return None

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    driver = FakeUserDriver(USERS, current=0)
    actuator = _actuator(driver, FakeHelperManager())

    res = await actuator.manage_user("switch", 11)

    assert res.ok
    assert "ephemeral" in res.message


@pytest.mark.asyncio
async def test_switch_out_of_guest_carries_the_ephemeral_note(monkeypatch):
    async def _instant_sleep(_s, *a, **k):
        return None

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    users = [
        _user(0, "Owner", FLAG_ADMIN),
        _user(11, "Guest", FLAG_GUEST, current=True),
    ]
    driver = FakeUserDriver(users, current=11)
    actuator = _actuator(driver, FakeHelperManager())

    res = await actuator.manage_user("switch", 0)

    assert res.ok
    assert "ephemeral" in res.message


@pytest.mark.asyncio
async def test_switch_succeeds_even_when_helper_reattach_fails(monkeypatch):
    """The switch already happened; a dead helper is a diagnostic, not a failure."""

    async def _instant_sleep(_s, *a, **k):
        return None

    class _BrokenHelper:
        def switch_session(self, serial, user_id):
            raise RuntimeError("helper dead")

    monkeypatch.setattr("artemis.mcp.actuators.adb.asyncio.sleep", _instant_sleep)
    driver = FakeUserDriver(USERS, current=0)
    actuator = _actuator(driver, _BrokenHelper())

    res = await actuator.manage_user("switch", 10)

    assert res.ok
    assert "re-attachment failed" in res.message
