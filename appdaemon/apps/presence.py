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
        self.pets_home_alone = self.entities.input_boolean.pets_home_alone.state == "on"
        for room in ["entryway", "kitchen", "bedroom", "office"]:
            self.rooms[room] = Room(
                room, f"binary_sensor.{room}_multisensor_motion", self
            )
        self.rooms["entryway"].add_sensor("binary_sensor.doorbell_ringing")
        self.rooms["entryway"].add_sensor("binary_sensor.doorbell_motion")
        self.rooms["kitchen"].add_sensor("binary_sensor.kitchen_door")
        self.__last_device_date = self.date()
        self.listen_event(self.__handle_new_device, "device_tracker_new_device")
        self.listen_state(self.__handle_presence_change, "person")
        self.listen_state(
            self.__handle_doorbell, "binary_sensor.doorbell_ringing", new="True"
        )

    def anyone_home(self, **kwargs) -> bool:
        """Check if anyone is home."""
        del kwargs  # required to overwrite inbuilt function
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
                self.pets_home_alone = False
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

    def __handle_doorbell(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Handle doorbell when it rings."""
        del entity, attribute, old, new, kwargs
        self.log("Doorbell rung")
        if self.control.apps["media"].is_playing:
            self.control.apps["media"].pause()
        if self.scene in ("TV", "Sleep"):
            self.scene = "Night"


class Room:
    """Report on presence for an individual room."""

    def __init__(self, room_id: str, sensor_id: str, controller: Presence):
        """Initialise room presence and start listening for presence change."""
        self.__room_id = room_id
        self.__controller = controller
        try:
            vacant = self.__controller.get_state(sensor_id) == "off"
            last_changed = self.__controller.convert_utc(
                self.__controller.get_state(sensor_id, attribute="last_changed")
            ).replace(tzinfo=None) + timedelta(
                minutes=self.__controller.get_tz_offset()
            )
        except ValueError:
            self.__controller.notify(
                f"Sensor in {room_id} is {self.__controller.get_state(sensor_id)}",
                title="Sensor Error",
                targets="dan",
            )
            self.__controller.log(
                f"Initialising room {room_id} with default state", level="WARNING"
            )
            vacant = True
            last_changed = self.__controller.datetime()
        self.__last_vacated = last_changed - timedelta(hours=0 if vacant else 2)
        self.__last_entered = last_changed - timedelta(hours=2 if vacant else 0)
        self.__callbacks = {}
        self.__controller.listen_state(self.__handle_presence_change, sensor_id)
        self.__controller.log(
            f"Room '{room_id}' initialised as last {'vacat' if vacant else 'enter'}ed at "
            f"{self.__last_vacated if vacant else self.__last_entered}",
            level="DEBUG",
        )

    def is_vacant(self, vacating_delay: int = 0) -> bool:
        """Check if vacant based on last time vacated and entered, with optional delay."""
        return (
            self.__last_entered
            < self.__last_vacated
            < self.__controller.datetime() - timedelta(seconds=vacating_delay)
        )

    def seconds_in_room(self, vacating_delay: int = 0) -> int:
        """Return number of seconds room has been occupied (or vacant if negative)."""
        return (
            self.__last_vacated
            - self.__controller.datetime()
            + timedelta(seconds=vacating_delay)
            if self.is_vacant(vacating_delay)
            else self.__controller.datetime() - self.__last_entered
        ).total_seconds()

    def __handle_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """If room presence changes, trigger all registered callbacks."""
        del entity, attribute, old, kwargs
        is_vacant = new == "off"
        self.__controller.log(
            f"The {self.__room_id} is now {'vacant' if is_vacant else 'occupied'}",
            level="DEBUG",
        )
        if is_vacant:
            self.__last_vacated = self.__controller.datetime()
        else:
            self.__last_entered = self.__controller.datetime()
            if (
                not self.__controller.pets_home_alone
                and "Away" in self.__controller.scene
            ):
                self.__controller.pets_home_alone = True
                self.__controller.control.apps["climate"].handle_pets_home_alone()
        for handle, callback in list(self.__callbacks.items()):
            self.__controller.cancel_timer(callback["timer_handle"])
            if not is_vacant or callback["vacating_delay"] == 0:
                callback["callback"](is_vacant=is_vacant)
                self.__controller.log(f"Called callback: {callback}", level="DEBUG")
            else:
                self.__callbacks[handle]["timer_handle"] = self.__controller.run_in(
                    callback["callback"],
                    callback["vacating_delay"],
                    is_vacant=True,
                )
                self.__controller.log(
                    f"Set vacation timer for callback: {callback}", level="DEBUG"
                )

    def add_sensor(self, sensor_id: str):
        """Add additional sensor to room."""
        self.__controller.listen_state(self.__handle_presence_change, sensor_id)

    def register_callback(self, callback, vacating_delay: int = 0) -> uuid.UUID:
        """Register a callback for when presence changes, including an optional delay."""
        handle = uuid.uuid4().hex
        self.__callbacks[handle] = {
            "callback": callback,
            "vacating_delay": vacating_delay,
            "timer_handle": None,
        }
        if 0 < -1 * self.seconds_in_room() < vacating_delay:
            self.__callbacks[handle]["timer_handle"] = self.__controller.run_in(
                callback,
                vacating_delay + self.seconds_in_room(),
                is_vacant=True,
            )

        self.__controller.log(f"Registered callback: {callback}", level="DEBUG")
        return handle

    def cancel_callback(self, handle):
        """Cancel a callback (and its timer if it has one) by passing its handle."""
        if handle in self.__callbacks:
            self.__controller.cancel_timer(self.__callbacks[handle]["timer_handle"])
            del self.__callbacks[handle]
