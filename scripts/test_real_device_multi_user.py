#!/usr/bin/env python3
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

"""Acceptance test: Android multi-user switching against real physical device."""

import asyncio
import sys
import time
from typing import Any

from adbutils import adb

from artemis.clients.screen_client_factory import create_screen_client
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators.adb import AdbActuator
import argparse
from artemis.platform import platform
from artemis.runtime.helper_manager import helper_manager
from artemis.utils.logger import get_logger

logger = get_logger("acceptance.multi_user")


def log_step(title: str) -> None:
    print(f"\n{'='*70}\n[STEP] {title}\n{'='*70}")


async def run_acceptance_tests(serial: str | None = None, target_user_id: int | None = None) -> bool:
    if not serial:
        devices = adb.device_list()
        if not devices:
            print("❌ No ADB devices found.")
            return False
        serial = devices[0].serial
    print(f"Starting Multi-User Acceptance Test on device {serial}")

    # 0. Initialize Context and Actuator
    ui_client = create_screen_client(serial)
    ctx = ArtemisContext(
        trace_id="acceptance-multi-user",
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id=serial,
            device_width=1080,
            device_height=2400,
        ),
        adb_client=adb,
        ui_adb_client=ui_client,
    )
    controller = UnifiedMobileController(ctx)
    actuator = AdbActuator(ctx, controller)
    driver = controller.driver

    passed_all = True

    try:
        # -------------------------------------------------------------
        # Step 1: Validate User Enumeration and Current User
        # -------------------------------------------------------------
        log_step("1. Validate User Enumeration and Current User")

        # 1.1 Driver-level enumeration
        users = await driver.list_users()
        print(f"driver.list_users() returned {len(users)} profiles:")
        for u in users:
            print(f"  - ID {u.user_id}: '{u.name}' (kind={u.kind}, flags=0x{u.flags:x}, running={u.is_running}, current={u.is_current})")

        assert len(users) >= 2, f"Expected at least 2 users, got {len(users)}"
        u0 = next((u for u in users if u.user_id == 0), None)
        assert u0 is not None, "User 0 not found in user list"
        assert u0.is_current, "User 0 should be current initially"

        if target_user_id is not None:
            target_user = next((u for u in users if u.user_id == target_user_id), None)
            assert target_user is not None, f"Specified target user ID {target_user_id} not found in user list"
        else:
            # Pick first non-owner, non-managed secondary user
            target_user = next((u for u in users if u.user_id != 0 and not u.is_managed_profile), None)
            assert target_user is not None, "No suitable secondary user profile found on device"
            target_user_id = target_user.user_id

        target_name = target_user.name

        # 1.2 Driver-level current user
        current_id = await driver.get_current_user()
        print(f"driver.get_current_user() returned: {current_id}")
        assert current_id == 0, f"Expected current user 0, got {current_id}"

        # 1.3 manage_user action 'list'
        res_list = await actuator.manage_user("list")
        print(f"actuator.manage_user('list'): ok={res_list.ok}, message='{res_list.message}'")
        assert res_list.ok, f"manage_user('list') failed: {res_list.message}"
        assert f"ID 0 '{u0.name}'" in res_list.message
        assert f"ID {target_user_id} '{target_name}'" in res_list.message
        assert "[current]" in res_list.message

        # 1.4 manage_user action 'current'
        res_curr = await actuator.manage_user("current")
        print(f"actuator.manage_user('current'): ok={res_curr.ok}, message='{res_curr.message}'")
        assert res_curr.ok, f"manage_user('current') failed: {res_curr.message}"
        assert "ID 0" in res_curr.message
        assert u0.name in res_curr.message
        print(">>> Step 1 PASSED: User enumeration and current user validated.")
        print(">>> Step 1 PASSED: User enumeration and current user validated.")

        # -------------------------------------------------------------
        # Step 2: Edge Cases / Guard Rails
        # -------------------------------------------------------------
        log_step("2. Verify Edge Cases and Guard Rails")
        
        # 2.1 Invalid user id
        res_invalid = await actuator.manage_user("switch", user_id=999)
        print(f"actuator.manage_user('switch', user_id=999): ok={res_invalid.ok}, code={res_invalid.code}, message='{res_invalid.message}'")
        assert not res_invalid.ok, "Expected failure for non-existent user 999"
        assert res_invalid.code == ActionCode.TARGET_NOT_FOUND, f"Expected TARGET_NOT_FOUND, got {res_invalid.code}"
        assert "no user profile with ID 999" in res_invalid.message

        # 2.2 Invalid action name
        res_bad_action = await actuator.manage_user("delete_all")
        print(f"actuator.manage_user('delete_all'): ok={res_bad_action.ok}, code={res_bad_action.code}, message='{res_bad_action.message}'")
        assert not res_bad_action.ok
        assert res_bad_action.code == ActionCode.INVALID_ARGS

        print(">>> Step 2 PASSED: Guard rails and edge cases validated.")

        # -------------------------------------------------------------
        # Step 3: Validate Autonomous User Switching to Target User
        # -------------------------------------------------------------
        log_step(f"3. Validate Autonomous User Switching to User {target_user_id} ('{target_name}')")
        t0 = time.time()
        res_switch_target = await actuator.manage_user("switch", user_id=target_user_id)
        elapsed_target = time.time() - t0
        print(f"Switch to User {target_user_id} took {elapsed_target:.2f}s: ok={res_switch_target.ok}, code={res_switch_target.code}, message='{res_switch_target.message}'")
        assert res_switch_target.ok, f"Switch to User {target_user_id} failed: {res_switch_target.message} (code={res_switch_target.code})"

        # Verify foreground user is now target_user_id
        now_curr_target = await driver.get_current_user()
        print(f"Current user after switch: {now_curr_target}")
        assert now_curr_target == target_user_id, f"Expected current user {target_user_id}, got {now_curr_target}"

        # Verify manage_user('current') confirms target user
        res_curr_target = await actuator.manage_user("current")
        print(f"actuator.manage_user('current'): {res_curr_target.message}")
        assert res_curr_target.ok
        assert f"ID {target_user_id}" in res_curr_target.message
        assert target_name in res_curr_target.message

        # Verify accessibility helper session is attached for target user
        session_target = helper_manager.session(serial)
        print(f"Helper session: {session_target}")
        assert session_target is not None, "Helper session not found"
        assert session_target.user_id == target_user_id, f"Expected helper session user_id {target_user_id}, got {session_target.user_id}"
        assert helper_manager.ping(session_target.local_port) is not None, f"Helper ping failed in User {target_user_id}"

        # Verify UI hierarchy retrieval in target user
        elements_target = await controller.get_ui_elements()
        print(f"UI elements retrieved in User {target_user_id}: {len(elements_target)} elements")
        assert len(elements_target) > 0, f"UI elements list in User {target_user_id} was empty"

        print(f">>> Step 3 PASSED: Autonomous switch to User {target_user_id} succeeded, helper re-attached, and hierarchy retrieved.")

        # -------------------------------------------------------------
        # Step 4: Validate Switching Back to User 0
        # -------------------------------------------------------------
        log_step(f"4. Validate Switching Back to User 0 ('{u0.name}')")
        t0 = time.time()
        res_switch_0 = await actuator.manage_user("switch", user_id=0)
        elapsed_0 = time.time() - t0
        print(f"Switch back to User 0 took {elapsed_0:.2f}s: ok={res_switch_0.ok}, code={res_switch_0.code}, message='{res_switch_0.message}'")
        assert res_switch_0.ok, f"Switch back to User 0 failed: {res_switch_0.message} (code={res_switch_0.code})"

        # Verify foreground user is now 0
        now_curr_0 = await driver.get_current_user()
        print(f"Current user after return switch: {now_curr_0}")
        assert now_curr_0 == 0, f"Expected current user 0, got {now_curr_0}"

        # Verify manage_user('current') confirms User 0
        res_curr_0 = await actuator.manage_user("current")
        print(f"actuator.manage_user('current'): {res_curr_0.message}")
        assert res_curr_0.ok
        assert "ID 0" in res_curr_0.message
        assert u0.name in res_curr_0.message

        # Verify accessibility helper session is attached for User 0
        session_0 = helper_manager.session(serial)
        print(f"Helper session: {session_0}")
        assert session_0 is not None, "Helper session not found"
        assert session_0.user_id == 0, f"Expected helper session user_id 0, got {session_0.user_id}"
        assert helper_manager.ping(session_0.local_port) is not None, "Helper ping failed in User 0"

        # Verify UI hierarchy retrieval in User 0
        elements_0 = await controller.get_ui_elements()
        print(f"UI elements retrieved in User 0: {len(elements_0)} elements")
        assert len(elements_0) > 0, "UI elements list in User 0 was empty"

        print(">>> Step 4 PASSED: Switch back to User 0 succeeded, helper re-attached, and hierarchy retrieved.")

    except Exception as exc:
        logger.error(f"Acceptance test failed with exception: {exc}")
        print(f"\n❌ ACCEPTANCE TEST FAILED: {exc}")
        passed_all = False
    finally:
        # -------------------------------------------------------------
        # Step 5: Tablet safety restore (ensure User 0)
        # -------------------------------------------------------------
        log_step("5. Safety Check: Guarantee Tablet Left on User 0")
        try:
            final_user = await driver.get_current_user()
            print(f"Current user at teardown: {final_user}")
            if final_user != 0:
                print("Switching back to User 0 for device safety...")
                await driver.switch_user(0)
                # Settle wait
                for _ in range(15):
                    if await driver.get_current_user() == 0:
                        break
                    await asyncio.sleep(1.0)
                final_user = await driver.get_current_user()
                print(f"Teardown switch complete. Final user: {final_user}")
            assert final_user == 0, f"Teardown safety assertion failed: device on user {final_user}"
            print(" Tablet safely verified on User 0.")
        except Exception as e:
            print(f"⚠️ Teardown safety check error: {e}")
            passed_all = False

    return passed_all


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Acceptance test: Android multi-user switching against real physical device.")
    parser.add_argument("--device-id", "--serial", dest="serial", default=None, help="ADB serial number of target device")
    parser.add_argument("--target-user", dest="target_user", type=int, default=None, help="Target secondary user ID to switch into")
    args = parser.parse_args()

    success = asyncio.run(run_acceptance_tests(serial=args.serial, target_user_id=args.target_user))
    sys.exit(0 if success else 1)
