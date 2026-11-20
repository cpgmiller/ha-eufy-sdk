"""
Vacuum platform — one StateVacuumEntity per robot that declares `vacuum_clean`.

The robot's mode-control verbs (`startCleaning`, `pauseCleaning`, `resumeCleaning`,
`returnToDock`) and the find-robot beep (`locate`) are momentary SDK methods, not
writable properties, so — like `lock` and `siren` — this calls `client.action()`
rather than routing through `switch.py`. STOP is not offered: the SDK marks
`stopCleaning` unverified and leaves it off the device, so the call would always fail.

State is the SDK's decoded `activity` read, which the bridge keeps current from the
robot's WorkStatus pushes; the entity writes nothing optimistic, because every verb is
answered by a WorkStatus push within seconds.

Rooms are Home Assistant segments, read from the bridge's decoded map (`device.map`).
A segment id is `<map id>_<room id>`, because a room id only means something on its
own map; `clean_area` sends the rooms of the robot's current map to `cleanRooms`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.vacuum import (
    Segment,
    StateVacuumEntity,
    VacuumActivity,
    VacuumEntityFeature,
)
from homeassistant.exceptions import HomeAssistantError

from .api import EufySdkApiClientError
from .entity import EufySdkDeviceEntity, has_capability

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

    from .coordinator import EufySdkDataUpdateCoordinator
    from .data import EufySdkConfigEntry

# The SDK's `VacuumActivity` union → Home Assistant's. The SDK's own "unknown" (a
# WorkStatus it cannot classify) is deliberately absent: it maps to None, HA's unknown.
_ACTIVITY: dict[str, VacuumActivity] = {
    "idle": VacuumActivity.IDLE,
    "error": VacuumActivity.ERROR,
    "docked": VacuumActivity.DOCKED,
    "cleaning": VacuumActivity.CLEANING,
    "returning": VacuumActivity.RETURNING,
    "paused": VacuumActivity.PAUSED,
}

_BASE_FEATURES = (
    VacuumEntityFeature.STATE
    | VacuumEntityFeature.START
    | VacuumEntityFeature.PAUSE
    | VacuumEntityFeature.RETURN_HOME
    | VacuumEntityFeature.CLEAN_AREA
)


def rooms_of(vacuum_map: dict[str, Any] | None) -> tuple[int | None, dict[int, str]]:
    """
    Return the map id and `{room id: name}` from a `device.map` summary.

    A robot may list one room several times, named on some entries and not others, so
    entries are merged by id, keeping the first name seen. A room the user never named
    is labelled by its id.
    """
    if not vacuum_map:
        return None, {}
    map_id = vacuum_map.get("mapId")
    rooms: dict[int, str | None] = {}
    for room in vacuum_map.get("rooms") or []:
        room_id = room.get("id")
        if not isinstance(room_id, int):
            continue
        if rooms.get(room_id) is None:
            rooms[room_id] = room.get("name") or None
    named = {rid: name or f"Room {rid}" for rid, name in rooms.items()}
    return (map_id if isinstance(map_id, int) else None), named


async def async_setup_entry(
    hass: HomeAssistant,  # noqa: ARG001
    entry: EufySdkConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Create a vacuum entity per device that declares the `vacuum_clean` capability."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        EufySdkVacuum(coordinator, sn)
        for sn, dev in coordinator.data.items()
        if has_capability(dev, "vacuum_clean")
    )


class EufySdkVacuum(EufySdkDeviceEntity, StateVacuumEntity):
    """A eufy robot vacuum, driven by the SDK's mode-control verbs."""

    # The robot itself: the entity takes the device's name.
    _attr_name = None

    def __init__(self, coordinator: EufySdkDataUpdateCoordinator, sn: str) -> None:
        """Bind to a robot serial; offer `locate` only where the device declares it."""
        super().__init__(coordinator, sn)
        self._attr_unique_id = f"{sn}_vacuum"
        features = _BASE_FEATURES
        if has_capability(coordinator.data.get(sn), "locate"):
            features |= VacuumEntityFeature.LOCATE
        self._attr_supported_features = features

    @property
    def activity(self) -> VacuumActivity | None:
        """The decoded `activity` read; None while unreported or unclassified."""
        value = self.device.get("state", {}).get("activity")
        return _ACTIVITY.get(value) if isinstance(value, str) else None

    async def async_start(self) -> None:
        """Resume a paused clean, otherwise start a full clean."""
        verb = (
            "resumeCleaning"
            if self.activity == VacuumActivity.PAUSED
            else "startCleaning"
        )
        await self.client.action(self._sn, verb)

    async def async_pause(self) -> None:
        """Pause the current clean."""
        await self.client.action(self._sn, "pauseCleaning")

    async def async_return_to_base(self, **_: Any) -> None:
        """Send the robot back to its dock."""
        await self.client.action(self._sn, "returnToDock")

    async def async_locate(self, **_: Any) -> None:
        """Sound the find-robot beep."""
        await self.client.action(self._sn, "locate")

    async def _async_rooms(self) -> tuple[int | None, dict[int, str]]:
        """Return the current map id and rooms; none when the bridge has no map."""
        try:
            return rooms_of(await self.client.vacuum_map(self._sn))
        except EufySdkApiClientError:
            return None, {}

    async def async_get_segments(self) -> list[Segment]:
        """Each room of the robot's current map, as a cleanable segment."""
        map_id, rooms = await self._async_rooms()
        if map_id is None:
            return []
        return [
            Segment(id=f"{map_id}_{room_id}", name=name)
            for room_id, name in rooms.items()
        ]

    async def async_clean_segments(self, segment_ids: list[str], **_: Any) -> None:
        """Clean the requested rooms of the current map, in the order given."""
        map_id, rooms = await self._async_rooms()
        if map_id is None:
            msg = "The robot's map is not known yet: open it once in the eufy app"
            raise HomeAssistantError(msg)
        targets: list[dict[str, int]] = []
        for segment_id in segment_ids:
            seg_map, _, seg_room = segment_id.partition("_")
            if seg_map != str(map_id) or not seg_room.isdigit():
                continue
            room_id = int(seg_room)
            if room_id in rooms and all(t["id"] != room_id for t in targets):
                targets.append({"id": room_id, "order": len(targets) + 1})
        if not targets:
            msg = "None of the requested rooms are on the robot's current map"
            raise HomeAssistantError(msg)
        await self.client.action(self._sn, "cleanRooms", map_id, targets, 1)

    async def async_added_to_hass(self) -> None:
        """Flag a repair when the robot's rooms no longer match the area mapping."""
        await super().async_added_to_hass()
        self.hass.async_create_task(self._async_check_segments())

    async def _async_check_segments(self) -> None:
        """Raise the segments-changed issue if rooms differ from the last mapping."""
        last_seen = self.last_seen_segments
        if last_seen is None:
            return
        current = await self.async_get_segments()
        if not current:
            return
        if {(s.id, s.name) for s in current} != {(s.id, s.name) for s in last_seen}:
            self.async_create_segments_issue()
