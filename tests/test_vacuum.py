# ruff: noqa: ANN201, D100, D102, INP001, PT009, PT027, SLF001

import unittest
from unittest.mock import AsyncMock, Mock

from homeassistant.components.vacuum import VacuumActivity, VacuumEntityFeature
from homeassistant.exceptions import HomeAssistantError

from custom_components.eufy_sdk.api import EufySdkApiClientError
from custom_components.eufy_sdk.vacuum import (
    EufySdkVacuum,
    async_setup_entry,
    rooms_of,
)

ROBOT = "T2292B0000000001"
ROBOT_NO_LOCATE = "T2351P0000000001"
CAMERA = "T8161P0000000001"


def _coordinator(activity: str | None = None) -> Mock:
    coord = Mock()
    coord.data = {
        ROBOT: {
            "name": "Robovac",
            "capabilities": ["vacuum_clean", "suction", "locate", "info"],
            "state": {} if activity is None else {"activity": activity},
        },
        ROBOT_NO_LOCATE: {"name": "Robovac 2", "capabilities": ["vacuum_clean"]},
        CAMERA: {"name": "Garden", "capabilities": ["camera", "battery"]},
    }
    coord.config_entry.runtime_data.client.action = AsyncMock(return_value=None)
    return coord


class VacuumSetupTest(unittest.IsolatedAsyncioTestCase):
    """Only vacuum_clean devices get an entity; locate only where declared."""

    async def test_only_vacuum_clean_devices_get_an_entity(self):
        coord = _coordinator()
        entry = Mock()
        entry.runtime_data.coordinator = coord
        added: list = []
        await async_setup_entry(Mock(), entry, added.extend)

        self.assertEqual(sorted(e._sn for e in added), [ROBOT, ROBOT_NO_LOCATE])
        robot = next(e for e in added if e._sn == ROBOT)
        self.assertEqual(robot.unique_id, f"{ROBOT}_vacuum")

    def test_locate_follows_the_locate_capability(self):
        coord = _coordinator()
        with_locate = EufySdkVacuum(coord, ROBOT)
        without = EufySdkVacuum(coord, ROBOT_NO_LOCATE)

        self.assertTrue(with_locate.supported_features & VacuumEntityFeature.LOCATE)
        self.assertFalse(without.supported_features & VacuumEntityFeature.LOCATE)
        self.assertFalse(with_locate.supported_features & VacuumEntityFeature.STOP)


class VacuumActivityTest(unittest.TestCase):
    """The decoded activity read maps onto HA's VacuumActivity, else None."""

    def test_maps_each_sdk_activity(self):
        cases = {
            "idle": VacuumActivity.IDLE,
            "error": VacuumActivity.ERROR,
            "docked": VacuumActivity.DOCKED,
            "cleaning": VacuumActivity.CLEANING,
            "returning": VacuumActivity.RETURNING,
            "paused": VacuumActivity.PAUSED,
        }
        for sdk, ha in cases.items():
            with self.subTest(sdk=sdk):
                self.assertEqual(EufySdkVacuum(_coordinator(sdk), ROBOT).activity, ha)

    def test_unknown_or_unreported_is_none(self):
        self.assertIsNone(EufySdkVacuum(_coordinator("unknown"), ROBOT).activity)
        self.assertIsNone(EufySdkVacuum(_coordinator(), ROBOT).activity)


class VacuumCommandTest(unittest.IsolatedAsyncioTestCase):
    """Each HA command sends its SDK verb through the bridge's device.action."""

    async def _sent(self, activity: str | None, call: str) -> str:
        coord = _coordinator(activity)
        await getattr(EufySdkVacuum(coord, ROBOT), call)()
        client = coord.config_entry.runtime_data.client
        client.action.assert_awaited_once()
        sn, verb = client.action.await_args.args
        self.assertEqual(sn, ROBOT)
        return verb

    async def test_start_starts_a_clean(self):
        self.assertEqual(await self._sent("docked", "async_start"), "startCleaning")

    async def test_start_while_paused_resumes(self):
        self.assertEqual(await self._sent("paused", "async_start"), "resumeCleaning")

    async def test_pause(self):
        self.assertEqual(await self._sent("cleaning", "async_pause"), "pauseCleaning")

    async def test_return_to_base(self):
        verb = await self._sent("cleaning", "async_return_to_base")
        self.assertEqual(verb, "returnToDock")

    async def test_locate(self):
        self.assertEqual(await self._sent("docked", "async_locate"), "locate")


MAP = {
    "mapId": 51170,
    "rooms": [
        {"id": 2, "name": "Living Room"},
        {"id": 4, "name": None},
        {"id": 4, "name": "Hallway"},
        {"id": 4, "name": None},
        {"id": 5, "name": "Bedroom"},
        {"id": 9, "name": None},
    ],
}


class RoomsOfTest(unittest.TestCase):
    """The device.map room list is merged by id, keeping the first name seen."""

    def test_merges_repeated_rooms_and_labels_unnamed_ones(self):
        map_id, rooms = rooms_of(MAP)
        self.assertEqual(map_id, 51170)
        self.assertEqual(
            rooms, {2: "Living Room", 4: "Hallway", 5: "Bedroom", 9: "Room 9"}
        )

    def test_no_map(self):
        self.assertEqual(rooms_of(None), (None, {}))


class VacuumSegmentTest(unittest.IsolatedAsyncioTestCase):
    """Rooms become `<map>_<room>` segments; clean_segments sends cleanRooms."""

    def _vacuum(self, vacuum_map: dict | None = MAP) -> tuple[EufySdkVacuum, Mock]:
        coord = _coordinator("docked")
        client = coord.config_entry.runtime_data.client
        client.vacuum_map = AsyncMock(return_value=vacuum_map)
        return EufySdkVacuum(coord, ROBOT), client

    async def test_clean_area_is_offered(self):
        vacuum, _ = self._vacuum()
        self.assertTrue(vacuum.supported_features & VacuumEntityFeature.CLEAN_AREA)

    async def test_segments_are_the_current_maps_rooms(self):
        vacuum, _ = self._vacuum()
        segments = await vacuum.async_get_segments()
        self.assertEqual(
            [(s.id, s.name) for s in segments],
            [
                ("51170_2", "Living Room"),
                ("51170_4", "Hallway"),
                ("51170_5", "Bedroom"),
                ("51170_9", "Room 9"),
            ],
        )

    async def test_no_map_means_no_segments(self):
        vacuum, client = self._vacuum(None)
        self.assertEqual(await vacuum.async_get_segments(), [])
        client.vacuum_map.side_effect = EufySdkApiClientError("unknown cmd")
        self.assertEqual(await vacuum.async_get_segments(), [])

    async def test_clean_segments_sends_rooms_in_order(self):
        vacuum, client = self._vacuum()
        await vacuum.async_clean_segments(["51170_5", "51170_2", "51170_5"])
        client.action.assert_awaited_once_with(
            ROBOT,
            "cleanRooms",
            51170,
            [{"id": 5, "order": 1}, {"id": 2, "order": 2}],
            1,
        )

    async def test_other_maps_and_unknown_rooms_are_skipped(self):
        vacuum, client = self._vacuum()
        await vacuum.async_clean_segments(["999_2", "51170_77", "51170_4"])
        client.action.assert_awaited_once_with(
            ROBOT, "cleanRooms", 51170, [{"id": 4, "order": 1}], 1
        )

    async def test_nothing_cleanable_raises(self):
        vacuum, client = self._vacuum()
        with self.assertRaises(HomeAssistantError):
            await vacuum.async_clean_segments(["999_2"])
        vacuum_no_map, _ = self._vacuum(None)
        with self.assertRaises(HomeAssistantError):
            await vacuum_no_map.async_clean_segments(["51170_2"])
        client.action.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
