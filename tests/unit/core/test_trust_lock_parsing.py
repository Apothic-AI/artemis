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

"""Per-user trust-block parsing (dumpsys trust) and its lock-state integration."""

from artemis.core.diagnostics.probes.adb_probe import (
    AdbDeviceProbe,
    parse_user_device_locked,
)

# ``dumpsys trust`` shapes seen across Android 11-15 builds: numeric block
# headers, name+id headers, (current) markers, and true/false/1/0 spellings.
NUMERIC_BLOCKS = (
    'User "0":\n    deviceLocked=false\n'
    'User "10":\n    deviceLocked=true\n'
    'User "11":\n    deviceLocked=false\n'
)
NAME_ID_BLOCKS = 'User "Owner" (id=0): deviceLocked=0\nUser "Work" (id=10): deviceLocked=1\n'
CURRENT_MARKER_BLOCKS = (
    'User "0" (current):\n    deviceLocked = 0\nUser "11":\n    deviceLocked = 1\n'
)


def test_numeric_user_blocks_are_parsed_per_user():
    assert parse_user_device_locked(NUMERIC_BLOCKS, 0) is False
    assert parse_user_device_locked(NUMERIC_BLOCKS, 10) is True
    assert parse_user_device_locked(NUMERIC_BLOCKS, 11) is False


def test_name_id_blocks_are_parsed_per_user():
    assert parse_user_device_locked(NAME_ID_BLOCKS, 0) is False
    assert parse_user_device_locked(NAME_ID_BLOCKS, 10) is True


def test_current_marker_blocks_are_parsed_per_user():
    assert parse_user_device_locked(CURRENT_MARKER_BLOCKS, 0) is False
    assert parse_user_device_locked(CURRENT_MARKER_BLOCKS, 11) is True


def test_unknown_or_blockless_user_is_none():
    assert parse_user_device_locked(NUMERIC_BLOCKS, 99) is None
    assert parse_user_device_locked("", 0) is None
    assert parse_user_device_locked(None, 0) is None


def test_block_without_lock_field_falls_through_to_none():
    trust = 'User "10":\n    trustIsManaged=false\n'
    assert parse_user_device_locked(trust, 10) is None


def test_lock_state_targets_the_requested_user_block():
    policy = ""
    # The first block (user 0) is unlocked; user 10 is locked.
    assert AdbDeviceProbe._parse_device_lock_state(policy, NUMERIC_BLOCKS, 10) is True
    assert AdbDeviceProbe._parse_device_lock_state(policy, NUMERIC_BLOCKS, 0) is False


def test_lock_state_defaults_preserve_current_user_behavior():
    policy = ""
    # No user_id (default 0): the existing current-user heuristics still apply
    # to legacy dumps without per-user numeric blocks.
    trust = 'User "Owner" (current): deviceLocked=1'
    assert AdbDeviceProbe._parse_device_lock_state(policy, trust) is True
    assert AdbDeviceProbe._parse_device_lock_state(policy, trust, 0) is True


def test_lock_state_keeps_legacy_policy_fallback():
    assert AdbDeviceProbe._parse_device_lock_state("mShowingLockscreen=true", "", 5) is True
    assert AdbDeviceProbe._parse_device_lock_state("", "", 5) is None
