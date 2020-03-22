"""Automates airconditioning control.

Monitors climate inside and out, controlling airconditioning units in the house
according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""
from __future__ import annotations

import statistics
from typing import Type

from meteocalc import Temp, heat_index

import app


class Climate(app.App):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.suggested = False
        self.aircon_trigger_timer = None
        self.temperature_monitor = None
        self.aircons = None
        self.climate_control_before_away = None

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.temperature_monitor = TemperatureMonitor(self)
        self.aircons = {
            aircon: Aircon(f"climate.{aircon}", self)
            for aircon in ["bedroom", "living_room", "dining_room"]
        }
        self.climate_control_before_away = self.climate_control
        self.listen_state(self.climate_control_change, "input_boolean.climate_control")
        self.listen_state(self.settings_change, "input_number", duration=3)
        self.listen_state(self.aircon_change, "input_boolean.aircon")
        self.listen_state(self.scene_change, "input_select.scene")
        self.handle_inside_temperature()

    @property
    def climate_control(self) -> bool:
        """Climate control setting is stored in Home Assistant as an input_boolean."""
        return self.get_state("input_boolean.climate_control") == "on"

    @climate_control.setter
    def climate_control(self, new_setting: bool):
        """Call the input_boolean/turn_on/off service to set climate control."""
        if new_setting != self.climate_control:
            self.call_service(
                f"input_boolean/turn_{'on' if new_setting is True else 'off'}",
                entity_id="input_boolean.climate_control",
            )

    def climate_control_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Act on enabling/disabling of climate control by the user or the system."""
        del entity, attribute, old, kwargs
        self.log(f"{'En' if new == 'on' else 'Dis'}abling climate control")
        if new == "on":
            self.temperature_monitor.start_monitoring()
            self.handle_inside_temperature()
        else:
            if self.aircon_trigger_timer is not None:
                self.cancel_timer(self.aircon_trigger_timer)
            self.allow_suggestion()

    def settings_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Act on climate control setting changes made by the user."""
        del attribute, old, kwargs
        self.log(f"Climate control setting '{entity}' changed to {new}")
        if "target_temperature" in entity:
            mode = self.get_state("climate.bedroom")
            if (
                mode == "cool" and entity == "input_number.cooling_target_temperature"
            ) or (
                mode == "heat" and entity == "input_number.heating_target_temperature"
            ):
                if self.aircon is True:
                    self.turn_aircon_on(mode)
        elif "temperature_trigger" in entity:
            if self.aircon_trigger_timer is not None:
                self.cancel_timer(self.aircon_trigger_timer)
            self.temperature_monitor.adjust_current_high_temperature_trigger()
        self.handle_inside_temperature()

    @property
    def aircon(self) -> bool:
        """Aircon setting is stored in Home Assistant as an input_boolean."""
        return self.get_state("input_boolean.aircon") == "on"

    @aircon.setter
    def aircon(self, new_setting: bool):
        """Call the input_boolean/turn_on/off service to set aircon."""
        self.call_service(
            f"input_boolean/turn_{'on' if new_setting is True else 'off'}",
            entity_id="input_boolean.aircon",
        )

    def aircon_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Act on aircon setting changes from user or the system."""
        del entity, attribute, old, kwargs
        self.log(f"Turning aircon {new}")
        if self.aircon_trigger_timer is not None:
            self.cancel_timer(self.aircon_trigger_timer)
        if new == "on":
            self.disable_climate_control_if_would_trigger_off()
            if self.temperature_monitor.is_below_target_temperature():
                mode = "heat"
            elif self.temperature_monitor.is_above_target_temperature():
                mode = "cool"
            else:
                mode = self.temperature_monitor.closer_to_heat_or_cool()
            self.log(
                f"The temperature inside ({self.temperature_monitor.inside_temperature}"
                f" degrees) is {'above' if mode == 'cool' else 'below'} the target ("
                f"{self.get_state(f'input_number.{mode}ing_target_temperature')} degrees)"
            )
            self.turn_aircon_on(mode)
        else:
            self.disable_climate_control_if_would_trigger_on()
            self.turn_aircon_off()

    def scene_change(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ):  # pylint: disable=too-many-arguments #noqa
        """Change temperature triggers and rechecks forecasts if necessary."""
        del entity, attribute, kwargs
        self.temperature_monitor.adjust_current_high_temperature_trigger()
        if "Away" in new and "Away" not in old:
            self.climate_control_before_away = self.climate_control
            self.climate_control = False
            self.aircon = False
        elif "Away" not in new and "Away" in old:
            self.climate_control = self.climate_control_before_away
        if new == "Sleep" or old == "Sleep":
            self.transition_aircon_for_sleep()
        if self.climate_control is False and any(
            new == "Day" and old == "Sleep",
            new == "Night" and old == "Day",
            "Away" not in new and "Away" in old,
        ):
            self.suggest_if_trigger_forecast()
        self.handle_inside_temperature()

    def handle_inside_temperature(self):
        """Control aircon or suggest based on changes in inside temperature."""
        if self.aircon is False:
            if self.temperature_monitor.is_too_hot_or_cold():
                if self.climate_control is True:
                    self.turn_aircon_on_after_delay()
                else:
                    self.suggest_climate_control()
        elif self.temperature_monitor.is_within_target_temperatures():
            message = (
                f"A desirable inside temperature of"
                f" {self.temperature_monitor.inside_temperature}º has been reached,"
            )
            if self.climate_control is True:
                self.aircon = False
                self.notify(
                    f"{message} turning aircon off",
                    title="Aircon",
                    targets="anyone_home" if self.anyone_home() else "all",
                )
            else:
                self.suggest(f"{message} consider enabling climate control")

    def handle_outside_temperature(self):
        """If nicer outside, suggest (if appropriate) to open up the house."""
        if (
            self.climate_control is True
            and self.aircon is True
            and self.temperature_monitor.is_outside_temperature_nicer()
        ):
            self.suggest(
                f"Outside ({self.temperature_monitor.outside_temperature}º) is a more"
                " pleasant temperature than inside ("
                f"{self.temperature_monitor.inside_temperature}º),"
                " consider opening up the house."
            )

    def suggest_if_trigger_forecast(self):
        """Suggest user enables control if extreme's forecast (only if disabled)."""
        self.allow_suggestion()
        forecast = self.temperature_monitor.get_forecast_if_will_trigger()
        if forecast is not None:
            self.suggest(
                f"It's forecast to reach {forecast}º, consider enabling climate control."
            )

    def suggest_climate_control(self):
        """Suggest user enables control (variants for if outside is nicer or not)."""
        if self.temperature_monitor.is_outside_temperature_nicer():
            self.suggest(
                f"Outside ({self.temperature_monitor.outside_temperature}º)"
                " is a more pleasant temperature than inside"
                f"({self.temperature_monitor.inside_temperature})º),"
                " consider opening up the house or enabling climate control."
            )
        else:
            self.suggest(
                f"It's {self.temperature_monitor.inside_temperature}º inside right now,"
                " consider enabling climate control."
            )

    def turn_aircon_on_after_delay(self):
        """Start a timer to turn aircon on soon, and notify user."""
        if self.aircon_trigger_timer is None:
            self.aircon_trigger_timer = self.run_in(
                self.aircon_trigger_timer_up, self.args["aircon_trigger_delay"] * 60,
            )
            self.notify(
                f"Temperature inside is {self.temperature_monitor.inside_temperature}º,"
                f" aircon will be turned on in {self.args['aircon_trigger_delay']}"
                " minutes (unless you disable climate control or change temperature settings)",
                title="Aircon",
                targets="anyone_home" if self.anyone_home() else "all",
            )

    def aircon_trigger_timer_up(self, kwargs: dict):
        """Timer requires kwargs, would otherwise use turn_aircon_on."""
        del kwargs
        self.aircon = True

    def turn_aircon_on(self, mode: str):
        """Turn aircon on, handling sleep scene."""
        if self.scene == "Sleep":
            self.aircons["bedroom"].turn_on(mode)
            self.aircons["bedroom"].set_fan_mode("low")
        else:
            for aircon in self.aircons.keys():
                self.aircons[aircon].turn_on(mode)
                self.aircons[aircon].set_fan_mode("auto")
        self.log(f"Turned aircon on to '{mode}' mode")
        self.allow_suggestion()

    def turn_aircon_off(self):
        """Turn all aircon units off and allow suggestions again."""
        self.log("Turning aircon off")
        for aircon in self.aircons.values():
            aircon.turn_off()
        self.allow_suggestion()

    def transition_aircon_for_sleep(self):
        """Have only bedroom aircon on when in Sleep scene, otherwise all on."""
        if self.climate_control or self.suggested is False:
            self.temperature_monitor.start_monitoring()
        if self.aircon is True:
            if self.scene == "Sleep":
                self.aircons["living_room"].turn_off()
                self.aircons["dining_room"].turn_off()
            else:
                self.turn_aircon_on(self.get_state("climate.bedroom"))

    def disable_climate_control_if_would_trigger_on(self):
        """Disables climate control only if it would immediately trigger aircon on."""
        if (
            self.climate_control is True
            and self.temperature_monitor.is_too_hot_or_cold()
        ):
            self.notify(
                "The current temperature ("
                f"{self.temperature_monitor.inside_temperature}º) will immediately"
                " trigger aircon on again - disabling climate control to prevent this",
                title="Climate Control",
                targets="anyone_home" if self.anyone_home() else "all",
            )
            self.climate_control = False

    def disable_climate_control_if_would_trigger_off(self):
        """Disables climate control only if it would immediately trigger aircon off."""
        if self.temperature_monitor.is_within_target_temperatures():
            self.climate_control = False
            self.notify(
                "Inside is already within the desired temperature range,"
                " climate control is now disabled"
                " (you'll need to manually turn aircon off)",
                title="Climate Control",
                targets="anyone_home" if self.anyone_home() else "all",
            )

    def suggest(self, message: str):
        """Make a suggestion to the users, but only if one has not already been sent."""
        if not self.suggested:
            self.suggested = True
            self.notify(
                message,
                title="Aircon",
                targets="anyone_home" if self.anyone_home() else "all",
            )
            if self.climate_control is False:
                self.temperature_monitor.stop_monitoring()

    def allow_suggestion(self):
        """Allow suggestions to be made again. Use after user events & scene changes."""
        if self.suggested:
            self.suggested = False


class Sensor:
    """Capture temperature and humidity data from generic sensor entities."""

    @staticmethod
    def detect_type_and_create(sensor_id: str, controller: Climate) -> Type["Sensor"]:
        """Detect the sensor type and create an object with the relevant subclass."""
        if "climate" in sensor_id:
            sensor_type = ClimateSensor
        elif "multisensor" in sensor_id:
            sensor_type = MultiSensor
        elif "bom_weather" in sensor_id:
            sensor_type = WeatherSensor
        else:
            sensor_type = Sensor
        return sensor_type(sensor_id, controller)

    def __init__(self, sensor_id: str, monitor: TemperatureMonitor):
        """Initialise sensor with appropriate variables and a monitor to callback to."""
        self.sensor_id = sensor_id
        self.monitor = monitor
        if "bedroom" in self.sensor_id:
            self.location = "bedroom"
        elif "bom_weather" in self.sensor_id:
            self.location = "outside"
        else:
            self.location = "inside"
        self.measures = {"temperature": None, "humidity": None}
        self.listeners = {"temperature": None, "humidity": None}

    def is_enabled(self) -> bool:
        """Check if the sensor is enabled - matches temperature_listener usage."""
        return self.listeners["temperature"] is not None

    def disable(self):
        """Disable the sensor by cancelling its listeners."""
        for name, listener in self.listeners.items():
            if listener is not None:
                self.monitor.controller.cancel_listen_state(listener)
                self.listeners[name] = None

    def handle_change(
        self, entity: str, attribute: str, old: float, new: float, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Save new value then recalculate inside temperature and handle any change."""
        del entity, attribute, old
        self.measures[kwargs["measure"]] = float(new)
        self.monitor.calculate_and_handle_inside_temperature()


class ClimateSensor(Sensor):
    """Capture temperature and humidity data from climate.x entities."""

    def enable(self):
        """Initialise sensor values and listen for further updates."""
        for name, listener in self.listeners.items():
            self.measures[name] = float(
                self.monitor.controller.get_state(
                    self.sensor_id, attribute=f"current_{name}"
                )
            )
            if listener is None:
                self.listeners[name] = self.monitor.controller.listen_state(
                    self.handle_change,
                    self.sensor_id,
                    attribute=f"current_{name}",
                    measure=name,
                )


class MultiSensor(Sensor):
    """Capture temperature and humidity data from multisensor entities."""

    def enable(self):
        """Initialise sensor values and listen for further updates."""
        for name, listener in self.listeners.items():
            self.measures[name] = float(
                self.monitor.controller.get_state(f"{self.sensor_id}_{name}")
            )
            if listener is None:
                self.listeners[name] = self.monitor.controller.listen_state(
                    self.handle_change, f"{self.sensor_id}_{name}", measure=name,
                )


class WeatherSensor(Sensor):
    """Capture temperature data from bom_weather entities."""

    def __init__(self, sensor_id: str, monitor: TemperatureMonitor):
        """Keep only the temperature listener as methods change monitor's attribute."""
        super().__init__(sensor_id, monitor)
        del (
            self.measures["temperature"],
            self.measures["humidity"],
            self.listeners["humidity"],
        )

    def enable(self):
        """Initialise with current temperature value and listen for further updates."""
        self.monitor.outside_temperature = float(
            self.monitor.controller.get_state(self.sensor_id)
        )
        if self.listeners["temperature"] is None:
            self.listeners["temperature"] = self.monitor.controller.listen_state(
                self.handle_change, self.sensor_id
            )

    def handle_change(
        self, entity: str, attribute: str, old: float, new: float, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Save new value then recalculate inside temperature and handle any change."""
        del entity, attribute, old, kwargs
        self.monitor.outside_temperature = float(new)
        self.monitor.controller.log(
            f"Outside temperature changed to {self.monitor.outside_temperature} degrees"
        )
        self.monitor.controller.handle_outside_temperature()


class TemperatureMonitor:
    """Monitor various sensors to provide temperatures in & out, and check triggers."""

    def __init__(self, controller: Climate):
        """Initialise with Climate controller and create sensor objects."""
        self.controller = controller
        self.inside_temperature = self.controller.get_state(
            "climate.bedroom", attribute=f"current_temperature"
        )
        self.current_max_temperature_trigger = float(
            self.controller.get_state("input_number.day_high_temperature_trigger")
        )
        self.sensors = {
            sensor_id: Sensor.detect_type_and_create(sensor_id, self)
            for sensor_id in [
                "climate.bedroom",
                "climate.living_room",
                "climate.dining_room",
                "sensor.kitchen_multisensor",
                "sensor.multisensor_2",
                "sensor.bom_weather_feels_like_c",
            ]
        }
        self.last_inside_temperature = None
        self.outside_temperature = None
        self.start_monitoring()

    @property
    def inside_temperature(self) -> float:
        """Get the calculated inside temperature as saved to Home Assistant."""
        return float(self.controller.get_state("sensor.apparent_inside_temperature"))

    @inside_temperature.setter
    def inside_temperature(self, temperature: float):
        """Store the calculated inside temperature in Home Assistant."""
        self.controller.set_state(
            "sensor.apparent_inside_temperature", state=temperature
        )

    def start_monitoring(self):
        """Get values from appropriate sensors and calculate inside temperature."""
        for sensor in self.sensors.values():
            if (
                self.controller.scene != "sleep"
                or sensor.location == "outside"
                or (self.controller.scene == "sleep" and sensor.location == "bedroom")
            ):
                sensor.enable()
            else:
                sensor.disable()
        self.calculate_inside_temperature()

    def stop_monitoring(self):
        """Stop listening for sensor updates."""
        for sensor in self.sensors.values():
            sensor.disable()
        self.controller.log("Stopped monitoring temperatures")

    def calculate_inside_temperature(self):
        """Use stored sensor values to calculate the 'feels like' temperature inside."""
        self.last_inside_temperature = self.inside_temperature
        temperatures = []
        humidities = []
        for sensor in self.sensors.values():
            if sensor.is_enabled() and sensor.location != "outside":
                temperatures.append(sensor.measures["temperature"])
                humidities.append(sensor.measures["humidity"])
        self.inside_temperature = round(
            heat_index(
                temperature=Temp(statistics.mean(temperatures), "c",),
                humidity=statistics.mean(humidities),
            ).c,
            1,
        )
        if self.last_inside_temperature != self.inside_temperature:
            self.controller.log(
                f"Inside temperature calculated as {self.inside_temperature} degrees"
            )

    def calculate_and_handle_inside_temperature(self):
        """Calculate inside temperature then get controller to handle if changed."""
        self.calculate_inside_temperature()
        if self.inside_temperature != self.last_inside_temperature:
            self.controller.handle_inside_temperature()

    def is_within_target_temperatures(self) -> bool:
        """Check if temperature is not above or below target temperatures."""
        return not (
            self.is_above_target_temperature() or self.is_below_target_temperature()
        )

    def is_above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature, with a buffer."""
        return (
            self.inside_temperature
            > float(
                self.controller.get_state("input_number.cooling_target_temperature")
            )
            - self.controller.args["target_trigger_buffer"]
        )

    def is_below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature, with a buffer."""
        return (
            self.inside_temperature
            < float(
                self.controller.get_state("input_number.heating_target_temperature")
            )
            + self.controller.args["target_trigger_buffer"]
        )

    def is_outside_temperature_nicer(self) -> bool:
        """Check if outside is a nicer temperature than inside."""
        mode = self.controller.get_state("climate.bedroom")
        hotter_outside = (
            self.inside_temperature
            < self.outside_temperature - self.controller.args["inside_outside_trigger"]
        )
        colder_outside = (
            self.inside_temperature
            > self.outside_temperature + self.controller.args["inside_outside_trigger"]
        )
        too_hot_or_cold_outside = (
            not float(self.controller.get_state("input_number.low_temperature_trigger"))
            <= self.outside_temperature
            <= self.current_max_temperature_trigger
        )
        vs_str = f"({self.outside_temperature} vs {self.inside_temperature} degrees)"
        if any(
            [
                mode == "heat" and hotter_outside,
                mode == "cool" and colder_outside,
                mode == "off"
                and (self.is_too_hot_or_cold() and not too_hot_or_cold_outside),
            ]
        ):
            self.controller.log(f"Outside temperature is nicer than inside {vs_str}")
            return True
        self.controller.log(f"Outside temperature is not better than inside {vs_str}")
        return False

    def is_too_hot_or_cold(self) -> bool:
        """Check if temperature inside is above or below the max/min triggers."""
        if (
            float(self.controller.get_state("input_number.low_temperature_trigger"))
            < self.inside_temperature
            < self.current_max_temperature_trigger
        ):
            return False
        self.controller.log(
            f"It's too hot or cold inside ({self.inside_temperature} degrees)"
        )
        return True

    def closer_to_heat_or_cool(self) -> str:
        """Return if temperature inside is closer to needing heating or cooling."""
        if (
            self.inside_temperature
            > (
                float(
                    self.controller.get_state("input_number.cooling_target_temperature")
                )
                + float(
                    self.controller.get_state("input_number.heating_target_temperature")
                )
            )
            / 2
        ):
            return "cool"
        return "heat"

    def get_forecast_if_will_trigger(self) -> float:
        """Return the forecasted temperature if it exceeds thresholds."""
        forecasts = [
            float(
                self.controller.get_state(
                    f"sensor.dark_sky_apparent_temperature_{hour}h"
                )
            )
            for hour in ["2", "4", "6", "8"]
        ]
        max_forecast = max(forecasts)
        if max_forecast >= self.current_max_temperature_trigger:
            return max_forecast
        min_forecast = min(forecasts)
        if min_forecast <= float(
            self.controller.get_state("input_number.low_temperature_trigger")
        ):
            return min_forecast
        return None

    def adjust_current_high_temperature_trigger(self):
        """Adjust the current max temperature trigger based on the current scene."""
        self.current_max_temperature_trigger = float(
            self.controller.get_state(
                f"input_number.{'day' if 'Day' in self.controller.scene else 'night'}"
                "_high_temperature_trigger"
            )
        )


class Aircon:
    """Control a specific aircon unit."""

    def __init__(self, aircon_id: str, controller: Climate):
        """Initialise with an aircon's entity_id and the Climate controller."""
        self.aircon_id = aircon_id
        self.controller = controller

    def turn_on(self, mode: str):
        """Turn on the aircon unit with the specified mode and configured temperature."""
        if mode == "cool":
            target_temperature = float(
                self.controller.get_state("input_number.cooling_target_temperature")
            )
        elif mode == "heat":
            target_temperature = float(
                self.controller.get_state("input_number.heating_target_temperature")
            )
        if self.controller.get_state(self.aircon_id) != mode:
            self.controller.call_service(
                "climate/set_hvac_mode", entity_id=self.aircon_id, hvac_mode=mode
            )
        if (
            self.controller.get_state(self.aircon_id, attribute="temperature")
            != target_temperature
        ):
            self.controller.call_service(
                "climate/set_temperature",
                entity_id=self.aircon_id,
                temperature=target_temperature,
            )

    def turn_off(self):
        """Turn off the aircon unit if it's on."""
        if self.controller.get_state(self.aircon_id) != "off":
            self.controller.call_service("climate/turn_off", entity_id=self.aircon_id)

    def set_fan_mode(self, fan_mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')."""
        if self.controller.get_state(self.aircon_id, attribute="fan_mode") != fan_mode:
            self.controller.call_service(
                "climate/set_fan_mode", entity_id=self.aircon_id, fan_mode=fan_mode
            )
