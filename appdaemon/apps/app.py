"""AppDaemon subclass from which all apps inherit API and common functionality from.

This class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps. It also includes a basic device class
that can be inherited for specific device types and easily configured to respond to
environmental changes.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING

import appdaemon.plugins.hass.hassapi as hass

if TYPE_CHECKING:
    from climate import Climate
    from control import Control
    from lights import Lights
    from media import Media
    from presence import Presence
    from safety import Safety

    from appdaemon.apps.appdaemon.entity import Entity


class IDs:
    """System and user IDs defined by Home Assistant when referencing state context."""

    _named_ids = MappingProxyType(
        {
            None: "System",
            "57bea01aa68f44eb94ac2031ecb5b7ba": "System",
            "15ff7a86d4ae4d38a60003ad4064ff78": "Dan",
            "9a175674be354863afb9634adc4b8980": "Rachel",
        },
    )

    @classmethod
    def get_name(cls, id_value):
        """Get the user (or system) name for a given ID."""
        return cls._named_ids.get(id_value, "Unknown")

    @classmethod
    def is_system(cls, id_value):
        """Check if an ID refers to the system rather than a user."""
        return cls.get_name(id_value) == "System"


class App(hass.Hass):
    """Utility functions and methods for Home Assistant interaction."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.constants = self.args

    def initialize(self):
        """AppDaemon calls when app is ready."""

    def get_setting(self, setting_name: str) -> int:
        """Get UI input_number setting values."""
        if setting_name.endswith("_time"):
            return self.get_state(f"input_datetime.{setting_name}")
        return int(float(self.get_state(f"input_number.{setting_name}")))
        # TODO: detect more types

    def cancel_timer(self, handle):
        """Cancel timer or ignore if it is invalid or has already triggered."""
        super().cancel_timer(handle, silent=True)

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
                    if self.control.constants["mobiles"][person]["type"] == "iOS":
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
                    name=self.control.constants["mobiles"][person]["name"],
                    data=data,
                )
        self.log(f"Notified '{targets}': \"{kwargs['title']}: {message}\"")

    @property
    def climate(self) -> Climate:
        """Get the Climate app instance."""
        return self.get_app("Climate")

    @property
    def control(self) -> Control:
        """Get the Control app instance."""
        return self.get_app("Control")

    @property
    def lights(self) -> Lights:
        """Get the Lights app instance."""
        return self.get_app("Lights")

    @property
    def media(self) -> Media:
        """Get the Media app instance."""
        return self.get_app("Media")

    @property
    def presence(self) -> Presence:
        """Get the Presence app instance."""
        return self.get_app("Presence")

    @property
    def safety(self) -> Safety:
        """Get the Safety app instance."""
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
        self.device_type, device_name = controller.split_entity(device_id)
        self.device: Entity = controller.get_entity(device_id)
        self.controller = controller
        self.constants = controller.constants
        self.control_input_boolean = (
            "input_boolean.control_" + device_name + control_input_boolean_suffix
        )
        self.room = room
        self.linked_rooms = linked_rooms
        self.last_adjustment_time = self.controller.get_now_ts()
        devices = [self.device_id]
        if self.device_type == "group":
            devices += self.controller.get_state(self.device_id, "entity_id")
        for device in devices:
            self.controller.listen_state(
                self.__handle_user_adjustment,
                entity_id=device,
                attribute="context",
                new=lambda new: not IDs.is_system(new["user_id"]),
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
        """Check if device automatic control is enabled (via Home Assistant)."""
        return self.controller.get_state(self.control_input_boolean) == "on"

    @control_enabled.setter
    def control_enabled(self, enabled: bool):
        """Enable/disable automatic control of device."""
        if self.control_enabled != enabled:
            self.controller.call_service(
                f"input_boolean/turn_{'on' if enabled else 'off'}",
                entity_id=self.control_input_boolean,
            )
        if enabled:
            self.adjust_for_conditions()

    def turn_on_for_conditions(self):
        """Override this in child class to turn device on with best settings."""
        self.turn_on()

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Override this in child class to adjust device settings appropriately."""
        del check_if_would_adjust_only
        return False

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
            self.last_adjustment_time = self.controller.get_now_ts()

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
            self.last_adjustment_time = self.controller.get_now_ts()

    def call_service(self, service: str, **kwargs: dict):
        """Call one of the device's services in Home Assistant."""
        self.device.call_service(service, **kwargs)
        self.last_adjustment_time = self.controller.get_now_ts()

    def get_attribute(
        self,
        attribute: str,
        default: str | float | None = None,
    ) -> str | float | list | None:
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
        except (ValueError, TypeError):
            return value

    def __handle_user_adjustment(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Handle manual adjustment of the device via the UI."""
        del attribute, old, kwargs
        user = IDs.get_name(new["user_id"])
        self.controller.log(
            f"'{user}' changed {entity} from UI: "
            f"{self.controller.get_state(entity, 'all')}",
        )
        self.handle_user_adjustment(user)

    def handle_user_adjustment(self, user: str):
        """Override this in child class to adjust device settings appropriately."""
        if self.adjust_for_conditions(check_if_would_adjust_only=True):
            if not self.control_enabled:
                return
            self.control_enabled = False
            self.controller.notify(
                "Automatic control is now disabled for the "
                f"{self.device.friendly_name.lower()} to prevent it from immediately "
                f"overriding {user}'s manual adjustments",
                title=f"{self.device.friendly_name.title()} Control Disabled",
                targets="anyone_home_else_all",
            )

    def __handle_control_enabled(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Adjust device appropriately when automatic control is enabled."""
        del entity, attribute, old, new, kwargs
        self.adjust_for_conditions()
        if self.on:
            self.turn_on_for_conditions()
