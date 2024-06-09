"""Automates airconditioning, fan, and heater control.

Monitors climate inside and out, controlling airconditioning units, fans, and
heaters in the house according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""

# TODO: adjust ALL documentation text for all apps
from __future__ import annotations

from math import ceil

from app import App, Device
from presence import PresenceDevice


class Climate(App):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.suggested = False
        self.aircons: dict[str, Aircon] = {}
        self.heaters: dict[str, Heater] = {}
        self.fans: dict[str, Fan] = {}
        self.climate_control_history = {}
        # TODO: listen to all climate UI changes here, disable climate control for that device if it conflicts
        # TODO: consider adding all self.entities.group.climate_control states to history to restore when back from Away
        # TODO: think about Away to/from home logic more
        # TODO: if any climate control changed from UI while away, update history with new value

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.__climate_control_enabled = (
            self.entities.group.climate_control.state == "on"
        )
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                self.climate_control_history[device] = device.climate_control
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
            for room in ("bedroom", "office")
            # TODO: https://app.asana.com/0/1207020279479204/1207033183115368/f
            # for room in ("bedroom", "office", "nursery")
        }
        self.fans["bedroom"].companion_device = self.aircons["bedroom"]
        self.fans["office"].companion_device = self.heaters["office"]
        # self.fans["nursery"].companion_device=self.heaters["nursery"]
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                self.climate_control_history[device] = device.climate_control
                device.monitor_presence()
                device.check_conditions_and_adjust()
        for temperatures in ("weighted_average_inside", "outside"):
            self.listen_state(
                self.handle_temperature_change,
                f"sensor.{temperatures}_apparent_temperature",
            )

    @property
    def climate_control_enabled(self) -> bool:
        """Get climate control setting that has been synced to Home Assistant."""
        return self.__climate_control_enabled

    @climate_control_enabled.setter
    def climate_control_enabled(self, enable: bool):
        """Enable/disable climate control and reflect state in UI."""
        if self.climate_control_enabled == enable:
            return
        self.log(f"'{'En' if enable else 'Dis'}abling' climate control")
        self.__climate_control_enabled = enable
        if enable:
            for device_group in (self.aircons, self.fans, self.heaters):
                for device in device_group.values():
                    device.climate_control = self.climate_control_history[device]
            self.check_conditions_and_adjust()
        else:
            for device_group in (self.aircons, self.fans, self.heaters):
                for device in device_group.values():
                    self.climate_control_history[device] = device.climate_control
                    device.climate_control = False
            self.allow_suggestion()

    @property
    def aircon(self) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        # TODO: consider renaming to all_aircon and creating any_aircon as well (can check self.aircons)
        return self.entities.group.all_aircon.state != "off"

    def turn_aircon(self, state: bool):
        """Turn on/off aircon and sync state to Home Assistant UI."""
        self.log(f"Turning each aircon '{'on' if state else 'off'}' if not already so")
        if state:
            for aircon in self.aircons.values():
                aircon.turn_on_for_current_conditions()
        else:
            for aircon in self.aircons.values():
                aircon.turn_off()

    def get_setting(self, setting_name: str) -> float:
        """Get temperature target and trigger settings, accounting for Sleep scene."""
        if self.control.scene == "Sleep" or self.control.bed_time:
            setting_name = f"sleep_{setting_name}"
        return float(self.get_state(f"input_number.{setting_name}"))

    def adjust_temperature_targets_and_triggers(self):
        """Reset climate control using latest settings?"""
        self.check_conditions_and_adjust()
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.adjust_temperature_targets_and_triggers()
        # TODO: is this needed now?? check where it's called

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
            if not self.control.apps["presence"].pets_home_alone:
                self.climate_control_enabled = False
                for device_group in (self.aircons, self.fans):
                    for device in device_group.values():
                        device.turn_off()
            else:
                self.climate_control_enabled = True
            for heater in self.heaters.values():
                heater.turn_off()
        else:
            self.climate_control_enabled = True
        if new_scene == "Sleep":
            self.end_pre_condition_bedrooms()
        elif new_scene == "Morning":
            self.heaters[
                "nursery"
            ].monitor_presence()  # TODO: remove when nursery presence is reliable
        self.aircons["bedroom"].preferred_fan_mode = (
            "low" if new_scene in ("Sleep", "Morning") else "auto"
        )
        if self.climate_control_enabled:
            self.check_conditions_and_adjust()
        elif not self.climate_control_enabled and any(
            [
                new_scene == "Day" and old_scene in ["Sleep", "Morning"],
                new_scene == "Night" and old_scene == "Day",
                "Away" not in new_scene and "Away" in old_scene,
                self.control.apps["presence"].pets_home_alone,
            ],
        ):  # TODO: consider simplifying and removing old_scene?
            self.suggest_if_extreme_forecast()

    def check_conditions_and_adjust(self):
        """Control aircon or suggest based on changes in inside temperature."""
        """Handle each case (house open, outside nicer, climate control status)?"""
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.check_conditions_and_adjust()
        if self.control.apps["presence"].pets_home_alone and not self.aircon:
            # TODO: notify if no aircon can turn on because of the following:
            # IF (bedroom door closed AND no pet or human presence detected)
            # AND (kitchen OR balcony doors are open)
            # AND climate control enabled for each device?
            # TODO: but don't notify if kitchen or balcony door is open and outdoor temperature is ok
            # TODO: suggest user turns on aircon manually if they are concerned
            pass

    def pre_condition_bedrooms(self):
        """Pre-cool/heat the bedroom and nursery for nice sleeping conditions."""
        for device in (self.aircons["bedroom"], self.heaters["nursery"]):
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

    def disable_climate_control_if_would_trigger_on(self):
        """Disables climate control only if it would immediately trigger aircon on."""
        # TODO: This needs an overhaul
        if self.climate_control and self.too_hot_or_cold:
            self.climate_control_enabled = False
            self.climate_control_history["overridden"] = True
            self.notify(
                "The current temperature ("
                f"{self.inside_temperature}º) will immediately "
                "trigger aircon on again - "
                "climate control is now disabled to prevent this",
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def disable_climate_control_if_would_trigger_off(self):
        """Disables climate control only if it would immediately trigger aircon off."""
        # TODO: This needs an overhaul
        if self.climate_control and self.within_target_temperatures:
            self.climate_control_enabled = False
            self.climate_control_history["overridden"] = True
            self.notify(
                "Inside is already within the desired temperature range,"
                " climate control is now disabled"
                " (you'll need to manually turn aircon off)",
                title="Climate Control",
                targets="anyone_home_else_all",
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
        self.adjust_temperature_targets_and_triggers()

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

    # TODO: simpler to just make this a PresenceDevice than do multiple inheritance?

    def __init__(
        self,
        **kwargs: dict,
    ):
        """Initialise with device parameters and prepare for presence adjustments?"""
        super().__init__(**kwargs)
        self.climate_control_id = (
            f"input_boolean.climate_control_{self.device_id.split('.')[1]}"
        )
        if self.device_type == "fan":
            self.climate_control_id += "_fan"
        self.control_input_boolean = self.climate_control_id
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
    def climate_control(self) -> bool:
        """"""
        return self.controller.get_state(self.climate_control_id) == "on"

    @climate_control.setter
    def climate_control(self, enabled: bool):
        """"""
        if self.climate_control != enabled:
            self.controller.call_service(
                f"input_boolean/turn_{'on' if enabled else 'off'}",
                entity_id=self.climate_control_id,
            )
            # TODO: does this even occur? maybe if we need to disable due to UI changes
            # TODO: anything else than needs updating?
        if enabled:
            self.check_conditions_and_adjust()

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

    def check_conditions_and_adjust(self):
        """Override this in child class to adjust device settings appropriately."""

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
        doors: list[str] = (),
    ):
        """Initialise with an aircon's id, room(s), and the Climate controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            room=room,
            linked_rooms=linked_rooms,
        )
        self.__preferred_fan_mode = self.fan_mode
        # TODO: set adjustment_delay?
        self.vacating_delay = 60 * float(
            controller.entities.input_number.aircon_vacating_delay.state,
        )
        self.doors = []
        for door in doors:
            door_id = temperature_sensor_id = f"binary_sensor.{door}_door"
            self.doors.append(self.controller.get_entity(temperature_sensor_id))
            self.controller.listen_state(
                self.handle_door_change,
                door_id,
                new="off",
                constrain_input_boolean=self.control_input_boolean,
            )
        self.door_listeners = []
        self.__door_open_delay = None
        self.door_open_delay = 60 * float(
            controller.entities.input_number.aircon_door_check_delay.state,
        )

    @property
    def fan_mode(self) -> str:
        """Set the fan mode to the specified level (main options: 'low', 'auto')?"""
        return self.get_attribute("fan_mode")

    @property
    def preferred_fan_mode(self) -> str:
        """Set the fan mode to the specified level (main options: 'low', 'auto')?"""
        return self.__preferred_fan_mode

    @preferred_fan_mode.setter
    def preferred_fan_mode(self, fan_mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')?"""
        self.__preferred_fan_mode = fan_mode
        if self.on and self.fan_mode != fan_mode:
            self.call_service("set_fan_mode", fan_mode=fan_mode)

    def turn_on_for_current_conditions(self):
        """Set the aircon unit to heat or cool at desired settings."""
        mode = (
            "cool"
            if self.above_target_temperature or self.closer_to_hot_than_cold
            else "heat"
        )
        if self.device.state != mode:
            self.call_service("set_hvac_mode", hvac_mode=mode)
        target_temperature = self.controller.get_setting(
            mode + "ing_target_temperature",
        ) + self.controller.args["target_buffer"] * (
            1 if mode == "heat" else -1
        )  # TODO: potentially remove target_buffer?
        if self.get_attribute("temperature") != target_temperature:
            self.call_service("set_temperature", temperature=target_temperature)
        if self.fan_mode != self.preferred_fan_mode:
            self.call_service("set_fan_mode", fan_mode=self.preferred_fan_mode)
        self.controller.allow_suggestion()
        # TODO: add a temperature buffer on min/max trigger and targets in pet mode for efficiency !! IMPORTANT
        # What about cooling? Fans? Take into account pet presence in each room?
        # Bayesian probably can do a good job of detecting pet presence, and/or increase vacating delay

    def check_conditions_and_adjust(self):
        """Adjust aircon based on current conditions and target temperatures."""
        if not self.climate_control:
            return  # TODO: I don't think this guard clause is necessary now that control_input_boolean are used
        # TODO: reset suggestions more often? on any climate UI changes?
        if not self.on:
            if self.too_hot_or_cold and (self.ignoring_vacancy or not self.vacant):
                if not self.door_open:
                    if (
                        self.controller.entities.group.all_aircon.state == "off"
                        and self.controller.control.apps["presence"].pets_home_alone
                        and not self.controller.control.apps["presence"].anyone_home
                    ):
                        self.controller.notify(
                            f"It is {self.room_temperature}º in the {self.room}, "
                            "turning aircon on for the pets",
                            title="Climate Control",
                        )
                    self.turn_on_for_current_conditions()
                elif (
                    not self.outside_temperature_nicer
                    and self.controller.control.apps["presence"].anyone_home
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
            self.turn_off()
        elif (
            self.outside_temperature_nicer
            and self.controller.control.apps["presence"].anyone_home
        ):
            self.controller.suggest(
                f"Outside ({self.room_temperature}º) "
                f"is a more pleasant temperature than the {self.room} "
                f"({self.room_temperature}º), consider opening up the house",
            )

    @property
    def door_open(self) -> bool:
        """Check if any doors are open."""
        return any(door.state == "on" for door in self.doors)
        # TODO: maybe change all repeated get_state usages to storing the actual entitiy as a variable and getting state from that

    @property
    def door_open_delay(self) -> float:
        """Seconds to delay before registering a door as open."""
        return self.__door_open_delay

    @door_open_delay.setter
    def door_open_delay(self, seconds: float) -> float:
        """Set the number of seconds to delay before registering a door as open."""
        if self.__door_open_delay != seconds:
            self.__door_open_delay = seconds
            if self.door_listeners:
                for listener in self.door_listeners:
                    self.controller.cancel_listen_state(listener)
                self.door_listeners = []
            if seconds > 0:
                for door in self.doors:
                    self.door_listeners.append(
                        self.controller.listen_state(
                            self.handle_door_change,
                            door.entity_id,
                            new="on",
                            duration=seconds,
                        ),
                    )
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
        del entity, attribute, old, new, kwargs
        self.check_conditions_and_adjust()

    def call_service(self, service: str, **parameters: dict):
        """Call one of the device's services in Home Assistant and wait for response."""
        super().call_service(service, **parameters, return_result=True)


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

    def check_conditions_and_adjust(self):
        """Calculate best fan speed for the current conditions and set accordingly."""
        if not self.climate_control:
            return
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
        self.reverse = not hot
        self.speed = speed
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
    def climate_control(self):
        """"""
        return ClimateDevice.climate_control.fget(self)

    @climate_control.setter
    def climate_control(self, enabled: bool):
        """"""
        if (
            enabled
            and not self.safe_when_vacant
            and not self.controller.control.apps["presence"].anyone_home
        ):
            enabled = False
        ClimateDevice.climate_control.fset(self, enabled)

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
        super().turn_on()  # TODO: this could require Device.turn_on(), test and find out

    def check_conditions_and_adjust(self):
        """Turn the heater on/off based on current and target temperatures."""
        if not self.climate_control:
            return  # TODO: all these guard clauses might be redundant now we have control_input_boolean
        if not self.on:
            if self.room_temperature < self.target_temperature - self.controller.args[
                "target_buffer"
            ] and (self.ignoring_vacancy or not self.vacant):
                self.turn_on()
        elif self.room_temperature > 2 * self.target_temperature + self.controller.args[
            "target_buffer"
        ] or (not self.ignoring_vacancy and self.vacant):
            self.turn_off()
            # TODO: figure out a better way to manage apparent temperature triggering nursery heater off than 2 *
