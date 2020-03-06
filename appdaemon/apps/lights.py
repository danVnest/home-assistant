"""Coordinates lighting based primarily on scenes.

Includes circadian adjustments to brightness and kelvin settings for ergonomics.
Also includes localised adjustments based on motion detection.

User defined variables are configued in lights.yaml
"""
import datetime
from typing import Tuple

import appdaemon.plugins.hass.hassapi as hass


class Lights(hass.Hass):
    """Control lights based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        self.circadian_brightness = None
        self.circadian_kelvin = None
        self.circadian_timer = None
        self.circadian_start_datetime = None
        self.circadian_end_datetime = None
        self.lights = {"tv": None, "hall": None, "bedroom": None, "kitchen": None}
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Initialise lights and start listening to scene events.

        Appdaemon defined init function called once ready after __init__.
        """
        self.redate_circadian(None)
        self.run_daily(self.redate_circadian, "00:00:01")
        self.lights["tv"] = Light("group.tv_lights", self)
        self.lights["hall"] = Light("light.hall", self)
        self.lights["bedroom"] = Light("light.bedroom", self)
        self.lights["kitchen"] = MotionLight(
            "light.kitchen", "sensor.kitchen_multisensor_motion", self
        )
        self.listen_event(self.scene_change, "SCENE")

    def scene_change(self, event_name: str, data: dict, kwargs: dict):
        """Change lighting based on the new scene."""
        del event_name, kwargs
        if self.circadian_timer is not None:
            self.cancel_timer(self.circadian_timer)
            self.circadian_timer = None
        self.lights["kitchen"].scene = data["scene"]
        if data["scene"] == "day":
            self.lights["tv"].brightness = 0
            self.lights["hall"].brightness = 0
            self.lights["bedroom"].brightness = 0
        elif data["scene"] == "night":
            self.circadian_progression(None)
            self.circadian_timer = self.run_every(
                self.circadian_progression,
                self.datetime() + datetime.timedelta(seconds=60),
                60,
            )  # (end_time - start_time) / 255) ≈ 60 seconds
        elif data["scene"] == "bright":
            bright = 255
            white = 4500
            self.lights["tv"].adjust(bright, white)
            self.lights["hall"].adjust(bright, white)
            self.lights["bedroom"].adjust(bright, white)
        elif data["scene"] == "tv":
            self.lights["tv"].adjust(self.args["tv_brightness"], self.args["tv_kelvin"])
            self.lights["hall"].adjust(
                self.args["tv_brightness"], self.args["tv_kelvin"]
            )
        elif data["scene"] == "sleep":
            self.lights["tv"].brightness = 0
            self.lights["hall"].brightness = 0
            self.lights["bedroom"].brightness = 0
        elif data["scene"] == "away_day":
            self.lights["tv"].brightness = 0
            self.lights["hall"].brightness = 0
            self.lights["bedroom"].brightness = 0
        elif data["scene"] == "away_night":
            self.lights["tv"].brightness = 0
            self.lights["hall"].adjust(255, kelvin=4500)
            self.lights["bedroom"].brightness = 0
        self.log(f"Light scene changed to {data['scene']}")

    def circadian_progression(self, kwargs: dict):
        """Calculate appropriate lighting levels and implement."""
        del kwargs
        self.calculate_circadian_progress()
        self.lights["kitchen"].adjust_circadian(
            self.circadian_brightness, self.circadian_kelvin
        )
        self.lights["tv"].adjust(self.circadian_brightness, self.circadian_kelvin)
        self.lights["hall"].adjust(self.circadian_brightness, self.circadian_kelvin)
        self.log("Adjusted lighting based on circadian progression", level="DEBUG")

    def calculate_circadian_progress(self):
        """Calculate appropriate lighting levels based on the current time of night."""
        time = self.time()
        if time <= self.circadian_start_datetime.time():
            self.circadian_brightness = self.args["max_circadian_brightness"]
            self.circadian_kelvin = self.args["max_circadian_kelvin"]
            self.cancel_timer(self.circadian_timer)
            self.log("Circadian progression not triggered - too early")
        elif time >= self.circadian_end_datetime.time():
            self.circadian_brightness = self.args["min_circadian_brightness"]
            self.circadian_kelvin = self.args["min_circadian_kelvin"]
            self.cancel_timer(self.circadian_timer)
            self.log("Circadian progression not triggered - already completed")
        else:
            circadian_progress = (self.circadian_end_datetime - self.datetime()) / (
                self.circadian_end_datetime - self.circadian_start_datetime
            )
            self.circadian_brightness = (
                self.args["min_circadian_brightness"]
                + (
                    self.args["max_circadian_brightness"]
                    - self.args["min_circadian_brightness"]
                )
                * circadian_progress
            )
            self.circadian_kelvin = (
                self.args["min_circadian_kelvin"]
                + (
                    self.args["max_circadian_kelvin"]
                    - self.args["min_circadian_kelvin"]
                )
                * circadian_progress
            )
            self.log(
                f"Circadian progress calculated as: {circadian_progress}",
                level="DEBUG",
            )

    def redate_circadian(self, kwargs: dict):
        """Configure the start and end times for lighting adjustment for today."""
        del kwargs
        self.circadian_start_datetime = datetime.datetime.combine(
            self.date(),
            (
                self.sunset()
                - datetime.timedelta(minutes=self.args["circadian_start_sunset_offset"])
            ).time(),
        )
        self.circadian_end_datetime = datetime.datetime.combine(
            self.date(),
            datetime.datetime.strptime(self.args["circadian_end_time"], "%H:%M").time(),
        )


class Light:
    """Control an individual light (or a pre-configured group)."""

    def __init__(self, light_id: str, controller: Lights):
        """Initialise with attributes for light parameters, and a Light controller."""
        self.light_id = light_id
        self.controller = controller
        self.brightness_before_off = 0
        self.kelvin_before_off = 4500
        self.dimmable = (
            self.controller.get_state(self.light_id, attribute="supported_features")
            == "0"
        )

    @property
    def brightness(self) -> int:
        """Get the brightness of the light from Home Assistant."""
        if self.dimmable is True:
            brightness = self.get_attribute("brightness")
            if brightness is None:
                brightness = 0
        else:
            brightness = 255 if self.controller.get_state(self.light_id) == "on" else 0
        return brightness

    @brightness.setter
    def brightness(self, value):
        """Set and validate light's brightness."""
        value = round(value)
        if self.brightness != value:
            if value < 0 or value > 255:
                self.controller.log(
                    f"Brightness ({value}) out of bounds for'{self.light_id}'",
                    level="WARNING",
                )
            self.controller.log(
                f"Setting {self.light_id}'s brightness from {self.brightness} to {value}",
                level="DEBUG",
            )
            if value != 0:
                self.controller.turn_on(self.light_id, brightness=value)
            else:
                self.brightness_before_off = self.brightness
                self.kelvin_before_off = self.kelvin
                self.controller.turn_off(self.light_id)

    @property
    def kelvin(self) -> int:
        """Get the colour warmth value of the light from Home Assistant."""
        kelvin = self.get_attribute("color_temp")
        return (
            20 * round(1e6 / 20 / kelvin)
            if kelvin is not None
            else self.kelvin_before_off
        )  # Home Assistant uses mireds, so convert from kelvin and round to mired step

    @kelvin.setter
    def kelvin(self, value):
        """Set and validate light's warmth of colour."""
        if value is not None and self.dimmable is True:
            value = 20 * round(
                value / 20
            )  # 20 is the biggest mired step (1e6/222 - 1e6/223)
            if self.kelvin != value:
                if value < 2000 or value > 4500:
                    self.controller.log(
                        f"Kelvin ({value}) out of bounds for'{self.light_id}'",
                        level="WARNING",
                    )
                self.controller.log(
                    f"Setting {self.light_id}'s kelvin from {self.kelvin} to {value}",
                    level="DEBUG",
                )
                self.controller.turn_on(self.light_id, kelvin=value)

    def adjust(self, brightness: int, kelvin: int):
        """Intelligently adjusts light brightness and kelvin in the nicest order."""
        change_kelvin_first = True
        if brightness <= self.brightness:  # when dimming, nice if brightness goes first
            change_kelvin_first = False
        elif self.brightness == 0:
            brightness_too_different = abs(brightness - self.brightness_before_off) > 50
            kelvin_too_different = abs(kelvin - self.kelvin_before_off) > 1000
            if brightness_too_different and kelvin_too_different:
                self.brightness = 1  # nice if turned on at low brightness first
            elif brightness_too_different:
                change_kelvin_first = False
        if change_kelvin_first:
            self.kelvin = kelvin
            self.brightness = brightness
        else:
            self.brightness = brightness
            self.kelvin = kelvin

    def get_attribute(self, attribute) -> int:
        """Get light's attribute (of the first entity if it's a group)."""
        value = self.controller.get_state(
            self.light_id
            if not self.light_id.startswith("group")
            else self.controller.get_state(self.light_id, attribute="entity_id")[0],
            attribute=attribute,
        )
        return float(value) if value is not None else None


class MotionLight(Light):
    """Control a light based on scene and motion detected by a sensor."""

    def __init__(self, light_id: str, sensor_id: str, controller: Lights):
        """Initialise with extra attributes for different states of motion."""
        super().__init__(light_id, controller)
        self.sensor_id = sensor_id
        self.state = "no_motion"
        self.detectors = dict.fromkeys(["no_motion", "motion"])
        self.long_motion_transitioner = None
        self.scene_state_attributes = {
            "day": {
                "no_motion": {"brightness": 0, "kelvin": None, "delay": "off"},
                "motion": {"brightness": 0, "kelvin": None},
                "long_motion": {"brightness": 0, "kelvin": None, "delay": "off"},
            }
        }
        self.scene_state_attributes["day_away"] = self.scene_state_attributes["day"]
        self._scene = "day"

    @property
    def scene(self) -> str:
        """Return what scene is currently set."""
        return self._scene

    @scene.setter
    def scene(self, new_scene: str):
        """Adjust to lighting for the new scene and reconfigure motion capture."""
        old_scene = self._scene
        if new_scene not in self.scene_state_attributes:
            self.configure_scene_with_user_args(new_scene)
        if (
            self.scene_state_attributes[old_scene]["no_motion"]["delay"]
            != self.scene_state_attributes[new_scene]["no_motion"]["delay"]
        ):
            self.configure_motion_monitoring(
                self.scene_state_attributes[new_scene]["no_motion"]["delay"],
                self.scene_state_attributes[old_scene]["no_motion"]["delay"],
            )
        self._scene = new_scene
        if self.state != "motion" or self.is_long_motion_transition_valid() is False:
            self.adjust_with_scene_state_attributes()

    def configure_motion_monitoring(self, new_delay, old_delay):
        """Start, stop, or reconfigure motion monitoring based on new and old delays."""
        monitor_motion = new_delay != "off"
        reconfigure = monitor_motion is True and old_delay != "off"
        if monitor_motion is False or reconfigure is True:
            self.controller.cancel_listen_state(self.detectors["no_motion"])
            self.detectors["no_motion"] = None
        if monitor_motion is False:
            self.state = "no_motion"
            self.controller.cancel_listen_state(self.detectors["motion"])
            self.detectors["motion"] = None
            if self.long_motion_transitioner is not None:
                self.controller.cancel_timer(self.long_motion_transitioner)
                self.long_motion_transitioner = None
        else:
            self.detectors["no_motion"] = self.controller.listen_state(
                self.no_motion,
                self.sensor_id,
                new="0",
                duration=new_delay,
                immediate=True,
            )
            if reconfigure is False:
                self.detectors["motion"] = self.controller.listen_state(
                    self.motion, self.sensor_id, old="0"
                )
        self.controller.log(
            f"Motion monitoring for {self.light_id} is"
            f" {'on' if monitor_motion is True else 'off'}"
            f"{' and reconfigured' if reconfigure is True else ''}"
        )

    def turn_off_and_disable(self):
        """Turn the light off and disable future changes based on motion (aka day)."""
        self.scene = "day"

    def configure_scene_with_user_args(self, scene: str):
        """Configure and store scene attributes with user specified values."""
        self.scene_state_attributes[scene] = {
            "no_motion": {
                "brightness": self.controller.args[f"{scene}_brightness"],
                "kelvin": self.controller.args[f"{scene}_kelvin"],
                "delay": self.controller.args[f"{scene}_no_motion_delay"],
            },
            "motion": {
                "brightness": self.controller.args[f"{scene}_motion_brightness"],
                "kelvin": self.controller.args[f"{scene}_motion_kelvin"],
            },
            "long_motion": {
                "brightness": self.controller.args[f"{scene}_long_motion_brightness"],
                "kelvin": self.controller.args[f"{scene}_long_motion_kelvin"],
                "delay": self.controller.args[f"{scene}_long_motion_delay"],
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
        if self.long_motion_transitioner is not None:
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
        if self.long_motion_transitioner is not None:
            self.controller.cancel_timer(self.long_motion_transitioner)
            self.long_motion_transitioner = None
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
            self.long_motion_transitioner = self.controller.run_in(
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
            abs(brightness_change), int(255 / (400 - 222) * abs(mired_change))
        )  # normalises to help account for non-proportional change between units
        if (
            steps > self.scene_state_attributes[self.scene]["long_motion"]["delay"] * 2
        ):  # can handle up to 2 steps per second
            steps = self.scene_state_attributes[self.scene]["long_motion"]["delay"] * 2
        return (
            brightness_change / steps if brightness_change != 0 else 0,
            kelvin_change / steps if kelvin_change != 0 else 0,
            steps,
            self.scene_state_attributes[self.scene]["long_motion"]["delay"] / steps,
        )

    def is_long_motion_transition_valid(self) -> bool:
        """Check if lighting values are between initial and long motion values."""
        brightness = sorted(
            [
                self.scene_state_attributes[self.scene]["motion"]["brightness"],
                self.scene_state_attributes[self.scene]["long_motion"]["brightness"],
            ]
        )
        if not brightness[0] < self.brightness < brightness[1]:
            return False
        kelvin = sorted(
            [
                self.scene_state_attributes[self.scene]["motion"]["kelvin"],
                self.scene_state_attributes[self.scene]["long_motion"]["kelvin"],
            ]
        )
        if not kelvin[0] < self.brightness < kelvin[1]:
            return False
        return True

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
                    f"Long motion transition complete for {self.light_id}"
                )
                self.long_motion_transitioner = None
                self.state = "long_motion"
                self.adjust_with_scene_state_attributes()
            else:
                self.adjust(
                    self.scene_state_attributes[self.scene]["long_motion"]["brightness"]
                    - kwargs["brightness_step"] * steps_remaining,
                    self.scene_state_attributes[self.scene]["long_motion"]["kelvin"]
                    - kwargs["kelvin_step"] * steps_remaining,
                )
                self.long_motion_transitioner = self.controller.run_in(
                    self.transition_towards_long_motion,
                    kwargs["step_time"],
                    brightness_step=kwargs["brightness_step"],
                    kelvin_step=kwargs["kelvin_step"],
                    step_time=kwargs["step_time"],
                    steps_remaining=steps_remaining,
                    scene=self.scene,
                )
            self.controller.log(
                f"Brightness: {self.brightness}"
                f" Kelvin: {self.kelvin}"
                f" Step: {kwargs['steps_remaining']}"
            )
