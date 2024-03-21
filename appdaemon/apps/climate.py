"""Automates airconditioning control.

Monitors climate inside and out, controlling airconditioning units in the house
according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""
from __future__ import annotations

from math import ceil

import app
from meteocalc import Temp, heat_index


class Climate(app.App):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__suggested = False
        self.__temperature_monitor = None
        self.__aircons = {}
        self.__fans = {}
        self.__door_open_listener = None
        self.__climate_control_history = {"overridden": False, "before_away": None}

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.__climate_control = (
            self.entities.input_boolean.climate_control.state == "on"
        )
        self.__climate_control_history["before_away"] = self.climate_control
        self.__aircon = self.entities.input_boolean.aircon.state == "on"
        self.__aircons = {
            aircon: Aircon(aircon, self)
            for aircon in ["bedroom", "living_room", "dining_room"]
        }
        self.__fans = {fan: Fan(fan, self) for fan in ["bedroom", "office", "nursery"]}
        self.__temperature_monitor = TemperatureMonitor(self)
        self.__temperature_monitor.configure_sensors()
        self.set_door_check_delay(
            float(self.entities.input_number.aircon_door_check_delay.state),
        )
        self.listen_state(
            self.__handle_door_change,
            "binary_sensor.kitchen_door",
            new="off",
            immediate=True,
        )

    @property
    def climate_control(self) -> bool:
        """Get climate control setting that has been synced to Home Assistant."""
        return self.__climate_control

    @climate_control.setter
    def climate_control(self, state: bool):
        """Enable/disable climate control and reflect state in UI."""
        self.log(f"'{'En' if state else 'Dis'}abling' climate control")
        self.__climate_control = state
        if state:
            self.handle_temperatures()
        else:
            self.__allow_suggestion()
            if self.control.apps["presence"].pets_home_alone:
                self.log("Turning pets_home_alone mode off as it needs climate control")
                self.control.apps["presence"].pets_home_alone = False
        if (
            self.__climate_control_history["overridden"]
            and (
                (
                    self.datetime(aware=True)
                    - self.convert_utc(
                        self.entities.input_boolean.climate_control.last_changed,
                    )
                ).total_seconds()
            )
            > self.args["override_threshold"]
        ):
            self.__climate_control_history["overridden"] = False
        self.__fans["bedroom"].ignore_vacancy(
            self.__should_bedroom_fan_ignore_vacancy(),
        )
        self.__fans["office"].ignore_vacancy(
            self.control.apps["presence"].pets_home_alone,
        )
        self.call_service(
            f"input_boolean/turn_{'on' if state else 'off'}",
            entity_id="input_boolean.climate_control",
        )

    @property
    def aircon(self) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        return self.__aircon

    @aircon.setter
    def aircon(self, state: bool):
        """Turn on/off aircon and sync state to Home Assistant UI."""
        if self.aircon == state:
            self.log(f"Ensuring aircon is '{'on' if state else 'off'}'")
        else:
            self.log(f"Turning aircon '{'on' if state else 'off'}'")
        if state:
            self.__disable_climate_control_if_would_trigger_off()
            self.__turn_aircon_on()
        else:
            self.__disable_climate_control_if_would_trigger_on()
            self.__turn_aircon_off()
        self.__aircon = state
        self.__fans["bedroom"].ignore_vacancy(
            self.__should_bedroom_fan_ignore_vacancy(),
        )
        self.call_service(
            f"input_boolean/turn_{'on' if state else 'off'}",
            entity_id="input_boolean.aircon",
        )
        if self.__climate_control_history["overridden"]:
            self.log("Re-enabling climate control")
            self.climate_control = True

    def get_setting(self, setting_name: str) -> float:
        """Get temperature target and trigger settings, accounting for Sleep scene."""
        if self.control.scene == "Sleep" or self.control.is_bed_time():
            setting_name = f"sleep_{setting_name}"
        return float(self.get_state(f"input_number.{setting_name}"))

    def reset(self):
        """Reset climate control using latest settings."""
        self.__temperature_monitor.configure_sensors()
        self.aircon = self.aircon
        self.handle_temperatures()

    def set_door_check_delay(self, minutes: float):
        """Configure listener for door opening, overwriting existing listener."""
        if self.__door_open_listener is not None:
            self.cancel_listen_state(self.__door_open_listener)
        self.__door_open_listener = self.listen_state(
            self.__handle_door_change,
            "binary_sensor.kitchen_door",
            new="on",
            duration=minutes * 60,
            immediate=True,
        )

    def recheck_fan_room_vacating_delay(self):
        """Update room vacating delay for each fan."""
        for fan in self.__fans.values():
            fan.configure_presence_adjustments()

    def transition_between_scenes(self, new_scene: str, old_scene: str):
        """Adjust aircon & temperature triggers, suggest climate control if suitable."""
        self.__temperature_monitor.configure_sensors()
        if "Away" in new_scene and "Away" not in old_scene:
            if not self.control.apps["presence"].pets_home_alone:
                self.__climate_control_history["before_away"] = self.climate_control
                self.climate_control = False
                self.aircon = False
        elif "Away" not in new_scene and "Away" in old_scene:
            self.climate_control = self.__climate_control_history["before_away"]
        if self.climate_control or not self.__suggested:
            self.handle_temperatures()
        if self.aircon:
            self.aircon = True
        elif self.climate_control is False and any(
            [
                new_scene == "Day" and old_scene in ["Sleep", "Morning"],
                new_scene == "Night" and old_scene == "Day",
                "Away" not in new_scene and "Away" in old_scene,
                self.control.apps["presence"].pets_home_alone,
            ],
        ):
            self.__suggest_if_trigger_forecast()
        self.__fans["bedroom"].ignore_vacancy(
            self.__should_bedroom_fan_ignore_vacancy(),
        )

    def handle_temperatures(self, *args):
        """Control aircon or suggest based on changes in inside temperature."""
        del args  # args required for listen_state callback
        if self.climate_control:
            self.__configure_fans_optimally()
        if self.aircon is False:
            if self.__temperature_monitor.is_too_hot_or_cold():
                self.__handle_too_hot_or_cold()
        elif self.__temperature_monitor.is_within_target_temperatures():
            message = (
                f"A desirable inside temperature of "
                f"{self.__temperature_monitor.inside_temperature}º has been reached,"
            )
            if self.climate_control is True:
                self.aircon = False
                self.notify(
                    f"{message} turning aircon off",
                    title="Climate Control",
                    targets="anyone_home_else_all",
                )
            else:
                self.__suggest(f"{message} consider enabling climate control")

    def __handle_too_hot_or_cold(self):
        """Handle each case (house open, outside nicer, climate control status)."""
        if self.control.apps["presence"].pets_home_alone:
            self.aircon = True
            door_open_message = (
                " in the bedroom only (kitchen door is open)"
                if self.control.apps["presence"].is_kitchen_door_open()
                else ""
            )
            self.notify(
                f"It is {self.__temperature_monitor.inside_temperature}º "
                "inside at home, turning aircon on for the pets" + door_open_message,
                title="Climate Control",
            )
        elif self.__temperature_monitor.is_outside_temperature_nicer():
            message_beginning = (
                f"Outside ({self.__temperature_monitor.outside_temperature}º) "
                "is a more pleasant temperature than inside "
                f"({self.__temperature_monitor.inside_temperature})º), consider"
            )
            if self.climate_control:
                if not self.control.apps["presence"].is_kitchen_door_open():
                    self.aircon = True
                    self.__suggest(f"{message_beginning} opening up the house")
            elif self.control.apps["presence"].anyone_home():
                if not self.control.apps["presence"].is_kitchen_door_open():
                    message_beginning += " opening up the house and/or"
                self.__suggest(f"{message_beginning} enabling climate control")
        else:
            message_beginning = (
                f"It's {self.__temperature_monitor.inside_temperature}º "
                "inside right now, consider"
            )
            if self.climate_control:
                if not self.control.apps["presence"].is_kitchen_door_open():
                    self.aircon = True
                else:
                    message_ending = (
                        " so airconditioning can turn on" if not self.aircon else ""
                    )
                    self.__suggest(
                        f"{message_beginning} closing up the house{message_ending}",
                    )
            else:
                if self.control.apps["presence"].is_kitchen_door_open():
                    message_beginning += " closing up the house and"
                self.__suggest(f"{message_beginning} enabling climate control")

    def __suggest_if_trigger_forecast(self):
        """Suggest user enables control if extreme's forecast."""
        self.__allow_suggestion()
        forecast = self.__temperature_monitor.get_forecast_if_will_trigger_aircon()
        if forecast is not None:
            self.__suggest(
                f"It's forecast to reach {forecast}º, "
                "consider enabling climate control",
            )

    def __turn_aircon_on(self):
        """Turn aircon on, calculate mode, handle Sleep/Morning scenes and bed time."""
        if self.__temperature_monitor.is_below_target_temperature():
            mode = "heat"
        elif self.__temperature_monitor.is_above_target_temperature():
            mode = "cool"
        else:
            mode = self.__temperature_monitor.closer_to_heat_or_cool()
        self.log(
            f"The temperature inside ({self.__temperature_monitor.inside_temperature} "
            f"degrees) is '{'above' if mode == 'cool' else 'below'}' the target ("
            f"{self.get_state(f'input_number.{mode}ing_target_temperature')} degrees)",
        )
        if (
            self.control.scene == "Sleep"
            or self.control.is_bed_time()
            or (
                self.control.apps["presence"].pets_home_alone
                and not self.control.apps["presence"].anyone_home()
                and self.control.apps["presence"].is_kitchen_door_open()
            )
        ):
            self.__aircons["bedroom"].turn_on(
                mode,
                "high"
                if self.control.pre_sleep_scene or self.control.scene != "Sleep"
                else "low",
            )
            for room in ["living_room", "dining_room"]:
                self.__aircons[room].turn_off()
        elif self.control.scene == "Morning":
            for aircon in self.__aircons:
                self.__aircons[aircon].turn_on(
                    mode,
                    "low" if aircon == "bedroom" else "auto",
                )
        else:
            for aircon in self.__aircons.values():
                aircon.turn_on(mode, "auto")
        self.log(f"Aircon is set to '{mode}' mode")
        self.__allow_suggestion()

    def __turn_aircon_off(self):
        """Turn all aircon units off and allow suggestions again."""
        for aircon in self.__aircons.values():
            aircon.turn_off()
        self.log("Aircon is 'off'")
        self.__allow_suggestion()

    def __configure_fans_optimally(self):
        """Calculate best fan speed for the current temperature and set accordingly."""
        if not self.__temperature_monitor.is_within_target_temperatures():
            speed_per_step = float(
                self.get_state(
                    "fan.bedroom",
                    "percentage_step",
                ),
            )
            fan_speed_levels = round(100 / speed_per_step)
            is_hot = self.__temperature_monitor.is_above_target_temperature()
            if is_hot:
                speed = speed_per_step * min(
                    fan_speed_levels,
                    ceil(
                        self.__temperature_monitor.inside_temperature
                        - self.get_setting("cooling_target_temperature"),
                    ),
                )
            elif self.aircon:
                speed = speed_per_step * 1
            self.log(
                f"A desired fan speed of '{speed}' was set",
                level="DEBUG",
            )
            for fan in self.__fans.values():
                fan.settings_when_on(speed, is_hot)
        else:
            for fan in self.__fans.values():
                fan.turn_off()

    def __disable_climate_control_if_would_trigger_on(self):
        """Disables climate control only if it would immediately trigger aircon on."""
        if self.climate_control and self.__temperature_monitor.is_too_hot_or_cold():
            self.climate_control = False
            self.__climate_control_history["overridden"] = True
            self.notify(
                "The current temperature ("
                f"{self.__temperature_monitor.inside_temperature}º) will immediately "
                "trigger aircon on again - "
                "climate control is now disabled to prevent this",
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def __disable_climate_control_if_would_trigger_off(self):
        """Disables climate control only if it would immediately trigger aircon off."""
        if (
            self.climate_control
            and self.__temperature_monitor.is_within_target_temperatures()
        ):
            self.climate_control = False
            self.__climate_control_history["overridden"] = True
            self.notify(
                "Inside is already within the desired temperature range,"
                " climate control is now disabled"
                " (you'll need to manually turn aircon off)",
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def __suggest(self, message: str):
        """Make a suggestion to the users, but only if one has not already been sent."""
        if not self.__suggested:
            self.__suggested = True
            self.notify(
                message,
                title="Climate Control",
                targets="anyone_home_else_all",
            )

    def __allow_suggestion(self):
        """Allow suggestions to be made again. Use after user events & scene changes."""
        if self.__suggested:
            self.__suggested = False

    def __handle_door_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ) -> None:
        """If the kitchen door status changes, check if aircon needs to change."""
        del entity, attribute, old, kwargs
        self.log(f"Kitchen door is now '{'open' if new == 'on' else 'closed'}'")
        if new == "off":
            self.handle_temperatures()
        elif self.aircon and self.get_state("climate.living_room") != "off":
            self.aircon = False
            self.notify(
                message="The kitchen door is open, turning aircon off",
                title="Climate Control",
                targets="anyone_home",
            )

    def __should_bedroom_fan_ignore_vacancy(self) -> bool:
        """Check all conditions for which the bedroom fan should ignore vacancy."""
        return (
            self.aircon
            or self.control.scene in ["Sleep", "Morning"]
            or self.control.apps["presence"].pets_home_alone
        )

    def terminate(self):
        """Cancel presence callbacks before termination.

        Appdaemon defined function called before termination.
        """
        for fan in self.__fans.values():
            fan.cancel_presence_callback()


class Sensor:
    """Capture temperature and humidity data from generic sensor entities."""

    @staticmethod
    def detect_type_and_create(sensor_id: str, controller: Climate) -> type[Sensor]:
        """Detect the sensor type and create an object with the relevant subclass."""
        if "climate" in sensor_id:
            sensor_type = ClimateSensor
        elif "multisensor" in sensor_id:
            sensor_type = MultiSensor
        elif "outside" in sensor_id:
            sensor_type = WeatherSensor
        else:
            sensor_type = Sensor
        return sensor_type(sensor_id, controller)

    def __init__(self, sensor_id: str, monitor: TemperatureMonitor):
        """Initialise sensor with appropriate variables and a monitor to callback to."""
        self.sensor_id: str = sensor_id
        self.monitor: TemperatureMonitor = monitor
        if "bedroom" in self.sensor_id:
            self.location = "bedroom"
        elif "outside" in self.sensor_id:
            self.location = "outside"
        else:
            self.location = "inside"
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

    def validate_measure(self, value: str) -> float:
        """Return numerical value of measure, warning if invalid."""
        try:
            return float(value)
        except ValueError:
            self.monitor.controller.log(
                f"'{self.sensor_id}' could not get measure",
                level="WARNING",
            )
            return None


class ClimateSensor(Sensor):
    """Capture temperature and humidity data from climate.x entities."""

    def get_measure(self, measure: str) -> float:
        """Get latest value from the sensor in Home Assistant."""
        return self.validate_measure(
            self.monitor.controller.get_state(
                self.sensor_id,
                attribute=f"current_{measure}",
            ),
        )

    def enable(self):
        """Initialise sensor values and listen for further updates."""
        for name, listener in self.listeners.items():
            if listener is None:
                self.listeners[name] = self.monitor.controller.listen_state(
                    self.monitor.handle_sensor_change,
                    self.sensor_id,
                    attribute=f"current_{name}",
                    measure=name,
                )


class MultiSensor(Sensor):
    """Capture temperature and humidity data from multisensor entities."""

    def get_measure(self, measure: str) -> float:
        """Get latest value from the sensor in Home Assistant."""
        return self.validate_measure(
            self.monitor.controller.get_state(f"{self.sensor_id}_{measure}"),
        )

    def enable(self):
        """Initialise sensor values and listen for further updates."""
        for name, listener in self.listeners.items():
            if listener is None:
                self.listeners[name] = self.monitor.controller.listen_state(
                    self.monitor.handle_sensor_change,
                    f"{self.sensor_id}_{name}",
                    measure=name,
                )


class WeatherSensor(Sensor):
    """Capture temperature data from a WeatherFlow Tempest weather station."""

    def __init__(self, sensor_id: str, monitor: TemperatureMonitor):
        """Keep only the temperature listener as methods change monitor's attribute."""
        super().__init__(sensor_id, monitor)
        del self.listeners["humidity"]

    def enable(self):
        """Listen for changes in temperature."""
        if self.listeners["temperature"] is None:
            self.listeners["temperature"] = self.monitor.controller.listen_state(
                self.monitor.controller.handle_temperatures,
                self.sensor_id,
            )


class TemperatureMonitor:
    """Monitor various sensors to provide temperatures in & out, and check triggers."""

    def __init__(self, controller: Climate):
        """Initialise with Climate controller and create sensor objects."""
        self.controller = controller
        self.__inside_temperature = (
            self.controller.entities.climate.bedroom.attributes.current_temperature
        )
        self.__sensors = {
            sensor_id: Sensor.detect_type_and_create(sensor_id, self)
            for sensor_id in [
                "climate.bedroom",
                "climate.living_room",
                "climate.dining_room",
                "sensor.kitchen_multisensor",
                "sensor.office_multisensor",
                "sensor.bedroom_multisensor",
                "sensor.outside_apparent_temperature",
            ]
        }
        self.__last_inside_temperature = None

    @property
    def inside_temperature(self) -> float:
        """Get the calculated inside temperature that's synced with Home Assistant."""
        return self.__inside_temperature

    @inside_temperature.setter
    def inside_temperature(self, temperature: float):
        """Sync the calculated inside temperature to Home Assistant."""
        self.__inside_temperature = temperature
        self.controller.set_state(
            "sensor.inside_apparent_temperature",
            state=temperature,
        )

    @property
    def outside_temperature(self) -> float:
        """Get the calculated outside temperature from Home Assistant."""
        return float(self.controller.entities.sensor.outside_apparent_temperature.state)

    def configure_sensors(self):
        """Get values from appropriate sensors and calculate inside temperature."""
        enable_bedroom_only = (
            self.controller.control.scene == "Sleep"
            or self.controller.control.is_bed_time()
            or (
                self.controller.control.apps["presence"].pets_home_alone
                and self.controller.control.apps["presence"].is_kitchen_door_open()
            )
        )
        for sensor in self.__sensors.values():
            if enable_bedroom_only and sensor.location == "inside":
                sensor.disable()
            else:
                sensor.enable()
        self.calculate_inside_temperature()

    def handle_sensor_change(
        self,
        entity: str,
        attribute: str,
        old: float,
        new: float,
        kwargs: dict,
    ):
        """Calculate inside temperature then get controller to handle if changed."""
        del entity, attribute, old, kwargs
        if new is not None:
            self.calculate_inside_temperature()

    def calculate_inside_temperature(self):
        """Use stored sensor values to calculate the apparent temperature inside."""
        self.__last_inside_temperature = self.inside_temperature
        temperatures = []
        humidities = []
        for sensor in self.__sensors.values():
            if sensor.is_enabled() and sensor.location != "outside":
                temperature = sensor.get_measure("temperature")
                if temperature is not None:
                    temperatures.append(temperature)
                humidity = sensor.get_measure("humidity")
                if humidity is not None:
                    humidities.append(humidity)
        self.inside_temperature = round(
            heat_index(
                temperature=Temp(
                    sum(temperatures) / len(temperatures),
                    "c",
                ),
                humidity=sum(humidities) / len(humidities),
            ).c,
            1,
        )
        if self.__last_inside_temperature != self.inside_temperature:
            self.controller.log(
                f"Inside temperature calculated as {self.inside_temperature} degrees",
                level="DEBUG",
            )
            self.controller.handle_temperatures()

    def is_within_target_temperatures(self) -> bool:
        """Check if temperature is not above or below target temperatures."""
        return not (
            self.is_above_target_temperature() or self.is_below_target_temperature()
        )

    def is_above_target_temperature(self) -> bool:
        """Check if temperature is above the target temperature, with a buffer."""
        return (
            self.inside_temperature
            > float(self.controller.get_setting("cooling_target_temperature"))
            - self.controller.args["target_trigger_buffer"]
        )

    def is_below_target_temperature(self) -> bool:
        """Check if temperature is below the target temperature, with a buffer."""
        return (
            self.inside_temperature
            < self.controller.get_setting("heating_target_temperature")
            + self.controller.args["target_trigger_buffer"]
        )

    def is_outside_temperature_nicer(self) -> bool:
        """Check if outside is a nicer temperature than inside."""
        mode = self.controller.entities.climate.bedroom.state
        hotter_outside = (
            self.inside_temperature
            < self.outside_temperature - self.controller.args["inside_outside_trigger"]
        )
        colder_outside = (
            self.inside_temperature
            > self.outside_temperature + self.controller.args["inside_outside_trigger"]
        )
        too_hot_or_cold_outside = (
            not self.controller.get_setting("low_temperature_aircon_trigger")
            <= self.outside_temperature
            <= self.controller.get_setting("high_temperature_aircon_trigger")
        )
        vs_str = f"({self.outside_temperature} vs {self.inside_temperature} degrees)"
        if any(
            [
                mode == "heat" and hotter_outside,
                mode == "cool" and colder_outside,
                mode == "off"
                and (self.is_too_hot_or_cold() and not too_hot_or_cold_outside),
            ],
        ):
            self.controller.log(
                f"Outside temperature is nicer than inside {vs_str}",
                level="DEBUG",
            )
            return True
        self.controller.log(
            f"Outside temperature is not better than inside {vs_str}",
            level="DEBUG",
        )
        return False

    def is_too_hot_or_cold(self) -> bool:
        """Check if temperature inside is above or below the max/min triggers."""
        if (
            self.controller.get_setting("low_temperature_aircon_trigger")
            < self.inside_temperature
            < self.controller.get_setting("high_temperature_aircon_trigger")
        ):
            return False
        self.controller.log(
            f"It's too hot or cold inside ({self.inside_temperature} degrees)",
            level="DEBUG",
        )
        return True

    def closer_to_heat_or_cool(self) -> str:
        """Return if temperature inside is closer to needing heating or cooling."""
        if (
            self.inside_temperature
            > (
                self.controller.get_setting("cooling_target_temperature")
                + self.controller.get_setting("heating_target_temperature")
            )
            / 2
        ):
            return "cool"
        return "heat"

    def get_forecast_if_will_trigger_aircon(self) -> float:
        """Return the forecasted temperature if it exceeds thresholds."""
        forecasts = [
            float(
                self.controller.get_state(
                    f"sensor.outside_apparent_temperature_{hour}h_forecast",
                ),
            )
            for hour in ["2", "4", "6", "8"]
        ]
        max_forecast = max(forecasts)
        if max_forecast >= self.controller.get_setting(
            "high_temperature_aircon_trigger",
        ):
            return max_forecast
        min_forecast = min(forecasts)
        if min_forecast <= self.controller.get_setting(
            "low_temperature_aircon_trigger",
        ):
            return min_forecast
        return None


class Aircon:
    """Control a specific aircon unit."""

    def __init__(self, aircon_id: str, controller: Climate):
        """Initialise with an aircon's entity_id and the Climate controller."""
        self.__aircon_id = f"climate.{aircon_id}"
        self.__controller = controller

    def turn_on(self, mode: str, fan: str | None = None):
        """Set the aircon unit to heat or cool at the preconfigured temperature."""
        if self.__controller.get_state(self.__aircon_id) != mode:
            self.__controller.call_service(
                "climate/set_hvac_mode",
                entity_id=self.__aircon_id,
                hvac_mode=mode,
                return_result=True,
            )
        target_temperature = self.__controller.get_setting(
            mode + "ing_target_temperature",
        )
        if (
            self.__controller.get_state(self.__aircon_id, attribute="temperature")
            != target_temperature
        ):
            self.__controller.call_service(
                "climate/set_temperature",
                entity_id=self.__aircon_id,
                temperature=target_temperature,
                return_result=True,
            )
        if fan:
            self.set_fan_mode(fan)

    def turn_off(self):
        """Turn off the aircon unit if it's on."""
        if self.__controller.get_state(self.__aircon_id) != "off":
            self.__controller.call_service(
                "climate/turn_off",
                entity_id=self.__aircon_id,
                return_result=True,
            )

    def set_fan_mode(self, fan_mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')."""
        if (
            self.__controller.get_state(self.__aircon_id, attribute="fan_mode")
            != fan_mode
        ):
            self.__controller.call_service(
                "climate/set_fan_mode",
                entity_id=self.__aircon_id,
                fan_mode=fan_mode,
                return_result=True,
            )


class Fan:
    """Control a specific ceiling fan."""

    def __init__(self, fan_id: str, controller: Climate):
        """Initialise with a fan's entity_id and the Climate controller."""
        self.__fan_id = f"fan.{fan_id}"
        self.__room = fan_id
        self.__controller = controller
        self.__speed = 0
        self.__is_cooling_direction = True
        self.__is_ignoring_vacancy = False
        self.__vacating_delay = None
        self.__last_adjustment = self.__controller.get_now_ts()
        self.__adjustment_timer = None
        self.__presence = None
        self.__presence_callback = None
        self.configure_presence_adjustments()

    def turn_off(self):
        """Turn the fan off if required."""
        self.__speed = 0
        self.__adjust()

    def __adjust(self):
        """Check if actual fan operation matches settings and update if not."""
        if not self.__controller.climate_control:
            return
        if self.__adjustment_timer is None and (
            self.__controller.get_now_ts() - self.__last_adjustment
            < self.__controller.args["fan_adjustment_interval"]
        ):
            self.__adjustment_timer = self.__controller.run_in(
                self.__adjust_after_delay,
                self.__last_adjustment
                + self.__controller.args["fan_adjustment_interval"]
                - self.__controller.get_now_ts(),
            )
            return
        if self.__speed > 0 and (self.__is_ignoring_vacancy or self.__presence):
            if (
                self.__controller.get_state(self.__fan_id, attribute="direction")
                == "forward"
            ) != self.__is_cooling_direction:
                self.__controller.call_service(
                    "fan/set_direction",
                    entity_id=self.__fan_id,
                    direction="forward" if self.__is_cooling_direction else "reverse",
                )
            if self.__controller.get_state(self.__fan_id) == "on":
                if (
                    int(
                        self.__controller.get_state(
                            self.__fan_id,
                            attribute="percentage",
                        ),
                    )
                    != self.__speed
                ):
                    self.__controller.call_service(
                        "fan/set_percentage",
                        entity_id=self.__fan_id,
                        percentage=self.__speed,
                    )
            else:
                self.__controller.call_service(
                    "fan/turn_on",
                    entity_id=self.__fan_id,
                    percentage=self.__speed,
                )
        elif self.__controller.get_state(self.__fan_id) != "off":
            self.__controller.call_service("fan/turn_off", entity_id=self.__fan_id)

    def __adjust_after_delay(self, kwargs: dict):
        """Delayed adjustment from timers initiated in __adjust()."""
        del kwargs
        self.__adjustment_timer = None
        self.__adjust()

    def settings_when_on(self, speed: int, is_cooling_direction: bool):  # noqa: FBT001
        """Set the desired speed and direction of the fan for when it should be on."""
        self.__speed = speed
        self.__is_cooling_direction = is_cooling_direction
        self.__adjust()

    def ignore_vacancy(self, ignore: bool):  # noqa: FBT001
        """Configure the fan to operate even when vacant (or not)."""
        self.__is_ignoring_vacancy = ignore
        self.__adjust()

    def configure_presence_adjustments(self):
        """Set vacating delay, current presence and a callback for when it changes."""
        self.cancel_presence_callback()
        self.__vacating_delay = 60 * float(
            self.__controller.entities.input_number.fan_vacating_delay.state,
        )
        self.__presence = (
            not self.__controller.control.apps["presence"]
            .rooms[self.__room]
            .is_vacant(self.__vacating_delay)
        )
        self.__presence_callback = (
            self.__controller.control.apps["presence"]
            .rooms[self.__room]
            .register_callback(
                self.__handle_presence_change,
                self.__vacating_delay,
            )
        )

    def __handle_presence_change(self, is_vacant: bool):  # noqa: FBT001
        """Adjust lighting based on presence in the room."""
        self.__presence = not is_vacant
        self.__adjust()

    def cancel_presence_callback(self):
        """Cancel the presence callback."""
        if self.__presence_callback:
            self.__controller.control.apps["presence"].rooms[
                self.__room
            ].cancel_callback(
                self.__presence_callback,
            )
