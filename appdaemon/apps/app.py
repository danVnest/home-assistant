"""AppDaemon subclass from which all apps inherit API and common functionality from.

This class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps. It also includes a basic device class
that can be inherited for specific device types and easily configured to respond to
environmental changes.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import appdaemon.plugins.hass.hassapi as hass

if TYPE_CHECKING:
    from control import Control
    from presence import Room


class App(hass.Hass):
    """Utility functions and methods for Home Assistant interaction."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.control: Control = None
        self.constants = {}

    def initialize(self):
        """Allow easy access to control app (which has access to all other apps)."""
        self.control = self.get_app("Control")

    def cancel_timer(self, handle):
        """Cancel timer after checking it is valid and running."""
        if self.timer_running(handle):
            super().cancel_timer(handle)

    def notify(self, message: str, **kwargs):
        """Send a notification (title required) to target users (anyone_home or all)."""
        targets = kwargs.get("targets", "all")
        if targets == "anyone_home_else_all":
            if self.control.apps["presence"].anyone_home:
                targets = "anyone_home"
            else:
                targets = "all"
        for person, info in self.entities.person.items():
            if any(
                [
                    targets == "all",
                    targets == "anyone_home" and info["state"] == "home",
                    targets == person,
                ],
            ):
                data = {"tag": kwargs["title"]}
                if "critical" in kwargs:
                    if self.control.args["mobiles"][person]["type"] == "iOS":
                        data.update(
                            {
                                "push": {
                                    "sound": {
                                        "name": "default",
                                        "critical": 1,
                                        "volume": 1.0,
                                    },
                                },
                            },
                        )
                    else:
                        data.update(
                            {
                                "ttl": 0,
                                "priority": "high",
                                "media_stream": "alarm_stream_max",
                                "tts_text": message,
                            },
                        )
                super().notify(
                    message,
                    title=kwargs["title"],
                    name=self.control.args["mobiles"][person]["name"],
                    data=data,
                )
        self.log(f"Notified '{targets}': \"{kwargs['title']}: {message}\"")


class Device:
    """Basic device that can be configured to respond to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: App,
        room: str,
        linked_rooms: list[str] = (),
    ):
        """Initialise with device parameters and prepare for presence adjustments."""
        self.device_id = device_id
        self.device_type = device_id.split(".")[0]
        self.device = controller.get_entity(device_id)
        self.controller = controller
        self.rooms: list[Room] = [
            self.controller.control.apps["presence"].rooms[room]
            for room in (room, *linked_rooms)
        ]
        self.vacating_delay = 0
        self.was_vacant_at_last_check = self.vacant
        self.presence_callbacks = None
        self.transition_period = 0
        self.transition_timer = None
        self.adjustment_delay = 0
        self.last_adjustment_time = self.controller.get_now_ts()
        self.adjustment_timer = None

    @property
    def on(self) -> bool:
        """Check if the device is currently on or not."""
        return self.device.state != "off"

    def turn_on(self, **kwargs: dict):
        """Turn the device on if it's off or adjust with provided parameters."""
        if not self.on or kwargs:
            if self.device_type != "group":
                self.device.turn_on(**kwargs)
            else:
                self.controller.call_service(
                    "homeassistant/turn_on",
                    entity_id=self.device_id,
                    **kwargs,
                )

    def turn_off(self):
        """Turn the device off if it's on."""
        if self.on:
            if self.device_type != "group":
                self.device.turn_off()
            else:
                self.controller.call_service(
                    "homeassistant/turn_off",
                    entity_id=self.device_id,
                )
        # TODO: https://app.asana.com/0/1207020279479204/1207387085648282/f
        # consider adding an optional argument to force off even if climate control enabled

    def call_service(self, service: str, **kwargs: dict):
        """Call one of the device's services in Home Assistant."""
        self.device.call_service(service, **kwargs)

    def get_attribute(
        self,
        attribute: str,
        default: str | float | None = None,
    ) -> str | float | None:
        """Get an attribute of the device (or group of synced devices)."""
        value = self.controller.get_state(
            self.device_id
            if self.device_type != "group"
            else self.controller.get_state(self.device_id, "entity_id")[0],
            attribute=attribute,
        )
        if value is None:
            return default
        try:
            return float(value)
        except ValueError:
            return value

    @property
    def vacant(self) -> bool:
        """If the room (and any linked rooms) are vacant."""
        return all(room.is_vacant(self.vacating_delay) for room in self.rooms)

    @property
    def ignoring_vacancy(self):
        """Check if the device is ignoring presence changes or not."""
        return not bool(self.presence_callbacks)

    def ignore_vacancy(self, ignore: bool = True):
        """Ignore presence changes by cancelling any presence callbacks."""
        if not self.ignoring_vacancy:
            for room in self.rooms:
                for callback in self.presence_callbacks:
                    room.cancel_callback(callback)
            self.presence_callbacks = []
        elif not ignore:
            self.monitor_presence()

    def monitor_presence(self):
        """Set callbacks for when presence changes."""
        self.ignore_vacancy()
        self.presence_callbacks = [
            room.register_callback(
                self.handle_presence_change,
                self.vacating_delay,
            )
            for room in self.rooms
        ]
        # TODO: check climate control first?

    def handle_presence_change(self, **kwargs):
        """Set device to adjust (with delay if required) when presence changes."""
        del kwargs
        if self.vacant != self.was_vacant_at_last_check:
            self.was_vacant_at_last_check = self.vacant
            # TODO: adjustment delay doesn't make sense here (vacating_delay does)
            # should only happen when changing settings due to temperature changes
            if (
                self.adjustment_delay > 0
                and self.adjustment_timer is None
                and (
                    self.controller.get_now_ts() - self.last_adjustment_time
                    < self.adjustment_delay
                )
            ):
                self.adjustment_timer = self.controller.run_in(
                    self.adjust_for_current_conditions_after_delay,
                    self.last_adjustment_time
                    + self.adjustment_delay
                    - self.controller.get_now_ts(),
                )
            else:
                self.transition_timer = None
                if self.should_transition_towards_occupied:
                    self.start_transition_towards_occupied()
                self.adjust_for_current_conditions()

    def adjust_for_current_conditions(self):
        """Override this in child class to adjust device settings appropriately."""

    def adjust_for_current_conditions_after_delay(self, **kwargs: dict):
        """Delayed adjustment from timers initiated when handling presence change."""
        del kwargs
        self.adjustment_timer = None
        self.adjust_for_current_conditions()

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
            self.adjust_for_current_conditions()
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
