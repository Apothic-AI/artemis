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

"""Per-user keyguard gating in the SDK agent's _ensure_device_unlocked."""

from unittest.mock import MagicMock

import pytest

from artemis.sdk.agent import Agent
from artemis.sdk.types.exceptions import AgentError


def _agent_with_trust(trust_output: str) -> Agent:
    agent = object.__new__(Agent)
    agent._device_context = MagicMock(device_id="device-123")
    agent._adb_client = MagicMock()
    agent._adb_client.device.return_value.shell.return_value = trust_output
    return agent


_NUMERIC = 'User "0":\n    deviceLocked=false\nUser "10":\n    deviceLocked=true\n'


@pytest.mark.asyncio
async def test_locked_target_profile_blocks_for_that_user():
    agent = _agent_with_trust(_NUMERIC)
    with pytest.raises(AgentError, match="keyguard is locked"):
        await agent._ensure_device_unlocked(user_id=10)


@pytest.mark.asyncio
async def test_locked_secondary_profile_does_not_block_user_zero():
    agent = _agent_with_trust(_NUMERIC)
    await agent._ensure_device_unlocked(user_id=0)


@pytest.mark.asyncio
async def test_name_id_blocks_are_parsed_per_user():
    trust = 'User "Owner" (id=0): deviceLocked=0\nUser "Work" (id=10): deviceLocked=1\n'
    agent = _agent_with_trust(trust)
    with pytest.raises(AgentError):
        await agent._ensure_device_unlocked(user_id=10)
    await agent._ensure_device_unlocked(user_id=0)


@pytest.mark.asyncio
async def test_unparsable_trust_output_falls_back_and_passes():
    agent = _agent_with_trust("no lock info here")
    await agent._ensure_device_unlocked(user_id=10)
