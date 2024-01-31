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
        self.__pets_home_alone = None
        self.__last_device_date = None

    def initialize(self):
        """Create rooms with sensors and listen for new devices and people.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.__pets_home_alone = (
            self.entities.input_boolean.pets_home_alone.state == "on"
        )
        for multisensor_room in ["entryway", "kitchen", "bedroom", "office"]:
            self.rooms[multisensor_room] = Room(
                multisensor_room,
                f"binary_sensor.{multisensor_room}_multisensor_motion",
                self,
            )
        for camera_room in [
            "front_door",
            "living_room",
            "back_deck",
            "back_door",
            "garage",
        ]:
            self.rooms[camera_room] = Room(
                camera_room,
                f"binary_sensor.{camera_room}_person_detected",
                self,
            )
            for motion_type in [
                "motion_detected",
                "pet_detected",
            ]:
                if self.entity_exists(f"binary_sensor.{camera_room}_{motion_type}"):
                    self.rooms[camera_room].add_sensor(
                        f"binary_sensor.{camera_room}_{motion_type}",
                    )
        self.rooms["front_door"].add_sensor(
            "binary_sensor.doorbell_ringing",
        )
        self.rooms["entryway"].add_sensor("binary_sensor.entryway_person_detected")
        self.rooms["entryway"].add_sensor("binary_sensor.entryway_motion_detected")
        self.rooms["kitchen"].add_sensor("binary_sensor.kitchen_door_motion")
        self.rooms["office"].add_sensor("binary_sensor.daniels_macbook_active_at_home")
        self.listen_state(
            self.__handle_doorbell,
            "binary_sensor.doorbell_ringing",
            new="on",
        )
        self.listen_state(self.__handle_presence_change, "person")
        self.__last_device_date = self.date()
        self.listen_event(self.__handle_new_device, "device_tracker_new_device")

    def anyone_home(self, **kwargs) -> bool:
        """Check if anyone is home."""
        del kwargs  # required to overwrite inbuilt function
        return any(
            person["state"] == "home" for person in self.entities.person.values()
        )

    @property
    def pets_home_alone(self) -> bool:
        """Get pets home alone setting that has been synced to Home Assistant."""
        return self.__pets_home_alone

    @pets_home_alone.setter
    def pets_home_alone(self, state: bool):
        """Enable/disable pets home alone mode and reflect state in UI."""
        self.log(f"'{'En' if state else 'Dis'}abling' pets home alone mode")
        self.__pets_home_alone = state
        self.call_service(
            f"input_boolean/turn_{'on' if state else 'off'}",
            entity_id="input_boolean.pets_home_alone",
        )
        if state:
            self.control.apps["climate"].climate_control = True

    def is_kitchen_door_open(self) -> bool:
        """Check if the kitchen door is open."""
        return self.get_state("binary_sensor.kitchen_door") == "on"

    def __handle_presence_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Change scene if everyone has left home or if someone has come back."""
        del attribute, kwargs
        self.log(f"'{entity}' is '{new}'")
        if new == "home":
            self.call_service("lock/unlock", entity_id="lock.door_lock")
            if "Away" in self.control.scene:
                self.pets_home_alone = False
                self.control.reset_scene()
        else:
            if old == "home":
                self.call_service("lock/lock", entity_id="lock.door_lock")
            if (
                "Away" not in self.control.scene
                and self.get_state(
                    f"person.{'rachel' if entity.endswith('dan') else 'dan'}",
                )
                != "home"
            ):
                away_scene = (
                    "Day"
                    if self.control.apps["lights"].is_lighting_sufficient()
                    else "Night"
                )
                self.control.scene = f"Away ({away_scene})"

    def __handle_new_device(self, event_name: str, data: dict, kwargs: dict):
        """If not home and someone adds a device, notify."""
        del event_name
        self.log(f"New device added: '{data}', '{kwargs}'")
        if (
            "Away" in self.control.scene
            and self.date() - self.__last_device_date
            > timedelta(
                hours=self.args["new_device_notification_delay"],
            )
        ):
            self.notify(
                f'A guest has added a device: "{data["host_name"]}"',
                title="Guest Device",
            )
            self.__last_device_date = self.date()

    def __handle_doorbell(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Handle doorbell when it rings."""
        del entity, attribute, old, new, kwargs
        self.notify(
            f"Someone rung the doorbell "
            f"(door is {self.entities.lock.door_lock.state})",
            title="Doorbell",
        )
        if self.control.apps["media"].is_playing:
            self.control.apps["media"].pause()
        if self.control.scene in ("TV", "Sleep"):
            self.control.scene = "Night"


class Room:
    """Report on presence for an individual room."""

    def __init__(self, room_id: str, sensor_id: str, controller: Presence):
        """Initialise room presence and start listening for presence change."""
        self.__room_id = room_id
        self.__sensors = [sensor_id]
        self.__controller = controller
        try:
            vacant = self.__controller.get_state(sensor_id) == "off"
            last_changed = self.__controller.convert_utc(
                self.__controller.get_state(sensor_id, attribute="last_changed"),
            ).replace(tzinfo=None) + timedelta(
                minutes=self.__controller.get_tz_offset(),
            )
        except ValueError:
            self.__controller.notify(
                f"Sensor in {room_id} is {self.__controller.get_state(sensor_id)}",
                title="Sensor Error",
                targets="dan",
            )
            self.__controller.log(
                f"Initialising room '{room_id}' with default state",
                level="WARNING",
            )
            vacant = True
            last_changed = self.__controller.datetime()
        self.__last_vacated = last_changed - timedelta(hours=0 if vacant else 2)
        self.__last_entered = last_changed - timedelta(hours=2 if vacant else 0)
        self.__callbacks = {}
        self.__controller.listen_state(self.__handle_presence_change, sensor_id)
        presence_message = "vacated" if vacant else "entered"
        self.__controller.log(
            f"Room '{room_id}' initialised as last '{presence_message}' at "
            f"{self.__last_vacated if vacant else self.__last_entered}",
            level="DEBUG",
        )

    def is_vacant(self, vacating_delay: int = 0) -> bool:
        """Check if vacant based on last time vacated/entered, with optional delay."""
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
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """If room presence changes, trigger all registered callbacks."""
        del attribute, kwargs
        if "unavailable" in (new, old):
            self.__controller.log(
                f"Ignoring '{'current' if new == 'unavailable' else 'previous'}' "
                f"unavailable '{self.__room_id}' sensor state",
                level="WARNING",
            )
            return
        is_vacant = new == "off"
        if is_vacant and any(
            self.__controller.get_state(sensor) == "on"
            for sensor in self.__sensors
            if sensor != entity
        ):
            self.__controller.log(
                f"Sensor '{entity}' reports no presence "
                "but at least one other sensor in the room indicates presence",
                level="DEBUG",
            )
            return
        self.__controller.log(
            f"The '{self.__room_id}' is now '{'vacant' if is_vacant else 'occupied'}'",
            level="DEBUG",
        )
        if is_vacant:
            self.__last_vacated = self.__controller.datetime()
        else:
            self.__last_entered = self.__controller.datetime()
            if "Away" in self.__controller.control.scene:
                if "_person_detected" in entity:
                    self.__controller.notify(
                        f"Person detected at the {self.__room_id} (front door is "
                        f"{self.__controller.entities.lock.door_lock.state})",
                        title="Person Detected",
                    )
                elif (
                    not self.__controller.pets_home_alone and "doorbell" not in entity
                ):
                    self.__controller.notify(
                        "Pets detected as home alone, enabling climate control",
            if (
                not self.__controller.pets_home_alone
                and "Away" in self.__controller.control.scene
                and "doorbell" not in entity
            ):
                self.__controller.notify(
                    "Pets detected as home alone, enabling climate control",
                    title="Climate Control",
                )
                self.__controller.pets_home_alone = True
                        title="Climate Control",
                    )
                    self.__controller.pets_home_alone = True
        for handle, callback in list(self.__callbacks.items()):
            self.__controller.cancel_timer(callback["timer_handle"])
            if not is_vacant or callback["vacating_delay"] == 0:
                callback["callback"](is_vacant=is_vacant)
                self.__controller.log(
                    f"Callback {handle} triggered by '{entity}'",
                    level="DEBUG",
                )
            else:
                self.__callbacks[handle]["timer_handle"] = self.__controller.run_in(
                    callback["callback"],
                    callback["vacating_delay"],
                    is_vacant=True,
                )
                self.__controller.log(
                    f"Set vacation timer for callback: {handle}",
                    level="DEBUG",
                )

    def add_sensor(self, sensor_id: str):
        """Add additional sensor to room."""
        self.__sensors.append(sensor_id)
        self.__controller.listen_state(self.__handle_presence_change, sensor_id)

    def register_callback(self, callback, vacating_delay: int = 0) -> uuid.UUID:
        """Register a callback for when presence changes, with an optional delay."""
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

        self.__controller.log(
            f"Registered callback for '{self.__room_id}' with handle: {handle}",
            level="DEBUG",
        )
        return handle

    def cancel_callback(self, handle):
        """Cancel a callback (and its timer if it has one) by passing its handle."""
        if handle in self.__callbacks:
            self.__controller.cancel_timer(self.__callbacks[handle]["timer_handle"])
            del self.__callbacks[handle]
