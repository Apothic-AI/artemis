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

"""Executor plumbing for manage_user: wire translation, state sync, canonical calls."""

import pytest

from artemis.mcp.action_executor import McpActionExecutor
from artemis.mcp.action_names import to_canonical_call


# --- Agent-args -> wire-args translation ----------------------------------------------


def _executor() -> McpActionExecutor:
    return McpActionExecutor.__new__(McpActionExecutor)


def test_translate_manage_user_forwards_args():
    name, args, finalize, recorded = _executor()._translate(
        "manage_user", {"action": "switch", "user_id": 10}, state=None
    )
    assert name == "manage_user"
    assert args == {"action": "switch", "user_id": 10}
    assert finalize is None
    assert recorded == {}


def test_translate_manage_user_coerces_string_user_id():
    _, args, _, _ = _executor()._translate(
        "manage_user", {"action": "switch", "user_id": "10"}, state=None
    )
    assert args["user_id"] == 10


def test_translate_manage_user_rejects_non_integer_user_id():
    with pytest.raises(ValueError, match="user_id"):
        _executor()._translate("manage_user", {"action": "switch", "user_id": "owner"}, None)


def test_translate_manage_user_keeps_optional_user_id_none():
    _, args, _, _ = _executor()._translate("manage_user", {"action": "list"}, state=None)
    assert args == {"action": "list", "user_id": None}


# --- State synchronization after a confirmed switch ------------------------------------


class _State:
    def __init__(self):
        self.current_user_id = 0


def test_sync_user_state_stamps_switch_only():
    state = _State()
    McpActionExecutor._sync_user_state({"action": "switch", "user_id": 10}, state)
    assert state.current_user_id == 10
    McpActionExecutor._sync_user_state({"action": "list", "user_id": 10}, state)
    assert state.current_user_id == 10


def test_sync_user_state_ignores_missing_or_bad_ids():
    state = _State()
    McpActionExecutor._sync_user_state({"action": "switch", "user_id": None}, state)
    McpActionExecutor._sync_user_state({"action": "switch", "user_id": "x"}, state)
    assert state.current_user_id == 0


# --- Pro operator canonical calls -------------------------------------------------------


def test_canonical_call_manage_user_switch():
    name, args = to_canonical_call({"action": "manage_user", "intent": "switch", "user_id": 10})
    assert name == "manage_user"
    assert args == {"action": "switch", "user_id": 10}


def test_canonical_call_manage_user_list_and_current():
    assert to_canonical_call({"action": "manage_user", "intent": "list"}) == (
        "manage_user",
        {"action": "list", "user_id": None},
    )
    assert to_canonical_call({"action": "manage_user", "intent": "current"}) == (
        "manage_user",
        {"action": "current", "user_id": None},
    )


def test_canonical_call_manage_user_switch_requires_user_id():
    with pytest.raises(ValueError, match="user_id"):
        to_canonical_call({"action": "manage_user", "intent": "switch"})


def test_canonical_call_manage_user_rejects_unknown_action():
    with pytest.raises(ValueError, match="Unsupported manage_user"):
        to_canonical_call({"action": "manage_user", "intent": "delete", "user_id": 1})
