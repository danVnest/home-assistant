"""AppDaemon subclass from which all apps inherit API and common functionality from.

This class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps. It also includes a basic device class
that can be inherited for specific device types and easily configured to respond to
environmental changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import appdaemon.plugins.hass.hassapi as hass

if TYPE_CHECKING:
    from control import Control


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
        self.control_input_boolean = None
        self.room = room
        self.linked_rooms = linked_rooms
        # TODO: listen to all climate UI changes here, disable climate control for that device if it conflicts
        # self.controller.listen_event(
        #     self.handle_state_change,
        #     "state_changed",
        #     entity_id=self.device_id,
        #     metadata=lambda metadata: metadata["context"]["user_id"] is not None,
        # )  # TODO: use this version once you're sure there's no key errors
        self.controller.listen_event(
            self.handle_state_change,
            "state_changed",
            entity_id=self.device_id,
        )

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

    def handle_state_change(self, event_name: str, data: dict, **kwargs: dict):
        """"""
        del event_name, kwargs
        try:
            if data["metadata"]["context"]["user_id"] is not None:
                self.controller.log(f"User changed {self.device_id}")
                self.handle_user_adjustment()
        except KeyError:
            self.controller.log(
                f"KeyError when detecting user initiated state change: {data}",
                level="WARNING",
            )
        # TODO: remove try and if is not None (and use lambda) once you're sure there's no key errors

    def handle_user_adjustment(self):
        """Override this in child class to adjust device settings appropriately."""
