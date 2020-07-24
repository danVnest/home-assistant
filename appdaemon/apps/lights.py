"""Coordinates lighting based primarily on scenes.

Includes circadian adjustments to brightness and kelvin settings for ergonomics,
localised adjustments based on presence callbacks, and luminance monitoring.

User defined variables are configued in lights.yaml
"""
import datetime
from typing import Tuple

import app


class Lights(app.App):
    """Control lights based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__circadian = {}
        self.lights = {}

    def initialize(self):
        """Initialise lights and start listening to scene events.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.redate_circadian(None)
        self.run_daily(self.redate_circadian, "00:00:01")
        self.lights["entryway"] = Light("group.entryway_lights", self)
        self.lights["kitchen"] = Light("light.kitchen", self)
        self.lights["tv"] = Light("group.tv_lights", self)
        self.lights["dining"] = Light("group.dining_lights", self)
        self.lights["hall"] = Light("light.hall", self)
        self.lights["bedroom"] = Light("light.bedroom", self)
        self.lights["office"] = Light("light.office", self)
        self.listen_state(
            self.__handle_luminance_change, "sensor.kitchen_multisensor_luminance",
        )

    def transition_to_scene(  # noqa: C901
        self, scene: str
    ):  # pylint: disable=too-many-branches
        """Change lighting based on the specified scene."""
        # TODO: add transition function to replace circadian, remove MotionLight
        if self.__circadian.get("timer") is not None:
            self.cancel_timer(self.__circadian["timer"])
            self.__circadian["timer"] = None
        if "Day" in scene:
            for light in self.lights.values():
                light.ignore_presence()
                light.turn_off()
        elif scene == "Night":
            self.__circadian_progression(None)
            self.__circadian["timer"] = self.run_every(
                self.__circadian_progression,
                self.datetime() + self.__circadian["time_step"],
                self.__circadian["time_step"].total_seconds(),
            )
        elif scene == "Bright":
            for light in self.lights.values():
                light.ignore_presence()
                light.adjust(self.args["max_brightness"], self.args["max_kelvin"])
        elif scene == "TV":
            kelvin = int(float(self.get_state("input_number.tv_kelvin")))
            self.lights["entryway"].set_presence_adjustments(
                occupied=(self.control.get_setting("tv_motion_brightness"), kelvin,)
            )
            self.lights["kitchen"].set_presence_adjustments(
                vacant=(self.control.get_setting("tv_brightness"), kelvin),
                entered=(self.control.get_setting("tv_motion_brightness"), kelvin),
                occupied=(self.args["max_brightness"], self.args["max_kelvin"]),
                transition_period=self.control.get_setting("tv_transition_period"),
                vacating_delay=self.control.get_setting("tv_vacating_delay"),
            )
            for light_name in ["tv", "hall"]:
                self.lights[light_name].adjust(
                    self.control.get_setting("tv_brightness"), kelvin
                )
            self.lights["dining"].turn_off()
        elif scene == "Sleep":
            for light_name in ["entryway", "kitchen"]:
                self.lights[light_name].set_presence_adjustments(
                    entered=(self.args["min_brightness"], self.args["min_kelvin"]),
                    occupied=(
                        self.control.get_setting("sleep_motion_brightness"),
                        self.control.get_setting("sleep_motion_kelvin"),
                    ),
                    transition_period=self.control.get_setting(
                        "sleep_transition_period"
                    ),
                    vacating_delay=self.control.get_setting("sleep_vacating_delay"),
                )
            for light_name in ["tv", "dining", "hall", "bedroom", "office"]:
                self.lights[light_name].turn_off()
        elif scene == "Morning":
            brightness = self.control.get_setting("morning_brightness")
            kelvin = self.control.get_setting("morning_kelvin")
            vacating_delay = self.control.get_setting("morning_vacating_delay")
            self.lights["entryway"].set_presence_adjustments(
                occupied=(brightness, kelvin), vacating_delay=vacating_delay
            )
            self.lights["kitchen"].set_presence_adjustments(
                vacant=(brightness, kelvin),
                occupied=(self.args["max_brightness"], kelvin),
                vacating_delay=vacating_delay,
            )
        elif scene == "Away (Night)":
            for light_name in ["entryway", "kitchen"]:
                self.lights[light_name].set_presence_adjustments(
                    occupied=(self.args["max_brightness"], self.args["max_kelvin"]),
                    vacating_delay=60,
                )
            self.lights["dining"].adjust(
                self.args["max_brightness"], self.args["max_kelvin"]
            )
            for light_name in ["tv", "dining", "hall", "bedroom", "office"]:
                self.lights[light_name].turn_off()
        self.log(f"Light scene changed to {scene}")

    def __circadian_progression(self, kwargs: dict):
        """Calculate appropriate lighting levels and implement."""
        del kwargs
        brightness, kelvin = self.calculate_circadian_brightness_kelvin()
        self.lights["entryway"].set_presence_adjustments(
            vacant=(self.args["min_brightness"], kelvin),
            occupied=(brightness, kelvin),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(brightness, kelvin),
            entered=(brightness, kelvin)
            if brightness > self.args["motion_brightness_threshold"]
            else (
                self.control.get_setting("night_motion_brightness"),
                self.control.get_setting("night_motion_kelvin"),
            ),
            occupied=(self.args["max_brightness"], kelvin),
            transition_period=self.control.get_setting("night_transition_period"),
            vacating_delay=self.control.get_setting("night_vacating_delay"),
        )
        for light_name in ["tv", "dining", "hall"]:
            self.lights[light_name].adjust(brightness, kelvin)
        for light_name in ["bedroom", "office"]:
            if self.lights[light_name].brightness != 0:
                self.lights[light_name].adjust(brightness, kelvin)
        self.log("Adjusted lighting based on circadian progression", level="DEBUG")

    def calculate_circadian_brightness_kelvin(self) -> Tuple[int, int]:
        """Calculate appropriate lighting levels based on the current time of night."""
        if (
            self.__circadian["start_time"].time()
            < self.time()
            < self.__circadian["end_time"].time()
        ):
            circadian_progress = (self.__circadian["end_time"] - self.datetime()) / (
                self.__circadian["end_time"] - self.__circadian["start_time"]
            )
            self.log(
                f"Circadian progress calculated as: {circadian_progress}",
                level="DEBUG",
            )
            return (
                int(
                    self.args["min_circadian_brightness"]
                    + (
                        self.args["max_circadian_brightness"]
                        - self.args["min_circadian_brightness"]
                    )
                    * circadian_progress
                ),
                int(
                    self.args["min_circadian_kelvin"]
                    + (
                        self.args["max_circadian_kelvin"]
                        - self.args["min_circadian_kelvin"]
                    )
                    * circadian_progress
                ),
            )
        self.cancel_timer(self.__circadian.get("timer"))
        if (
            self.parse_time(self.control.get_setting("morning_time"))
            < self.time()
            < self.__circadian["start_time"].time()
        ):
            self.log("Circadian progression not triggered - too early")
            return (
                self.args["max_circadian_brightness"],
                self.args["max_circadian_kelvin"],
            )
        self.log("Circadian progression not triggered - already completed")
        return (
            self.args["min_circadian_brightness"],
            self.args["min_circadian_kelvin"],
        )

    def redate_circadian(self, kwargs: dict):
        """Configure the start and end times for lighting adjustment for today."""
        del kwargs
        self.__circadian["start_time"] = datetime.datetime.combine(
            self.date(),
            self.parse_time(
                f"sunset - {self.get_state('input_datetime.circadian_start_sunset_offset')}"
            ),
        )
        self.__circadian["end_time"] = self.parse_datetime(
            self.get_state("input_datetime.circadian_end_time")
        )
        self.__circadian["time_step"] = (
            self.__circadian["end_time"] - self.__circadian["start_time"]
        ) / 125  # ≈125 discrete kelvin steps (more than the 100 brightness steps)

    def is_lighting_sufficient(self) -> bool:
        """Return if there is enough light to not require further lighting."""
        return (
            float(self.get_state("sensor.kitchen_multisensor_luminance"))
            - self.__lighting_luminance()
            >= self.args["night_max_luminance"]
        )

    def __lighting_luminance(self) -> float:
        """Return approximate luminance of the powered lighting affecting light sensors."""
        brightness = self.get_state("light.kitchen", attribute="brightness")
        return (
            0
            if brightness is None
            else brightness / 255 * self.args["lighting_luminance_factor"]
        )

    def __handle_luminance_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Change scene to day or night based on luminance levels."""
        del entity, attribute, old, kwargs
        if "Day" in self.scene:
            if (
                float(new) - self.__lighting_luminance()
                <= self.args["day_min_luminance"]
            ):
                self.log(f"Light levels are low ({new}%) transitioning to night scene")
                if self.control.media.is_playing:
                    self.scene = "TV"
                elif self.control.presence.anyone_home():
                    self.scene = "Night"
                else:
                    self.scene = "Away (Night)"
        elif self.scene != "Bright":
            if (
                float(new) - self.__lighting_luminance()
                >= self.args["night_max_luminance"]
            ):
                self.log(f"Light levels are high ({new}%), transitioning to day scene")
                self.scene = (
                    "Day" if self.control.presence.anyone_home() else "Away (Day)"
                )


class Light:
    """Control an individual light (or a pre-configured group)."""

    def __init__(self, light_id: str, controller: Lights):
        """Initialise with attributes for light parameters, and a Light controller."""
        self.light_id = light_id
        self.controller = controller
        self.__kelvin_before_off = (
            self.controller.args["max_brightness"]
            - self.controller.args["min_brightness"]
        ) / 2
        self.room_name = (
            light_id.split(".", 1)[-1]
            if light_id.startswith("light")
            else light_id.split(".", 1)[-1].split("_")[0]
        )
        self.__presence_adjustments = {}

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
                self.controller.log(
                    f"Setting {self.light_id}'s brightness to {value} (from {self.brightness})",
                    level="DEBUG",
                )
                self.controller.turn_on(self.light_id, brightness=value)
            else:
                self.turn_off()

    def __validate_brightness(self, value: int) -> int:
        """Return closest valid value for brightness."""
        validated_value = value
        if value < self.controller.args["min_brightness"]:
            validated_value = self.controller.args["min_brightness"] if value > 0 else 0
        elif value > self.controller.args["max_brightness"]:
            validated_value = self.controller.args["max_brightness"]
        if validated_value != value:
            self.controller.log(
                f"Brightness ({value}) out of bounds for '{self.light_id}'",
                level="WARNING",
            )
        return validated_value

    @property
    def kelvin(self) -> int:
        """Get the colour warmth value of the light from Home Assistant."""
        kelvin = self.__get_attribute("color_temp")
        return (
            20 * round(1e6 / 20 / kelvin)
            if kelvin is not None
            else self.__kelvin_before_off
        )  # Home Assistant uses mireds, so convert from kelvin and round to mired step

    @kelvin.setter
    def kelvin(self, value: int):
        """Set and validate light's warmth of colour."""
        value = self.__validate_kelvin(value)
        self.controller.log(
            f"Setting {self.light_id}'s kelvin to {value} (from {self.kelvin})",
            level="DEBUG",
        )
        self.controller.turn_on(self.light_id, kelvin=value)

    def __validate_kelvin(self, value: int) -> int:
        """Return closest valid value for kelvin."""
        validated_value = value
        if validated_value < self.controller.args["min_kelvin"]:
            validated_value = self.controller.args["min_kelvin"]
        if validated_value > self.controller.args["max_kelvin"]:
            validated_value = self.controller.args["max_kelvin"]
        if validated_value != value:
            self.controller.log(
                f"Kelvin ({value}) out of bounds for '{self.light_id}'",
                level="WARNING",
            )
        return 20 * int(value / 20)
        # 20 is the biggest mired step (1e6/222 - 1e6/223)

    def adjust(self, brightness: int, kelvin: int):
        """Adjust light brightness and kelvin at the same time."""
        if brightness == 0:
            self.turn_off()
        else:
            brightness = self.__validate_brightness(brightness)
            kelvin = self.__validate_kelvin(kelvin)
            self.controller.log(
                f"Adjusting {self.light_id} to {brightness} & {kelvin}K '"
                f"(from {self.brightness} & {self.kelvin})",
                level="DEBUG",
            )
            self.controller.turn_on(self.light_id, brightness=brightness, kelvin=kelvin)

    def turn_off(self):
        """Turn light off and record previous kelvin level."""
        if self.brightness != 0:
            self.__kelvin_before_off = self.kelvin
            self.controller.log(
                f"Turning {self.light_id}'s off "
                f"(previously at {self.brightness} brightness, {self.kelvin} kelvin)",
                level="DEBUG",
            )
            self.controller.turn_off(self.light_id)

    def __get_attribute(self, attribute: str, default: int = None) -> int:
        """Get light's attribute (of the first entity if it's a group)."""
        value = self.controller.get_state(
            self.light_id
            if not self.light_id.startswith("group")
            else self.controller.get_state(self.light_id, attribute="entity_id")[0],
            attribute=attribute,
            default=default,
        )
        return int(value) if value is not None else None

    def set_presence_adjustments(
        self,
        vacant: Tuple[int, int] = (0, 0),
        entered: Tuple[int, int] = (0, 0),
        occupied: Tuple[int, int] = (0, 0),
        transition_period: int = 0,
        vacating_delay: int = 0,
    ):  # pylint: disable=too-many-arguments
        """Configure the light to adjust based on presence in the room."""
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
            if self.controller.control.presence.rooms[self.room_name].is_vacant
            else "occupied"
        )
        if transition_period != 0 and entered != (0, 0):
            self.controller.log(
                f"Start transition", level="DEBUG",
            )
            if presence == "occupied":
                seconds_in_room = self.controller.control.presence.rooms[
                    self.room_name
                ].seconds_in_room()
                if transition_period < seconds_in_room:
                    presence = "entered"
                    # TODO: calculate how far through the transition we should be
        elif transition_period != 0 or entered != (0, 0):
            self.controller.log(
                f"{self.light_id} set to transition with invalid parameters",
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
            self.__presence_adjustments[
                "handle"
            ] = self.controller.control.presence.rooms[
                self.room_name
            ].register_callback(
                self.__handle_presence_change, vacating_delay
            )
        self.controller.log(
            f"Configured {self.light_id}'s presence adjustments", level="DEBUG",
        )

    def ignore_presence(self):
        """Set light to ignore presence by cancelling its presence callback."""
        if self.__presence_adjustments.get("handle") is not None:
            self.controller.control.presence.rooms[self.room_name].cancel_callback(
                self.__presence_adjustments["handle"]
            )
            self.__presence_adjustments["handle"] = None

    def __handle_presence_change(self, is_vacant: bool):
        """Adjust lighting based on presence in the room."""
        presence = "vacant" if is_vacant else "occupied"
        # if presence == "occupied" and self.__presence_adjustments["transition_period"] != 0:
        #    presence = "entered"
        self.adjust(
            self.__presence_adjustments[presence]["brightness"],
            self.__presence_adjustments[presence]["kelvin"],
        )


class MotionLight(Light):
    """Control a light based on scene and motion detected by a sensor."""

    def __init__(self, light_id: str, sensor_id: str, controller: Lights):
        """Initialise with extra attributes for different states of motion."""
        super().__init__(light_id, controller)
        self.sensor_id = sensor_id
        self.state = "no_motion"
        self.handles = dict.fromkeys(
            ["no_motion_listener", "motion_listener", "long_motion_transitioner"]
        )
        self.scene_state_attributes = {
            "Day": {
                "no_motion": {"brightness": 0, "kelvin": None, "delay": "off"},
                "motion": {"brightness": 0, "kelvin": None},
                "long_motion": {"brightness": 0, "kelvin": None, "delay": "off"},
            }
        }
        self.scene_state_attributes["Away (Day)"] = self.scene_state_attributes["Day"]
        self.configure_scene_with_user_args("Bright")
        self.scene_state_attributes["Morning"] = self.scene_state_attributes["Bright"]
        self._scene = "Day"

    @property
    def scene(self) -> str:
        """Return what scene is currently set."""
        return self._scene

    @scene.setter
    def scene(self, new_scene: str):
        """Adjust to lighting for the new scene and reconfigure motion capture."""
        if new_scene not in self.scene_state_attributes:
            self.configure_scene_with_user_args(new_scene)
        if (
            self.scene_state_attributes[new_scene]["no_motion"]["delay"]
            != self.scene_state_attributes[self._scene]["no_motion"]["delay"]
        ):
            self.configure_motion_monitoring(
                self.scene_state_attributes[new_scene]["no_motion"]["delay"],
                self.scene_state_attributes[self._scene]["no_motion"]["delay"],
            )
        self._scene = new_scene
        if self.state != "motion":
            self.adjust_with_scene_state_attributes()
        else:
            self.validate_long_motion_transition()

    def configure_motion_monitoring(self, new_delay, old_delay):
        """Start, stop, or reconfigure motion monitoring based on new and old delays."""
        monitor_motion = new_delay != "off"
        reconfigure = monitor_motion is True and old_delay != "off"
        if monitor_motion is False or reconfigure is True:
            self.controller.cancel_listen_state(self.handles["no_motion_listener"])
            self.handles["no_motion_listener"] = None
        if monitor_motion is False:
            self.state = "no_motion"
            self.controller.cancel_listen_state(self.handles["motion_listener"])
            self.handles["motion_listener"] = None
            if self.handles["long_motion_transitioner"] is not None:
                self.controller.cancel_timer(self.handles["long_motion_transitioner"])
                self.handles["long_motion_transitioner"] = None
        else:
            self.handles["no_motion_listener"] = self.controller.listen_state(
                self.no_motion,
                self.sensor_id,
                new="0",
                duration=new_delay,
                immediate=True,
            )
            if reconfigure is False:
                self.handles["motion_listener"] = self.controller.listen_state(
                    self.motion, self.sensor_id, old="0"
                )
        self.controller.log(
            f"Motion monitoring for {self.light_id} is"
            f" {'on' if monitor_motion is True else 'off'}"
            f"{' and reconfigured' if reconfigure is True else ''}",
            level="DEBUG",
        )

    def configure_scene_with_user_args(self, scene: str):
        """Configure and store scene attributes with user specified values."""
        yaml_scene = "away_night" if scene == "Away (Night)" else scene.lower()
        self.scene_state_attributes[scene] = {
            "no_motion": {
                "brightness": self.controller.args[f"{yaml_scene}_brightness"],
                "kelvin": self.controller.args[f"{yaml_scene}_kelvin"],
                "delay": self.controller.args[f"{yaml_scene}_no_motion_delay"],
            },
            "motion": {
                "brightness": self.controller.args[f"{yaml_scene}_motion_brightness"],
                "kelvin": self.controller.args[f"{yaml_scene}_motion_kelvin"],
            },
            "long_motion": {
                "brightness": self.controller.args[
                    f"{yaml_scene}_long_motion_brightness"
                ],
                "kelvin": self.controller.args[f"{yaml_scene}_long_motion_kelvin"],
                "delay": self.controller.args[f"{yaml_scene}_long_motion_delay"],
            },
        }

    def adjust_with_scene_state_attributes(self):
        """Adjust light with attributes for the current scene and state."""
        self.adjust(
            self.scene_state_attributes[self.scene][self.state]["brightness"],
            self.scene_state_attributes[self.scene][self.state]["kelvin"],
        )

    def adjust_circadian(self, brightness: int, kelvin: int):
        """Adjust lighting with new circadian values. Configures long_motion if dim."""
        self.scene_state_attributes[self.scene]["long_motion"]["delay"] = (
            "off"
            if brightness > self.controller.args["motion_brightness_threshold"]
            else self.controller.args["night_long_motion_delay"]
        )
        self.scene_state_attributes[self.scene]["no_motion"]["brightness"] = brightness
        self.scene_state_attributes[self.scene]["no_motion"]["kelvin"] = kelvin
        if self.handles["long_motion_transitioner"] is not None:
            self.scene_state_attributes[self.scene]["motion"]["brightness"] = brightness
            self.scene_state_attributes[self.scene]["motion"]["kelvin"] = kelvin
        if self.state == "no_motion":
            self.adjust_with_scene_state_attributes()

    def no_motion(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Adjust lighting when no motion detected, and configure for new state."""
        del entity, attribute, old, new, kwargs
        self.state = "no_motion"
        self.controller.log(f"No motion detected on {self.sensor_id}")
        if self.handles["long_motion_transitioner"] is not None:
            self.controller.cancel_timer(self.handles["long_motion_transitioner"])
            self.handles["long_motion_transitioner"] = None
        self.adjust_with_scene_state_attributes()

    def motion(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Set state to 'motion' and adjust lighting when motion is detected."""
        del entity, attribute, old, new, kwargs
        if self.state == "no_motion":
            self.state = "motion"
            self.controller.log(f"Motion detected on {self.sensor_id}")
            self.adjust_with_scene_state_attributes()
            self.start_transition_to_long_motion()

    def start_transition_to_long_motion(self):
        """Calculate the light change required and start the transition."""
        if self.scene_state_attributes[self.scene]["long_motion"]["delay"] != "off":
            (
                brightness_step,
                kelvin_step,
                steps,
                step_time,
            ) = self.calculate_long_motion_step()
            self.handles["long_motion_transitioner"] = self.controller.run_in(
                self.transition_towards_long_motion,
                step_time,
                brightness_step=brightness_step,
                kelvin_step=kelvin_step,
                steps_remaining=steps,
                step_time=step_time,
                scene=self.scene,
            )

    def calculate_long_motion_step(self) -> Tuple[int, int, int, float]:
        """Calculate the light change required for each transition step."""
        brightness_change = (
            self.scene_state_attributes[self.scene]["long_motion"]["brightness"]
            - self.brightness
        )
        kelvin_change = (
            self.scene_state_attributes[self.scene]["long_motion"]["kelvin"]
            - self.kelvin
        )
        if brightness_change == 0 and kelvin_change == 0:
            return (0, 0, 0, 0)
        mired_change = (
            1e6 / self.scene_state_attributes[self.scene]["long_motion"]["kelvin"]
            - 1e6 / self.kelvin
        )  # lights change in 1 mired increments, not kelvin
        steps = max(
            abs(brightness_change), int(255 / (400 - 222) * abs(mired_change)), 1
        )  # normalises to help account for non-proportional change between units
        if (
            steps > self.scene_state_attributes[self.scene]["long_motion"]["delay"] * 2
        ):  # can handle up to 2 steps per second
            steps = self.scene_state_attributes[self.scene]["long_motion"]["delay"] * 2
        brightness_step = brightness_change / steps
        kelvin_step = kelvin_change / steps
        step_time = (
            self.scene_state_attributes[self.scene]["long_motion"]["delay"] / steps
        )
        self.controller.log(
            f"Transitioning to long motion state with: steps = {steps}, step_time ="
            f" {step_time}, brightness_step = {brightness_step}, kelvin_step ="
            f" {kelvin_step}",
            level="DEBUG",
        )
        return brightness_step, kelvin_step, steps, step_time

    def validate_long_motion_transition(self):
        """Ensure lighting values are between initial and long motion values."""
        brightness_limits = sorted(
            [
                self.scene_state_attributes[self.scene]["motion"]["brightness"],
                self.scene_state_attributes[self.scene]["long_motion"]["brightness"],
            ]
        )
        kelvin_limits = sorted(
            [
                self.scene_state_attributes[self.scene]["motion"]["kelvin"],
                self.scene_state_attributes[self.scene]["long_motion"]["kelvin"],
            ]
        )
        if self.brightness < brightness_limits[0]:
            self.brightness = brightness_limits[0]
        elif self.brightness > brightness_limits[1]:
            self.brightness = brightness_limits[1]
        if self.kelvin < kelvin_limits[0]:
            self.kelvin = kelvin_limits[0]
        elif self.kelvin > kelvin_limits[1]:
            self.kelvin = kelvin_limits[1]

    def transition_towards_long_motion(self, kwargs: dict):
        """Step towards lighting settings for after a long period of motion."""
        if self.state == "motion":
            if kwargs["scene"] != self.scene:
                (
                    kwargs["brightness_step"],
                    kwargs["kelvin_step"],
                    kwargs["steps_remaining"],
                    kwargs["step_time"],
                ) = self.calculate_long_motion_step()
            steps_remaining = kwargs["steps_remaining"] - 1
            if steps_remaining <= 0:
                self.controller.log(
                    f"Long motion transition complete for {self.light_id}",
                    level="DEBUG",
                )
                self.handles["long_motion_transitioner"] = None
                self.state = "long_motion"
                self.adjust_with_scene_state_attributes()
            else:
                self.adjust(
                    self.scene_state_attributes[self.scene]["long_motion"]["brightness"]
                    - kwargs["brightness_step"] * steps_remaining,
                    self.scene_state_attributes[self.scene]["long_motion"]["kelvin"]
                    - kwargs["kelvin_step"] * steps_remaining,
                )
                self.handles["long_motion_transitioner"] = self.controller.run_in(
                    self.transition_towards_long_motion,
                    kwargs["step_time"],
                    brightness_step=kwargs["brightness_step"],
                    kelvin_step=kwargs["kelvin_step"],
                    step_time=kwargs["step_time"],
                    steps_remaining=steps_remaining,
                    scene=self.scene,
                )
