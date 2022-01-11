"""Monitors presence in the house.

Provides utility functions for other apps to check presence,
as well as the ability to register callbacks when presence changes.

User defined variables are configued in presence.yaml
"""
import uuid
from datetime import timedelta

import app


class Presence(app.App):
    """Monitor presence in the house."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.rooms = {}
        self.__last_device_date = None

    def initialize(self):
        """Create rooms with sensors and listen for new devices and people.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for room in ["entryway", "kitchen", "bedroom", "office"]:
            self.rooms[room] = Room(room, f"sensor.{room}_multisensor_motion", self)
        self.rooms["kitchen"].add_sensor("binary_sensor.kitchen_door_sensor")
        self.__last_device_date = self.date()
        self.listen_event(self.__handle_new_device, "device_tracker_new_device")
        self.listen_state(self.__handle_presence_change, "person")

    def anyone_home(self, **kwargs) -> bool:
        """Check if anyone is home."""
        return any(
            person["state"] == "home" for person in self.entities.person.values()
        )

    def __handle_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Change scene if everyone has left home or if someone has come back."""
        del attribute, kwargs
        self.log(f"{entity} is {new}")
        if new == "home":
            self.call_service("lock/unlock", entity_id="lock.door_lock")
            if "Away" in self.scene:
                self.control.reset_scene()
        else:
            if old == "home":
                self.call_service("lock/lock", entity_id="lock.door_lock")
            if (
                "Away" not in self.scene
                and self.get_state(
                    f"person.{'rachel' if entity.endswith('dan') else 'dan'}"
                )
                != "home"
            ):
                away_scene = (
                    "Day"
                    if self.control.apps["lights"].is_lighting_sufficient()
                    else "Night"
                )
                self.scene = f"Away ({away_scene})"

    def __handle_new_device(self, event_name: str, data: dict, kwargs: dict):
        """If not home and someone adds a device, notify."""
        del event_name
        self.log(f"New device added: {data}, {kwargs}")
        if "Away" in self.scene:
            if self.__last_device_date < self.date() - timedelta(hours=3):
                self.notify(
                    f'A guest has added a device: "{data["host_name"]}"',
                    title="Guest Device",
                )
                self.__last_device_date = self.date()


class Room:
    """Report on presence for an individual room."""

    def __init__(self, room_id: str, sensor_id: str, controller: Presence):
        """Initialise room presence and start listening for presence change."""
        self.room_id = room_id
        self.controller = controller
        try:
            vacant = float(self.controller.get_state(sensor_id)) == 0
            last_changed = self.controller.convert_utc(
                self.controller.get_state(sensor_id, attribute="last_changed")
            ).replace(tzinfo=None) + timedelta(minutes=self.controller.get_tz_offset())
        except ValueError:
            self.controller.notify(
                f"Sensor in {room_id} is {self.controller.get_state(sensor_id)}",
                title="Sensor Error",
                targets="dan",
            )
            self.controller.log(
                f"Initialising room {room_id} with default state", level="WARNING"
            )
            vacant = True
            last_changed = self.controller.datetime()
        self.last_vacated = last_changed - timedelta(hours=0 if vacant else 2)
        self.last_entered = last_changed - timedelta(hours=2 if vacant else 0)
        self.callbacks = {}
        self.controller.listen_state(self.__handle_presence_change, sensor_id, old="0")
        self.controller.listen_state(self.__handle_presence_change, sensor_id, new="0")
        self.controller.log(
            f"Room '{room_id}' initialised as last {'vacat' if vacant else 'enter'}ed at "
            f"{self.last_vacated if vacant else self.last_entered}",
            level="DEBUG",
        )

    def is_vacant(self, vacating_delay: int = 0) -> bool:
        """Check if vacant based on last time vacated and entered, with optional delay."""
        return (
            self.last_entered
            < self.last_vacated
            < self.controller.datetime() - timedelta(seconds=vacating_delay)
        )

    def seconds_in_room(self, vacating_delay: int = 0) -> int:
        """Return number of seconds room has been occupied (or vacant if negative)."""
        return (
            self.last_vacated
            - self.controller.datetime()
            + timedelta(seconds=vacating_delay)
            if self.is_vacant(vacating_delay)
            else self.controller.datetime() - self.last_entered
        ).total_seconds()

    def __handle_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """If room presence changes, trigger all registered callbacks."""
        del entity, attribute, old, kwargs
        is_vacant = float(new) == 0
        self.controller.log(
            f"The {self.room_id} is now {'vacant' if is_vacant else 'occupied'}",
            level="DEBUG",
        )
        if is_vacant:
            self.last_vacated = self.controller.datetime()
        else:
            self.last_entered = self.controller.datetime()
        for handle, callback in list(self.callbacks.items()):
            self.controller.cancel_timer(callback["timer_handle"])
            if not is_vacant or callback["vacating_delay"] == 0:
                callback["callback"](is_vacant=is_vacant)
                self.controller.log(f"Called callback: {callback}", level="DEBUG")
            else:
                self.callbacks[handle]["timer_handle"] = self.controller.run_in(
                    callback["callback"], callback["vacating_delay"], is_vacant=True,
                )
                self.controller.log(
                    f"Set vacation timer for callback: {callback}", level="DEBUG"
                )

    def __handle_binary_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Wrapper of __handle_presence_change for binary sensors."""
        old = 1 if old == "on" else 0
        new = 1 if new == "on" else 0
        self.__handle_presence_change(entity, attribute, old, new, kwargs)

    def add_sensor(self, sensor_id: str):
        """Add binary sensor where any change represents presence in the room."""
        if sensor_id.startswith("binary_sensor"):
            self.controller.listen_state(
                self.__handle_binary_presence_change, sensor_id
            )

    def register_callback(self, callback, vacating_delay: int = 0) -> uuid.UUID:
        """Register a callback for when presence changes, including an optional delay."""
        handle = uuid.uuid4().hex
        self.callbacks[handle] = {
            "callback": callback,
            "vacating_delay": vacating_delay,
            "timer_handle": None,
        }
        if 0 < -1 * self.seconds_in_room() < vacating_delay:
            self.callbacks[handle]["timer_handle"] = self.controller.run_in(
                callback, vacating_delay + self.seconds_in_room(), is_vacant=True,
            )

        self.controller.log(f"Registered callback: {callback}", level="DEBUG")
        return handle

    def cancel_callback(self, handle):
        """Cancel a callback (and its timer if it has one) by passing its handle."""
        if handle in self.callbacks:
            self.controller.cancel_timer(self.callbacks[handle]["timer_handle"])
            del self.callbacks[handle]
