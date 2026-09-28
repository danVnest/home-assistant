"""Automates airconditioning, fan, heater, and humidifier control.

Monitors climate inside and out, controlling airconditioning units, fans, heaters,
and humidifiers in the house according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""

# TODO: rearrange all properties and methods more logically
from __future__ import annotations

from math import floor
from typing import TYPE_CHECKING, Any, cast, override

from app import App, Device
from presence import PresenceDevice

if TYPE_CHECKING:
    from appdaemon.entity import Entity


class Climate(App):
    """Control all climate devices based on user settings & environmental conditions."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.aircons: dict[str, Aircon] = {}
        self.heaters: dict[str, Heater] = {}
        self.fans: dict[str, Fan] = {}
        self.humidifiers: dict[str, Humidifier] = {}
        self.suggested: bool = False

    @override
    def initialize(self) -> None:
        """Initialise all climate devices and monitor temperatures."""
        super().initialize()
        self.aircons = {
            "bedroom": Aircon(
                device_id="climate.bedroom_aircon",
                controller=self,
                room="bedroom",
                doors=("bedroom_balcony", "kitchen", "dining_room_balcony"),
            ),
            "living_room": Aircon(
                device_id="climate.living_room_aircon",
                controller=self,
                room="living_room",
                linked_rooms=("dining_room", "kitchen"),
                doors=("kitchen", "dining_room_balcony", "bedroom_balcony"),
            ),
            "dining_room": Aircon(
                device_id="climate.dining_room_aircon",
                controller=self,
                room="dining_room",
                linked_rooms=("living_room", "kitchen"),
                doors=("dining_room_balcony", "kitchen", "bedroom_balcony"),
            ),
        }
        self.heaters = {
            "nursery": Heater(
                device_id="climate.nursery_heater",
                controller=self,
                room="nursery",
                safe_when_vacant=True,
            ),
            "office": Heater(
                device_id="switch.office_heater",
                controller=self,
                room="office",
            ),
        }
        self.fans = {
            room: Fan(
                device_id=f"fan.{room}",
                controller=self,
                room=room,
            )
            for room in ("bedroom", "nursery", "office")
        }
        self.humidifiers = {
            room: Humidifier(
                device_id=f"humidifier.{room}",
                controller=self,
                room=room,
            )
            for room in ("bedroom", "nursery")
        }
        self.fans["bedroom"].companion_device = self.aircons["bedroom"]
        self.fans["nursery"].companion_device = self.heaters["nursery"]
        self.fans["office"].companion_device = self.heaters["office"]
        for device_group in (self.aircons, self.heaters, self.humidifiers, self.fans):
            for device in device_group.values():
                device.monitor_presence()
                device.adjust_for_conditions()
        for temperatures in ("weighted_average_inside", "outside"):
            self.listen_state(
                self.handle_temperature_change,
                f"sensor.{temperatures}_apparent_temperature",
            )

    @property
    def any_climate_control_enabled(self) -> bool:
        """True if any climate control is enabled."""
        return self.entities.group.any_climate_control.state == "on"

    @property
    def all_climate_control_enabled(self) -> bool:
        """True if all climate control is enabled."""
        return self.entities.group.any_climate_control.state == "on"

    @all_climate_control_enabled.setter
    def all_climate_control_enabled(self, enable: bool) -> None:
        """Enable/disable climate control and reflect state in UI."""
        if self.all_climate_control_enabled == enable:
            return
        self.log(f"'{'En' if enable else 'Dis'}abling' all climate control")
        if enable:
            self.suggest_for_conditions()
        else:
            self.allow_suggestion()

    @property
    def any_aircon_on(self) -> bool:
        """True if any aircon units are on."""
        return self.entities.group.any_aircon.state != "off"

    @property
    def all_aircon_on(self) -> bool:
        """True if all aircon units are on."""
        return self.entities.group.all_aircon.state != "off"

    @all_aircon_on.setter
    def all_aircon_on(self, on: bool) -> None:
        """Turn all aircon units on or off."""
        for aircon in self.aircons.values():
            if on:
                aircon.turn_off()
            else:
                aircon.turn_on_for_conditions()

    def toggle_airconditioning(self, user: str | None = None) -> None:
        """Toggle airconditioning on/off."""
        self.all_aircon_on = not self.all_aircon_on
        if user:
            for aircon in self.aircons.values():
                aircon.handle_user_adjustment(user)

    @override
    def get_number_setting(self, setting_name: str) -> float:
        """Get climate settings (e.g. targets and triggers) for current scene."""
        if (
            self.control.scene == "Sleep" or self.control.bed_time
        ) and self.entity_exists(f"input_number.sleep_{setting_name}"):
            setting_name = f"sleep_{setting_name}"
        return super().get_number_setting(setting_name)

    def update_door_check_delay(self, seconds: float) -> None:
        """Update the delay before registering a door as open for each aircon."""
        self.allow_suggestion()
        for aircon in self.aircons.values():
            aircon.door_open_delay = seconds

    def update_vacating_delays(self, device_type: str, seconds: float) -> None:
        """Update room vacating delay for each device of specified type."""
        self.allow_suggestion()
        for device in getattr(self, f"{device_type}s").values():
            if not hasattr(device, "safe_when_vacant") or device.safe_when_vacant:
                device.vacating_delay = seconds

    def transition_to_scene(self, scene: str) -> None:
        """Adjust aircon & temperature triggers, suggest climate control if suitable."""
        if scene.startswith("Away"):
            device_groups_to_turn_off = [self.heaters, self.humidifiers]
            if not self.presence.pets_home_alone:
                device_groups_to_turn_off.extend([self.aircons, self.fans])
            for device_group in device_groups_to_turn_off:
                for device in device_group.values():
                    device.turn_off()
        self.aircons["bedroom"].preferred_fan_mode = (
            "low" if self.control.napping_in_bedroom else "auto"
        )
        if scene == "Morning":
            self.aircons["living_room"].ignore_vacancy()
        else:
            self.aircons["living_room"].monitor_presence()
        for humidifier in self.humidifiers.values():
            humidifier.already_notified_of_empty_water_tank = False
        if self.any_climate_control_enabled:
            self.adjust_for_conditions()
            self.suggest_for_conditions()
        if scene in ("Day", "Sleep") or self.presence.pets_home_alone:
            self.notify_if_extreme_forecast_and_control_disabled()
        self.allow_suggestion()

    def suggest_for_conditions(self) -> None:
        """Suggest climate control actions based on temperature and airflow."""
        if self.suggested:
            return
        if (
            self.presence.pets_home_alone
            and not self.any_aircon_on
            and self.too_hot_or_cold
        ):
            if all(aircon.door_open for aircon in self.aircons.values()):
                reason = "the door(s) are open"
                if self.outside_too_hot_or_cold:
                    reason += f" (but outside is {self.outside_temperature:.1f}°)"
                reason += ", consider"
            elif any(
                not aircon.control_enabled and not aircon.door_open
                for aircon in self.aircons.values()
            ):
                reason = "climate control is disabled for some or all aircon, "
                "consider enabling them and/or"
            else:
                self.log(
                    "Aircon isn't on for the pets yet because room temperature "
                    "is nicer than the average inside temperature",
                    level="DEBUG",
                )
                return
            self.suggest(
                f"It is {self.inside_temperature:.1f}° inside but aircon won't turn on "
                f"for the pets because {reason} turning aircon on manually",
            )

    def adjust_for_conditions(self) -> None:
        """Control aircon or suggest based on changes in inside temperature."""
        for device_group in (self.aircons, self.heaters, self.humidifiers, self.fans):
            for device in device_group.values():
                device.adjust_for_conditions()

    def condition_room_for_sleep(self, room: str) -> None:
        """Cool/heat/humidify the given room for nice sleeping conditions."""
        device_groups = (self.aircons, self.heaters, self.humidifiers, self.fans)
        for device in (
            device_group[room] for device_group in device_groups if room in device_group
        ):
            device.ignore_vacancy()
            device.adjust_for_conditions()

    def condition_room_normally(self, room: str) -> None:
        """Restore normal presence-based device functionality in the given room."""
        device_groups = (self.aircons, self.heaters, self.humidifiers, self.fans)
        for device in (
            device_group[room] for device_group in device_groups if room in device_group
        ):
            device.monitor_presence()

    def notify_if_extreme_forecast_and_control_disabled(self) -> None:
        """Notify if extreme temperatures are forecast and climate not controlled."""
        extreme_forecast = self.entities.sensor.extreme_forecast.state
        if extreme_forecast not in (None, "unavailable", "unknown") and any(
            not device.control_enabled
            for device_group in (
                [self.aircons, self.fans]
                if float(extreme_forecast)
                >= self.get_number_setting("high_temperature_aircon_trigger")
                else [self.aircons, self.heaters]
            )
            for device in device_group.values()
            if self.control.scene != "Sleep" or device.room in ("bedroom", "nursery")
        ):
            self.notify(
                f"It's forecast to reach {float(extreme_forecast):.1f}°, "
                "consider enabling additional climate control",
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def suggest(self, message: str) -> None:
        """Make a suggestion to the users, but only if one has not already been sent."""
        if self.suggested:
            return
        self.suggested = True
        self.notify(message, title="Climate Control", targets="anyone_home_else_all")

    def allow_suggestion(self) -> None:
        """Allow suggestions to be made again. Use after user events & scene changes."""
        self.suggested = False

    def validate_temperature_setting(self, setting: str) -> None:
        """Check if a given temperature setting is valid, update if not."""
        sleep = "sleep_" if "sleep" in setting else ""
        target = "target" in setting
        heat = "heat" in setting
        high = "high" in setting
        modifier = heat if target else high
        checks = {
            f"{sleep}{'low' if modifier else 'high'}_temperature_aircon_trigger": 1
            if modifier
            else -1,
            f"{sleep}{'cool' if modifier else 'heat'}ing_target_temperature": 1
            if high or (target and not heat)
            else -1,
        }
        self._validate_setting_given_checks(setting, checks)

    def validate_humidity_setting(self, setting: str) -> None:
        """Check if a given humidity setting is valid, update if not."""
        sleep = "sleep_" if "sleep" in setting else ""
        checks: dict[str, int] = {}
        if "target" in setting:
            checks[f"{sleep}high_humidity_aircon_trigger"] = -1
            checks[f"{sleep}low_humidity_humidifier_trigger"] = 1
        else:
            checks[f"{sleep}target_humidity"] = 1 if "high" in setting else -1
        self._validate_setting_given_checks(setting, checks)

    def _validate_setting_given_checks(
        self,
        setting: str,
        checks: dict[str, int],
    ) -> None:
        """Check if a setting is valid using pre-formulated checks, update if not."""
        setting_type = "temperature" if "temperature" in setting else "humidity"
        adjustments = {}
        for check, polarity in checks.items():
            if (
                self.get_number_setting(setting) - self.get_number_setting(check)
            ) * polarity < self.constants["setting_buffer"][setting_type]:
                adjustments[check] = (
                    self.constants["setting_buffer"][setting_type] * polarity
                )
        for other_setting, adjustment in adjustments.items():
            other_setting_id = f"input_number.{other_setting}"
            valid_other = max(
                min(
                    self.get_number_setting(setting) - adjustment,
                    cast("float", self.get_state(other_setting_id, attribute="max")),
                ),
                cast("float", self.get_state(other_setting_id, attribute="min")),
            )
            self.call_service(
                "input_number/set_value",
                entity_id=other_setting_id,
                value=valid_other,
            )
            self.log(
                f"The new '{setting}' value conflicts, adjusting "
                f"the corresponding setting '{other_setting}' to '{valid_other}°'",
                level="WARNING",
            )
            getattr(self, f"validate_{setting_type}_setting")(other_setting)
        self.allow_suggestion()
        self.adjust_for_conditions()
        self.suggest_for_conditions()

    def terminate(self) -> None:
        """Cancel presence callbacks before termination (auto run by Appdaemon)."""
        for device_group in (self.aircons, self.heaters, self.humidifiers, self.fans):
            for device in device_group.values():
                device.ignore_presence()

    @property
    def inside_temperature(self) -> float:
        """Apparent temperature inside the house."""
        return float(
            self.entities.sensor.weighted_average_inside_apparent_temperature.state,
        )

    @property
    def outside_temperature(self) -> float:
        """Apparent temperature outside the house."""
        return float(self.entities.sensor.outside_apparent_temperature.state)

    def handle_temperature_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle a change in inside or outside temperature."""
        del entity, attribute, old, kwargs
        if new not in (None, "unavailable", "unknown"):
            # TODO: https://app.asana.com/0/1207020279479204/1207217352886591/f
            # be more resilient towards unavailable/unknown temperatures
            self.suggest_for_conditions()

    @property
    def inside_too_hot_or_cold(self) -> bool:
        """True if the inside temperature is above or below the max/min triggers."""
        return not (
            self.get_number_setting("low_temperature_aircon_trigger")
            < self.inside_temperature
            < self.get_number_setting("high_temperature_aircon_trigger")
        )

    @property
    def outside_too_hot_or_cold(self) -> bool:
        """True if the outside temperature exceeds desired indoor thresholds."""
        return not (
            self.get_number_setting("low_temperature_aircon_trigger")
            <= self.outside_temperature
            <= self.get_number_setting("high_temperature_aircon_trigger")
        )


class ClimateDevice(Device):
    """Climate device configured to respond to environmental changes."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "",
    ) -> None:
        """Initialise with device parameters and prepare for environment changes."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            control_input_boolean_suffix,
        )
        self.controller: Climate = self.controller
        self.temperature_sensors: list[Entity] = []
        for _room in (room, *linked_rooms):
            temperature_sensor_id = f"sensor.{_room}_apparent_temperature_ignoring_wind"
            self.temperature_sensors.append(
                self.controller.get_entity(temperature_sensor_id),
            )
            if "humidity_source_value" not in self.temperature_sensors[-1].attributes:
                self.log(
                    f"Temperature sensor '{temperature_sensor_id}' is unavailable",
                    level="WARNING",
                )
            self.controller.listen_state(
                self.handle_temperature_change,
                temperature_sensor_id,
                duration=0.5,
                constrain_input_boolean=self.control_input_boolean,
            )
        self.humidity_sensors = [
            sensor
            for sensor in self.temperature_sensors
            if "humidity_source_value" in sensor.attributes
        ]
        self.target_reduction: float = (
            self.constants["target_reduction"]
            .get(self.__class__.__name__.lower(), {})
            .get(self.room, 0)
        )
        self.adjustment_timer: str | None = None

    @property
    def room_temperature(self) -> float:
        """Average temperature from all sensors in the room."""
        return sum(
            float(temperature_sensor.state)
            for temperature_sensor in self.temperature_sensors
        ) / len(self.temperature_sensors)

    @property
    def room_humidity(self) -> float:
        """Average humidity from all sensors in the room."""
        return sum(
            float(humidity_sensor.attributes["humidity_source_value"])
            for humidity_sensor in self.humidity_sensors
        ) / len(self.temperature_sensors)

    @property
    def room_within_target_temperatures(self) -> bool:
        """True if the room temperature is not above or below target temperatures."""
        return not (
            self.room_above_target_temperature or self.room_below_target_temperature
        )

    @property
    def room_above_target_temperature(self) -> bool:
        """True if the room temperature is above the target temperature."""
        return (
            self.room_temperature
            > self.controller.get_number_setting("cooling_target_temperature")
            + self.target_reduction
        )

    @property
    def room_below_target_temperature(self) -> bool:
        """True if the room temperature is below the target temperature."""
        return (
            self.room_temperature
            < self.controller.get_number_setting("heating_target_temperature")
            - self.target_reduction
        )

    @property
    def room_too_hot_or_cold(self) -> bool:
        """True if the room temperature is above or below the max/min triggers."""
        return not (
            self.controller.get_number_setting("low_temperature_aircon_trigger")
            < self.room_temperature
            < self.controller.get_number_setting("high_temperature_aircon_trigger")
        )

    @property
    def room_closer_to_hot_than_cold(self) -> bool:
        """True if the room temperature is closer to needing cooling than heating."""
        return self.room_temperature + self.outside_temperature > (
            self.controller.get_number_setting("cooling_target_temperature")
            + self.controller.get_number_setting("heating_target_temperature")
        )

    @property
    def outside_temperature(self) -> float:
        """Apparent temperature outside the house."""
        return self.controller.outside_temperature

    @property
    def outside_hotter(self) -> bool:
        """True if outside is hotter than in the room."""
        return (
            self.room_temperature
            < self.outside_temperature - self.constants["inside_outside_trigger"]
        )

    @property
    def outside_colder(self) -> bool:
        """True if outside is colder than in the room."""
        return (
            self.room_temperature
            > self.outside_temperature + self.constants["inside_outside_trigger"]
        )

    @property
    def outside_too_hot_or_cold(self) -> bool:
        """True if outside temperature exceeds desired indoor thresholds."""
        return self.controller.outside_too_hot_or_cold

    @property
    def room_too_dry(self) -> bool:
        """True if the room is too dry based on desired target humidity settings."""
        return self.room_humidity < self.controller.get_number_setting(
            "low_humidity_humidifier_trigger",
        )

    @property
    def room_too_humid(self) -> bool:
        """True if the room is too humid based on desired target humidity settings."""
        return self.room_humidity > self.controller.get_number_setting(
            "high_humidity_aircon_trigger",
        )

    @property
    def room_dry_enough(self) -> bool:
        """True if the room is dry enough based on desired target humidity settings."""
        return self.room_humidity < self.controller.get_number_setting(
            "target_humidity",
        )

    def handle_temperature_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Adjust for new conditions with a delay if appropriate."""
        del entity, attribute, old, new, kwargs
        if self.device.state in (None, "unavailable", "unknown"):
            self.log("Device is unavailable - ignoring sensor change", level="DEBUG")
            return
        if self.adjustment_delay == 0:
            self.adjust_for_conditions()
            return
        was_recent_adjustment = (
            self.controller.get_now_ts() - self.last_adjustment_time
            < self.constants["adjustment_delay"]
        )
        if self.adjustment_timer is None and was_recent_adjustment:
            run_in = (
                self.last_adjustment_time
                + self.constants["adjustment_delay"]
                - self.controller.get_now_ts()
            )
            self.adjustment_timer = self.controller.run_in(
                self.adjust_for_conditions_after_delay,
                run_in,
            )
            if self.debugging:
                self.log(
                    f"Set to adjust for conditions in {run_in:.1f} seconds "
                    "as it was already adjusted recently",
                    level="DEBUG",
                )
        elif self.adjustment_timer is None or not was_recent_adjustment:
            if self.adjustment_timer:
                self.controller.cancel_timer(self.adjustment_timer)
                self.adjustment_timer = None
            self.adjust_for_conditions()
        else:
            self.log(
                "Already set to adjust for conditions shortly - "
                "ignoring latest sensor change",
                level="DEBUG",
            )

    def adjust_for_conditions_after_delay(self, **kwargs: Any) -> None:
        """Delayed adjustment from timers initiated when handling presence change."""
        del kwargs
        self.adjustment_timer = None
        self.adjust_for_conditions()

    def adjust_for_conditions_after_initial_checks(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Use in adjust_for_conditions after initial checks."""
        if not self.available:
            return False
        adjuster = (
            self.adjust_for_conditions_from_on
            if self.on
            else self.adjust_for_conditions_from_off
        )
        return adjuster(check_if_would_adjust_only=check_if_would_adjust_only)

    def adjust_for_conditions_from_off(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Override this in child class to adjust device settings appropriately."""
        return False if check_if_would_adjust_only else None

    def adjust_for_conditions_from_on(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Override this in child class to adjust device settings appropriately."""
        return False if check_if_would_adjust_only else None


class Aircon(ClimateDevice, PresenceDevice):
    """Control a specific aircon unit."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "",
        doors: tuple[str, ...] = (),
    ) -> None:
        """Initialise an aircon unit with all required parameters."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            control_input_boolean_suffix,
        )
        self.preferred_fan_mode = "auto"
        swing_modes: list[str] = self.device.attributes.get("swing_modes", [])
        self.preferred_swing_mode = "both" if "both" in swing_modes else "rangefull"
        if "rangefull" not in swing_modes:
            self.preferred_swing_mode = swing_modes[0] if swing_modes else "rangefull"
            self.log(
                f"No valid swing mode available ({swing_modes = }), "
                f"defaulting to '{self.preferred_swing_mode}'",
                level="WARNING",
            )
        self.turn_off_timer_handle: str | None = None
        self.vacating_delay = 60 * self.controller.get_number_setting(
            "aircon_vacating_delay",
        )
        self.doors: list[Entity] = []
        for door in doors:
            door_id = f"binary_sensor.{door}_door"
            self.doors.append(self.controller.get_entity(door_id))
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="off",
            )
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="on",
                duration=self.constants["aircon"]["reduce_fan"]["delay"],
            )
        self.__door_open_delay: float = 0
        self.door_open_delay = 60 * self.controller.get_number_setting(
            "aircon_door_check_delay",
        )
        self.user_adjusted_on_time_threshold = 1

    @property
    def best_mode_for_conditions(self) -> str:
        """Best climate mode (cool/heat/dry) for current room conditions."""
        if self.room_too_humid and self.room_within_target_temperatures:
            return "dry"
        if self.room_above_target_temperature or self.room_closer_to_hot_than_cold:
            return "cool"
        return "heat"

    @property
    def mode_aided_by_outside_temperature(self) -> bool:
        """True if the outside temperature is improving the inside temperature."""
        return (
            (self.device.state == "heat" and self.outside_hotter)
            or (self.device.state == "cool" and self.outside_colder)
            or (
                self.device.state == "off"
                and self.room_too_hot_or_cold
                and not self.outside_too_hot_or_cold
            )
        )

    @property
    def target_temperature(self) -> float:
        """Aircon's current target temperature, or room temperature if it's off."""
        if not self.on:
            return self.room_temperature
        target: str | None = self.device.attributes.get("temperature")
        if target is None:
            self.log(
                "Target temperature is not available, defaulting to room temperature",
                level="WARNING",
            )
            return self.room_temperature
        return float(target)

    @property
    def desired_target_temperature(self) -> float:
        """Desired room target temperature based on settings and conditions."""
        mode = self.best_mode_for_conditions
        if mode not in ("cool", "heat"):
            mode = "cool"
        return self.controller.get_number_setting(mode + "ing_target_temperature")

    @property
    def fan_mode(self) -> str:
        """Aircon's current fan mode (main options: 'low', 'auto')."""
        fan_mode: str | None = self.device.attributes.get("fan_mode")
        if fan_mode is None:
            self.log("Fan mode is not available, defaulting to 'auto'", level="WARNING")
            return "auto"
        return fan_mode

    @fan_mode.setter
    def fan_mode(self, mode: str) -> None:
        """Set the fan mode to the specified level (main options: 'low', 'auto')."""
        if self.on and self.fan_mode != mode:
            self.call_service("set_fan_mode", fan_mode=mode)

    @property
    def swing_mode(self) -> str:
        """Aircon's current swing mode (main options: 'rangefull', 'both')."""
        swing_mode: str | None = self.device.attributes.get("swing_mode")
        if swing_mode is None:
            self.log(
                "Swing mode is not available, defaulting to 'rangefull'",
                level="WARNING",
            )
            return "rangefull"
        return swing_mode

    @override
    def turn_on_for_conditions(self) -> None:
        """Set the aircon unit to heat or cool at desired settings."""
        mode = self.best_mode_for_conditions
        if self.device.state != mode:
            self.call_service("set_hvac_mode", hvac_mode=mode)
        desired_target_temperature = self.desired_target_temperature
        if self.target_temperature != desired_target_temperature:
            self.call_service("set_temperature", temperature=desired_target_temperature)
        if self.fan_mode != self.preferred_fan_mode and not self.door_open:
            self.fan_mode = self.preferred_fan_mode
        if self.swing_mode != self.preferred_swing_mode:
            self.call_service("set_swing_mode", swing_mode=self.preferred_swing_mode)

    def turn_off_after_delay(self, **kwargs: Any) -> None:
        """Turn the aircon off after the required delay when a door opens."""
        del kwargs
        if self.door_open:
            self.log(
                "Turning off because a door has been open for "
                f"{self.door_open_delay / 60} minutes",
            )
            self.turn_off()

    @property
    def would_turn_on_adjust_for_conditions(self) -> bool:
        """True if turn_on_for_conditions would actually make any changes."""
        return (
            self.device.state != self.best_mode_for_conditions
            or self.target_temperature != self.desired_target_temperature
            or self.fan_mode != self.preferred_fan_mode
            or self.swing_mode != self.preferred_swing_mode
        )

    @override
    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Adjust aircon based on current conditions and target temperatures."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        if (
            self.controller.control.scene.startswith("Away")
            and not self.controller.presence.pets_home_alone
        ) or (self.room != "bedroom" and self.controller.control.scene == "Sleep"):
            if not check_if_would_adjust_only and self.on:
                self.log("Turning off because no one is home or the scene is 'Sleep'")
                self.turn_off()
            return self.on
        return self.adjust_for_conditions_after_initial_checks(
            check_if_would_adjust_only=check_if_would_adjust_only,
        )

    @override
    def adjust_for_conditions_from_off(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Adjust aircon based on current conditions and target temperatures."""
        if (
            (self.room_too_hot_or_cold or self.room_too_humid)
            and (self.ignoring_vacancy or not self.vacant)
            and (not self.door_open or self.mode_aided_by_outside_temperature)
        ):
            if check_if_would_adjust_only:
                return True
            self.log("Turning on because it's too hot/cold/humid")
            if self.debugging:
                self.log(
                    f"({self.room_too_hot_or_cold = } or {self.room_too_humid = }) "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = }) "
                    f"and ({not self.door_open = } or "
                    f"{self.mode_aided_by_outside_temperature = })",
                    level="DEBUG",
                )
            self.notify_if_turning_on_for_pets()
            self.turn_on_for_conditions()
        elif not check_if_would_adjust_only and self.debugging:
            self.log(
                f"Staying off because: ("
                f"{not self.room_too_hot_or_cold = } and {not self.room_too_humid = }) "
                f"or ({not self.ignoring_vacancy = } and {self.vacant = }) "
                f"or ({self.door_open = } and "
                f"{not self.mode_aided_by_outside_temperature = })",
                level="DEBUG",
            )
        return False

    @override
    def adjust_for_conditions_from_on(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Adjust aircon based on current conditions and target temperatures."""
        if (
            (
                self.room_within_target_temperatures
                and (self.device.state != "dry" or self.room_dry_enough)
            )
            or (not self.ignoring_vacancy and self.vacant)
            or (self.door_open and not self.mode_aided_by_outside_temperature)
        ):
            if check_if_would_adjust_only:
                return True
            self.log(
                "Turning aircon off because it's within targets "
                "(or room is vacant, or door is open)",
            )
            if self.debugging:
                self.log(
                    f"({self.room_within_target_temperatures = } and "
                    f"({self.device.state != 'dry'} or {self.room_dry_enough = })) "
                    f"or ({not self.ignoring_vacancy = } and {self.vacant = }) "
                    f"or ({self.door_open = } and "
                    f"{not self.mode_aided_by_outside_temperature = })",
                    level="DEBUG",
                )
            self.turn_off()
        else:
            if check_if_would_adjust_only:
                return self.would_turn_on_adjust_for_conditions
            if self.debugging:
                self.log(
                    f"Staying on because: "
                    f"({not self.room_within_target_temperatures = } or ("
                    f"{self.device.state == 'dry'} and {not self.room_dry_enough = })) "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = }) "
                    f"and ({not self.door_open = } or "
                    f"{self.mode_aided_by_outside_temperature = })",
                    level="DEBUG",
                )
            self.turn_on_for_conditions()  # already on, this will ensure best settings
            if (
                not self.door_open
                and self.mode_aided_by_outside_temperature
                and self.controller.presence.anyone_home
            ):
                self.controller.suggest(
                    f"Outside ({self.controller.outside_temperature:.1f}°) "
                    f"is a more pleasant temperature than the {self.room} "
                    f"({self.room_temperature:.1f}°), consider opening up the house",
                )
        return False

    @property
    def door_open(self) -> bool:
        """True if any doors are open (and have been for the required delay)."""
        return any(
            door.state != "off" and door.last_changed_seconds >= self.door_open_delay
            for door in self.doors
        )

    @property
    def door_open_delay(self) -> float:
        """Seconds to delay before registering a door as open."""
        return self.__door_open_delay

    @door_open_delay.setter
    def door_open_delay(self, seconds: float) -> None:
        """Set the number of seconds to delay before registering a door as open."""
        if self.__door_open_delay != seconds:
            self.__door_open_delay = seconds
            self.adjust_for_conditions()

    def handle_door_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """When a nearby door status changes, check if aircon needs to change."""
        del attribute, old, kwargs
        self.controller.cancel_timer(self.turn_off_timer_handle)
        if not (self.control_enabled and self.available):
            return
        if new == "on" and self.on and not self.mode_aided_by_outside_temperature:
            if (
                self.door_open_delay - self.constants["aircon"]["reduce_fan"]["delay"]
                <= 0
            ):
                self.log("Turning off because an outside door is open")
                self.turn_off()
            else:
                self.turn_off_timer_handle = self.controller.run_in(
                    self.turn_off_after_delay,
                    self.door_open_delay
                    - self.constants["aircon"]["reduce_fan"]["delay"],
                    constrain_input_boolean=self.control_input_boolean,
                )
                if (
                    entity == self.doors[0].entity_id
                    and self.fan_mode == "auto"
                    and abs(self.room_temperature - self.target_temperature)
                    > self.constants["aircon"]["reduce_fan"]["temperature_threshold"]
                ):
                    self.log("Reducing aircon fan to low while an outside door is open")
                    self.fan_mode = "low"
        elif not self.door_open:
            if self.on:
                self.fan_mode = self.preferred_fan_mode
            else:
                self.adjust_for_conditions()

    @override
    def handle_user_adjustment(self, user: str) -> None:
        """Handle manual aircon adjustment appropriately."""
        if (
            self.control_enabled
            and self.on
            and self.device.last_changed_seconds < self.user_adjusted_on_time_threshold
        ):
            self.turn_on_for_conditions()
        super().handle_user_adjustment(user)
        self.controller.allow_suggestion()

    def notify_if_turning_on_for_pets(self) -> None:
        """Notify if aircon is turning on for the pets."""
        if (
            not self.controller.any_aircon_on
            and self.controller.presence.pets_home_alone
            and not self.controller.presence.anyone_home
        ):
            self.controller.notify(
                f"It is {self.room_temperature:.1f}° in the {self.room}, "
                "turning aircon on for the pets",
                title="Climate Control",
            )


class Fan(ClimateDevice, PresenceDevice):
    """Control a fan and configure responses to environmental changes."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "_fan",
        companion_device: ClimateDevice | None = None,
    ) -> None:
        """Initialise a fan with all required parameters."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            control_input_boolean_suffix,
        )
        speed_per_level: str | None = self.device.attributes.get("percentage_step")
        if speed_per_level is None:
            self.log("Speed step is not available, defaulting to 11%", level="WARNING")
        self.speed_per_level = round(float(speed_per_level)) if speed_per_level else 11
        self.speed_levels = round(100 / self.speed_per_level)
        self.minimum_speed = self.speed_per_level * 1
        self.reverse_desired = self.reverse
        self.reversing_timer: str | None = None
        self.reversing_steps_remaining: list[str | float] = []
        self.companion_device = companion_device
        self.vacating_delay = 60 * self.controller.get_number_setting(
            "fan_vacating_delay",
        )

    @property
    def speed(self) -> float:
        """Fan speed (0 is off, 100 is full speed)."""
        if not self.on:
            return 0
        speed: str | None = self.device.attributes.get("percentage")
        if speed is None:
            self.log("Speed is not available, defaulting to 0%")
        return float(speed) if speed else 0

    def validate_speed(self, speed: float) -> float:
        """Round speed down to nearest level and ensure it's between 0% and 100%."""
        # This matches the speed values that HA rounds to.
        # While step is reported as 11.111...% HA uses 11% - except at 99% it uses 100%.
        valid_speed = max(
            0,
            min(100, floor(speed / self.speed_per_level) * self.speed_per_level),
        )
        return valid_speed if valid_speed != 99 else 100  # noqa: PLR2004

    @property
    def desired_cooling_speed(self) -> float:
        """Fan speed required to lower apparent room temperature to the target."""
        speed = (self.room_temperature - self.target_temperature) / self.constants[
            "fan"
        ]["cooling_per_speed"]
        if self.debugging:
            self.log(
                f"Speed required to reduce temperature to the target "
                f"{self.target_temperature:.1f}C is {speed:.1f}% (currently "
                f"{self.room_temperature_with_wind_chill:.1f}C and {self.speed:.0f}%) ",
                level="DEBUG",
            )
        return speed

    @property
    def reverse(self) -> bool:
        """True if the fan's spin direction is set to reverse."""
        return self.device.attributes.get("direction") == "reverse"

    @property
    def target_temperature(self) -> float:
        """Fan's target temperature for the room."""
        return self.controller.get_number_setting("cooling_target_temperature")

    @property
    def cooling_effect(self) -> float:
        """Reduction in apparent temperature caused by the fan's current speed."""
        return self.cooling_effect_for_speed(self.speed)

    def cooling_effect_for_speed(
        self,
        speed: float,
        *,
        reverse: bool | None = None,
    ) -> float:
        """Calculate the reduction in apparent temperature caused by a given speed."""
        if reverse is None:
            reverse = self.reverse_desired
        return (
            self.constants["fan"]["cooling_per_speed"]
            * speed
            / (
                self.constants["fan"]["cooling_reduction_factor_when_reverse"]
                if reverse
                else 1
            )
        )

    @property
    def room_temperature_with_wind_chill(self) -> float:
        """Apparent room temperature if the fan was off."""
        return self.room_temperature - self.cooling_effect

    @property
    def reverse_to_match_companion_device(self) -> bool:
        """True if the fan should be in reverse to match the companion device."""
        return isinstance(self.companion_device, Heater) or (
            isinstance(self.companion_device, Aircon)
            and (
                self.companion_device.device.state == "heat"
                or (
                    self.companion_device.device.state == "heat_cool"
                    and not self.room_closer_to_hot_than_cold
                )
            )
        )

    def could_disturb_sleep_if_adjusted_to(
        self,
        speed: float,
        *,
        reverse: bool,
    ) -> bool:
        """Check if the fan could disturb sleep if adjusted."""
        return (
            self.room in ("bedroom", "nursery")
            and self.controller.control.napping_in(self.room, sustained=True)
            and (
                reverse != self.reverse or (not self.on and speed >= self.minimum_speed)
            )
        )

    @override
    def turn_on_for_conditions(self) -> None:
        """Turn the fan on with appropriate speed and direction for the environment."""
        reverse = (
            self.reverse_to_match_companion_device
            if (self.companion_device and self.companion_device.on)
            else not self.room_closer_to_hot_than_cold
        )
        self.adjust(
            max(self.minimum_speed, self.desired_cooling_speed),
            reverse=reverse,
        )

    @override
    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Calculate the best fan speed for current conditions and set accordingly."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        reverse = self.reverse_desired
        speed = 0
        if self.companion_device and self.companion_device.on:
            reverse = self.reverse_to_match_companion_device
            speed = max(self.minimum_speed, self.desired_cooling_speed)
        elif (
            (
                "Away" not in self.controller.control.scene
                or self.controller.presence.pets_home_alone
            )
            and (self.ignoring_vacancy or not self.vacant)
            and not self.room_within_target_temperatures
        ):
            if self.room_closer_to_hot_than_cold:
                reverse = False
                speed = self.desired_cooling_speed
        if self.could_disturb_sleep_if_adjusted_to(speed, reverse=reverse):
            if self.debugging:
                self.log(
                    f"The desired settings ({speed:.0f}% "
                    f"{'reverse' if reverse else 'forward'}) could disturb sleep - "
                    "turning off instead",
                    level="DEBUG",
                )
            speed = 0
        if not check_if_would_adjust_only:
            self.adjust(speed, reverse=reverse)
            return None
        return self.speed != speed or (speed != 0 and self.reverse_desired != reverse)

    def adjust(self, speed: float, *, reverse: bool) -> None:
        """Adjust the fan direction and speed in the correct order."""
        if self.reversing_timer:
            self.controller.cancel_timer(self.reversing_timer)
            self.reversing_timer = None
        speed = self.validate_speed(speed)
        if speed == 0:
            self.turn_off()
            return
        self.reverse_desired = reverse
        if reverse != self.reverse:
            temperature_change = self.cooling_effect - self.cooling_effect_for_speed(
                speed,
                reverse=reverse,
            )
            self.log(
                f"Changing spin direction to "
                f"'{'reverse' if self.reverse_desired else 'forward'}' and speed from "
                f"{self.speed:.0f}% to {speed}%, which will change the apparent "
                f"temperature by {temperature_change:.1f}C to "
                f"{self.room_temperature + temperature_change:.1f}C",
            )
            if not self.on:
                self.turn_on(percentage=self.minimum_speed)
                self.reversing_steps_remaining = ["reverse"]
            elif self.speed != self.minimum_speed:
                self.call_service("set_percentage", percentage=self.minimum_speed)
                self.reversing_steps_remaining = ["reverse"]
            else:
                self.call_service(
                    "set_direction",
                    direction="reverse" if reverse else "forward",
                )
                self.reversing_steps_remaining: list[str | float] = []
            if speed != self.minimum_speed:
                self.reversing_steps_remaining += [speed]
            self.reversing_timer = self.controller.run_in(
                self.continue_reverse,
                self.constants["fan"]["reversing_delay"],
            )
            return
        if speed == self.speed:
            return
        temperature_change = self.cooling_effect - self.cooling_effect_for_speed(speed)
        self.log(
            f"Changing speed from {self.speed:.0f}% to {speed}%, "
            f"which will change the apparent temperature by {temperature_change:.1f}C "
            f"to {self.room_temperature + temperature_change:.1f}C",
        )
        if self.on:
            self.call_service("set_percentage", percentage=speed)
        else:
            self.turn_on(percentage=speed)

    def continue_reverse(self, **kwargs: Any) -> None:
        """Continue the remaining fan reversal steps (reverse or change speed)."""
        del kwargs
        if not self.reversing_steps_remaining:
            return
        if self.reversing_steps_remaining[0] == "reverse":
            if self.reverse != self.reverse_desired:
                self.call_service(
                    "set_direction",
                    direction="reverse" if self.reverse_desired else "forward",
                )
            else:
                del self.reversing_steps_remaining[0]
        elif self.speed != self.reversing_steps_remaining[0]:
            self.call_service(
                "set_percentage",
                percentage=self.reversing_steps_remaining[0],
            )
        else:
            del self.reversing_steps_remaining[0]
        self.reversing_timer = (
            self.controller.run_in(
                self.continue_reverse,
                self.constants["fan"]["reversing_delay"],
            )
            if self.reversing_steps_remaining
            else None
        )


class Heater(ClimateDevice, PresenceDevice):
    """Control a heater and configure responses to environmental changes."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "",
        safe_when_vacant: bool = False,
    ) -> None:
        """Initialise a heater with all required parameters."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            control_input_boolean_suffix,
        )
        door_id = f"binary_sensor.{room}_door"
        if self.controller.entity_exists(door_id):
            self.door = self.controller.get_entity(door_id)
            self.controller.listen_state(self.handle_door_change, door_id)
        else:
            self.door = None
        self.safe_when_vacant = safe_when_vacant
        self.vacating_delay = (
            60 * self.controller.get_number_setting("heater_vacating_delay")
            if safe_when_vacant
            else 0
        )

    @property
    def desired_target_temperature(self) -> float:
        """User specified desired target temperature for the heater."""
        return self.controller.get_number_setting("heating_target_temperature")

    @property
    def target_temperature(self) -> float:
        """Heater's current target temperature."""
        if self.device_type == "climate":
            return self.desired_target_temperature
        target: str | None = self.device.attributes.get("temperature")
        if target is None:
            self.log(
                "Target temperature is not available, "
                "defaulting to user setting 'heating_target_temperature'",
                level="WARNING",
            )
            return self.desired_target_temperature
        return float(target)

    @target_temperature.setter
    def target_temperature(self, target: float) -> None:
        """Set the heater's target temperature."""
        if self.should_update_target_temperature:
            self.call_service("set_temperature", temperature=target)

    @property
    def should_update_target_temperature(self) -> bool:
        """False if the device's target temperature is as desired (or can't be set)."""
        return (
            self.device_type == "climate"
            and self.target_temperature != self.desired_target_temperature
        )

    @property
    def on_when_away_and_not_safe(self) -> bool:
        """True if the device is on, not safe, and no-one home."""
        return (
            not self.safe_when_vacant
            and self.on
            and (
                not self.controller.presence.anyone_home
                or self.controller.presence.manual_guest_mode
                or self.controller.control.scene.startswith("Away")
            )
        )

    @override
    def turn_on_for_conditions(self) -> None:
        """Turn the heater on and adjust the target temperature if appropriate."""
        self.target_temperature = self.desired_target_temperature
        self.turn_on()

    @property
    def room_too_cold(self) -> bool:
        """True if the room is too cold based on the desired target temperature."""
        return self.room_temperature < self.desired_target_temperature

    @property
    def room_warm_enough(self) -> bool:
        """True if the room is warm enough based on the desired target temperature."""
        return (
            self.room_temperature
            > self.desired_target_temperature
            + self.constants["heater"]["target_buffer"]
        )

    @override
    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Turn the heater on/off based on current and target temperatures."""
        if self.on_when_away_and_not_safe:
            if check_if_would_adjust_only:
                return True
            self.turn_off()
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        return self.adjust_for_conditions_after_initial_checks(
            check_if_would_adjust_only=check_if_would_adjust_only,
        )

    @override
    def adjust_for_conditions_from_off(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Adjust the heater based on current conditions and the target temperature."""
        if (
            self.room_too_cold
            and (self.ignoring_vacancy or not self.vacant)
            and (self.door and self.door.state == "off")
        ):
            if check_if_would_adjust_only:
                return True
            self.log("Turning on because it's too cold")
            if self.debugging:
                self.log(
                    f"{self.room_too_cold = } "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = }) "
                    f"and {(self.door and self.door.state == "off") = }",
                    level="DEBUG",
                )
            self.turn_on_for_conditions()
        elif self.debugging:
            self.log(
                "Staying off because: "
                f"{not self.room_too_cold = } "
                f"or ({not self.ignoring_vacancy = } and {self.vacant = }) "
                f"or {not (self.door and self.door.state == "off") = }",
                level="DEBUG",
            )
        return False

    @override
    def adjust_for_conditions_from_on(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Adjust the heater based on current conditions and the target temperature."""
        if (
            self.room_warm_enough
            or (not self.ignoring_vacancy and self.vacant)
            or (self.door and self.door.state != "off")
        ):
            if check_if_would_adjust_only:
                return True
            self.log(
                "Turning off because it's warm enough "
                "(or room is vacant, or door is open)",
            )
            if self.debugging:
                self.log(
                    f"{self.room_warm_enough = } "
                    f"or ({not self.ignoring_vacancy = } and {self.vacant = }) "
                    f"or {(self.door and self.door.state != "off") = }",
                    level="DEBUG",
                )
            self.turn_off()
        elif check_if_would_adjust_only:
            return self.target_temperature != self.desired_target_temperature
        else:
            if self.debugging:
                self.log(
                    f"Staying on because: "
                    f"{not self.room_warm_enough = } "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = }) "
                    f"and {not (self.door and self.door.state != "off") = }",
                    level="DEBUG",
                )
            self.target_temperature = self.desired_target_temperature
        return False

    def handle_door_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """When a nearby door status changes, check if heater needs to change."""
        del entity, attribute, old, new, kwargs
        self.adjust_for_conditions()

    @override
    def handle_user_adjustment(self, user: str) -> None:
        """Handle manual heater adjustment appropriately."""
        if self.on_when_away_and_not_safe:
            self.turn_off()
            self.controller.notify(
                f"The {self.device.friendly_name.lower()} is not safe "
                "to turn on when no-one is home",
                title=f"{self.device.friendly_name.title()} Turned Off",
                targets="anyone_home_else_all",
            )
        else:
            super().handle_user_adjustment(user)


class Humidifier(ClimateDevice, PresenceDevice):
    """Control a humidifier and configure responses to environmental changes."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "_humidifier",
    ) -> None:
        """Initialise a humidifier with all required parameters."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            control_input_boolean_suffix,
        )
        self.faults = controller.get_entity(f"sensor.{room}_humidifier_faults")
        self.beeper = controller.get_entity(f"switch.{room}_humidifier_beeper")
        self.light = controller.get_entity(f"light.{room}_humidifier")
        self.room_light = controller.get_entity(f"light.{room}")
        self.vacating_delay = 60 * self.controller.get_number_setting(
            "humidifier_vacating_delay",
        )
        self.controller.listen_state(
            self.handle_empty_water_tank,
            self.faults.entity_id,
            new=lambda x: x != "no faults",
            old="no faults",
        )
        self.controller.listen_state(
            self.sync_lighting,
            self.light.entity_id,
            immediate=True,
        )
        self.controller.listen_state(self.sync_lighting, self.device_id, new="on")
        self.controller.listen_state(
            self.disable_beep,
            self.beeper.entity_id,
            new="on",
            immediate=True,
        )
        self.already_notified_of_empty_water_tank = False

    @property
    def target_humidity(self) -> float:
        """The humidifier's target humidity."""
        humidity: str | None = self.device.attributes.get("humidity")
        if humidity is None:
            self.log(
                "Target humidity is not available, "
                "defaulting to user setting 'target_humidity'",
                level="WARNING",
            )
            return self.controller.get_number_setting("target_humidity")
        return float(humidity)

    @target_humidity.setter
    def target_humidity(self, target: float) -> None:
        """Set the humidifier's target humidity."""
        if self.target_humidity != self.controller.get_number_setting(
            "target_humidity",
        ):
            self.call_service("set_humidity", humidity=target)

    @property
    def constant_humidity_mode(self) -> bool:
        """True if the humidifier is set to reach and maintain a constant humidity."""
        return self.device.attributes.get("mode") == "Constant Humidity"

    def set_constant_humidity_mode(self) -> None:
        """Set the humidifier to reach and maintain a constant humidity."""
        if self.on and not self.constant_humidity_mode:
            self.call_service("set_mode", mode="Constant Humidity")

    @override
    def turn_on_for_conditions(self) -> None:
        """Turn the humidifier on and adjust the target humidity if appropriate."""
        self.set_constant_humidity_mode()
        self.target_humidity = self.controller.get_number_setting("target_humidity")
        self.turn_on()

    @property
    def empty_water_tank(self) -> bool:
        """True if the humidifier's water tank is empty."""
        return self.faults.state != "no faults"

    def handle_empty_water_tank(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle empty water tank status by calling notification method."""
        del entity, attribute, old, new, kwargs
        if self.empty_water_tank:
            self.notify_of_empty_water_tank()

    def notify_of_empty_water_tank(self) -> None:
        """Notify that the humidifier's water tank is empty (only once per scene)."""
        if not self.already_notified_of_empty_water_tank:
            self.controller.notify(
                f"Refill the {self.room} humidifier water tank so it can turn on",
                title=f"{self.device.friendly_name.title()} Water Tank Empty",
                targets="anyone_home",
            )
            self.already_notified_of_empty_water_tank = True

    @override
    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Turn the humidifier on/off based on current and target humidities."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        return self.adjust_for_conditions_after_initial_checks(
            check_if_would_adjust_only=check_if_would_adjust_only,
        )

    @override
    def adjust_for_conditions_from_off(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Turn the humidifier on/off based on current and target humidities."""
        if self.room_too_dry and (self.ignoring_vacancy or not self.vacant):
            if self.empty_water_tank:
                self.notify_of_empty_water_tank()
                return False
            if check_if_would_adjust_only:
                return True
            self.log("Turning on because it's too dry")
            if self.debugging:
                self.log(
                    f"{self.room_too_dry = } "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = })",
                    level="DEBUG",
                )
            self.turn_on_for_conditions()
        elif self.debugging:
            self.log(
                "Staying off because: "
                f"{not self.room_too_dry = } "
                f"or ({not self.ignoring_vacancy = } and {self.vacant = })",
                level="DEBUG",
            )
        return False

    @override
    def adjust_for_conditions_from_on(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool | None:
        """Turn the humidifier on/off based on current and target humidities."""
        if self.room_too_humid or (not self.ignoring_vacancy and self.vacant):
            if check_if_would_adjust_only:
                return True
            self.log("Turning off because it's too humid (or room is vacant)")
            if self.debugging:
                self.log(
                    f"{self.room_too_humid = } "
                    f"or ({not self.ignoring_vacancy = } and {self.vacant = })",
                    level="DEBUG",
                )
            self.turn_off()
        else:
            if (
                not self.constant_humidity_mode
                or self.target_humidity
                != self.controller.get_number_setting("target_humidity")
            ):
                if check_if_would_adjust_only:
                    return True
                self.set_constant_humidity_mode()
                self.target_humidity = self.controller.get_number_setting(
                    "target_humidity",
                )
            if self.debugging:
                self.log(
                    f"Staying on because: "
                    f"{not self.room_too_humid = } "
                    f"and ({self.ignoring_vacancy = } or {not self.vacant = })",
                    level="DEBUG",
                )
        return False

    def sync_lighting(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Sync the humidifier's light with the room's light."""
        del entity, attribute, old, new, kwargs
        if not self.on:
            return
        state = "on" if self.room_light.state == "on" else "off"
        if self.light.state not in (state, "unavailable"):
            if self.debugging:
                self.log(
                    f"Syncing light (currently '{self.light.state}') with "
                    f"the room lighting (currently '{self.room_light.state}')",
                    level="DEBUG",
                )
            getattr(self.light, f"turn_{state}")()

    def disable_beep(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Ensure the humidifier is set to not beep on status change."""
        del entity, attribute, old, new, kwargs
        self.beeper.turn_off()
