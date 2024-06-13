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
    from climate import Climate
    from control import Control
    from lights import Lights
    from media import Media
    from presence import Presence
    from safety import Safety


class App(hass.Hass):
    """Utility functions and methods for Home Assistant interaction."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.constants = {}

    def initialize(self):
        """AppDaemon calls when app is ready."""

    def cancel_timer(self, handle):
        """Cancel timer after checking it is valid and running."""
        if self.timer_running(handle):
            super().cancel_timer(handle)

    def notify(self, message: str, **kwargs):
        """Send a notification (title required) to target users (anyone_home or all)."""
        targets = kwargs.get("targets", "all")
        if targets == "anyone_home_else_all":
            targets = "anyone_home" if self.presence.anyone_home else "all"
        for person, info in self.entities.person.items():
            if any(
                (
                    targets == "all",
                    targets == "anyone_home" and info["state"] == "home",
                    targets == person,
                ),
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

    @property
    def climate(self) -> Climate:
        """"""
        return self.get_app("Climate")

    @property
    def control(self) -> Control:
        """"""
        return self.get_app("Control")

    @property
    def lights(self) -> Lights:
        """"""
        return self.get_app("Lights")

    @property
    def media(self) -> Media:
        """"""
        return self.get_app("Media")

    @property
    def presence(self) -> Presence:
        """"""
        return self.get_app("Presence")

    @property
    def safety(self) -> Safety:
        """"""
        return self.get_app("Safety")


class Device:
    """Basic device that can be configured to respond to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: App,
        room: str,
        linked_rooms: list[str] = (),
        control_input_boolean_suffix: str = "",
    ):
        """Initialise with device parameters and prepare for presence adjustments."""
        self.device_id = device_id
        self.device_type = device_id.split(".")[0]
        self.device = controller.get_entity(device_id)
        self.controller = controller
        self.control_input_boolean = (
            "input_boolean.control_"
            + device_id.split(".")[1]
            + control_input_boolean_suffix
        )
        self.room = room
        self.linked_rooms = linked_rooms
        devices = [self.device_id]
        if self.device_type == "group":
            devices += self.controller.get_state(self.device_id, "entity_id")
        for device in devices:
            self.controller.listen_state(
                self.__handle_user_adjustment,
                entity_id=device,
                attribute="context",
                new=lambda new: new["user_id"]
                not in (None, "57bea01aa68f44eb94ac2031ecb5b7ba"),
            )
        self.controller.listen_state(
            self.__handle_control_enabled,
            self.control_input_boolean,
            new="on",
        )

    @property
    def on(self) -> bool:
        """Check if the device is currently on or not."""
        return self.device.state != "off"

    @property
    def control_enabled(self) -> bool:
        """"""
        return self.controller.get_state(self.control_input_boolean) == "on"

    @control_enabled.setter
    def control_enabled(self, enabled: bool):
        """"""
        if self.control_enabled != enabled:
            self.controller.call_service(
                f"input_boolean/turn_{'on' if enabled else 'off'}",
                entity_id=self.control_input_boolean,
            )
        if enabled:
            self.check_conditions_and_adjust()

    def check_conditions_and_adjust(
        self,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Override this in child class to adjust device settings appropriately."""
        del check_if_would_adjust_only

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

    def __handle_user_adjustment(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """"""
        del entity, attribute, old, kwargs
        user = "Rachel" if new["user_id"].startswith("9a17567") else "Dan"
        self.controller.log(f"'{user}' changed {self.device_id} from UI")
        self.handle_user_adjustment()

    def handle_user_adjustment(self):
        """Override this in child class to adjust device settings appropriately."""

    def __handle_control_enabled(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """"""
        del entity, attribute, old, new, kwargs
        self.check_conditions_and_adjust()
