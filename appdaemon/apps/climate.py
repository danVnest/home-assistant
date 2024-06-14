"""Automates airconditioning, fan, and heater control.

Monitors climate inside and out, controlling airconditioning units, fans, and
heaters in the house according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""

# TODO: adjust ALL documentation text for all apps
# TODO: rearrange all properties and methods more logically
from __future__ import annotations

from math import ceil
from typing import TYPE_CHECKING

from app import App, Device
from presence import PresenceDevice

if TYPE_CHECKING:
    from appdaemon.entity import Entity


class Climate(App):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.suggested = False
        self.aircons: dict[str, Aircon] = {}
        self.heaters: dict[str, Heater] = {}
        self.fans: dict[str, Fan] = {}

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.aircons = {
            "bedroom": Aircon(
                device_id="climate.bedroom_aircon",
                controller=self,
                room="bedroom",
                doors=["bedroom_balcony"],
            ),
            "living_room": Aircon(
                device_id="climate.living_room_aircon",
                controller=self,
                room="living_room",
                linked_rooms=["dining_room", "kitchen"],
                doors=["kitchen", "dining_room_balcony"],
            ),
            "dining_room": Aircon(
                device_id="climate.dining_room_aircon",
                controller=self,
                room="dining_room",
                linked_rooms=["living_room", "kitchen"],
                doors=["kitchen", "dining_room_balcony"],
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
            for room in ("bedroom", "office", "nursery")
        }
        self.fans["bedroom"].companion_device = self.aircons["bedroom"]
        self.fans["office"].companion_device = self.heaters["office"]
        self.fans["nursery"].companion_device = self.heaters["nursery"]
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.monitor_presence()
                device.check_conditions_and_adjust()
        for temperatures in ("weighted_average_inside", "outside"):
            self.listen_state(
                self.handle_temperature_change,
                f"sensor.{temperatures}_apparent_temperature",
            )

    @property
    def any_climate_control_enabled(self) -> bool:
        """Get climate control setting that has been synced to Home Assistant."""
        return self.entities.group.any_climate_control.state == "on"

    @property
    def all_climate_control_enabled(self) -> bool:
        """Get climate control setting that has been synced to Home Assistant."""
        return self.entities.group.any_climate_control.state == "on"

    @all_climate_control_enabled.setter
    def all_climate_control_enabled(self, enable: bool):
        """Enable/disable climate control and reflect state in UI."""
        if self.all_climate_control_enabled == enable:
            return
        self.log(f"'{'En' if enable else 'Dis'}abling' all climate control")
        if enable:
            self.check_conditions_and_adjust()
        else:
            self.allow_suggestion()

    @property
    def any_aircon_on(self) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        return self.entities.group.any_aircon.state != "off"

    @property
    def all_aircon_on(self) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        return self.entities.group.all_aircon.state != "off"

    @all_aircon_on.setter
    def all_aircon_on(self, on: bool) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        for aircon in self.aircons.values():
            if on:
                aircon.turn_off()
            else:
                aircon.turn_on_for_current_conditions()

    def toggle_airconditioning(self, user: str | None = None) -> None:
        """Toggle airconditioning on/off."""
        self.all_aircon_on = not self.all_aircon_on
        if user:
            for aircon in self.aircons.values():
                aircon.handle_user_adjustment(user)

    def get_setting(self, setting_name: str) -> float:
        """Get temperature target and trigger settings, accounting for Sleep scene."""
        if self.control.scene == "Sleep" or self.control.bed_time:
            setting_name = f"sleep_{setting_name}"
        return float(self.get_state(f"input_number.{setting_name}"))

    def update_door_check_delay(self, seconds: float):
        """Update the delay before registering a door as open for each aircon."""
        for aircon in self.aircons.values():
            aircon.door_open_delay = seconds

    def update_aircon_vacating_delays(self, seconds: float):
        """Update room vacating delay for each fan."""
        for aircon in self.aircons.values():
            aircon.vacating_delay = seconds

    def update_fan_vacating_delays(self, seconds: float):
        """Update room vacating delay for each fan."""
        for fan in self.fans.values():
            fan.vacating_delay = seconds

    def update_heater_vacating_delays(self, seconds: float):
        """Update room vacating delay for each heater."""
        for heater in self.heaters.values():
            if heater.safe_when_vacant:
                heater.vacating_delay = seconds

    def transition_between_scenes(self, new_scene: str, old_scene: str):
        """Adjust aircon & temperature triggers, suggest climate control if suitable."""
        if "Away" in new_scene:
            if not self.presence.pets_home_alone:
                for device_group in (self.aircons, self.fans):
                    for device in device_group.values():
                        device.turn_off()
            for heater in self.heaters.values():
                heater.turn_off()
        if new_scene == "Sleep":
            self.end_pre_condition_bedrooms()
        elif new_scene == "Morning":
            self.heaters["nursery"].monitor_presence()
            # TODO: remove when nursery presence is reliable
            self.aircons["dining_room"].monitor_presence()
            # TODO: remove dining room once presence detects dogs properly?
        self.aircons["bedroom"].preferred_fan_mode = (
            "low" if new_scene in ("Sleep", "Morning") else "auto"
        )
        if self.any_climate_control_enabled:
            self.check_conditions_and_adjust()
        elif any(
            [
                new_scene == "Day" and old_scene in ["Sleep", "Morning"],
                new_scene == "Night" and old_scene == "Day",
                "Away" not in new_scene and "Away" in old_scene,
                self.presence.pets_home_alone,
            ],
        ):  # TODO: consider simplifying and removing old_scene?
            self.suggest_if_extreme_forecast()

    def check_conditions_and_adjust(self):
        """Control aircon or suggest based on changes in inside temperature."""
        """Handle each case (house open, outside nicer, climate control status)?"""
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.check_conditions_and_adjust()
        if (
            self.presence.pets_home_alone
            and not self.any_aircon_on
            and self.too_hot_or_cold
        ):
            if all(aircon.door_open for aircon in self.aircons.values()):
                reason = "the door(s) are open"
                if self.too_hot_or_cold_outside:
                    reason += f" (but outside is f{self.outside_temperature}º)"
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
                f"It is {self.inside_temperature}º inside but aircon won't turn on "
                f"for the pets because {reason} turning aircon on manually",
            )
            # TODO: above doesn't account for the situation where the bedroom door is closed

    def pre_condition_bedrooms(self):
        """Pre-cool/heat the bedroom and nursery for nice sleeping conditions."""
        for device in (
            self.aircons["bedroom"],
            self.heaters["nursery"],
            self.aircons["dining_room"],
        ):
            # TODO: remove dining room once presence detects dogs properly?
            device.ignore_vacancy()
            device.check_conditions_and_adjust()

    def end_pre_condition_bedrooms(self):
        """Return bedroom and nursery climate control to normal."""
        self.aircons["bedroom"].monitor_presence()
        # self.heaters["nursery"].monitor_presence() # TODO: uncomment when nursery presence is reliable

    def suggest_if_extreme_forecast(self):
        """Suggest user enables control if extreme temperatures are forecast."""
        self.allow_suggestion()
        forecast = self.extreme_forecast
        if forecast is not None:
            self.suggest(
                f"It's forecast to reach {forecast}º, "  # TODO: format to one decimal place
                "consider enabling climate control",
            )

    def suggest(self, message: str):
        """Make a suggestion to the users, but only if one has not already been sent."""
        if not self.suggested:
            self.suggested = True
            self.notify(
                message,
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def allow_suggestion(self):
        """Allow suggestions to be made again. Use after user events & scene changes."""
        if self.suggested:
            self.suggested = False

    def validate_target_and_trigger(self, target_or_trigger):
        """Check if a given target/trigger temperature pair is valid, update if not."""
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115364/f
        # what if heating target is below cooling target?
        # what if aircon high trigger is below aircon low trigger?
        sleep = "sleep_" if "sleep" in target_or_trigger else ""
        if "target" in target_or_trigger:
            other = (
                f"input_number.{sleep}"
                f"{'high' if 'cool' in target_or_trigger else 'low'}"
                "_temperature_aircon_trigger"
            )
            modifier = -1 if "cool" in target_or_trigger else 1
        else:
            other = (
                f"input_number.{sleep}"
                f"{'cool' if 'high' in target_or_trigger else 'heat'}"
                "ing_target_temperature"
            )
            modifier = -1 if "low" in target_or_trigger else 1
        if (
            float(self.get_state(f"input_number.{target_or_trigger}"))
            - float(self.get_state(other))
        ) * modifier < 0:
            valid_other = float(self.get_state(f"input_number.{target_or_trigger}"))
            self.call_service(
                "input_number/set_value",
                entity_id=other,
                value=valid_other,
            )
            self.log(
                f"The '{target_or_trigger}' value is not valid, adjusting "
                f"the corresponding target/trigger to '{valid_other}º'",
                level="WARNING",
            )
        self.check_conditions_and_adjust()

    def terminate(self):
        """Cancel presence callbacks before termination????

        Appdaemon defined function called before termination.
        """
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.ignore_vacancy()

    # TODO: consider making a TemperatureChecker class with all the following checks
    # devices can use with their own temperature
    @property
    def inside_temperature(self) -> float:
        """Get the calculated inside temperature that's synced with Home Assistant."""
        return float(
            self.entities.sensor.weighted_average_inside_apparent_temperature.state,
        )

    @property
    def outside_temperature(self) -> float:
        """Get the calculated outside temperature from Home Assistant."""
        return float(self.entities.sensor.outside_apparent_temperature.state)

    def handle_temperature_change(
        self,
        entity: str,
        attribute: str,
        old: float,
        new: float,
        **kwargs: dict,
    ):
        """Calculate inside temperature then get controller to handle if changed."""
        del entity, attribute, old, kwargs
        if new not in (None, "unavailable", "unknown"):
            # TODO: https://app.asana.com/0/1207020279479204/1207217352886591/f
            # be more resilient towards unavailable/unknown temperatures
            self.check_conditions_and_adjust()

    @property
    def within_target_temperatures(self) -> bool:
        """Check if temperature is not above or below target temperatures."""
        return not (self.above_target_temperature or self.below_target_temperature)

    @property
    def above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature."""
        return self.inside_temperature > float(
            self.get_setting("cooling_target_temperature"),
        )

    @property
    def below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature."""
        return self.inside_temperature < self.get_setting("heating_target_temperature")

    @property
    def hotter_outside(self) -> bool:
        """Check if temperature is higher outside than inside."""
        return (
            self.inside_temperature
            < self.outside_temperature - self.args["inside_outside_trigger"]
        )

    @property
    def colder_outside(self) -> bool:
        """Check if temperature is lower outside than inside."""
        return (
            self.inside_temperature
            > self.outside_temperature + self.args["inside_outside_trigger"]
        )

    @property
    def too_hot_or_cold_outside(self) -> bool:
        """Check if outside temperature exceeds desired indoor thresholds."""
        return not (
            self.get_setting("low_temperature_aircon_trigger")
            <= self.outside_temperature
            <= self.get_setting("high_temperature_aircon_trigger")
        )

    @property
    def outside_temperature_nicer(self) -> bool:
        """Check if outside is a nicer temperature than inside."""
        mode = self.entities.climate.bedroom_aircon.state
        # TODO: use aircon group state instead?
        return any(
            (
                mode == "heat" and self.hotter_outside,
                mode == "cool" and self.colder_outside,
                mode == "off"
                and (self.too_hot_or_cold and not self.too_hot_or_cold_outside),
            ),
        )

    @property
    def too_hot_or_cold(self) -> bool:
        """Check if temperature inside is above or below the max/min triggers."""
        return not (
            self.get_setting("low_temperature_aircon_trigger")
            < self.inside_temperature
            < self.get_setting("high_temperature_aircon_trigger")
        )

    @property
    def closer_to_hot_than_cold(self) -> bool:
        """Return if temperature inside is closer to needing cooling than heating."""
        return (
            self.inside_temperature
            > (
                self.get_setting("cooling_target_temperature")
                + self.get_setting("heating_target_temperature")
            )
            / 2
        )

    @property
    def extreme_forecast(self) -> float:
        """Return the forecasted temperature if it exceeds thresholds."""
        forecasts = [
            float(
                self.get_state(f"sensor.outside_apparent_temperature_{hour}h_forecast"),
            )
            for hour in ["2", "4", "6", "8"]
        ]
        max_forecast = max(forecasts)
        if max_forecast >= self.get_setting("high_temperature_aircon_trigger"):
            return max_forecast
        min_forecast = min(forecasts)
        if min_forecast <= self.get_setting("low_temperature_aircon_trigger"):
            return min_forecast
        return None


class ClimateDevice(Device):
    """Climate device that can be configured to respond to environmental changes?"""

    def __init__(
        self,
        **kwargs: dict,
    ):
        """Initialise with device parameters and prepare for presence adjustments?"""
        super().__init__(
            **kwargs,
        )
        self.temperature_sensors = []
        for room in (self.room, *self.linked_rooms):
            temperature_sensor_id = f"sensor.{room}_apparent_temperature"
            self.temperature_sensors.append(
                self.controller.get_entity(temperature_sensor_id),
            )
            self.controller.listen_state(
                self.handle_temperature_change,
                temperature_sensor_id,
                constrain_input_boolean=self.control_input_boolean,
            )
        self.adjustment_delay = 0
        self.last_adjustment_time = self.controller.get_now_ts()
        self.adjustment_timer = None

    @property
    def room_temperature(self) -> float:
        """"""
        return sum(
            float(temperature_sensor.state)
            for temperature_sensor in self.temperature_sensors
        ) / len(self.temperature_sensors)

    # TODO: consider making a TemperatureChecker class with all the following checks
    # e.g. to switch room_temperature and inside_temperature

    @property
    def within_target_temperatures(self) -> bool:
        """Check if temperature is not above or below target temperatures."""
        return not (self.above_target_temperature or self.below_target_temperature)

    @property
    def above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature."""
        return self.room_temperature > float(
            self.controller.get_setting("cooling_target_temperature"),
        )

    @property
    def below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature."""
        return self.room_temperature < self.controller.get_setting(
            "heating_target_temperature",
        )

    @property
    def too_hot_or_cold(self) -> bool:
        """Check if temperature inside is above or below the max/min triggers."""
        return not (
            self.controller.get_setting("low_temperature_aircon_trigger")
            < self.room_temperature
            < self.controller.get_setting("high_temperature_aircon_trigger")
        )

    @property
    def closer_to_hot_than_cold(self) -> bool:
        """Return if temperature inside is closer to needing cooling than heating."""
        return (
            self.room_temperature
            > (
                self.controller.get_setting("cooling_target_temperature")
                + self.controller.get_setting("heating_target_temperature")
            )
            / 2
        )

    @property
    def hotter_outside(self) -> bool:
        """Check if temperature is higher outside than inside."""
        return (
            self.room_temperature
            < self.controller.outside_temperature
            - self.controller.args["inside_outside_trigger"]
        )

    @property
    def colder_outside(self) -> bool:
        """Check if temperature is lower outside than inside."""
        return (
            self.room_temperature
            > self.controller.outside_temperature + self.args["inside_outside_trigger"]
        )

    @property
    def too_hot_or_cold_outside(self) -> bool:
        """Check if outside temperature exceeds desired indoor thresholds."""
        return not (
            self.controller.get_setting("low_temperature_aircon_trigger")
            <= self.controller.outside_temperature
            <= self.controller.get_setting("high_temperature_aircon_trigger")
        )

    @property
    def outside_temperature_nicer(self) -> bool:
        """Check if outside is a nicer temperature than inside."""
        mode = self.controller.entities.climate.bedroom_aircon.state
        # TODO: use aircon group state instead?
        return any(
            (
                mode == "heat" and self.hotter_outside,
                mode == "cool" and self.colder_outside,
                mode == "off"
                and (self.too_hot_or_cold and not self.too_hot_or_cold_outside),
            ),
        )

    def handle_temperature_change(
        self,
        entity: str,
        attribute: str,
        old: float,
        new: float,
        **kwargs: dict,
    ):
        """Calculate inside temperature then get controller to handle if changed?"""
        del entity, attribute, old, new, kwargs
        if (
            self.adjustment_delay > 0
            and self.adjustment_timer is None
            and (
                self.controller.get_now_ts() - self.last_adjustment_time
                < self.adjustment_delay
            )
        ):
            self.adjustment_timer = self.controller.run_in(
                self.check_conditions_and_adjust_after_delay,
                self.last_adjustment_time
                + self.adjustment_delay
                - self.controller.get_now_ts(),
            )
        self.check_conditions_and_adjust()

    def check_conditions_and_adjust_after_delay(self, **kwargs: dict):
        """Delayed adjustment from timers initiated when handling presence change."""
        del kwargs
        self.adjustment_timer = None
        self.check_conditions_and_adjust()

    def adjust_temperature_targets_and_triggers(self):
        """"""
        if self.on:
            self.turn_on()
        else:
            self.check_conditions_and_adjust()


class Aircon(ClimateDevice, PresenceDevice):
    """Control a specific aircon unit."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
        doors: list[Entity] = (),
    ):
        """Initialise with an aircon's id, room(s), and the Climate controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            room=room,
            linked_rooms=linked_rooms,
        )
        self.preferred_fan_mode = self.fan_mode
        # TODO: set adjustment_delay?
        self.vacating_delay = 60 * float(
            controller.entities.input_number.aircon_vacating_delay.state,
        )
        self.doors: list[Entity] = []
        for door in doors:
            door_id = f"binary_sensor.{door}_door"
            self.doors.append(self.controller.get_entity(door_id))
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="off",
                constrain_input_boolean=self.control_input_boolean,
            )
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="on",
                duration=self.controller.args["aircon_reduce_fan_delay"],
            )
        self.__door_open_delay = None
        self.door_open_delay = 60 * float(
            controller.entities.input_number.aircon_door_check_delay.state,
        )

    @property
    def fan_mode(self) -> str:
        """Set the fan mode to the specified level (main options: 'low', 'auto')?"""
        return self.get_attribute("fan_mode")

    @fan_mode.setter
    def fan_mode(self, mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')?"""
        if self.on and self.fan_mode != mode:
            self.call_service("set_fan_mode", fan_mode=mode)

    def turn_on_for_current_conditions(
        self,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Set the aircon unit to heat or cool at desired settings."""
        mode = (
            "cool"
            if self.above_target_temperature or self.closer_to_hot_than_cold
            else "heat"
        )
        if self.device.state != mode:
            if check_if_would_adjust_only:
                return True
            self.call_service("set_hvac_mode", hvac_mode=mode)
        target_temperature = self.controller.get_setting(
            mode + "ing_target_temperature",
        ) + self.controller.args["target_buffer"] * (
            1 if mode == "heat" else -1
        )  # TODO: potentially remove target_buffer?
        if self.get_attribute("temperature") != target_temperature:
            if check_if_would_adjust_only:
                return True
            self.call_service("set_temperature", temperature=target_temperature)
        if self.fan_mode != self.preferred_fan_mode:
            if check_if_would_adjust_only:
                return True
            if not self.door_open:
                self.fan_mode = self.preferred_fan_mode
        if check_if_would_adjust_only:
            return False
        self.controller.allow_suggestion()
        return True
        # TODO: add a temperature buffer on min/max trigger and targets in pet mode for efficiency !! IMPORTANT
        # What about cooling? Fans? Take into account pet presence in each room?
        # Bayesian probably can do a good job of detecting pet presence, and/or increase vacating delay

    def check_conditions_and_adjust(
        self,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Adjust aircon based on current conditions and target temperatures."""
        if not self.control_enabled or (
            "Away" in self.controller.control.scene
            and not self.controller.presence.pets_home_alone
        ):
            return False
        # TODO: reset suggestions more often? on any climate UI changes?
        # TODO: this is too complex, move parts to another method
        if not self.on:
            if self.too_hot_or_cold and (self.ignoring_vacancy or not self.vacant):
                if not self.door_open:
                    if (
                        not check_if_would_adjust_only
                        and not self.controller.any_aircon_on
                        and self.controller.presence.pets_home_alone
                        and not self.controller.presence.anyone_home
                    ):
                        self.controller.notify(
                            f"It is {self.room_temperature}º in the {self.room}, "
                            "turning aircon on for the pets",
                            title="Climate Control",
                        )
                    if (
                        check_if_would_adjust_only
                        and self.turn_on_for_current_conditions(
                            check_if_would_adjust_only,
                        )
                    ):
                        return True
                    self.turn_on_for_current_conditions()
                elif (
                    not check_if_would_adjust_only
                    and not self.outside_temperature_nicer
                    and self.controller.presence.anyone_home
                ):
                    self.controller.suggest(
                        f"It's {self.room_temperature}º in the {self.room} right now, "
                        "consider closing up the house so aircon can turn on",
                    )
        elif (
            self.door_open
            or self.within_target_temperatures
            or (not self.ignoring_vacancy and self.vacant)
        ):
            if check_if_would_adjust_only:
                return True
            self.turn_off()
        elif (
            not check_if_would_adjust_only
            and self.outside_temperature_nicer
            and self.controller.presence.anyone_home
        ):
            self.controller.suggest(
                f"Outside ({self.room_temperature}º) "
                f"is a more pleasant temperature than the {self.room} "
                f"({self.room_temperature}º), consider opening up the house",
            )
        return False

    @property
    def door_open(self) -> bool:
        """Check if any doors are open (and have been for the required delay)."""
        return any(
            door.state == "on" and door.last_changed_seconds >= self.door_open_delay
            for door in self.doors
        )
        # TODO: maybe change all repeated get_state usages to storing the actual entitiy as a variable and getting state from that
        # useful attributes: friendly_name, last_changed/_seconds, entity_name, domain, entity_id

    @property
    def door_open_delay(self) -> float:
        """Seconds to delay before registering a door as open."""
        return self.__door_open_delay

    @door_open_delay.setter
    def door_open_delay(self, seconds: float) -> float:
        """Set the number of seconds to delay before registering a door as open."""
        if self.__door_open_delay != seconds:
            self.__door_open_delay = seconds
            self.check_conditions_and_adjust()

    def handle_door_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ) -> None:
        """If the kitchen door status changes, check if aircon needs to change."""
        del entity, attribute, old, kwargs
        if not self.on or self.control_enabled:
            return
        if (
            new == "on"
            and self.fan_mode == "auto"
            and abs(self.room_temperature - self.target_temperature)
            > self.controller.args["aircon_reduce_fan_temperature_threshold"]
        ):
            self.fan_mode = "low"
            # TODO: set timer to turn aircon off
        else:
            self.fan_mode = self.preferred_fan_mode

    def call_service(self, service: str, **parameters: dict):
        """Call one of the device's services in Home Assistant and wait for response."""
        super().call_service(service, **parameters, return_result=True)

    def handle_user_adjustment(self, user: str):
        """"""
        if self.on:
            self.turn_on_for_current_conditions()
        super().handle_user_adjustment(user)


class Fan(ClimateDevice, PresenceDevice):
    """Control a fan and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
        companion_device: ClimateDevice | None = None,
    ):
        """Initialise with a fan's id, room(s), speed, direction, and controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            control_input_boolean_suffix="_fan",
            room=room,
            linked_rooms=linked_rooms,
        )
        self.speed_per_level = self.get_attribute("percentage_step")
        self.speed_levels = round(100 / self.speed_per_level)
        self.companion_device = companion_device
        self.vacating_delay = 60 * float(
            controller.entities.input_number.fan_vacating_delay.state,
        )
        self.adjustment_delay = controller.args["fan_adjustment_delay"]

    @property
    def speed(self) -> bool:
        """Get the fan's speed if it was on (100 is full speed)."""
        return self.get_attribute("percentage")

    @speed.setter
    def speed(self, new_speed: int):
        """Set the fan's speed (0 is off, 100 is full speed)?"""
        if self.speed == new_speed or (new_speed == 0 and not self.on):
            return
        if self.on:
            if new_speed == 0:
                self.turn_off()
            else:
                self.call_service("set_percentage", percentage=new_speed)
        else:
            self.turn_on(percentage=new_speed)

    @property
    def reverse(self) -> bool:
        """True if the fan's spin direction is set to reverse."""
        return self.get_attribute("direction") == "reverse"

    @reverse.setter
    def reverse(self, new_reverse: bool):
        """Set the fan's spin direction (forward or reverse)."""
        if self.reverse != new_reverse:
            self.call_service(
                "set_direction",
                direction="reverse" if new_reverse else "forward",
            )

    def check_conditions_and_adjust(
        self,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Calculate best fan speed for the current conditions and set accordingly."""
        if not self.control_enabled or (
            "Away" in self.controller.control.scene
            and not self.controller.presence.pets_home_alone
        ):
            return False
        speed = 0
        hot = not self.reverse
        if (
            not self.ignoring_vacancy
            and self.vacant
            and not self.within_target_temperatures
        ):
            hot = self.above_target_temperature
            if hot:
                speed = self.speed_per_level * min(
                    self.speed_levels,
                    ceil(
                        self.room_temperature
                        - self.controller.get_setting("cooling_target_temperature"),
                    ),
                )
                speed = 0  # TODO: remove this once the infinite toggle from apparent temperature change is solved
            elif self.companion_device and self.controller.control.scene not in (
                "Sleep",
                "Morning",
            ):
                if self.companion_device.on:
                    speed = self.speed_per_level * 1
        if check_if_would_adjust_only:
            return self.speed != speed or self.reverse == hot
        self.reverse = not hot
        self.speed = speed
        return True
        # TODO: remove all redundant debug logging throughout project, only have where there are complex checks
        # self.controller.log(
        #     f"A desired fan speed of '{speed}' was set in the '{self.room}'",
        #     level="DEBUG",
        # )


class Heater(ClimateDevice, PresenceDevice):
    """Control a heater and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
        safe_when_vacant: bool = False,
    ):
        """Initialise with a heater's id, room(s), and the Climate controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            room=room,
            linked_rooms=linked_rooms,
        )
        self.safe_when_vacant = safe_when_vacant
        self.vacating_delay = (
            60
            * float(self.controller.entities.input_number.heater_vacating_delay.state)
            if safe_when_vacant
            else 0
        )

    @property
    def target_temperature(self) -> float:
        """Get the heater's target temperature."""
        return self.controller.get_setting("heating_target_temperature")

    def turn_on(self):
        """Turn the heater on and adjust the target temperature if possible."""
        if (
            self.device_type == "climate"
            and self.get_attribute("temperature") != self.target_temperature
        ):
            self.call_service("set_temperature", temperature=self.target_temperature)
        super().turn_on()

    def check_conditions_and_adjust(
        self,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Turn the heater on/off based on current and target temperatures."""
        if not self.control_enabled or (
            not self.safe_when_vacant
            and (
                not self.controller.presence.anyone_home
                or "Away" in self.controller.control.scene
            )
        ):
            self.controller.log(
                f"Climate control is off but '{self.room}' heater's ",
                "check_conditions_and_adjust was triggered",
                level="DEBUG",
            )
            return False
        if not self.on:
            if self.room_temperature < self.target_temperature - self.controller.args[
                "target_buffer"
            ] and (self.ignoring_vacancy or not self.vacant):
                if check_if_would_adjust_only:
                    return True
                self.turn_on()
        elif self.room_temperature > 2 * self.target_temperature + self.controller.args[
            "target_buffer"
        ] or (not self.ignoring_vacancy and self.vacant):
            if check_if_would_adjust_only:
                return True
            self.turn_off()
            # TODO: figure out a better way to manage apparent temperature triggering nursery heater off than 2 *
        return False
