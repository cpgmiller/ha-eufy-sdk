# ruff: noqa: ANN201, D100, D102, INP001, PT009, PT027, SLF001

import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.const import SIGNAL_MAP_ROTATION
from custom_components.eufy_sdk.select import (
    MAP_ROTATION_OPTIONS,
    EufySdkMapRotationSelect,
    async_setup_entry,
)

ROBOT = "T2292B0000000001"
CAMERA = "T8161P0000000001"


def _coordinator() -> Mock:
    coord = Mock()
    coord.data = {
        ROBOT: {"name": "Robovac", "capabilities": ["vacuum_clean"]},
        CAMERA: {"name": "Garden", "capabilities": ["camera"]},
    }
    coord.config_entry.runtime_data.map_rotation = {}
    return coord


def _select(coord: Mock) -> EufySdkMapRotationSelect:
    select = EufySdkMapRotationSelect(coord, ROBOT)
    select.hass = MagicMock()
    return select


class MapRotationSelectTest(unittest.IsolatedAsyncioTestCase):
    """A local, restored rotation shared with the map image."""

    async def test_only_vacuums_get_one(self):
        coord = _coordinator()
        entry = Mock()
        entry.runtime_data.coordinator = coord
        entry.runtime_data.properties = {}
        added: list = []
        with patch(
            "custom_components.eufy_sdk.select.solix_devices_with", return_value=[]
        ):
            await async_setup_entry(MagicMock(), entry, added.extend)
        rotations = [e for e in added if isinstance(e, EufySdkMapRotationSelect)]
        self.assertEqual([e._sn for e in rotations], [ROBOT])
        self.assertEqual(rotations[0].unique_id, f"{ROBOT}_map_rotation")

    async def test_select_stores_and_signals(self):
        coord = _coordinator()
        select = _select(coord)
        with (
            patch("custom_components.eufy_sdk.select.async_dispatcher_send") as send,
            patch.object(select, "async_write_ha_state"),
        ):
            await select.async_select_option("90")
        self.assertEqual(coord.config_entry.runtime_data.map_rotation[ROBOT], 90)
        send.assert_called_once_with(select.hass, SIGNAL_MAP_ROTATION.format(ROBOT))
        self.assertEqual(select.current_option, "90")

    async def test_rejects_unknown_rotation(self):
        select = _select(_coordinator())
        with self.assertRaises(HomeAssistantError):
            await select.async_select_option("45")

    async def test_restores_saved_choice(self):
        coord = _coordinator()
        select = _select(coord)
        with (
            patch.object(
                select,
                "async_get_last_state",
                AsyncMock(return_value=Mock(state="270")),
            ),
            patch("custom_components.eufy_sdk.select.async_dispatcher_send"),
            patch(
                "homeassistant.helpers.update_coordinator.CoordinatorEntity.async_added_to_hass",
                AsyncMock(),
            ),
            patch(
                "homeassistant.helpers.restore_state.RestoreEntity.async_added_to_hass",
                AsyncMock(),
            ),
        ):
            await select.async_added_to_hass()
        self.assertEqual(select.current_option, "270")
        self.assertEqual(coord.config_entry.runtime_data.map_rotation[ROBOT], 270)

    def test_options(self):
        self.assertEqual(MAP_ROTATION_OPTIONS, ("0", "90", "180", "270"))


if __name__ == "__main__":
    unittest.main()
