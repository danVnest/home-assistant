"""Automates airconditioning, fan, and heater control.

Monitors climate inside and out, controlling airconditioning units, fans, and
heaters in the house according to user defined temperature thresholds.
The system can be disabled by its users, in which case suggestions are made
(via notifications) instead based on the same thresholds using forecasted and
current temperatures.

User defined variables are configued in climate.yaml
"""

from __future__ import annotations

from math import ceil

from app import App, Device


class Climate(App):
    """Control aircon based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.suggested = False
        self.aircons: dict[str, Aircon] = {}
        self.heaters: dict[str, Heater] = {}
        self.fans: dict[str, Fan] = {}
        self.door_open_listener = None
        self.climate_control_history = {"overridden": False, "before_away": None}

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.__climate_control = self.entities.group.climate_control.state == "on"
        self.climate_control_history["before_away"] = self.climate_control
        self.aircons = {
            room: Aircon(f"climate.{room}", self, room)
            for room in ["bedroom", "living_room", "dining_room"]
        }
        self.fans = {
            room: Fan(
                # "fan.office",
                f"fan.{room}",
                self,
                room,
            )
            for room in ("bedroom", "office")
            # TODO: https://app.asana.com/0/1207020279479204/1207033183115368/f
            # for room in ("bedroom", "office", "nursery")
        }
        self.heaters = {
            "nursery": Heater(
                "climate.nursery_heater",
                self,
                "nursery",
                vacating_delay=1800,
            ),
            "office": Heater(
                "switch.office_heater",
                self,
                "office",
            ),
        }
        for temperatures in ("weighted_average_inside", "outside"):
            self.listen_state(
                self.handle_temperature_change,
                f"sensor.{temperatures}_apparent_temperature",
            )
        self.set_door_check_delay(
            float(self.entities.input_number.aircon_door_check_delay.state),
        )
        self.listen_state(
            self.handle_door_change,
            "binary_sensor.kitchen_door",
            new="off",
            immediate=True,
        )
        # TODO: check climate control before monitoring presence (or in device)?
        for fan in self.fans.values():
            fan.monitor_presence()
        self.heaters["office"].monitor_presence()

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
            self.allow_suggestion()
            if self.control.apps["presence"].pets_home_alone:
                self.log("Turning pets_home_alone mode off as it needs climate control")
                self.control.apps["presence"].pets_home_alone = False
        if (
            self.climate_control_history["overridden"]
            and (
                (
                    self.datetime(aware=True)
                    - self.convert_utc(
                        self.entities.group.climate_control.last_changed,
                    )
                ).total_seconds()
            )
            > self.args["override_threshold"]
        ):
            self.climate_control_history["overridden"] = False
        self.fans["bedroom"].ignore_vacancy(
            self.should_bedroom_fan_ignore_vacancy(),
        )
        self.fans["office"].ignore_vacancy(
            self.control.apps["presence"].pets_home_alone,
        )
        self.call_service(
            f"homeassistant/turn_{'on' if state else 'off'}",
            entity_id="group.climate_control",
        )

    @property
    def aircon(self) -> bool:
        """Get aircon setting that has been synced to Home Assistant."""
        return self.entities.group.all_aircon.state == "on"

    @aircon.setter
    def aircon(self, state: bool):
        """Turn on/off aircon and sync state to Home Assistant UI."""
        if self.aircon == state:
            self.log(f"Ensuring aircon is '{'on' if state else 'off'}'")
        else:
            self.log(f"Turning aircon '{'on' if state else 'off'}'")
        if state:
            self.disable_climate_control_if_would_trigger_off()
            self.turn_aircon_on()
        else:
            self.disable_climate_control_if_would_trigger_on()
            self.turn_aircon_off()
        self.__aircon = state
        self.fans["bedroom"].ignore_vacancy(
            self.should_bedroom_fan_ignore_vacancy(),
        )
        if self.climate_control_history["overridden"]:
            self.log("Re-enabling climate control")
            self.climate_control = True

    def get_setting(self, setting_name: str) -> float:
        """Get temperature target and trigger settings, accounting for Sleep scene."""
        if self.control.scene == "Sleep" or self.control.bed_time:
            setting_name = f"sleep_{setting_name}"
        return float(self.get_state(f"input_number.{setting_name}"))

    def reset(self):
        """Reset climate control using latest settings."""
        self.configure_sensors()
        self.aircon = self.aircon
        self.handle_temperatures()

    def set_door_check_delay(self, minutes: float):
        """Configure listener for door opening, overwriting existing listener."""
        if self.door_open_listener is not None:
            self.cancel_listen_state(self.door_open_listener)
        self.door_open_listener = self.listen_state(
            self.handle_door_change,
            "binary_sensor.kitchen_door",
            new="on",
            duration=minutes * 60,
            immediate=True,
        )

    def recheck_fan_room_vacating_delay(self):
        """Update room vacating delay for each fan."""
        for fan in self.fans.values():
            fan.configure_presence_adjustments()

    def transition_between_scenes(self, new_scene: str, old_scene: str):
        """Adjust aircon & temperature triggers, suggest climate control if suitable."""
        self.configure_sensors()
        if "Away" in new_scene and "Away" not in old_scene:
            if not self.control.apps["presence"].pets_home_alone:
                self.climate_control_history["before_away"] = self.climate_control
                self.climate_control = False
                self.aircon = False
                for fan in self.fans.values():
                    fan.turn_off()
            else:
                self.fans["nursery"].turn_off()
            for heater in self.heaters.values():
                heater.turn_off()
        elif "Away" not in new_scene and "Away" in old_scene:
            if (
                self.climate_control
                and self.aircon
                and self.control.apps["presence"].kitchen_door_open
            ):
                self.aircon = False
            self.climate_control = self.climate_control_history["before_away"]
        if self.climate_control or not self.suggested:
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
            self.suggest_if_extreme_forecast()
        self.fans["bedroom"].ignore_vacancy(
            self.should_bedroom_fan_ignore_vacancy(),
        )

    def handle_temperatures(self, *args):
        """Control aircon or suggest based on changes in inside temperature."""
        del args  # args required for listen_state callback
        if self.climate_control:
            self.adjust_aircon()
            self.adjust_fans()
            self.adjust_heaters()
        if self.aircon is False:
            if self.too_hot_or_cold:
                self.handle_too_hot_or_cold()
        elif self.within_target_temperatures:
            message = (
                f"A desirable inside temperature of "
                f"{self.inside_temperature}º has been reached,"
            )
            if self.climate_control is True:
                self.aircon = False
                self.notify(
                    f"{message} turning aircon off",
                    title="Climate Control",
                    targets="anyone_home_else_all",
                )
            else:
                self.suggest(f"{message} consider enabling climate control")

    def handle_too_hot_or_cold(self):
        """Handle each case (house open, outside nicer, climate control status)."""
        if self.control.apps["presence"].pets_home_alone:
            self.aircon = True
            door_open_message = (
                " in the bedroom only (kitchen door is open)"
                if self.control.apps["presence"].kitchen_door_open
                else ""
            )
            self.notify(
                f"It is {self.inside_temperature}º "
                "inside at home, turning aircon on for the pets" + door_open_message,
                title="Climate Control",
            )
        elif self.outside_temperature_nicer:
            message_beginning = (
                f"Outside ({self.outside_temperature}º) "
                "is a more pleasant temperature than inside "
                f"({self.inside_temperature}º), consider"
            )
            if self.climate_control:
                if not self.control.apps["presence"].kitchen_door_open:
                    self.aircon = True
                    self.suggest(f"{message_beginning} opening up the house")
            elif self.control.apps["presence"].anyone_home:
                if not self.control.apps["presence"].kitchen_door_open:
                    message_beginning += " opening up the house and/or"
                self.suggest(f"{message_beginning} enabling climate control")
        else:
            message_beginning = (
                f"It's {self.inside_temperature}º inside right now, consider"
            )
            if self.climate_control:
                if not self.control.apps["presence"].kitchen_door_open:
                    self.aircon = True
                else:
                    message_ending = (
                        " so airconditioning can turn on" if not self.aircon else ""
                    )
                    self.suggest(
                        f"{message_beginning} closing up the house{message_ending}",
                    )
            else:
                if self.control.apps["presence"].kitchen_door_open:
                    message_beginning += " closing up the house and"
                self.suggest(f"{message_beginning} enabling climate control")

    def suggest_if_extreme_forecast(self):
        """Suggest user enables control if extreme temperatures are forecast."""
        self.allow_suggestion()
        forecast = self.extreme_forecast
        if forecast is not None:
            self.suggest(
                f"It's forecast to reach {forecast}º, "  # TODO: format to one decimal place
                "consider enabling climate control",
            )

    def turn_aircon_on(self):
        """Turn aircon on, calculate mode, handle Sleep/Morning scenes and bed time."""
        if self.above_target_temperature or self.closer_to_hot_than_cold:
            mode = "cool"
        else:
            mode = "heat"
        self.log(
            f"The temperature inside ({self.inside_temperature} "
            f"degrees) is '{'above' if mode == 'cool' else 'below'}' the target ("
            f"{self.get_state(f'input_number.{mode}ing_target_temperature')} degrees)",
        )
        if (
            self.control.scene == "Sleep"
            or self.control.bed_time
            or (
                self.control.apps["presence"].pets_home_alone
                and not self.control.apps["presence"].anyone_home
                and self.control.apps["presence"].kitchen_door_open
            )
        ):
            self.aircons["bedroom"].turn_on(
                mode,
                "high"
                if self.control.pre_sleep_scene or self.control.scene != "Sleep"
                else "low",
            )
            for room in ["living_room", "dining_room"]:
                self.aircons[room].turn_off()
        elif self.control.scene == "Morning":
            for aircon in self.aircons:
                self.aircons[aircon].turn_on(
                    mode,
                    "low" if aircon == "bedroom" else "auto",
                )
        else:
            for aircon in self.aircons.values():
                aircon.turn_on(mode, "auto")
        self.log(f"Aircon is set to '{mode}' mode")
        self.allow_suggestion()

    def turn_aircon_off(self):
        """Turn all aircon units off and allow suggestions again."""
        for aircon in self.aircons.values():
            aircon.turn_off()
        self.log("Aircon is 'off'")
        self.allow_suggestion()

    def adjust_fans(self):
        """Calculate best fan speed for the current temperature and set accordingly."""
        # TODO: https://app.asana.com/0/1207020279479204/1207128730253543/f
        # Configure fans based on individual room temperatures plus aircon/heater status
        speed = 0
        hot = False
        if not self.within_target_temperatures:
            speed_per_step = 100 / self.args["fan_speed_levels"]
            hot = self.above_target_temperature
            if hot:
                speed = speed_per_step * min(
                    self.args["fan_speed_levels"],
                    ceil(
                        self.inside_temperature
                        - self.get_setting("cooling_target_temperature"),
                    ),
                )
            elif self.aircon and self.control.scene not in ("Sleep", "Morning"):
                speed = speed_per_step * 1
        for fan in self.fans.values():
            fan.settings_when_on(speed, not hot)
        self.log(
            f"A desired fan speed of '{speed}' was set",
            level="DEBUG",
        )

    def adjust_heaters(self):
        """Configure best heater settings for the current temperature in each room."""
        buffer = 2
        if "Away" in self.control.scene:
            for room, heater in self.heaters.items():
                if heater.on:
                    heater.turn_off()
                    self.log(
                        f"Turning off '{room}' heater in Away scene "
                        "- should have already been turned off!",
                        level="WARNING",
                    )
            return
            # TODO: perhaps just return as the user must turn it on while away scene
            # wait for a bit to make sure it never get's here otherwise.

        conditions = {
            "nursery": (
                self.control.bed_time
                or self.control.scene == "Sleep"
                or not self.heaters["nursery"].vacant
            ),
            "office": not self.heaters["office"].vacant,
        }
        for room, heater in self.heaters.items():
            temperature = float(
                self.get_state(f"sensor.{room}_apparent_temperature"),
            )
            if not heater.on:
                if (
                    temperature
                    < self.get_setting(
                        "heating_target_temperature",
                    )
                    - buffer
                    and conditions[room]
                ):
                    heater.turn_on()
            elif (
                temperature
                > self.get_setting(
                    "heating_target_temperature",
                )
                + buffer
                or not conditions[room]
            ):
                heater.turn_off()

    # TODO: register presence callbacks to trigger these
    # TODO: add better buffer and vacancy time variables
    # TODO: set temperature target for nursery
    # TODO: check for pets in office

    def adjust_aircon(self):
        """Configure best aircon settings for the current temperature in each room."""
        if "Away" not in self.control.scene and (
            self.control.bed_time or self.control.scene == "Sleep"
        ):
            self.log(
                "NOT COMPLETE",
                level="DEBUG",
            )
            # "dog_bed_area": (
            #     self.control.bed_time() or self.control.scene == "Sleep"
            # ),

    def disable_climate_control_if_would_trigger_on(self):
        """Disables climate control only if it would immediately trigger aircon on."""
        if self.climate_control and self.too_hot_or_cold:
            self.climate_control = False
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
        if self.climate_control and self.within_target_temperatures:
            self.climate_control = False
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
        self.log(f"Kitchen door is now '{'open' if new == 'on' else 'closed'}'")
        if new == "off":
            self.handle_temperatures()
        elif self.aircon and self.entities.climate.living_room.state != "off":
            self.aircon = False
            self.notify(
                message="The kitchen door is open, turning aircon off",
                title="Climate Control",
                targets="anyone_home",
            )

    def should_bedroom_fan_ignore_vacancy(self) -> bool:
        """Check all conditions for which the bedroom fan should ignore vacancy."""
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115370/f
        # change to reliably check if bedroom is occupied
        return (
            self.aircon
            or self.control.scene in ["Sleep", "Morning"]
            or self.control.apps["presence"].pets_home_alone
        )

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
        self.reset()

    def terminate(self):
        """Cancel presence callbacks before termination.

        Appdaemon defined function called before termination.
        """
        for device_group in (self.aircons, self.fans, self.heaters):
            for device in device_group.values():
                device.ignore_vacancy()

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

    def configure_sensors(self):
        """Get values from appropriate sensors and calculate inside temperature."""
        # enable_bedroom_only = (
        #     self.control.scene == "Sleep"
        #     or self.control.bed_time
        #     or (
        #         self.control.apps["presence"].pets_home_alone
        #         and self.control.apps["presence"].kitchen_door_open
        #     )
        # )
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115370/f
        # bedroom only is very dependant on the multisensor being functional, check it is!

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
        if new is not None:
            self.handle_temperatures()

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
        mode = self.entities.climate.bedroom.state
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


class Aircon(Device):
    """Control a specific aircon unit."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
    ):
        """Initialise with an aircon's id, room(s), and the Climate controller."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
        )
        self.vacating_delay = 60 * float(
            controller.entities.input_number.fan_vacating_delay.state,
        )
        # TODO: add an aircon vacating delay? for what scenario?
        # TODO: room and vacating delay not needed for aircon (yet?), how to handle?

    def turn_on(self, mode: str, fan: str | None = None):
        """Set the aircon unit to heat or cool at the preconfigured temperature."""
        if self.device.state != mode:
            self.call_service("set_hvac_mode", hvac_mode=mode)
        target_temperature = self.controller.get_setting(
            mode + "ing_target_temperature",
        ) + self.controller.args["target_buffer"] * (1 if mode == "heat" else -1)
        if self.get_attribute("temperature") != target_temperature:
            self.call_service("set_temperature", temperature=target_temperature)
        if fan:
            self.set_fan_mode(fan)

    def set_fan_mode(self, fan_mode: str):
        """Set the fan mode to the specified level (main options: 'low', 'auto')."""
        if self.get_attribute("fan_mode") != fan_mode:
            self.call_service("set_fan_mode", fan_mode=fan_mode)

    def call_service(self, service: str, **parameters: dict):
        """Call one of the device's services in Home Assistant and wait for response."""
        super().call_service(service, **parameters, return_result=True)


class Fan(Device):
    """Control a fan and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
    ):
        """Initialise with a fan's id, room(s), speed, direction, and controller."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
        )
        self.speed_desired = 0
        self.reverse_desired = False
        self.vacating_delay = 60 * float(
            controller.entities.input_number.fan_vacating_delay.state,
        )
        self.adjustment_delay = controller.args["fan_adjustment_delay"]

    @property
    def speed(self) -> bool:
        """Get the fan's speed (0 is off, 100 is full speed)."""
        return self.get_attribute("percentage") if self.on else 0

    @speed.setter
    def speed(self, new_speed: int):
        """Set the fan's speed (0 is off, 100 is full speed)."""
        if self.speed == new_speed:
            return
        if new_speed == 0:
            super().turn_off()
        elif self.on:
            self.call_service("set_percentage", percentage=new_speed)
        else:
            self.turn_on(percentage=new_speed)

    @property
    def in_reverse(self) -> bool:
        """True if the fan's spin direction is set to reverse."""
        return self.get_attribute("direction") == "reverse"

    @in_reverse.setter
    def in_reverse(self, new_in_reverse: bool):
        """Set the fan's spin direction (forward or reverse)."""
        if self.in_reverse != new_in_reverse:
            self.call_service(
                "set_direction",
                direction="reverse" if new_in_reverse else "forward",
            )

    def turn_off(self):
        """Turn the device off if it's on."""
        self.speed_desired = 0
        super().turn_off()

    def adjust_for_current_conditions(self):
        """Check if actual fan operation matches desired settings and update if not."""
        if self.speed > 0 and (self.ignoring_vacancy or not self.vacant):
            self.in_reverse = self.reverse_desired
            self.speed = self.speed_desired
        else:
            super().turn_off()

    def settings_when_on(self, speed: int, reverse: bool):
        """Set the desired speed and direction of the fan for when it should be on."""
        self.speed_desired = speed
        self.reverse_desired = reverse
        self.adjust_for_current_conditions()


class Heater(Device):
    """Control a heater and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Climate,
        room: str,
        linked_rooms: list[str] = (),
        vacating_delay: int = 0,
    ):
        """Initialise with a heater's id, room(s), and the Climate controller."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
        )
        self.vacating_delay = vacating_delay
