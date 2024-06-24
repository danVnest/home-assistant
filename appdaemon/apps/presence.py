"""Monitors presence in the house.

Provides utility functions for other apps to check presence,
as well as the ability to register callbacks when presence changes.

User defined variables are configued in presence.yaml
"""

from __future__ import annotations

import uuid
from datetime import timedelta

from app import App, Device


class Presence(App):
    """Monitor presence in the house."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.rooms = {}
        self.__pets_home_alone = False
        self.last_device_date = None

    def initialize(self):
        """Create rooms with sensors and listen for new devices and people.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.pets_home_alone = self.entities.input_boolean.pets_home_alone.state == "on"
        # TODO: https://app.asana.com/0/1207020279479204/1207033183175547/f
        # add overide functionality so that if pets home alone mode is manually turned off it doesn't turn on again until after someone comes home and leaves again
        for multisensor_room in ["entryway", "dining_room", "bathroom"]:
            self.rooms[multisensor_room] = Room(
                multisensor_room,
                f"{multisensor_room}_multisensor_motion",
                self,
            )
        for presence_sensor_room in ["kitchen", "bedroom", "nursery", "office"]:
            self.rooms[presence_sensor_room] = Room(
                presence_sensor_room,
                f"{presence_sensor_room}_presence_sensor_occupancy",
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
                f"{camera_room}_person_detected",
                self,
            )
            for motion_type in [
                "motion_detected",
                "pet_detected",
            ]:
                if self.entity_exists(f"binary_sensor.{camera_room}_{motion_type}"):
                    self.rooms[camera_room].add_sensor(
                        f"{camera_room}_{motion_type}",
                    )
        self.rooms["front_door"].add_sensor(
            "doorbell_ringing",
        )
        self.rooms["entryway"].add_sensor("entryway_person_detected")
        self.rooms["entryway"].add_sensor("entryway_motion_detected")
        self.rooms["kitchen"].add_sensor("kitchen_door_motion")
        self.rooms["living_room"].add_sensor("tv_playing")
        self.rooms["living_room"].add_sensor("hall_multisensor_motion")
        self.rooms["office"].add_sensor("daniels_macbook_active_at_home")
        self.rooms["nursery"].add_sensor("owlet_attached")
        # TODO: https://app.asana.com/0/1207020279479204/1207033183175551/f
        # create floorplan for UI, show lighting and presence (combine person and pet?) - create template sensors for room presence or change UI from here
        # TODO: https://app.asana.com/0/1207020279479204/1165239627642113/f
        # potentially simplify this code by using the above template sensors that combine all motion detectors in a room
        self.listen_state(
            self.handle_doorbell,
            "binary_sensor.doorbell_ringing",
            new="on",
        )
        self.listen_state(self.handle_presence_change, "person")
        self.last_device_date = self.date()
        self.listen_event(self.handle_new_device, "device_tracker_new_device")

    @property
    def anyone_home(self) -> bool:
        """Check if anyone is home."""
        # del kwargs  # required to overwrite inbuilt function
        # TODO: if defining as property overrides kwargs version, try elsewhere?
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
        if self.__pets_home_alone != state:
            self.__pets_home_alone = state
            self.call_service(
                f"input_boolean/turn_{'on' if state else 'off'}",
                entity_id="input_boolean.pets_home_alone",
            )
            if state:
                self.climate.climate_control_enabled = True
                # TODO: don't do this, create specific self.climate.handle_pets_home_alone() method which uses individual climate control history and only enables necessary devices from that

    @property
    def door_locked(self) -> bool:
        """True if the front door is locked."""
        return self.entities.lock.door_lock.state == "locked"

    def lock_door(self):
        """Lock the front door if not already locked."""
        if not self.door_locked:
            self.call_service("lock/lock", entity_id="lock.door_lock")

    def unlock_door(self):
        """Unlock the front door if not already unlocked."""
        if self.door_locked:
            self.call_service("lock/unlock", entity_id="lock.door_lock")

    # TODO: probably move above to Climate

    def handle_presence_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Change scene if everyone has left home or if someone has come back."""
        del attribute, kwargs
        self.log(f"'{entity}' is '{new}'")
        if new == "home":
            self.unlock_door()
            if "Away" in self.control.scene:
                self.pets_home_alone = False
                self.control.reset_scene()
        else:
            if old == "home":
                self.lock_door()
            if (
                "Away" not in self.control.scene
                and self.get_state(
                    f"person.{'rachel' if entity.endswith('dan') else 'dan'}",
                )
                != "home"
            ):
                self.control.scene = (
                    "Away (Night)"
                    if self.entities.binary_sensor.dark_outside.state == "on"
                    else "Away (Day)"
                )

    def handle_new_device(self, event_name: str, data: dict, **kwargs: dict):
        """If not home and someone adds a device, notify."""
        del event_name
        self.log(f"New device added: '{data}', '{kwargs}'")
        if (
            "Away" in self.control.scene
            and self.date() - self.last_device_date
            > timedelta(
                hours=self.constants["new_device_notification_delay"],
            )
        ):
            self.notify(
                f'A guest has added a device: "{data["host_name"]}"',
                title="Guest Device",
            )
            self.last_device_date = self.date()

    def handle_doorbell(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Handle doorbell when it rings."""
        del entity, attribute, old, new, kwargs
        self.notify(
            f"Someone rung the doorbell "
            f"(door is {self.entities.lock.door_lock.state})",
            title="Doorbell",
        )
        if self.media.playing:
            self.media.pause()
        if self.control.scene in ("TV", "Sleep"):
            self.control.scene = "Night"


class Room:
    """Report on presence for an individual room."""

    def __init__(self, room_id: str, sensor_id: str, controller: Presence):
        """Initialise room presence and start listening for presence change."""
        self.room_id = room_id
        sensor_id = f"binary_sensor.{sensor_id}"
        self.sensors = [sensor_id]
        self.controller = controller
        try:
            vacant = self.controller.get_state(sensor_id) == "off"
            last_changed = self.controller.convert_utc(
                self.controller.get_state(sensor_id, attribute="last_changed"),
            ).replace(tzinfo=None) + timedelta(
                minutes=self.controller.get_tz_offset(),
            )
        except ValueError:
            self.controller.notify(
                f"Sensor in {room_id} is {self.controller.get_state(sensor_id)}",
                title="Sensor Error",
                targets="dan",
            )
            self.controller.log(
                f"Initialising room '{room_id}' with default state",
                level="WARNING",
            )
            vacant = True
            last_changed = self.controller.datetime()
        self.last_vacated = last_changed - timedelta(hours=0 if vacant else 2)
        self.last_entered = last_changed - timedelta(hours=2 if vacant else 0)
        self.callbacks = {}
        self.controller.listen_state(self.handle_presence_change, sensor_id)
        presence_message = "vacated" if vacant else "entered"
        self.controller.log(
            f"Room '{room_id}' initialised as last '{presence_message}' at "
            f"{self.last_vacated if vacant else self.last_entered}",
            level="DEBUG",
        )

    def is_vacant(self, vacating_delay: float = 0) -> bool:
        """Check if vacant based on last time vacated/entered, with optional delay."""
        return (
            self.last_entered
            < self.last_vacated
            < self.controller.datetime() - timedelta(seconds=vacating_delay)
        )

    def seconds_in_room(self, vacating_delay: float = 0) -> float:
        """Return number of seconds room has been occupied (or vacant if negative)."""
        return (
            self.last_vacated
            - self.controller.datetime()
            + timedelta(seconds=vacating_delay)
            if self.is_vacant(vacating_delay)
            else self.controller.datetime() - self.last_entered
        ).total_seconds()

    def handle_presence_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """If room presence changes, trigger all registered callbacks."""
        del attribute, kwargs
        if "unavailable" in (new, old):
            self.controller.log(
                f"Ignoring '{'current' if new == 'unavailable' else 'previous'}' "
                f"unavailable '{self.room_id}' sensor state",
                level="WARNING",
            )
            return
        vacant = new == "off"
        reentry = False
        if vacant:
            if any(
                self.controller.get_state(sensor) == "on"
                for sensor in self.sensors
                if sensor != entity
            ):
                self.controller.log(
                    f"Sensor '{entity}' reports no presence "
                    "but at least one other sensor in the room indicates presence",
                    level="DEBUG",
                )
                return
            self.last_vacated = self.controller.datetime()
        else:
            reentry = self.last_entered > self.last_vacated
            self.last_entered = self.controller.datetime()
            if "Away" in self.controller.control.scene:
                if "_person_detected" in entity:
                    self.controller.notify(
                        f"Person detected at the {self.room_id} (front door is "
                        f"{self.controller.entities.lock.door_lock.state})",
                        title="Person Detected",
                    )
                    # TODO: https://app.asana.com/0/1207020279479204/1203851145721574/f
                    # trigger alarm if not doorbell?
                    # should I change this message? why would the door not be locked!?
                # elif (
                # TODO: https://app.asana.com/0/1207020279479204/1207033183175569/f
                # when should pets home alone mode not be triggered? Only trigger for living_room, kitchen, entryway
                # )
                elif (
                    not self.controller.pets_home_alone
                    and "Away" in self.controller.control.scene
                    and "doorbell" not in entity
                    # TODO: https://app.asana.com/0/1207020279479204/1207033183175569/f
                    # this seems hacky, fix?
                ):
                    self.controller.notify(
                        "Pets detected as home alone, enabling climate control",
                        title="Climate Control",
                    )
                    self.controller.pets_home_alone = True
        if reentry:
            self.controller.log(
                f"The '{self.room_id}' was re-entered - no callbacks called",
                level="DEBUG",
            )
            return
        self.controller.log(
            f"The '{self.room_id}' is now '{'vacant' if vacant else 'occupied'}'",
            level="DEBUG",
        )
        for handle, callback in list(self.callbacks.items()):
            self.controller.cancel_timer(callback["timer_handle"])
            if not vacant or callback["vacating_delay"] == 0:
                callback["callback"]()
                self.controller.log(
                    f"Callback {handle} triggered by '{entity}'",
                    level="DEBUG",
                )
            else:
                self.callbacks[handle]["timer_handle"] = self.controller.run_in(
                    callback["callback"],
                    callback["vacating_delay"],
                    constrain_input_boolean=callback["control_input_boolean"],
                )
                self.controller.log(
                    f"Set vacation timer for callback: {handle}",
                    level="DEBUG",
                )

    def add_sensor(self, sensor_id: str):
        """Add additional binary presence sensor to room."""
        sensor_id = f"binary_sensor.{sensor_id}"
        self.sensors.append(sensor_id)
        self.controller.listen_state(self.handle_presence_change, sensor_id)

    def register_callback(
        self,
        callback,
        vacating_delay: float,
        control_input_boolean: str,
    ) -> uuid.UUID:
        """Register a callback for when presence changes, with an optional delay."""
        handle = uuid.uuid4().hex
        self.callbacks[handle] = {
            "callback": callback,
            "vacating_delay": vacating_delay,
            "timer_handle": None,
            "control_input_boolean": control_input_boolean,
        }
        if 0 < -1 * self.seconds_in_room() < vacating_delay:
            self.callbacks[handle]["timer_handle"] = self.controller.run_in(
                callback,
                vacating_delay + self.seconds_in_room(),
                constrain_input_boolean=control_input_boolean,
            )

        self.controller.log(
            f"Registered callback for '{self.room_id}' with handle: {handle}",
            level="DEBUG",
        )
        return handle

    def cancel_callback(self, handle):
        """Cancel a callback (and its timer if it has one) by passing its handle."""
        if handle in self.callbacks:
            self.controller.cancel_timer(self.callbacks[handle]["timer_handle"])
            del self.callbacks[handle]


class PresenceDevice(Device):
    """Basic device that can be configured to respond to environmental changes."""

    def __init__(
        self,
        **kwargs: dict,
    ):
        """Initialise with device parameters and prepare for presence adjustments."""
        super().__init__(**kwargs)
        self.rooms: list[Room] = [
            self.controller.presence.rooms[room]
            for room in (self.room, *self.linked_rooms)
        ]
        self.__vacating_delay = 0
        self.was_vacant_at_last_check = self.vacant
        self.presence_callbacks = None
        self.transition_period = 0
        self.transition_timer = None

    @property
    def vacant(self) -> bool:
        """If the room (and any linked rooms) are vacant."""
        return all(room.is_vacant(self.vacating_delay) for room in self.rooms)

    @property
    def ignoring_vacancy(self):
        """Check if the device is ignoring presence changes or not."""
        return not bool(self.presence_callbacks)

    @property
    def vacating_delay(self) -> float:
        """The number of seconds between room vacancy triggering device adjustment."""
        return self.__vacating_delay

    @vacating_delay.setter
    def vacating_delay(self, seconds: float):
        """If monitoring presence then update with new vacating delay."""
        if self.vacating_delay != seconds:
            self.__vacating_delay = seconds
            if not self.ignoring_vacancy:
                self.ignore_vacancy()
                self.monitor_presence()

    def ignore_vacancy(self):
        """Ignore presence changes by cancelling any presence callbacks."""
        if not self.ignoring_vacancy:
            for room in self.rooms:
                for callback in self.presence_callbacks:
                    room.cancel_callback(callback)
            self.presence_callbacks = []
        # TODO: just set self.ignore_vacancy to True and check with control_input_boolean

    def monitor_presence(self):
        """Set callbacks for when presence changes."""
        if self.ignoring_vacancy:
            self.presence_callbacks = [
                room.register_callback(
                    self.handle_presence_change,
                    self.vacating_delay,
                    self.control_input_boolean,
                )
                for room in self.rooms
            ]
            self.handle_presence_change()
        # TODO: just set self.ignore_vacancy to False and check with control_input_boolean

    def handle_presence_change(self, **kwargs):
        """Set device to adjust (with delay if required) when presence changes."""
        del kwargs
        if self.vacant != self.was_vacant_at_last_check:
            self.was_vacant_at_last_check = self.vacant
            self.transition_timer = None
            if self.should_transition_towards_occupied:
                self.start_transition_towards_occupied()
            self.adjust_for_conditions()

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Override this in child class to adjust device settings appropriately."""
        del check_if_would_adjust_only
        return False

    @property
    def transition_progress(self) -> float:
        """Progress of transition between presence configurations (from 0 to 1)."""
        if self.transition_period == 0:
            return 1
        seconds_in_room = max(
            room.seconds_in_room(self.vacating_delay) for room in self.rooms
        )
        if not (0 < seconds_in_room < self.transition_period):
            return 1
        return seconds_in_room / self.transition_period

    @property
    def should_transition_towards_occupied(self) -> bool:
        """Check if the current settings should trigger transition to occupied."""
        return self.transition_progress < 1

    def start_transition_towards_occupied(
        self,
        step_time: float = 0,
        steps_remaining: int = 0,
        **kwargs: dict,
    ):
        """Help child class to transition deivce slowly from vacant to occupied."""
        if step_time == 0 or steps_remaining == 0 or kwargs is None:
            return
        self.transition_timer = uuid.uuid4().hex
        self.controller.run_in(
            self.transition_towards_occupied,
            step_time,
            step_time=step_time,
            steps_remaining=steps_remaining,
            timer_id=self.transition_timer,
            constrain_input_boolean=self.control_input_boolean,
            **kwargs,
        )
        self.controller.log(
            "Starting transition from entered state to occupied state with: "
            f"step_time = {step_time}, steps_remaining = {steps_remaining}, "
            f"settings = {kwargs}",
            level="DEBUG",
        )

    def transition_towards_occupied(self, **kwargs: dict):
        """Scheduling for child to step towards occupied device settings."""
        if kwargs["timer_id"] != self.transition_timer:
            return
        kwargs["steps_remaining"] = kwargs["steps_remaining"] - 1
        if kwargs["steps_remaining"] <= 0:
            self.controller.log(
                f"Transition to occupied complete for '{self.device_id}'",
                level="DEBUG",
            )
            self.transition_timer = None
            self.adjust_for_conditions()
        else:
            self.controller.log(
                f"{kwargs['steps_remaining']} steps until '{self.device_id}' "
                "transition to occupied state is complete",
                level="DEBUG",
            )
            self.controller.run_in(
                self.transition_towards_occupied,
                kwargs["step_time"],
                **kwargs,
            )
