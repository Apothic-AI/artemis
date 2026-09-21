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

"""Screen-client resilience across user switches: uiautomator restart, helper rebind."""

from unittest.mock import MagicMock

from artemis.clients.accessibility_client import AccessibilityClient
from artemis.clients.screen_client_factory import FallbackScreenClient
from artemis.clients.ui_automator_client import UIAutomatorClient


def test_fallback_restart_stops_u2_and_rebinds_helper():
    helper = MagicMock(spec=AccessibilityClient)
    u2 = MagicMock(spec=UIAutomatorClient)
    client = FallbackScreenClient(
        "dev",
        helper=helper,
        uiautomator_factory=MagicMock(return_value=u2),
    )
    client._uiautomator = u2  # a live fallback server from before the switch

    client.handle_user_switch(10)

    u2.handle_user_switch.assert_called_once_with(10)
    assert client._uiautomator is None
    assert client.active_backend is None
    helper.switch_user.assert_called_once_with(10)


def test_fallback_restart_without_a_user_id_skips_the_helper():
    helper = MagicMock(spec=AccessibilityClient)
    client = FallbackScreenClient(
        "dev",
        helper=helper,
        uiautomator_factory=MagicMock(return_value=MagicMock(spec=UIAutomatorClient)),
    )
    client.handle_user_switch()
    helper.switch_user.assert_not_called()


def test_uiautomator_client_restart_drops_connection_and_stops_server():
    client = UIAutomatorClient("dev")
    device = MagicMock(spec=["info", "stop_uiautomator"])
    client._device = device

    client.handle_user_switch(10)

    assert client._device is None
    device.stop_uiautomator.assert_called_once()
