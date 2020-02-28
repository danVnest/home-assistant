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

import appdaemon.plugins.hass.hassapi as hass
from meteocalc import Temp, heat_index


class Climate(hass.Hass):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        self.climate_control_enabled = True
        self.suggested = False
        self.scene = None
        self.aircon_trigger_timer = None
        self.temperature_monitor = None
        self.aircons = None
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        self.temperature_monitor = TemperatureMonitor(self)
        self.aircons = {
            aircon: Aircon(f"climate.{aircon}", self)
            for aircon in ["bedroom", "living_room", "dining_room"]
        }
        self.listen_event(self.climate_control_change, "CLIMATE")
        self.listen_event(self.scene_change, "SCENE")
        self.handle_inside_temperature()

    def climate_control_change(self, event_name: str, data: dict, kwargs: dict):
        """Control climate based on commands sent via users."""
        del event_name, kwargs
        self.log(f"Climate control command: '{data['command']}' received")
        self.allow_suggestion()
        if data["command"] == "enabled":
            self.enable_climate_control()
        elif data["command"] == "disabled":
            self.disable_climate_control()
        elif data["command"] == "on":
            self.disable_climate_control_if_would_trigger_off()
            if self.temperature_monitor.is_below_target_temperature():
                self.turn_aircon_on("heat")
            elif self.temperature_monitor.is_above_target_temperature():
                self.turn_aircon_on("cool")
            else:
                self.turn_aircon_on(self.temperature_monitor.closer_to_heat_or_cool())
        elif data["command"] == "off":
            self.disable_climate_control_if_would_trigger_on()
            self.turn_aircon_off()

    def scene_change(self, event_name: str, data: dict, kwargs: dict):
        """Change temperature triggers and rechecks forecasts if necessary."""
        del event_name, kwargs
        self.scene = data["scene"]
        self.allow_suggestion()
        if self.scene == "day":
            self.temperature_monitor.current_max_temperature_trigger = self.args[
                "max_day_temperature_trigger"
            ]
            self.suggest_climate_control_if_disabled_and_forecast()
        elif self.scene == "night" or self.scene == "tv" or self.scene == "sleep":
            self.temperature_monitor.current_max_temperature_trigger = self.args[
                "max_night_temperature_trigger"
            ]
            if self.scene == "sleep":
                self.aircons["living_room"].turn_off()
                self.aircons["dining_room"].turn_off()
            self.suggest_climate_control_if_disabled_and_forecast()
        elif self.scene == "away_day" or self.scene == "away_night":
            self.disable_climate_control()
            self.turn_aircon_off()
        self.handle_inside_temperature()

    def handle_inside_temperature(self):
        """Control aircon or suggest based on changes in inside temperature."""
        if self.get_state("climate.bedroom") == "off":
            mode = None
            if self.temperature_monitor.is_too_cold():
                mode = "heat"
            elif self.temperature_monitor.is_too_hot():
                mode = "cool"
            if mode:
                if self.climate_control_enabled:
                    self.turn_aircon_on_after_delay(mode)
                else:
                    self.suggest_climate_control()
        elif self.temperature_monitor.is_within_target_temperatures():
            message = (
                f"A desirable inside temperature of"
                f" {self.temperature_monitor.inside_temperature}º has been reached,"
            )
            if self.climate_control_enabled:
                self.turn_aircon_off()
                self.notify(f"{message} turning aircon off")
            else:
                self.suggest(f"{message} consider enabling climate control")

    def handle_outside_temperature(self):
        """If nicer outside, suggest (if appropriate) to open up the house."""
        if (
            self.climate_control_enabled
            and self.get_state("climate.bedroom") != "off"
            and self.temperature_monitor.is_outside_temperature_nicer()
        ):
            self.suggest(
                f"Outside ({self.temperature_monitor.outside_temperature}º) is a more"
                " pleasant temperature than inside ("
                f"{self.temperature_monitor.inside_temperature}º),"
                " consider opening up the house."
            )

    def suggest_climate_control_if_disabled_and_forecast(self):
        """Suggest user enables control if extreme's forecast (only if disabled)."""
        if not self.climate_control_enabled:
            if self.temperature_monitor.is_maximum_forecast():
                self.suggest(
                    "It's forecast to reach"
                    f" {self.temperature_monitor.forecast['maximum']}"
                    "º, consider enabling climate control."
                )
            if self.temperature_monitor.is_minimum_forecast():
                self.suggest(
                    "It's forecast to reach"
                    f" {self.temperature_monitor.forecast['minimum']}º,"
                    " consider enabling climate control."
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

    def turn_aircon_on_after_delay(self, mode: str):
        """Start a timer to turn aircon on soon, and notify user."""
        if self.aircon_trigger_timer is None:
            self.aircon_trigger_timer = self.run_in(
                self.aircon_trigger_timer_up,
                self.args["aircon_trigger_delay"] * 60,
                mode=mode,
            )
            self.notify(
                f"Temperature inside is {self.temperature_monitor.inside_temperature}º,"
                f" aircon will be turned on in {self.args['aircon_trigger_delay']}"
                " minutes (unless you disable climate control)"
            )

    def aircon_trigger_timer_up(self, kwargs: dict):
        """Timer requires kwargs, would otherwise use turn_aircon_on."""
        self.turn_aircon_on(kwargs["mode"])

    def turn_aircon_on(self, mode: str):
        """Turn aircon on, handling sleep scene."""
        if self.scene == "sleep":
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

    def enable_climate_control(self):
        """Enable climate control and reinitialise temperature_monitor."""
        if not self.climate_control_enabled:
            self.log("Enabling climate control")
            self.climate_control_enabled = True
            self.temperature_monitor.start_monitoring()
            self.handle_inside_temperature()
        else:
            self.log(
                "Attempted to enable climate control when already enabled",
                level="WARNING",
            )

    def disable_climate_control(self):
        """Disables climate control through flag checks elsewhere."""
        self.climate_control_enabled = False
        self.log("Disabled climate control")

    def disable_climate_control_if_would_trigger_on(self):
        """Disables climate control only if it would immediately trigger aircon on."""
        if (
            self.climate_control_enabled
            and self.temperature_monitor.is_too_hot_or_cold()
        ):
            self.notify(
                "The current temperature ("
                f"{self.temperature_monitor.inside_temperature}º) will immediately"
                " trigger aircon on again - disabling climate control to prevent this"
            )
            self.disable_climate_control()

    def disable_climate_control_if_would_trigger_off(self):
        """Disables climate control only if it would immediately trigger aircon off."""
        if self.temperature_monitor.is_within_target_temperatures():
            self.disable_climate_control()
            self.notify(
                "Inside is already within the desired temperature range,"
                " climate control is now disabled"
                " (you'll need to manually turn aircon off)"
            )

    def suggest(self, message: str):
        """Make a suggestion to the users, but only if one has not already been sent."""
        if not self.suggested:
            self.suggested = True
            self.notify(message)
            if not self.climate_control_enabled:
                self.temperature_monitor.stop_monitoring()

    def allow_suggestion(self):
        """Allow suggestions to be made again. Use after user events & scene changes."""
        self.temperature_monitor.start_monitoring()
        if self.suggested:
            self.suggested = False

    def notify(self, message: str, **kwargs):
        """Send a notification to users and log the message."""
        super().notify(message, title="Climate Control")
        self.log(f"NOTIFICATION: {message}")


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
        self.current_max_temperature_trigger = self.controller.args[
            "max_day_temperature_trigger"
        ]
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
        self.inside_temperature = None
        self.last_inside_temperature = None
        self.outside_temperature = None
        self.forecast = {"minimum": None, "maximum": None}
        self.start_monitoring()

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
        if (
            not self.is_above_target_temperature()
            and not self.is_below_target_temperature()
        ):
            return True
        return False

    def is_above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature, with a buffer."""
        if (
            self.inside_temperature
            > self.controller.args["max_target_temperature"]
            - self.controller.args["target_trigger_buffer"]
        ):
            self.controller.log(
                f"The temperature inside ({self.inside_temperature} degrees) is above "
                f"the target ({self.controller.args['max_target_temperature']} degrees)"
            )
            return True
        return False

    def is_below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature, with a buffer."""
        if (
            self.inside_temperature
            < self.controller.args["min_target_temperature"]
            + self.controller.args["target_trigger_buffer"]
        ):
            self.controller.log(
                f"The temperature inside ({self.inside_temperature} degrees) is below "
                f"the target ({self.controller.args['min_target_temperature']} degrees)"
            )
            return True
        return False

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
            self.outside_temperature > self.current_max_temperature_trigger
            or self.outside_temperature
            < self.controller.args["min_temperature_trigger"]
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

    def is_too_cold(self) -> bool:
        """Check if temperature inside is below the minimum trigger."""
        if self.inside_temperature < self.controller.args["min_temperature_trigger"]:
            self.controller.log(
                f"The temperature inside ({self.inside_temperature} degrees) is below"
                " the minimum trigger "
                f"({self.controller.args['min_temperature_trigger']} degrees)"
            )
            return True
        return False

    def is_too_hot(self) -> bool:
        """Check if temperature inside is above the maximum trigger."""
        if self.inside_temperature > self.current_max_temperature_trigger:
            self.controller.log(
                f"The temperature inside ({self.inside_temperature} degrees) is above"
                " the maximum trigger "
                f"({self.current_max_temperature_trigger} degrees)"
            )
            return True
        return False

    def is_too_hot_or_cold(self) -> bool:
        """Check if temperature inside is above or below the max/min triggers."""
        return self.is_too_cold() or self.is_too_hot()

    def closer_to_heat_or_cool(self) -> str:
        """Return if temperature inside is closer to needing heating or cooling."""
        if (
            self.inside_temperature
            > (
                self.controller.args["max_target_temperature"]
                + self.controller.args["min_target_temperature"]
            )
            / 2
        ):
            return "cool"
        return "heat"

    def is_minimum_forecast(self) -> bool:
        """Check if the minimum temperature forecast is below the minimum trigger."""
        forecast = self.controller.get_state("sensor.bom_forecast_min_temp_c_0")
        if forecast != "n/a" and forecast is not None:
            self.forecast["minimum"] = float(forecast)
            if (
                self.forecast["minimum"]
                > self.controller.args["min_temperature_trigger"]
            ):
                return True
        else:
            self.controller.log(
                "Minimum temperature forecast is 'n/a' or None", level="WARNING"
            )
        return False

    def is_maximum_forecast(self) -> bool:
        """Check if the maximum temperature forecast is above the maximum trigger."""
        forecast = self.controller.get_state("sensor.bom_forecast_max_temp_c_0")
        if forecast != "n/a" and forecast is not None:
            self.forecast["maximum"] = float(forecast)
            if self.forecast["maximum"] < self.current_max_temperature_trigger:
                return True
        else:
            self.controller.log(
                "Maximum temperature forecast is 'n/a' or None", level="WARNING"
            )
        return False


class Aircon:
    """Control a specific aircon unit."""

    def __init__(self, aircon_id: str, controller: Climate):
        """Initialise with an aircon's entity_id and the Climate controller."""
        self.aircon_id = aircon_id
        self.controller = controller

    def turn_on(self, mode: str):
        """Turn on the aircon unit with the specified mode and configured temperature."""
        if mode == "cool":
            target_temperature = self.controller.args["max_target_temperature"]
        elif mode == "heat":
            target_temperature = self.controller.args["min_target_temperature"]
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
