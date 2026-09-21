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

"""ADB-backed actuator: the reference implementation of the actuator contract.

Outcome messages describe the dispatched command, such as
"Tapped at [x, y] (normalized)." The agent verifies the effect from subsequent
observations.

Error convention: device-level refusals the controller *reports* (an ``error`` field,
a falsy success flag) come back as ``ActionResult(ok=False)`` with the historical
message. Unexpected exceptions are allowed to propagate -- the caller (executor or MCP
server layer) owns the ``"Error during X: {e}"`` wrapping, exactly as the original
executor's ``try/except`` did.
"""

import asyncio
import inspect
import re
import time
from typing import Any

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_manifest import DEVICE_ACTIONS, ExtensionTool
from artemis.mcp.action_types import ActionCode, ActionResult
from artemis.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = ["AdbActuator", "find_element_at_coords", "ensure_focus_at_coords"]


def find_element_at_coords(elements: list[dict], x: int, y: int) -> dict | None:
    """Finds the smallest (leaf-most) focusable element containing pixel [x, y].

    Moved from ``artemis.mcp.adb_server`` so both the stdio server and in-process
    actuator share one implementation.
    """
    matching_element = None
    min_area = float("inf")

    for elem in elements:
        is_focusable = (
            elem.get("focusable") == "true"
            or elem.get("clickable") == "true"
            or "EditText" in str(elem.get("class", ""))
        )
        if not is_focusable:
            continue

        bounds_str = elem.get("bounds")
        if bounds_str and isinstance(bounds_str, str):
            match = re.match(r"\[(\-?\d+),(\-?\d+)\]\[(\-?\d+),(\-?\d+)\]", bounds_str)
            if match:
                x1, y1, x2, y2 = map(int, match.groups())
                if x1 <= x <= x2 and y1 <= y <= y2:
                    area = (x2 - x1) * (y2 - y1)
                    if area < min_area:
                        min_area = area
                        matching_element = elem

    return matching_element


async def ensure_focus_at_coords(controller, x: int, y: int) -> str | None:
    """Ensures the element at pixel [x, y] is focused, tapping only if needed.

    Returns an error string on failure, ``None`` on success. Moved from
    ``artemis.mcp.adb_server``.
    """
    try:
        elements = await controller.get_ui_elements()
        elem = find_element_at_coords(elements, x, y)
        if elem and elem.get("focused") == "true":
            logger.info(f"Element under [{x}, {y}] is already focused. Skipping tap.")
            return None
    except Exception as e:
        logger.warning(f"Failed to check focus status: {e}. Falling back to unconditional tap.")

    result = await controller.tap_at(x=x, y=y)
    if hasattr(result, "error") and result.error:
        return result.error

    # Wait for the UI to settle and keyboard to pop up after tapping
    await asyncio.sleep(1.0)
    return None


class AdbActuator:
    """Drives an Android device through ``UnifiedMobileController`` over ADB."""

    #: Bounds of the settle poll after ``am switch-user`` and of the wait for a
    #: human to perform a firmware-blocked switch by hand (see ``manage_user``).
    SWITCH_SETTLE_TIMEOUT_SECONDS = 15.0
    MANUAL_SWITCH_CONFIRM_TIMEOUT_SECONDS = 45.0
    SWITCH_POLL_INTERVAL_SECONDS = 1.0

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        helper_manager: Any | None = None,
    ):
        self.ctx = ctx
        self.controller = controller or UnifiedMobileController(ctx)
        self.helper_manager = helper_manager

    # --- Contract --------------------------------------------------------------------

    def capabilities(self) -> frozenset[str]:
        return DEVICE_ACTIONS

    def extensions(self) -> list[ExtensionTool]:
        return []

    # --- Coordinate helpers ----------------------------------------------------------

    def _dims(self) -> tuple[int, int]:
        width = getattr(self.ctx.device, "device_width", 1080) if self.ctx.device else 1080
        height = getattr(self.ctx.device, "device_height", 2400) if self.ctx.device else 2400
        return width or 1080, height or 2400

    def _to_px(self, nx: int, ny: int) -> tuple[int, int]:
        width, height = self._dims()
        x = int(max(0, min(width - 1, int(nx) * width / 1000)))
        y = int(max(0, min(height - 1, int(ny) * height / 1000)))
        return x, y

    # --- Required action -------------------------------------------------------------

    async def click_sequence(
        self, points: list[tuple[int, int]], delay_ms: int = 50
    ) -> ActionResult:
        outcomes = []
        for i, (nx, ny) in enumerate(points):
            x, y = self._to_px(nx, ny)
            result = await self.controller.tap_at(x, y)
            err = getattr(result, "error", None) if result else None
            if err:
                return ActionResult.failure(
                    "click_sequence",
                    f"Error executing click at step {i + 1}: {err}",
                    detail=str(err),
                )
            outcomes.append(f"[{nx}, {ny}]")
            if i < len(points) - 1:
                await asyncio.sleep(max(0, delay_ms) / 1000.0)

        return ActionResult.success(
            "click_sequence",
            f"Tapped in sequence at {'; '.join(outcomes)} (normalized).",
        )

    # --- Optional actions ------------------------------------------------------------

    async def click(self, nx: int, ny: int, times: int = 1, delay_ms: int = 100) -> ActionResult:
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(x, y, times=times, delay_ms=delay_ms)
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure("click", f"Error executing click: {err}", detail=str(err))
        return ActionResult.success(
            "click",
            f"Tapped at [{nx}, {ny}] (normalized).",
            normalized_coordinates=[int(nx), int(ny)],
        )

    async def long_press(self, nx: int, ny: int, duration_ms: int = 1000) -> ActionResult:
        x, y = self._to_px(nx, ny)
        result = await self.controller.tap_at(
            x, y, long_press=True, long_press_duration=duration_ms
        )
        err = getattr(result, "error", None) if result else None
        if err is not None:
            return ActionResult.failure(
                "long_press", f"Error executing long press: {err}", detail=str(err)
            )
        return ActionResult.success(
            "long_press",
            f"Long-pressed at [{nx}, {ny}] (normalized) for {duration_ms}ms.",
            normalized_coordinates=[int(nx), int(ny)],
            duration_ms=duration_ms,
        )

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        coords = None
        if target:
            nx, ny = target
            x, y = self._to_px(nx, ny)
            coords = [int(nx), int(ny)]
            err = await ensure_focus_at_coords(self.controller, x, y)
            if err:
                return ActionResult.failure(
                    "input_text",
                    f"Error focusing element: {err}",
                    detail=str(err),
                    normalized_coordinates=coords,
                )
        if clear_exist:
            if hasattr(self.controller, "erase_text") and not await self.controller.erase_text():
                return ActionResult.failure(
                    "input_text",
                    "Failed to clear existing text",
                    normalized_coordinates=coords,
                )
        elif hasattr(self.controller, "press_key"):
            # KEYCODE_MOVE_END: move cursor to the end for reliable append.
            await self.controller.press_key("123")
        if hasattr(self.controller, "type_text") and not await self.controller.type_text(
            text, clear_existing=False
        ):
            return ActionResult.failure(
                "input_text",
                f"Failed typing '{text}'.",
                normalized_coordinates=coords,
            )

        return ActionResult.success(
            "input_text",
            f"Typed '{text}'"
            + (f" at [{coords[0]}, {coords[1]}] (normalized)." if coords else "."),
            normalized_coordinates=coords,
        )

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 800,
    ) -> ActionResult:
        nx1, ny1 = start
        nx2, ny2 = end
        px1, py1 = self._to_px(nx1, ny1)
        px2, py2 = self._to_px(nx2, ny2)
        err = await self.controller.swipe_coords(px1, py1, px2, py2, duration_ms)
        if err:
            return ActionResult.failure("swipe", f"Error dragging: {err}", detail=str(err))
        return ActionResult.success(
            "swipe",
            f"Swiped from [{nx1}, {ny1}] to [{nx2}, {ny2}] (normalized).",
            normalized_coordinates=[int(nx1), int(ny1), int(nx2), int(ny2)],
            duration_ms=duration_ms,
        )

    async def press_key(self, key: str) -> ActionResult:
        key_str = str(key).lower()
        if not key_str:
            return ActionResult.failure(
                "press_key",
                f"Error executing key press '{key}'.",
                code=ActionCode.INVALID_ARGS,
            )

        if key_str == "back" and hasattr(self.controller, "go_back"):
            await self.controller.go_back()
        elif key_str == "home" and hasattr(self.controller, "go_home"):
            await self.controller.go_home()
        elif key_str == "enter" and hasattr(self.controller, "press_enter"):
            await self.controller.press_enter()
        elif hasattr(self.controller, "press_key"):
            # Pass unrecognized keycodes through verbatim (KEYCODE_* names, numeric
            # codes): the driver resolves or forwards them, matching the historical
            # adb_server behavior of accepting any Android key event.
            res = await self.controller.press_key(key)
            err = getattr(res, "error", None) if res else None
            if err:
                return ActionResult.failure(
                    "press_key",
                    f"Error executing key press '{key}': {err}",
                    detail=str(err),
                )
            if not res:
                return ActionResult.failure("press_key", f"Error executing key press '{key}'.")

        return ActionResult.success("press_key", f"Pressed key '{key}'.")

    async def manage_app(self, action: str, app_name: str) -> ActionResult:
        # Imported lazily: launch_app pulls in tool wrappers that are costly at import.
        from artemis.tools.mobile.launch_app import find_package
        from artemis.utils.app_launch_utils import launch_app_with_retries

        res = find_package(self.ctx, app_name, use_fallback=False)
        pkg = await res if inspect.iscoroutine(res) else res
        if not pkg:
            return ActionResult.failure(
                "manage_app",
                f"Error finding package for app: {app_name}",
                code=ActionCode.PACKAGE_NOT_FOUND,
            )
        target_pkg = pkg

        if action.lower() == "launch":
            res_launch = launch_app_with_retries(self.ctx, target_pkg)
            if inspect.iscoroutine(res_launch):
                res_launch = await res_launch
            if isinstance(res_launch, tuple):
                success, error_msg = res_launch
            else:
                success, error_msg = bool(res_launch), ""

            if success:
                return ActionResult.success(
                    "manage_app",
                    f"Launched app '{app_name}' ({target_pkg}); foreground confirmed.",
                )
            return ActionResult.failure(
                "manage_app",
                f"Failed to launch app '{app_name}': {error_msg}",
                detail=error_msg or None,
            )
        elif action.lower() == "stop":
            if hasattr(self.controller, "terminate_app"):
                res_term = self.controller.terminate_app(target_pkg)
                if inspect.iscoroutine(res_term):
                    await res_term
            return ActionResult.success(
                "manage_app", f"Force-stopped app '{app_name}' ({target_pkg})."
            )
        return ActionResult.failure(
            "manage_app",
            f"Invalid manage_app action: {action}",
            code=ActionCode.INVALID_ARGS,
        )

    async def manage_user(self, action: str, user_id: int | None = None) -> ActionResult:
        """Switch, list, or inspect Android user profiles (multi-user devices).

        ``list`` formats every profile with its id, name, account class and
        running state; ``current`` reports the foreground profile; ``switch``
        validates the target (exists, not a managed work profile), gates on the
        target profile's secure keyguard, executes ``am switch-user`` and polls
        until the foreground user matches, then re-attaches the accessibility
        helper inside the new profile and synchronizes ``DeviceContext``.
        """
        action_l = str(action or "").lower()
        if action_l == "list":
            return await self._list_users()
        if action_l == "current":
            return await self._current_user()
        if action_l == "switch":
            return await self._switch_user(user_id)
        return ActionResult.failure(
            "manage_user",
            f"Invalid manage_user action: {action}",
            code=ActionCode.INVALID_ARGS,
        )

    async def _list_users(self) -> ActionResult:
        driver = self.controller.driver
        try:
            users = await driver.list_users()
        except Exception as e:
            return ActionResult.failure(
                "manage_user", f"Error listing device users: {e}", detail=repr(e)
            )
        if not users:
            return ActionResult.failure(
                "manage_user", "The device reports no Android user profiles."
            )
        rows = []
        for u in users:
            markers = []
            if u.is_current:
                markers.append("current")
            elif u.is_running:
                markers.append("running")
            rows.append(
                f"ID {u.user_id} '{u.name or '?'}' ({u.kind}){self._markers_suffix(markers)}"
            )
        return ActionResult.success(
            "manage_user",
            f"Device user profiles: {'; '.join(rows)}.",
        )

    async def _current_user(self) -> ActionResult:
        driver = self.controller.driver
        try:
            current_id = await driver.get_current_user()
        except Exception as e:
            return ActionResult.failure(
                "manage_user", f"Error reading the current device user: {e}", detail=repr(e)
            )
        info = None
        try:
            info = next(u for u in await driver.list_users() if u.user_id == current_id)
        except Exception as exc:
            logger.debug(f"Profile metadata lookup for user {current_id} failed: {exc}")
        if info is not None:
            message = f"Current device user: profile '{info.name or '?'}' (ID {info.user_id}, {info.kind})."
        else:
            message = f"Current device user: ID {current_id}."
        return ActionResult.success("manage_user", message)

    async def _switch_user(self, user_id: int | None) -> ActionResult:
        if user_id is None:
            return ActionResult.failure(
                "manage_user",
                "manage_user(action='switch') requires a target 'user_id'.",
                code=ActionCode.INVALID_ARGS,
            )
        user_id = int(user_id)
        driver = self.controller.driver

        try:
            current_id = await driver.get_current_user()
        except Exception as e:
            return ActionResult.failure(
                "manage_user", f"Error reading the current device user: {e}", detail=repr(e)
            )

        info = None
        current_info = None
        users: list[Any] = []
        try:
            users = await driver.list_users()
            info = next((u for u in users if u.user_id == user_id), None)
            current_info = next((u for u in users if u.user_id == current_id), None)
        except Exception as exc:
            logger.debug(f"User enumeration during switch to {user_id} failed: {exc}")

        if current_id == user_id:
            # Already the foreground profile: either a no-op call or a human
            # completed a manual switch after a BLOCKED pause. Skip the command
            # and re-attach the helper for this profile.
            return await self._finish_switch(user_id, info)

        if info is None:
            known = ", ".join(str(u.user_id) for u in users) if users else "unknown"
            return ActionResult.failure(
                "manage_user",
                f"The device has no user profile with ID {user_id} (known profiles: {known}).",
                code=ActionCode.TARGET_NOT_FOUND,
            )
        if info.is_managed_profile:
            return ActionResult.failure(
                "manage_user",
                f"User {user_id} is a managed work profile. Work profiles run concurrently"
                " beside their parent profile and cannot be switched into; drive it from"
                " its parent or through the work-profile toggle instead.",
                code=ActionCode.INVALID_ARGS,
            )

        # Keyguard gate: a credential-locked target profile would present its
        # lock screen after the switch, and automation never guesses PINs.
        try:
            locked = await self._target_user_locked(user_id)
        except Exception as exc:
            logger.debug(f"Keyguard probe for user {user_id} failed: {exc}")
            locked = None
        if locked is True:
            return ActionResult.failure(
                "manage_user",
                f"Target user {user_id} is locked with secure credentials. Unlock manually.",
                code=ActionCode.BLOCKED,
            )

        notes: list[str] = []
        if info.is_guest or (current_info is not None and current_info.is_guest):
            notes.append(
                "Note: a guest profile was involved in this switch; guest storage is"
                " ephemeral and is wiped when the guest session ends."
            )

        from artemis.drivers.android.adb_driver import UserSwitchRestrictedError

        try:
            accepted = await driver.switch_user(user_id)
        except UserSwitchRestrictedError as exc:
            # OEM firmware refused the automated switch: request a manual
            # profile switch on the device screen and poll a bounded window
            # for the human to complete it; a later retry short-circuits on
            # "already current" once they are done.
            confirmed = await self._wait_current_user(
                user_id, self.MANUAL_SWITCH_CONFIRM_TIMEOUT_SECONDS
            )
            if not confirmed:
                return ActionResult.failure(
                    "manage_user",
                    "Firmware restricted automated user switching. Please switch to "
                    f"User {user_id} manually on the device to continue.",
                    code=ActionCode.BLOCKED,
                    detail=str(exc),
                )
            notes.append("The profile switch was performed manually on the device screen.")
        except Exception as e:
            return ActionResult.failure(
                "manage_user", f"Error switching device user: {e}", detail=repr(e)
            )
        else:
            if not accepted:
                return ActionResult.failure(
                    "manage_user",
                    f"The device refused switching to user {user_id}.",
                )
            if not await self._wait_current_user(user_id, self.SWITCH_SETTLE_TIMEOUT_SECONDS):
                return ActionResult.failure(
                    "manage_user",
                    f"Timed out waiting for the device to switch to user {user_id}.",
                    code=ActionCode.TIMEOUT,
                )

        return await self._finish_switch(user_id, info, notes=notes)

    # --- manage_user helpers ----------------------------------------------------------

    @staticmethod
    def _markers_suffix(markers: list[str]) -> str:
        return f" [{', '.join(markers)}]" if markers else ""

    def _helper(self) -> Any:
        """The configured helper manager, or the process-wide default."""
        if self.helper_manager is not None:
            return self.helper_manager
        from artemis.runtime.helper_manager import helper_manager

        return helper_manager

    async def _target_user_locked(self, user_id: int) -> bool | None:
        """Trust-state lock flag of the target profile (``None`` when unknown)."""
        from artemis.core.diagnostics.probes.adb_probe import parse_user_device_locked

        output = await self.controller.driver.execute_shell("dumpsys trust")
        return parse_user_device_locked(str(output), user_id)

    async def _wait_current_user(self, user_id: int, timeout_seconds: float) -> bool:
        """Bounded poll until the foreground user matches ``user_id``."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        while True:
            try:
                if await self.controller.driver.get_current_user() == user_id:
                    return True
            except Exception as exc:
                logger.debug(f"'am get-current-user' poll failed: {exc}")
            if time.monotonic() >= deadline:
                return False
            await asyncio.sleep(self.SWITCH_POLL_INTERVAL_SECONDS)

    async def _finish_switch(
        self,
        user_id: int,
        info: Any = None,
        notes: list[str] | None = None,
    ) -> ActionResult:
        """Helper re-attachment and context synchronization after a confirmed switch.

        The previous profile's accessibility service died at the switch, so the
        helper session is rebuilt (provisioning included) for the new profile
        and the hierarchy dump is verified. The switch itself already happened:
        a failed re-attachment is reported as a diagnostic note, never a failed
        action.
        """
        notes = list(notes or [])
        if info is not None and info.is_guest:
            notes.append(
                "Note: the active profile is a guest account; its storage is ephemeral"
                " and is wiped when the guest session ends."
            )

        serial = getattr(self.ctx.device, "device_id", None) or getattr(
            self.controller.driver, "device_id", None
        )
        if serial:
            handled = False
            prepare = getattr(self.controller.driver, "prepare_for_user_switch", None)
            if prepare is not None:
                for attempt in range(3):
                    try:
                        # Resets fallback UiAutomation clients; the fallback client
                        # also re-binds its own accessibility helper when it owns one.
                        handled = await prepare(user_id)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            logger.debug(f"Screen client restart across the user switch failed: {exc}")
                        await asyncio.sleep(1.0)
            if not handled:
                for attempt in range(3):
                    try:
                        await asyncio.to_thread(self._helper().switch_session, serial, user_id)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            notes.append(f"Accessibility helper re-attachment failed: {exc}")
                        await asyncio.sleep(1.0)
            # Verify the re-attached backend actually dumps the new profile's UI.
            for attempt in range(3):
                try:
                    elements = await self.controller.get_ui_elements()
                    if elements:
                        break
                    if attempt == 2:
                        notes.append("The screen hierarchy dump came back empty after the switch.")
                    await asyncio.sleep(1.0)
                except Exception as exc:
                    if attempt == 2:
                        notes.append(f"Screen hierarchy verification after the switch failed: {exc}")
                    await asyncio.sleep(1.0)

        try:
            self.ctx.device.current_user_id = user_id
        except Exception as exc:
            logger.debug(f"Could not stamp current_user_id on the device context: {exc}")

        if info is not None:
            message = f"Switched device user to profile '{info.name or '?'}' (ID {info.user_id})."
        else:
            message = f"Switched device user to ID {user_id}."
        if notes:
            message += " " + " ".join(notes)
        return ActionResult.success("manage_user", message)

    async def wait_for_delay(self, time_in_ms: int) -> ActionResult:
        delay_s = max(0.0, float(time_in_ms) / 1000.0)
        await asyncio.sleep(delay_s)
        return ActionResult.success(
            "wait_for_delay",
            f"Waited {time_in_ms}ms.",
            duration_ms=int(time_in_ms),
        )

    async def wait_for_text(
        self, text: str, wait_state: str | None = None, timeout_ms: int | None = None
    ) -> ActionResult:
        timeout_s = (timeout_ms or 5000) / 1000.0
        start = time.time()
        found = False
        target_state = (wait_state or "appear").lower()

        while time.time() - start < timeout_s:
            tree = await self.controller.get_ui_elements()
            text_present = text.lower() in str(tree or "").lower()
            if target_state == "appear" and text_present:
                found = True
                break
            elif target_state == "disappear" and not text_present:
                found = True
                break
            await asyncio.sleep(0.5)

        if found:
            return ActionResult.success(
                "wait_for_text",
                f"Text '{text}' {'appeared' if target_state == 'appear' else 'disappeared'}"
                f" in the UI tree after {int((time.time() - start) * 1000)}ms.",
            )
        return ActionResult.failure(
            "wait_for_text",
            f"Timed out waiting for text '{text}' to {target_state}.",
            code=ActionCode.TIMEOUT,
        )

    async def open_link(self, url: str) -> ActionResult:
        success = await self.controller.open_url(url)
        if success:
            return ActionResult.success("open_link", f"Opened link '{url}'.")
        return ActionResult.failure("open_link", f"Failed to open link '{url}'.")

    async def erase_one_char(self) -> ActionResult:
        success = await self.controller.erase_text(nb_chars=1)
        if success:
            return ActionResult.success("erase_one_char", "Erased one character.")
        return ActionResult.failure("erase_one_char", "Failed to erase one character.")

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        x, y = self._to_px(nx, ny)
        err = await ensure_focus_at_coords(self.controller, x, y)
        if err:
            return ActionResult.failure(
                "focus_and_clear_text",
                f"Error focusing element: {err}",
                detail=str(err),
                normalized_coordinates=[int(nx), int(ny)],
            )
        success = await self.controller.erase_text()
        if success:
            return ActionResult.success(
                "focus_and_clear_text",
                f"Cleared text at [{nx}, {ny}] (normalized).",
                normalized_coordinates=[int(nx), int(ny)],
            )
        return ActionResult.failure("focus_and_clear_text", "Failed to erase text.")

    # --- Internal observation primitives ---------------------------------------------

    async def take_screenshot(self) -> str:
        return await self.controller.take_screenshot()

    async def get_ui_elements(self) -> Any:
        return await self.controller.get_ui_elements()

    async def get_screen_data(self) -> Any:
        return await self.controller.get_screen_data()
