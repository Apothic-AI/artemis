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

"""Telemetry for user switches: step phrases, checker boundaries, context/state fields."""

from types import SimpleNamespace

from artemis.agents.checker.checker import (
    USER_SWITCH_BOUNDARY_MARKER,
    _format_history,
    is_user_switch_step,
)
from artemis.context import DeviceContext, DevicePlatform
from artemis.graph.state import State
from artemis.graph.visibility import NODE_VISIBILITY, validate_manifest
from artemis.utils.task_tree import format_action_clean


# --- Step action phrases ---------------------------------------------------------------


def test_switch_phrase_names_the_profile():
    record = {"action": "manage_user", "intent": "switch", "user_name": "Test", "user_id": 10}
    assert format_action_clean(record) == "Switched device user to profile 'Test' (ID 10)"


def test_switch_phrase_without_a_name_falls_back_to_the_id():
    assert (
        format_action_clean({"action": "manage_user", "intent": "switch", "user_id": 10})
        == "Switched device user to ID 10"
    )


def test_list_and_current_phrases():
    assert format_action_clean({"action": "manage_user", "intent": "list"}) == (
        "Listed device user profiles"
    )
    assert format_action_clean({"action": "manage_user", "intent": "current", "user_id": 0}) == (
        "Checked current device user (ID 0)"
    )


def test_flash_record_shape_renders_the_same_phrase():
    record = {
        "action": "manage_user",
        "args": {"action": "switch", "user_id": 11},
    }
    assert format_action_clean(record) == "Switched device user to ID 11"


# --- Checker boundary detection --------------------------------------------------------


def test_is_user_switch_step_detects_both_record_shapes():
    pro = {"action_taken": {"action": "manage_user", "intent": "switch", "user_id": 10}}
    flash = {"action_taken": {"action": "manage_user", "args": {"action": "switch", "user_id": 10}}}
    assert is_user_switch_step(pro)
    assert is_user_switch_step(flash)


def test_is_user_switch_step_rejects_non_switch_actions():
    assert not is_user_switch_step({"action_taken": {"action": "manage_user", "intent": "list"}})
    assert not is_user_switch_step({"action_taken": {"action": "tap", "coordinates": [1, 2]}})
    assert not is_user_switch_step({"action_taken": "not a dict"})
    assert not is_user_switch_step({})


def test_history_marks_user_switch_boundaries():
    steps = [
        {
            "step_number": 1,
            "action_taken": {"action": "manage_user", "intent": "switch", "user_id": 10},
        },
        {"step_number": 2, "summary": "Tapped something"},
    ]
    ctx = SimpleNamespace(data_engine=None)
    out = _format_history(ctx, steps=steps)
    lines = out.splitlines()
    assert lines[0].endswith(USER_SWITCH_BOUNDARY_MARKER)
    assert USER_SWITCH_BOUNDARY_MARKER not in lines[1]


# --- Context & state fields ------------------------------------------------------------


def test_device_context_defaults_and_renders_the_user():
    ctx = DeviceContext()
    assert ctx.current_user_id == 0
    assert "Current Android user ID: 0" in ctx.to_str()
    ctx.current_user_id = 10
    assert "Current Android user ID: 10" in ctx.to_str()


def test_state_carries_the_current_user():
    state = State.initial("goal")
    assert state.current_user_id == 0
    state.current_user_id = 10
    assert state.current_user_id == 10


def test_visibility_manifest_admits_current_user_id():
    assert validate_manifest() == []
    assert "current_user_id" in NODE_VISIBILITY["operator"].reads
    assert "current_user_id" in NODE_VISIBILITY["validator"].reads
    assert "current_user_id" in NODE_VISIBILITY["validator"].writes


def test_state_reducer_keeps_the_latest_user():
    from artemis.graph.state import take_last

    assert take_last(0, 10) == 10
