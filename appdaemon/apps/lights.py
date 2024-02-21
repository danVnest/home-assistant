"""Coordinates lighting based primarily on scenes.

Includes circadian adjustments to brightness and kelvin settings for ergonomics,
localised adjustments based on presence callbacks, and luminance monitoring.

User defined variables are configued in lights.yaml
"""
from __future__ import annotations

import datetime
import uuid

import app


class Lights(app.App):
    """Control lights based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__circadian = {"timer": None}
        self.lights = {}
        self.constants["brightness_per_step"] = 2.55
        self.constants["kelvin_per_step"] = 20
        self.constants["max_steps_per_second"] = 2

    def initialize(self):
        """Initialise lights and start listening to scene events.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.lights["entryway"] = Light("group.entryway_lights", self)
        self.lights["kitchen"] = Light("light.kitchen", self)
        self.lights["kitchen_strip"] = Light("light.kitchen_strip", self, "kitchen")
        self.lights["tv"] = Light("group.tv_lights", self)
        self.lights["dining"] = Light("group.dining_lights", self)
        self.lights["hall"] = Light("light.hall", self)
        self.lights["office"] = Light("light.office", self)
        self.lights["bedroom"] = Light("light.bedroom", self)
        self.lights["nursery"] = Light("light.nursery", self)
        self.redate_circadian(None)
        self.run_daily(self.redate_circadian, "00:00:01")
        self.listen_state(
            self.__handle_kitchen_luminance_change,
            "sensor.kitchen_multisensor_luminance",
        )
        self.listen_state(
            self.__handle_bedroom_luminance_change,
            "sensor.bedroom_multisensor_luminance",
        )

    def terminate(self):
        """Cancel presence callbacks before termination.

        Appdaemon defined function called before termination.
        """
        for light in self.lights.values():
            light.ignore_presence()

    def transition_to_scene(self, scene: str):
        """Change lighting based on the specified scene."""
        self.cancel_timer(self.__circadian["timer"])
        if "Day" in scene:
            self.__transition_to_day_scene()
        elif scene == "Night":
            self.__start_circadian()
        elif scene == "Bright":
            self.__transition_to_bright_scene()
        elif scene == "TV":
            self.__transition_to_tv_scene()
        elif scene == "Sleep":
            self.__transition_to_sleep_scene()
        elif scene == "Morning":
            self.__transition_to_morning_scene()
        elif scene == "Away (Night)":
            self.__transition_to_away_scene()
        elif scene == "Custom":
            for light in self.lights.values():
                light.ignore_presence()
        self.log(f"Light scene changed to '{scene}'")

    def __transition_to_day_scene(self):
        """Configure lighting for the day scene."""
        self.lights["office"].set_presence_adjustments(
            occupied=(
                self.args["max_brightness"],
                self.lights["office"].kelvin_limits["max"],
            ),
            vacating_delay=self.control.get_setting("office_vacating_delay"),
        )
        if not self.lights["bedroom"].is_ignoring_presence():
            self.lights["bedroom"].set_presence_adjustments(
                occupied=(
                    self.args["max_brightness"],
                    self.lights["bedroom"].kelvin_limits["max"],
                ),
                vacating_delay=self.control.get_setting("bedroom_vacating_delay"),
            )
        for light_name in [
            "entryway",
            "kitchen",
            "kitchen_strip",
            "tv",
            "dining",
            "hall",
            "nursery",
        ]:
            self.lights[light_name].ignore_presence()
            self.lights[light_name].turn_off()

    def __transition_to_bright_scene(self):
        """Configure lighting for the bright scene."""
        for light in self.lights.values():
            light.ignore_presence()
            light.adjust_to_max()

    def __transition_to_tv_scene(self):
        """Configure lighting for the tv scene."""
        kelvin = int(float(self.entities.input_number.tv_kelvin.state))
        self.lights["entryway"].set_presence_adjustments(
            occupied=(
                self.control.get_setting("tv_motion_brightness"),
                kelvin,
            ),
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(self.control.get_setting("tv_brightness"), kelvin),
            entered=(self.control.get_setting("tv_motion_brightness"), kelvin),
            occupied=(
                self.args["max_brightness"],
                self.lights["kitchen"].kelvin_limits["max"],
            ),
            transition_period=self.control.get_setting("tv_transition_period"),
            vacating_delay=self.control.get_setting("tv_vacating_delay"),
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=(self.control.get_setting("tv_motion_brightness"), kelvin),
            occupied=(
                self.args["max_brightness"],
                self.lights["kitchen_strip"].kelvin_limits["max"],
            ),
            transition_period=self.control.get_setting("tv_transition_period"),
            vacating_delay=self.control.get_setting("tv_vacating_delay"),
        )
        for light_name in ["tv", "hall"]:
            self.lights[light_name].adjust(
                self.control.get_setting("tv_brightness"),
                kelvin,
            )
        for light_name in ["dining", "nursery"]:
            self.lights[light_name].turn_off()
        brightness, kelvin = self.calculate_circadian_brightness_kelvin()
        self.lights["office"].set_presence_adjustments(
            occupied=(
                brightness,
                kelvin,
            ),
            vacating_delay=self.control.get_setting("office_vacating_delay"),
        )
        if not self.lights["bedroom"].is_ignoring_presence():
            self.lights["bedroom"].set_presence_adjustments(
                occupied=(
                    brightness,
                    kelvin,
                ),
                vacating_delay=self.control.get_setting("bedroom_vacating_delay"),
            )

    def __transition_to_sleep_scene(self):
        """Configure lighting for the sleep scene."""
        for light_name in ["entryway", "kitchen"]:
            self.lights[light_name].set_presence_adjustments(
                entered=(
                    self.args["min_brightness"],
                    self.lights[light_name].kelvin_limits["min"],
                ),
                occupied=(
                    self.control.get_setting("sleep_motion_brightness"),
                    self.control.get_setting("sleep_motion_kelvin"),
                ),
                transition_period=self.control.get_setting(
                    "sleep_transition_period",
                ),
                vacating_delay=self.control.get_setting("sleep_vacating_delay"),
            )
        self.lights["office"].set_presence_adjustments(
            occupied=(
                self.args["min_brightness"],
                self.lights["office"].kelvin_limits["min"],
            ),
            vacating_delay=self.control.get_setting("office_vacating_delay"),
        )
        for light_name in ["kitchen_strip", "tv", "dining", "hall", "nursery"]:
            self.lights[light_name].ignore_presence()
            self.lights[light_name].turn_off()
        self.lights["bedroom"].ignore_presence()
        if not self.control.pre_sleep_scene:
            self.lights["bedroom"].turn_off()
        else:
            self.log("Kept bedroom light on")
            self.control.pre_sleep_scene = False

    def __transition_to_morning_scene(self):
        """Configure lighting for the morning scene."""
        brightness = self.control.get_setting("morning_brightness")
        kelvin = self.control.get_setting("morning_kelvin")
        vacating_delay = self.control.get_setting("morning_vacating_delay")
        self.lights["entryway"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(brightness, kelvin),
            occupied=(self.args["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            occupied=(self.args["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["office"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.control.get_setting("office_vacating_delay"),
        )
        for light_name in ["tv", "dining", "hall", "bedroom", "nursery"]:
            self.lights[light_name].turn_off()
        self.lights["bedroom"].ignore_presence()

    def __transition_to_away_scene(self):
        """Configure lighting for the away scene."""
        for light_name in ["entryway", "kitchen", "office"]:
            self.lights[light_name].set_presence_adjustments(
                occupied=(
                    self.args["max_brightness"],
                    self.lights[light_name].kelvin_limits["max"],
                ),
                vacating_delay=60,
            )
        self.lights["dining"].adjust_to_max()
        for light_name in ["kitchen_strip", "tv", "hall", "bedroom", "nursery"]:
            self.lights[light_name].ignore_presence()
            self.lights[light_name].turn_off()

    def __start_circadian(self):
        """Schedule a timer to periodically set the lighting appropriately."""
        circadian_progress = self.__calculate_circadian_progress()
        self.__circadian_progression({"circadian_progress": circadian_progress})
        if circadian_progress not in (0, 1):
            self.__circadian["timer"] = self.run_every(
                self.__circadian_progression,
                self.datetime() + self.__circadian["time_step"],
                self.__circadian["time_step"].total_seconds(),
            )
            self.log("Started circadian progression")

    def __circadian_progression(self, kwargs: dict):
        """Calculate appropriate lighting levels and implement."""
        circadian_progress = kwargs.get(
            "circadian_progress",
            self.__calculate_circadian_progress(),
        )
        if circadian_progress in (0, 1):
            self.cancel_timer(self.__circadian["timer"])
            next_start = self.__circadian["start_time"] + datetime.timedelta(
                days=circadian_progress,
            )
            self.__circadian["timer"] = self.run_every(
                self.__circadian_progression,
                next_start,
                self.__circadian["time_step"].total_seconds(),
            )
            self.log(
                f"Set circadian progression to commence at {next_start}",
                level="DEBUG",
            )
        brightness, kelvin = self.calculate_circadian_brightness_kelvin(
            circadian_progress,
        )
        self.lights["entryway"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(brightness, kelvin),
            entered=(brightness, kelvin)
            if brightness >= self.control.get_setting("night_motion_brightness")
            else (self.control.get_setting("night_motion_brightness"), kelvin),
            occupied=(
                self.args["max_brightness"],
                self.control.get_setting("night_motion_kelvin"),
            ),
            transition_period=self.control.get_setting("night_transition_period"),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=(brightness, kelvin)
            if brightness >= self.control.get_setting("night_motion_brightness")
            else (self.control.get_setting("night_motion_brightness"), kelvin),
            occupied=(
                self.args["max_brightness"],
                self.control.get_setting("night_motion_kelvin"),
            ),
            transition_period=self.control.get_setting("night_transition_period"),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        for light_name in ["tv", "dining", "hall"]:
            self.lights[light_name].adjust(brightness, kelvin)
        self.lights["office"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.control.get_setting("office_vacating_delay"),
        )
        self.lights["bedroom"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        self.log(
            "Adjusted lighting based on circadian progression to "
            f"brightness: {brightness} and kelvin: {kelvin}",
            level="DEBUG",
        )

    def __calculate_circadian_progress(self) -> float:
        """Calculate how far through the circadian rhythm we should be right now."""
        circadian_progress = (self.datetime() - self.__circadian["start_time"]) / (
            self.__circadian["end_time"] - self.__circadian["start_time"]
        )
        if not 0 < circadian_progress < 1:
            circadian_progress = (
                0
                if (
                    self.parse_time(self.control.get_setting("morning_time"))
                    < self.time()
                    < self.__circadian["start_time"].time()
                )
                else 1
            )
        self.log(
            f"Circadian progress calculated as: {circadian_progress}",
            level="DEBUG",
        )
        return circadian_progress

    def calculate_circadian_brightness_kelvin(
        self,
        circadian_progress: float | None = None,
    ) -> tuple[int, int]:
        """Calculate appropriate lighting levels based on the circadian progression."""
        if circadian_progress is None:
            circadian_progress = self.__calculate_circadian_progress()
        return (
            int(
                float(self.entities.input_number.initial_circadian_brightness.state)
                + (
                    float(self.entities.input_number.final_circadian_brightness.state)
                    - float(
                        self.entities.input_number.initial_circadian_brightness.state,
                    )
                )
                * circadian_progress,
            ),
            int(
                float(self.entities.input_number.initial_circadian_kelvin.state)
                + (
                    float(self.entities.input_number.final_circadian_kelvin.state)
                    - float(self.entities.input_number.initial_circadian_kelvin.state)
                )
                * circadian_progress,
            ),
        )

    def redate_circadian(self, kwargs: dict):
        """Configure the start and end times for lighting adjustment for today."""
        del kwargs
        start_time = datetime.datetime.combine(
            self.date(),
            self.sunset().time(),
        ) + datetime.timedelta(
            hours=float(
                self.entities.input_number.circadian_initial_sunset_offset.state,
            ),
        )
        end_time = self.parse_datetime(
            self.entities.input_datetime.circadian_end_time.state,
        )
        time_step = (end_time - start_time) / max(
            abs(
                float(self.entities.input_number.initial_circadian_brightness.state)
                - float(self.entities.input_number.final_circadian_brightness.state),
            )
            / self.constants["brightness_per_step"],
            abs(
                float(self.entities.input_number.initial_circadian_kelvin.state)
                - float(self.entities.input_number.final_circadian_kelvin.state),
            )
            / self.constants["kelvin_per_step"],
        )
        if time_step.total_seconds() < 0:
            self.error(
                "Circadian end time is before start time "
                f"(by {time_step.total_seconds()/-60} minutes)",
            )
            raise ValueError
        self.__circadian["start_time"] = start_time
        self.__circadian["end_time"] = end_time
        self.__circadian["time_step"] = time_step
        self.log(
            f"Circadian redated to start at {start_time.time()} with "
            f"time step of {time_step.total_seconds() / 60} minutes",
        )
        if self.control.scene == "Night":
            self.__start_circadian()

    def is_lighting_sufficient(self, room: str = "kitchen") -> bool:
        """Return if there is enough light to not require further lighting."""
        return (
            float(self.get_state(f"sensor.{room}_multisensor_luminance"))
            - self.__lighting_luminance()
            >= self.args["night_max_luminance"]
        )

    def __lighting_luminance(self, room: str = "kitchen") -> float:
        """Return approximate luminance of powered lights affecting light sensors."""
        return (
            self.lights[room].brightness
            / self.args["max_brightness"]
            * self.args["lighting_luminance_factor"]
        )

    def __handle_kitchen_luminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Change scene to day or night based on kitchen luminance levels."""
        del entity, attribute, old, kwargs
        if "Day" in self.control.scene:
            if (
                float(new) - self.__lighting_luminance()
                <= self.args["day_min_luminance"]
            ):
                self.log(f"Light levels are low ({new}%) transitioning to night scene")
                if self.control.apps["media"].is_playing:
                    self.control.scene = "TV"
                elif self.control.apps["presence"].anyone_home():
                    self.control.scene = "Night"
                else:
                    self.control.scene = "Away (Night)"
        elif self.control.scene == "Morning":
            self.__handle_kitchen_luminance_change_in_morning(new)
        elif (
            self.control.scene not in ("Bright", "Sleep")
            and float(new) - self.__lighting_luminance()
            >= self.args["night_max_luminance"]
        ):
            self.log(f"Light levels are high ({new}%), transitioning to day scene")
            self.control.scene = (
                "Day" if self.control.apps["presence"].anyone_home() else "Away (Day)"
            )

    def __handle_kitchen_luminance_change_in_morning(self, luminance: str):
        """Change kitchen vacancy lighting based on luminance levels."""
        if (
            float(luminance) - self.__lighting_luminance()
            >= self.args["night_max_luminance"]
        ):
            if self.lights["kitchen"].is_on_when_vacant():
                self.log(
                    f"Kitchen light levels are high ({luminance}%) "
                    "during morning scene, disabling kitchen vacancy light",
                )
                self.lights["kitchen"].set_presence_adjustments(
                    occupied=(
                        self.args["max_brightness"],
                        self.control.get_setting("morning_kelvin"),
                    ),
                    vacating_delay=self.control.get_setting("morning_vacating_delay"),
                )
        elif (
            float(luminance) - self.__lighting_luminance()
            <= self.args["day_min_luminance"]
            and not self.lights["kitchen"].is_on_when_vacant()
        ):
            self.log(
                f"Kitchen light levels are low ({luminance}%) during morning scene, "
                "enabling kitchen vacancy light",
            )
            kelvin = self.control.get_setting("morning_kelvin")
            self.lights["kitchen"].set_presence_adjustments(
                vacant=(self.control.get_setting("morning_brightness"), kelvin),
                occupied=(self.args["max_brightness"], kelvin),
                vacating_delay=self.control.get_setting("morning_vacating_delay"),
            )

    def __handle_bedroom_luminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Detect when to change scene from morning to day & set automatic lighting."""
        del entity, attribute, old, kwargs
        if self.control.scene == "Morning":
            if float(new) >= self.args["morning_max_luminance"]:
                self.log(
                    f"Bedroom light levels are high ({new}%), "
                    "transitioning to day scene",
                )
                self.control.scene = "Day"
        elif self.control.scene == "Day":
            if (
                float(new) - self.__lighting_luminance("bedroom")
                >= self.args["morning_max_luminance"]
            ):
                if not self.lights["bedroom"].is_ignoring_presence():
                    self.lights["bedroom"].ignore_presence()
                    self.log(
                        f"Bedroom light levels are high ({new}%), "
                        "automatic lighting disabled",
                    )
            elif self.lights["bedroom"].is_ignoring_presence():
                self.lights["bedroom"].set_presence_adjustments(
                    occupied=(
                        self.args["max_brightness"],
                        self.lights["bedroom"].kelvin_limits["max"],
                    ),
                    vacating_delay=self.control.get_setting(
                        "bedroom_vacating_delay",
                    ),
                )
                self.log(
                    f"Bedroom light levels are low ({new}%), "
                    "automatic lighting enabled",
                )


class Light:
    """Control an individual light (or a pre-configured group)."""

    def __init__(self, light_id: str, controller: Lights, room_name: str | None = None):
        """Initialise with attributes for light parameters, and a Light controller."""
        self.__light_id = light_id
        self.__controller = controller
        self.kelvin_limits = {
            "max": float(self.__get_attribute("max_color_temp_kelvin")),
            "min": float(self.__get_attribute("min_color_temp_kelvin")),
        }
        self.__kelvin_before_off = (
            self.__controller.args["max_brightness"]
            - self.__controller.args["min_brightness"]
        ) / 2
        self.room_name = (
            room_name
            if room_name is not None
            else (
                light_id.replace("light.", "")
                if light_id.startswith("light")
                else light_id.replace("group.", "").replace("_lights", "")
            )
        )
        self.__presence_adjustments = {"handle": None}
        self.__transition_timer = None

    @property
    def brightness(self) -> int:
        """Get the brightness of the light from Home Assistant."""
        return self.__get_attribute("brightness", 0)

    @brightness.setter
    def brightness(self, value: int):
        """Set and validate light's brightness."""
        if self.brightness != value:
            value = self.__validate_brightness(value)
            if value != 0:
                self.__controller.log(
                    f"Setting '{self.__light_id}' brightness to {value} "
                    f"(from {self.brightness})",
                    level="DEBUG",
                )
                self.__controller.turn_on(self.__light_id, brightness=value)
            else:
                self.turn_off()

    def __validate_brightness(self, value: int) -> int:
        """Return closest valid value for brightness."""
        validated_value = value
        if value < self.__controller.args["min_brightness"]:
            validated_value = (
                self.__controller.args["min_brightness"] if value > 0 else 0
            )
        elif value > self.__controller.args["max_brightness"]:
            validated_value = self.__controller.args["max_brightness"]
        if validated_value != value:
            self.__controller.log(
                f"Brightness ({value}) out of bounds for '{self.__light_id}'",
                level="WARNING",
            )
        return validated_value

    @property
    def kelvin(self) -> int:
        """Get the colour warmth value of the light from Home Assistant."""
        kelvin = self.__get_attribute("color_temp_kelvin")
        return kelvin if kelvin is not None else self.__kelvin_before_off

    @kelvin.setter
    def kelvin(self, value: int):
        """Set and validate light's warmth of colour."""
        value = self.__validate_kelvin(value)
        self.__controller.log(
            f"Setting {self.__light_id}'s kelvin to {value} (from {self.kelvin})",
            level="DEBUG",
        )
        self.__controller.turn_on(self.__light_id, kelvin=value)

    def __validate_kelvin(self, value: int) -> int:
        """Return closest valid value for kelvin."""
        validated_value = value
        if validated_value < self.kelvin_limits["min"]:
            validated_value = self.kelvin_limits["min"]
        if validated_value > self.kelvin_limits["max"]:
            validated_value = self.kelvin_limits["max"]
        if validated_value != value:
            self.__controller.log(
                f"Kelvin ({value}) out of bounds for '{self.__light_id}'",
                level="DEBUG",
            )
        return self.__controller.constants["kelvin_per_step"] * int(
            value / self.__controller.constants["kelvin_per_step"],
        )

    def adjust(self, brightness: int, kelvin: int):
        """Adjust light brightness and kelvin at the same time."""
        if brightness == 0:
            self.turn_off()
        else:
            brightness = self.__validate_brightness(brightness)
            kelvin = self.__validate_kelvin(kelvin)
            self.__controller.log(
                f"Adjusting '{self.__light_id}' to "
                f"brightness {brightness} and kelvin {kelvin} "
                f"(from {self.brightness} and {self.kelvin})",
                level="DEBUG",
            )
            self.__controller.turn_on(
                self.__light_id,
                brightness=brightness,
                kelvin=kelvin,
            )

    def adjust_to_max(self):
        """Adjust light brightness and kelvin at the same time to maximum values."""
        self.adjust(self.__controller.args["max_brightness"], self.kelvin_limits["max"])

    def turn_off(self):
        """Turn light off and record previous kelvin level."""
        if self.brightness != 0:
            self.__kelvin_before_off = self.kelvin
            self.__controller.log(
                f"Turning '{self.__light_id}' off (previously at"
                f" {self.brightness} brightness and {self.kelvin} kelvin)",
                level="DEBUG",
            )
            self.__controller.turn_off(self.__light_id)

    def __get_attribute(self, attribute: str, default: int | None = None) -> int:
        """Get light's attribute (of the first entity if it's a group)."""
        value = self.__controller.get_state(
            self.__light_id
            if not self.__light_id.startswith("group")
            else self.__controller.get_state(self.__light_id, attribute="entity_id")[0],
            attribute=attribute,
            default=default,
        )
        return int(value) if value is not None else None

    def set_presence_adjustments(
        self,
        vacant: tuple[int, int] = (0, 0),
        entered: tuple[int, int] = (0, 0),
        occupied: tuple[int, int] = (0, 0),
        transition_period: int = 0,
        vacating_delay: int = 0,
    ):
        """Configure the light to adjust based on presence in the room."""
        self.__transition_timer = None
        self.__presence_adjustments["vacant"] = {
            "brightness": vacant[0],
            "kelvin": vacant[1],
        }
        self.__presence_adjustments["entered"] = {
            "brightness": entered[0],
            "kelvin": entered[1],
        }
        self.__presence_adjustments["occupied"] = {
            "brightness": occupied[0],
            "kelvin": occupied[1],
        }
        self.__presence_adjustments["transition_period"] = transition_period
        presence = (
            "vacant"
            if self.__controller.control.apps["presence"]
            .rooms[self.room_name]
            .is_vacant(vacating_delay)
            else "occupied"
        )
        if transition_period != 0 and entered != (0, 0):
            if presence == "occupied":
                seconds_in_room = (
                    self.__controller.control.apps["presence"]
                    .rooms[self.room_name]
                    .seconds_in_room(vacating_delay)
                )
                if seconds_in_room < transition_period:
                    presence = "entered"
                    self.__start_transition_towards_occupied(
                        seconds_in_room / transition_period,
                    )
        elif transition_period != 0 or entered != (0, 0):
            self.__controller.log(
                f"'{self.__light_id}' set to transition with invalid parameters",
                level="WARNING",
            )
        if presence != "entered":
            self.adjust(
                self.__presence_adjustments[presence]["brightness"],
                self.__presence_adjustments[presence]["kelvin"],
            )
        if self.__presence_adjustments.get("vacating_delay") != vacating_delay:
            self.ignore_presence()
            self.__presence_adjustments["vacating_delay"] = vacating_delay
        if self.__presence_adjustments.get("handle") is None:
            self.__presence_adjustments["handle"] = (
                self.__controller.control.apps["presence"]
                .rooms[self.room_name]
                .register_callback(self.__handle_presence_change, vacating_delay)
            )
        self.__controller.log(
            f"Configured '{self.__light_id}' with presence '{presence}' and "
            f"presence adjustments: {self.__presence_adjustments}",
            level="DEBUG",
        )

    def ignore_presence(self):
        """Set light to ignore presence by cancelling its presence callback."""
        if self.__presence_adjustments.get("handle") is not None:
            self.__controller.control.apps["presence"].rooms[
                self.room_name
            ].cancel_callback(
                self.__presence_adjustments["handle"],
            )
            self.__presence_adjustments["handle"] = None

    def is_ignoring_presence(self) -> bool:
        """Check if the light is ignoring presence in the room or not."""
        return self.__presence_adjustments["handle"] is None

    def is_on_when_vacant(self) -> bool:
        """Check if the light is on when vacant."""
        if self.is_ignoring_presence():
            return self.brightness > 0
        return self.__presence_adjustments["vacant"]["brightness"] > 0

    def toggle_presence_adjustments(self, **adjustments: dict) -> bool:
        """Toggle between adjusting lighting based on presence and staying off."""
        if self.is_ignoring_presence():
            self.set_presence_adjustments(**adjustments)
            return True
        self.ignore_presence()
        self.turn_off()
        return False

    def __handle_presence_change(self, is_vacant: bool):  # noqa: FBT001
        """Adjust lighting based on presence in the room."""
        self.__transition_timer = None
        presence = "vacant" if is_vacant else "occupied"
        if (
            not is_vacant
            and self.__presence_adjustments["transition_period"] != 0
            and (
                self.__controller.control.apps["presence"]
                .rooms[self.room_name]
                .seconds_in_room(self.__presence_adjustments["vacating_delay"])
                < self.__presence_adjustments["transition_period"]
            )
        ):
            presence = "entered"
            self.__start_transition_towards_occupied()
        self.adjust(
            self.__presence_adjustments[presence]["brightness"],
            self.__presence_adjustments[presence]["kelvin"],
        )
        self.__controller.log(
            f"Lighting adjusted now the '{self.room_name}' is '{presence}'",
        )

    def __start_transition_towards_occupied(self, completion: float = 0):
        """Calculate the light change required and start the transition."""
        brightness_change = (
            self.__presence_adjustments["occupied"]["brightness"]
            - self.__presence_adjustments["entered"]["brightness"]
        ) * (1 - completion)
        kelvin_change = (
            self.__presence_adjustments["occupied"]["kelvin"]
            - self.__presence_adjustments["entered"]["kelvin"]
        ) * (1 - completion)
        if brightness_change == 0 and kelvin_change == 0:
            return
        steps = max(
            abs(brightness_change) / self.__controller.constants["brightness_per_step"],
            abs(kelvin_change) / self.__controller.constants["kelvin_per_step"],
            1,
        )
        max_steps = (
            self.__presence_adjustments["transition_period"]
            * self.__controller.constants["max_steps_per_second"]
        )
        if steps > max_steps:
            steps = max_steps
        brightness_step = brightness_change / steps
        kelvin_step = kelvin_change / steps
        step_time = self.__presence_adjustments["transition_period"] / steps
        self.__controller.log(
            "Starting transition from entered state to occupied state with: "
            f"steps = {steps}, step_time = {step_time}, "
            f"brightness_step = {brightness_step}, kelvin_step = {kelvin_step}, "
            f"completion = {completion}",
            level="DEBUG",
        )
        self.__transition_timer = uuid.uuid4().hex
        self.__controller.run_in(
            self.__transition_towards_occupied,
            step_time,
            brightness_step=brightness_step,
            kelvin_step=kelvin_step,
            steps_remaining=steps,
            step_time=step_time,
            timer_id=self.__transition_timer,
        )

    def __transition_towards_occupied(self, kwargs: dict):
        """Step towards occupied lighting settings."""
        if kwargs["timer_id"] != self.__transition_timer:
            return
        steps_remaining = kwargs["steps_remaining"] - 1
        if steps_remaining <= 0:
            self.__controller.log(
                f"Transition to occupied complete for '{self.__light_id}'",
                level="DEBUG",
            )
            self.__transition_timer = None
            self.adjust(
                self.__presence_adjustments["occupied"]["brightness"],
                self.__presence_adjustments["occupied"]["kelvin"],
            )
        else:
            self.__controller.log(
                f"Stepping towards occupied lighting for '{self.__light_id}'",
                level="DEBUG",
            )
            self.adjust(
                self.__presence_adjustments["occupied"]["brightness"]
                - kwargs["brightness_step"] * steps_remaining,
                self.__presence_adjustments["occupied"]["kelvin"]
                - kwargs["kelvin_step"] * steps_remaining,
            )
            self.__controller.run_in(
                self.__transition_towards_occupied,
                kwargs["step_time"],
                brightness_step=kwargs["brightness_step"],
                kelvin_step=kwargs["kelvin_step"],
                step_time=kwargs["step_time"],
                steps_remaining=steps_remaining,
                timer_id=kwargs["timer_id"],
            )
