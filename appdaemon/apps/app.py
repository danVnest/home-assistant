"""Classes from which all apps and devices inherit AppDaemon API & common functionality.

The App class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps.
The Device class should be inherited by more specific device classes for standardised
configuration to respond to environmental changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar, cast, override

import appdaemon.plugins.hass.hassapi as hass

if TYPE_CHECKING:
    from datetime import datetime, time

    from climate import Climate
    from control import Control
    from lights import Lights
    from media import Media
    from presence import Presence
    from safety import Safety

import logging


class App(hass.Hass):
    """Utility functions and methods for Home Assistant interaction."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.constants: dict[str, Any] = self.args

    def initialize(self) -> None:
        """AppDaemon calls when app is ready for initialisation."""

    def get_setting(self, setting_name: str, setting_type: str) -> str:
        """Get a UI input_number setting value as a string."""
        return cast("str", self.get_state(f"{setting_type}.{setting_name}"))

    def get_boolean_setting(self, setting_name: str) -> bool:
        """Get a UI input_boolean setting value as True or False."""
        return self.get_setting(setting_name, "input_boolean") == "on"

    def get_number_setting(self, setting_name: str) -> float:
        """Get a UI input_number setting value as a float."""
        return float(self.get_setting(setting_name, "input_number"))

    def get_integer_setting(self, setting_name: str) -> int:
        """Get a UI input_number setting value as an integer."""
        return int(self.get_number_setting(setting_name))

    def get_time_setting(self, setting_name: str) -> time:
        """Get a UI input_datetime setting time as a datetime.time object."""
        return self.parse_time(self.get_setting(setting_name, "input_datetime"))

    def get_datetime_setting(self, setting_name: str) -> datetime:
        """Get a UI input_datetime setting time as datetime.datetime object (today)."""
        return self.parse_datetime(self.get_setting(setting_name, "input_datetime"))

    @override
    def cancel_timer(self, handle: str | None, silent: bool = True) -> bool:
        """Cancel timer or ignore if it is invalid or has already triggered."""
        return super().cancel_timer(handle, silent=silent) if handle else False

    @override
    def notify(
        self,
        message: str,
        title: str | None = None,
        name: str | None = None,
        namespace: str | None = None,
        targets: str = "all",
        critical: bool = False,
        **kwargs: Any,
    ) -> None:
        """Send a notification to target users (dan, rachel, anyone_home, or all)."""
        del name
        if targets == "anyone_home_else_all":
            targets = "anyone_home" if self.presence.resident_home else "all"
        mobiles: dict[str, dict[str, str]] = self.control.constants["mobiles"]
        for person in ("dan", "rachel"):
            if any(
                (
                    targets == "all",
                    targets == person,
                    targets == "anyone_home"
                    and self.get_state(f"person.{person}") == "home",
                ),
            ):
                data = {"tag": title}
                if critical:
                    if mobiles[person]["type"] == "iOS":
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
                            {"ttl": 0, "priority": "high", "channel": "alarm_stream"},
                        )
                super().notify(
                    message,
                    title=title,
                    name=mobiles[person]["name"],
                    namespace=namespace,
                    data=data,
                    **kwargs,
                )
        self.log(f"Notified '{targets}': \"{title}: {message}\"")

    @property
    def climate(self) -> Climate:
        """Climate app instance."""
        return cast("Climate", self.get_app("Climate"))

    @property
    def control(self) -> Control:
        """Control app instance."""
        return cast("Control", self.get_app("Control"))

    @property
    def lights(self) -> Lights:
        """Lights app instance."""
        return cast("Lights", self.get_app("Lights"))

    @property
    def media(self) -> Media:
        """Media app instance."""
        return cast("Media", self.get_app("Media"))

    @property
    def presence(self) -> Presence:
        """Presence app instance."""
        return cast("Presence", self.get_app("Presence"))

    @property
    def safety(self) -> Safety:
        """Safety app instance."""
        return cast("Safety", self.get_app("Safety"))
    @property
    def debugging(self) -> bool:
        """True if debug logging is enabled - check before logging with f-strings."""
        return self.logger.isEnabledFor(logging.DEBUG)


Controller = TypeVar("Controller", bound=App)


class Device:
    """Basic device that can be configured to respond to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Controller,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "",
    ) -> None:
        """Initialise with device parameters and prepare for presence adjustments."""
        self.device_id = device_id
        self.device_type, device_name = device_id.split(".")
        self.device = controller.get_entity(device_id)
        self.room = room
        self.linked_rooms = linked_rooms
        self.controller = controller
        self.control_input_boolean = (
            f"input_boolean.control_{device_name}{control_input_boolean_suffix}"
        )
        self.last_adjustment_time = self.controller.get_now_ts()
        self.sub_device = None
        device_ids: list[str] = [device_id]
        if self.device_type == "group":
            device_ids.extend(
                cast("str", self.controller.get_state(self.device_id, "entity_id")),
            )
            self.sub_device = controller.get_entity(device_ids[1])
        for device in device_ids:
            self.controller.listen_state(
                self.__handle_user_adjustment,
                entity_id=device,
                attribute="context",
            )
        self.controller.listen_state(
            self.__handle_automatic_control_change,
            self.control_input_boolean,
            attribute="context",
        )

    @property
    def on(self) -> bool:
        """True if the device is on."""
        return self.device.state != "off"

    @property
    def control_enabled(self) -> bool:
        """True if automatic control is enabled for the device."""
        return self.controller.get_state(self.control_input_boolean) == "on"

    @control_enabled.setter
    def control_enabled(self, enabled: bool) -> None:
        """Enable/disable automatic control of the device."""
        if self.control_enabled != enabled:
            self.controller.call_service(
                f"input_boolean/turn_{'on' if enabled else 'off'}",
                entity_id=self.control_input_boolean,
            )
        if enabled:
            self.adjust_for_conditions()

    def turn_on_for_conditions(self) -> None:
        """Override this in a child class to turn device on with best settings."""
        self.turn_on()

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Override this in a child class to adjust device settings appropriately."""
        return False if check_if_would_adjust_only else None

    def turn_on(self, **kwargs: Any) -> None:
        """Turn the device on if it's off or adjust with provided parameters."""
        if not self.on or kwargs:
            if self.device_type != "group":
                self.device.turn_on(**kwargs)
            else:
                self.controller.call_service(
                    "homeassistant/turn_on",
                    entity_id=self.device_id,
                    namespace=None,
                    timeout=None,
                    callback=None,
                    **kwargs,
                )
            self.last_adjustment_time = self.controller.get_now_ts()

    def turn_off(self) -> None:
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

    def call_service(self, service: str, **data: Any) -> None:
        """Call one of the device's services."""
        self.device.call_service(service, timeout=None, callback=None, **data)
        self.last_adjustment_time = self.controller.get_now_ts()

    @property
    def constants(self) -> dict[str, Any]:
        """Constants as provided to the controlling AppDaemon app."""
        return self.controller.constants

    def __get_user(self, user_id: str | None) -> str:
        """Get the name of the user corresponding to a sensor state context ID."""
        return self.controller.control.constants["ids"].get(user_id, "Unknown")

    def __handle_user_adjustment(
        self,
        entity: str,
        attribute: str,
        old: dict,
        new: dict,
        **kwargs: Any,
    ) -> None:
        """Handle manual adjustment of the device via the UI."""
        del attribute, old, kwargs
        user = self.__get_user(new.get("user_id"))
        if user == "System":
            return
        self.log(
            f"'{user}' adjusted device via UI: "
            f"{self.controller.get_state(entity, 'all')}",
        )
        self.handle_user_adjustment(user)

    def handle_user_adjustment(self, user: str) -> None:
        """Override this in a child class to adjust device settings appropriately."""
        if self.control_enabled and self.adjust_for_conditions(
            check_if_would_adjust_only=True,
        ):
            self.control_enabled = False
            self.log(
                "Automatic control is now disabled to prevent it from immediately "
                f"overriding {user}'s manual adjustments",
            )

    def __handle_automatic_control_change(
        self,
        entity: str,
        attribute: str,
        old: dict,
        new: dict,
        **kwargs: Any,
    ) -> None:
        """Set device to adjust appropriately when automatic control is enabled."""
        del entity, attribute, old, kwargs
        user = self.__get_user(new.get("user_id"))
        if user == "System":
            return
        control_enabled = self.control_enabled
        self.log(
            f"Automatic control {'en' if control_enabled else 'dis'}abled by '{user}'",
        )
        if not control_enabled:
            return
        if not self.available:
            self.log("Device is unavailable for automatic control", level="WARNING")
            return
        self.adjust_for_conditions()
        if self.on:
            self.turn_on_for_conditions()

    def log(self, message: str, level: str = "INFO") -> None:
        """Log a message to the main log with device name prepended."""
        if level != "DEBUG" or self.debugging:
            self.controller.log(f"[{self.device.friendly_name}] {message}", level=level)

    @property
    def debugging(self) -> bool:
        """True if debug logging is enabled (check before logging with f-strings)."""
        return self.controller.debugging
