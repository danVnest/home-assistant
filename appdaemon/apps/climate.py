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

# from math import ceil
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
        self.humidifiers: dict[str, Humidifier] = {}

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
                doors=["bedroom_balcony", "kitchen", "dining_room_balcony"],
            ),
            "living_room": Aircon(
                device_id="climate.living_room_aircon",
                controller=self,
                room="living_room",
                linked_rooms=["dining_room", "kitchen"],
                doors=["kitchen", "dining_room_balcony", "bedroom_balcony"],
            ),
            "dining_room": Aircon(
                device_id="climate.dining_room_aircon",
                controller=self,
                room="dining_room",
                linked_rooms=["living_room", "kitchen"],
                doors=["kitchen", "dining_room_balcony", "bedroom_balcony"],
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
        self.humidifiers = {
            "nursery": Humidifier(
                device_id="humidifier.nursery",
                controller=self,
                room="nursery",
            ),
            "bedroom": Humidifier(
                device_id="humidifier.bedroom",
                controller=self,
                room="bedroom",
            ),
        }
        for device_group in (self.aircons, self.fans, self.heaters, self.humidifiers):
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
            self.adjust_for_conditions()
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
                aircon.turn_on_for_conditions()

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

    def update_humidifier_vacating_delays(self, seconds: float):
        """Update room vacating delay for each humidifier."""
        for humidifier in self.humidifiers.values():
            humidifier.vacating_delay = seconds

    def transition_between_scenes(self, new_scene: str, old_scene: str):
        """Adjust aircon & temperature triggers, suggest climate control if suitable."""
        if "Away" in new_scene:
            if not self.presence.pets_home_alone:
                for device_group in (self.aircons, self.fans):
                    for device in device_group.values():
                        device.turn_off()
            for device_group in (self.heaters, self.humidifiers):
                for device in device_group.values():
                    device.turn_off()
        if new_scene == "Sleep":
            self.end_pre_condition_for_sleep()
        elif new_scene == "Morning":
            self.heaters["nursery"].monitor_presence()
            self.humidifiers["nursery"].monitor_presence()
            # TODO: remove when nursery presence is reliable
            self.aircons["dining_room"].monitor_presence()
            # TODO: remove dining room once presence detects dogs properly?
        self.aircons["bedroom"].preferred_fan_mode = (
            "low" if new_scene in ("Sleep", "Morning") else "auto"
        )
        for humidifier in self.humidifiers.values():
            humidifier.already_notified_of_empty_water_tank = False
        if self.any_climate_control_enabled:
            self.adjust_for_conditions()
        elif any(
            (
                new_scene == "Day" and old_scene in ["Sleep", "Morning"],
                new_scene == "Night" and old_scene == "Day",
                "Away" not in new_scene and "Away" in old_scene,
                self.presence.pets_home_alone,
            ),
        ):  # TODO: consider simplifying and removing old_scene?
            self.suggest_if_extreme_forecast()

    def adjust_for_conditions(self):
        """Control aircon or suggest based on changes in inside temperature."""
        """Handle each case (house open, outside nicer, climate control status)?"""
        for device_group in (self.aircons, self.fans, self.heaters, self.humidifiers):
            for device in device_group.values():
                device.adjust_for_conditions()
        if (
            self.presence.pets_home_alone
            and not self.any_aircon_on
            and self.too_hot_or_cold
        ):
            if all(aircon.door_open for aircon in self.aircons.values()):
                reason = "the door(s) are open"
                if self.too_hot_or_cold_outside:
                    reason += f" (but outside is {self.outside_temperature}º)"
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

    def pre_condition_nursery(self):
        """Pre-heat/humidify the nursery for nice sleeping conditions."""
        for device in (
            self.heaters["nursery"],
            self.humidifiers["nursery"],
        ):
            device.ignore_vacancy()
            device.adjust_for_conditions()

    def pre_condition_for_sleep(self):
        """Pre-cool/heat/humidify bedroom/dog bed area for nice sleeping conditions."""
        for device in (
            self.aircons["bedroom"],
            self.humidifiers["bedroom"],
            self.aircons["dining_room"],
        ):
            # TODO: remove dining room once presence detects dogs properly?
            device.ignore_vacancy()
            device.adjust_for_conditions()

    def end_pre_condition_for_sleep(self):
        """Return bedroom and nursery climate control to normal."""
        self.aircons["bedroom"].monitor_presence()
        self.humidifiers["bedroom"].monitor_presence()
        # self.aircons["dining_room"].monitor_presence() # TODO: ?
        # self.heaters["nursery"].monitor_presence() # TODO: uncomment when nursery presence is reliable
        # self.humidifiers["nursery"].monitor_presence() # TODO: uncomment when nursery presence is reliable

    def suggest_if_extreme_forecast(self):
        """Suggest user enables control if extreme temperatures are forecast."""
        self.allow_suggestion()
        forecast = self.get_state("sensor.extreme_forecast")
        if forecast:
            self.suggest(
                f"It's forecast to reach {float(forecast):.1f}º, "
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
        self.adjust_for_conditions()

    def terminate(self):
        """Cancel presence callbacks before termination????

        Appdaemon defined function called before termination.
        """
        for device_group in (self.aircons, self.fans, self.heaters, self.humidifiers):
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
            self.adjust_for_conditions()

    @property
    def within_target_temperatures(self) -> bool:
        """Check if temperature is not above or below target temperatures."""
        return not (self.above_target_temperature or self.below_target_temperature)

    @property
    def above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature."""
        return self.inside_temperature > self.get_setting("cooling_target_temperature")

    @property
    def below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature."""
        return self.inside_temperature < self.get_setting("heating_target_temperature")

    @property
    def hotter_outside(self) -> bool:
        """Check if temperature is higher outside than inside."""
        return (
            self.inside_temperature
            < self.outside_temperature - self.constants["inside_outside_trigger"]
        )

    @property
    def colder_outside(self) -> bool:
        """Check if temperature is lower outside than inside."""
        return (
            self.inside_temperature
            > self.outside_temperature + self.constants["inside_outside_trigger"]
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


class ClimateDevice(Device):
    """Climate device that can be configured to respond to environmental changes?"""

    def __init__(
        self,
        monitor_temperature: bool = True,
        monitor_humidity: bool = False,
        **kwargs: dict,
    ):
        """Initialise with device parameters and prepare for presence adjustments?"""
        super().__init__(
            **kwargs,
        )
        self.temperature_sensors = []
        self.humidity_sensors = []
        for room in (self.room, *self.linked_rooms):
            temperature_sensor_id = f"sensor.{room}_apparent_temperature"
            humidity_sensor_id = f"sensor.{room}_humidity"
            self.temperature_sensors.append(
                self.controller.get_entity(temperature_sensor_id),
            )
            self.humidity_sensors.append(
                self.controller.get_entity(humidity_sensor_id),
            )
            if monitor_temperature:
                self.controller.listen_state(
                    self.handle_sensor_change,
                    temperature_sensor_id,
                    constrain_input_boolean=self.control_input_boolean,
                )
            if monitor_humidity:
                self.controller.listen_state(
                    self.handle_sensor_change,
                    humidity_sensor_id,
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

    @property
    def room_humidity(self) -> float:
        """"""
        return sum(
            float(humidity_sensor.state) for humidity_sensor in self.humidity_sensors
        ) / len(self.humidity_sensors)

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
            - self.constants["inside_outside_trigger"]
        )

    @property
    def colder_outside(self) -> bool:
        """Check if temperature is lower outside than inside."""
        return (
            self.room_temperature
            > self.controller.outside_temperature
            + self.constants["inside_outside_trigger"]
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

    def handle_sensor_change(
        self,
        entity: str,
        attribute: str,
        old: float,
        new: float,
        **kwargs: dict,
    ):
        """Adjust for new conditions with delay if appropriate."""
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
                self.adjust_for_conditions_after_delay,
                self.last_adjustment_time
                + self.adjustment_delay
                - self.controller.get_now_ts(),
            )
        else:
            if (
                self.adjustment_timer
            ):  # TODO: this should be moved inside super adjust_for_conditions
                self.controller.cancel_timer(self.adjustment_timer)
                self.adjustment_timer = None
            self.adjust_for_conditions()

    def adjust_for_conditions_after_delay(self, **kwargs: dict):
        """Delayed adjustment from timers initiated when handling presence change."""
        del kwargs
        self.adjustment_timer = None
        self.adjust_for_conditions()


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
        self.preferred_swing_mode = (
            "both" if "both" in self.get_attribute("swing_modes") else "rangefull"
        )
        # TODO: set adjustment_delay?
        self.turn_off_timer_handle = None
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
            )
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="on",
                duration=self.constants["aircon_reduce_fan_delay"],
            )
        self.__door_open_delay = None
        self.door_open_delay = 60 * float(
            controller.entities.input_number.aircon_door_check_delay.state,
        )
        self.user_adjusted_on_time_threshold = 1

    @property
    def best_mode_for_conditions(self) -> str:
        """?"""
        return (
            "cool"
            if self.above_target_temperature or self.closer_to_hot_than_cold
            else "heat"
        )

    @property
    def target_temperature(self) -> float:
        """"""
        return self.get_attribute("temperature") if self.on else self.room_temperature

    @property
    def desired_target_temperature(self) -> float:
        """"""
        mode = self.best_mode_for_conditions
        return self.controller.get_setting(
            mode + "ing_target_temperature",
        ) + self.constants["temperature_target_buffer"] * (
            1 if mode == "heat" else -1
        )  # TODO: potentially remove temperature_target_buffer?

    @property
    def fan_mode(self) -> str:
        """Get the aircon's current fan mode (main options: 'low', 'auto')."""
        return self.get_attribute("fan_mode")

    @fan_mode.setter
    def fan_mode(self, mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')."""
        if self.on and self.fan_mode != mode:
            self.call_service("set_fan_mode", fan_mode=mode)

    @property
    def swing_mode(self) -> str:
        """Get the aircon's current swing mode (main options: 'rangefull', 'both')."""
        return self.get_attribute("swing_mode")

    def turn_on_for_conditions(self) -> bool:
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
        self.controller.allow_suggestion()
        # TODO: add a temperature buffer on min/max trigger and targets in pet mode for efficiency !! IMPORTANT
        # What about cooling? Fans? Take into account pet presence in each room?
        # Bayesian probably can do a good job of detecting pet presence, and/or increase vacating delay

    def turn_off_after_delay(self, **kwargs: dict):
        """Turn aircon off after the required delay when a door opens."""
        del kwargs
        self.turn_off()

    @property
    def would_turn_on_adjust_for_conditions(self):
        """Check if turn_on_for_conditions would actually make any changes."""
        return (
            self.device.state != self.best_mode_for_conditions
            or self.target_temperature != self.desired_target_temperature
            or self.fan_mode != self.preferred_fan_mode
            or self.swing_mode != self.preferred_swing_mode
        )

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Adjust aircon based on current conditions and target temperatures."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        if (
            "Away" in self.controller.control.scene
            and not self.controller.presence.pets_home_alone
        ):
            if check_if_would_adjust_only:
                return self.on
            self.turn_off()
            return None
        # TODO: reset suggestions more often? on any climate UI changes?
        if not self.on:
            if (
                self.too_hot_or_cold
                and (self.ignoring_vacancy or not self.vacant)
                and not self.door_open
            ):
                if check_if_would_adjust_only:
                    return True
                self.turn_on_for_conditions()
                self.notify_if_turning_on_for_pets()
        elif (
            self.door_open
            or self.within_target_temperatures
            or (not self.ignoring_vacancy and self.vacant)
        ):
            if check_if_would_adjust_only:
                return True
            self.turn_off()
        else:
            if check_if_would_adjust_only:
                return self.on and self.would_turn_on_adjust_for_conditions
            if self.on:
                self.turn_on_for_conditions()
            self.suggest_if_temperature_outside_nicer()
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
            self.adjust_for_conditions()

    def handle_door_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ) -> None:
        """If the kitchen door status changes, check if aircon needs to change."""
        del attribute, old, kwargs
        self.controller.cancel_timer(self.turn_off_timer_handle)
        if not self.control_enabled:
            return
        if new == "on" and self.on:
            if self.vacating_delay - self.constants["aircon_reduce_fan_delay"] <= 0:
                self.turn_off()
            else:
                self.turn_off_timer_handle = self.controller.run_in(
                    self.turn_off_after_delay,
                    self.vacating_delay - self.constants["aircon_reduce_fan_delay"],
                    constrain_input_boolean=self.control_input_boolean,
                )
                if (
                    entity == self.doors[0].entity_id
                    and self.fan_mode == "auto"
                    and abs(self.room_temperature - self.target_temperature)
                    > self.constants["aircon_reduce_fan_temperature_threshold"]
                ):
                    self.fan_mode = "low"
        elif not self.door_open:
            if self.on:
                self.fan_mode = self.preferred_fan_mode
            else:
                self.adjust_for_conditions()

    def call_service(self, service: str, **parameters: dict):
        """Call one of the device's services in Home Assistant and wait for response."""
        super().call_service(service, **parameters, return_result=True)

    def handle_user_adjustment(self, user: str):
        """"""
        if (
            self.control_enabled
            and self.on
            and self.device.last_changed_seconds < self.user_adjusted_on_time_threshold
        ):
            self.turn_on_for_conditions()
        super().handle_user_adjustment(user)

    def notify_if_turning_on_for_pets(self):
        """Notify if aircon is turning on for the pets."""
        if (
            not self.controller.any_aircon_on
            and self.controller.presence.pets_home_alone
            and not self.controller.presence.anyone_home
        ):
            self.controller.notify(
                f"It is {self.room_temperature}º in the {self.room}, "
                "turning aircon on for the pets",
                title="Climate Control",
            )

    def suggest_if_temperature_outside_nicer(self):
        """Suggest opening up the house if outside is nicer."""
        if self.outside_temperature_nicer and self.controller.presence.anyone_home:
            self.controller.suggest(
                f"Outside ({self.controller.outside_temperature}º) "
                f"is a more pleasant temperature than the {self.room} "
                f"({self.room_temperature}º), consider opening up the house",
            )


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
        self.speed_per_level = round(self.get_attribute("percentage_step"))
        self.speed_levels = round(100 / self.speed_per_level)
        self.minimum_speed = self.speed_per_level * 1
        self.companion_device = companion_device
        self.vacating_delay = 60 * float(
            controller.entities.input_number.fan_vacating_delay.state,
        )
        self.adjustment_delay = self.constants["fan_adjustment_delay"]

    @property
    def speed(self) -> float:
        """Get the fan's speed (0 is off, 100 is full speed)."""
        return self.get_attribute("percentage") if self.on else 0

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
    def desired_speed_if_cooling(self) -> float:
        """"""
        return 0  # TODO: remove this once the infinite toggle from apparent temperature change is solved
        # return self.speed_per_level * max(
        #     min(
        #         self.speed_levels,
        #         ceil(
        #             (self.room_temperature - self.target_temperature)
        #             * self.constants["fan_speed_change_rate"],
        #         ),
        #     ),
        #     1,
        # )

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

    @property
    def target_temperature(self) -> float:
        """Get the fan's target temperature."""
        return self.controller.get_setting("cooling_target_temperature")

    def turn_on_for_conditions(self):
        """"""
        hot = self.closer_to_hot_than_cold
        self.reverse = not hot
        self.speed = self.desired_speed_if_cooling if hot else self.minimum_speed

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Calculate best fan speed for the current conditions and set accordingly."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        speed = 0
        hot = not self.reverse
        if (
            (
                "Away" not in self.controller.control.scene
                or self.controller.presence.pets_home_alone
            )
            and (self.ignoring_vacancy or not self.vacant)
            and not self.within_target_temperatures
        ):
            hot = self.above_target_temperature
            if hot:
                speed = self.desired_speed_if_cooling
            elif (
                self.companion_device
                and self.companion_device.device.state == "heat"
                and self.controller.control.scene
                not in (
                    "Sleep",
                    "Morning",
                )
            ):
                speed = self.minimum_speed
        if check_if_would_adjust_only:
            return self.reverse == hot or self.speed != speed
        self.reverse = not hot
        self.speed = speed
        return None


class Heater(ClimateDevice, PresenceDevice):
    """Control a heater and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
        *,
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
    def desired_target_temperature(self) -> float:
        """Get the heater's target temperature."""
        return self.controller.get_setting("heating_target_temperature")

    @property
    def target_temperature(self) -> float:
        """Get the heater's target temperature."""
        return (
            self.get_attribute("temperature")
            if self.device_type == "climate"
            else self.desired_target_temperature
        )

    @target_temperature.setter
    def target_temperature(self, target: float):
        """Set the heater's target temperature."""
        if self.should_update_target_temperature:
            self.call_service("set_temperature", temperature=target)

    @property
    def should_update_target_temperature(self):
        """"""
        return (
            self.device_type == "climate"
            and self.target_temperature != self.desired_target_temperature
        )

    @property
    def on_when_away_and_not_safe(self):
        """Check if device is on, not safe, and no-one home."""
        return (
            not self.safe_when_vacant
            and self.on
            and (
                not self.controller.presence.anyone_home
                or "Away" in self.controller.control.scene
            )
        )

    def turn_on_for_conditions(self):
        """Turn the heater on and adjust the target temperature if appropriate."""
        self.target_temperature = self.desired_target_temperature
        self.turn_on()

    @property
    def room_too_cold(self) -> bool:
        """"""
        return (
            self.room_temperature
            < self.desired_target_temperature
            - self.constants["temperature_target_buffer"]
        )

    @property
    def room_warm_enough(self) -> bool:
        """"""
        return (
            self.room_temperature
            > 2 * self.desired_target_temperature
            + self.constants["temperature_target_buffer"]
        )
        # TODO: figure out a better way to manage apparent temperature triggering nursery heater off than 2 *

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Turn the heater on/off based on current and target temperatures."""
        if self.on_when_away_and_not_safe:
            if check_if_would_adjust_only:
                return True
            self.turn_off()
        elif self.control_enabled or check_if_would_adjust_only:
            if not self.on:
                if self.room_too_cold and (self.ignoring_vacancy or not self.vacant):
                    if check_if_would_adjust_only:
                        return True
                    self.turn_on_for_conditions()
            elif self.room_warm_enough or (not self.ignoring_vacancy and self.vacant):
                if check_if_would_adjust_only:
                    return True
                self.turn_off()
            elif check_if_would_adjust_only:
                return self.target_temperature != self.desired_target_temperature
            else:
                self.target_temperature = self.desired_target_temperature
        return False

    def handle_user_adjustment(self, user: str):
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

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
    ):
        """Initialise with a humidifier's id, room(s), and the Climate controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            control_input_boolean_suffix="_humidifier",
            monitor_humidity=True,
            monitor_temperature=False,
            room=room,
            linked_rooms=linked_rooms,
        )
        self.vacating_delay = 60 * float(
            self.controller.entities.input_number.humidifier_vacating_delay.state,
        )
        self.controller.listen_state(
            self.handle_empty_water_tank,
            device_id,
            attribute="humidifier.fault",
            new=lambda x: x is not None and x > 0,
            old=0,
        )
        self.controller.listen_state(
            self.handle_empty_water_tank,
            device_id,
            new=lambda x: x != "off",
            old="off",
        )
        self.already_notified_of_empty_water_tank = False

    @property
    def desired_target_humidity(self) -> float:
        """Get the humidifier's target humidity."""
        return float(self.controller.get_state("input_number.humidifier_target"))

    @property
    def target_humidity(self) -> float:
        """Get the humidifier's target humidity."""
        return self.get_attribute("humidity")

    @target_humidity.setter
    def target_humidity(self, target: float):
        """Set the humidifier's target humidity."""
        if self.target_humidity != self.desired_target_humidity:
            self.call_service("set_humidity", humidity=target)

    @property
    def constant_humidity_mode(self) -> bool:
        """Check if the humidifier is set to reach and maintain a constant humidity."""
        return self.get_attribute("mode") == "Constant Humidity"

    def set_constant_humidity_mode(self):
        """Set the humidifier to reach and maintain a constant humidity."""
        if self.on and not self.constant_humidity_mode:
            self.call_service("set_mode", mode="Constant Humidity")

    def turn_on_for_conditions(self):
        """Turn the humidifier on and adjust the target humidity if appropriate."""
        self.set_constant_humidity_mode()
        self.target_humidity = self.desired_target_humidity
        self.turn_on()

    @property
    def empty_water_tank(self) -> bool:
        """Check if the humidifier's water tank is empty."""
        return self.get_attribute("humidifier.fault") > 0

    def handle_empty_water_tank(
        self,
        entity: str,
        attribute: str,
        old: float,
        new: float,
        **kwargs: dict,
    ):
        """Handle empty water tank status by calling notification method."""
        del entity, attribute, old, new, kwargs
        if self.empty_water_tank:
            self.notify_of_empty_water_tank()

    def notify_of_empty_water_tank(self):
        """Notify that the humidifier's water tank is empty (only once per scene)."""
        if not self.already_notified_of_empty_water_tank:
            self.controller.notify(
                f"Refill the {self.room} humidifier water tank so it can turn on",
                title=f"{self.device.friendly_name.title()} Water Tank Empty",
                targets="anyone_home",
            )
            self.already_notified_of_empty_water_tank = True

    @property
    def room_too_dry(self) -> bool:
        """"""
        return (
            self.room_humidity
            < self.desired_target_humidity - self.constants["humidity_target_buffer"]
        )

    @property
    def room_too_humid(self) -> bool:
        """"""
        return (
            self.room_humidity
            > self.desired_target_humidity + self.constants["humidity_target_buffer"]
        )

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Turn the humidifier on/off based on current and target humidities."""
        if not self.control_enabled and not check_if_would_adjust_only:
            return None
        if (
            self.room_too_dry
            and not self.on
            and (
                self.ignoring_vacancy
                or (not self.vacant and self.controller.control.scene == "Sleep")
            )
        ):
            if self.empty_water_tank:
                self.notify_of_empty_water_tank()
                return False
            if check_if_would_adjust_only:
                return True
            self.turn_on_for_conditions()
        elif self.on and (
            self.room_too_humid
            or (
                (self.controller.control.scene != "Sleep" or self.vacant)
                and not self.ignoring_vacancy
            )
        ):
            if check_if_would_adjust_only:
                return True
            self.turn_off()
        elif self.on and (
            not self.constant_humidity_mode
            or self.target_humidity != self.desired_target_humidity
        ):
            if check_if_would_adjust_only:
                return True
            self.set_constant_humidity_mode()
            self.target_humidity = self.desired_target_humidity
        return False
