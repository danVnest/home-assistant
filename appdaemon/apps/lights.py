"""Coordinates lighting based primarily on scenes.

Includes circadian adjustments to brightness and kelvin settings for ergonomics,
localised adjustments based on presence callbacks, and illuminance monitoring.

User defined variables are configued in lights.yaml
"""

from __future__ import annotations

import datetime
import logging

from app import App
from presence import PresenceDevice


class Lights(App):
    """Control lights based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.circadian = {"timer": None}
        self.__lights: dict[str, Light] = {}
        self.constants["brightness_per_step"] = 2.55
        self.constants["kelvin_per_step"] = 20
        self.constants["max_steps_per_second"] = 2
        # TODO: these constants should either be in yaml or determined another way (remove in app.py as well)

    def initialize(self):
        """Initialise lights and start listening to scene events.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.lights["entryway"] = Light(
            device_id="group.entryway_lights",
            controller=self,
            room="entryway",
            linked_rooms=["front_door"],
        )
        self.lights["kitchen"] = Light(
            device_id="light.kitchen",
            controller=self,
            room="kitchen",
            linked_rooms=["back_deck"],
        )
        self.lights["kitchen_strip"] = Light(
            device_id="light.kitchen_strip",
            controller=self,
            room="kitchen",
        )
        self.lights["tv"] = Light(
            device_id="group.tv_lights",
            controller=self,
            room="living_room",
        )
        self.lights["dining_room"] = Light(
            device_id="group.dining_room_lights",
            controller=self,
            room="dining_room",
        )
        self.lights["hall"] = Light(
            device_id="light.hall",
            controller=self,
            room="living_room",
        )
        self.lights["office"] = Light(
            device_id="light.office",
            controller=self,
            room="office",
        )
        self.lights["bedroom"] = Light(
            device_id="light.bedroom",
            controller=self,
            room="bedroom",
        )
        self.lights["nursery"] = Light(
            device_id="light.nursery",
            controller=self,
            room="nursery",
        )
        self.lights["bathroom"] = Light(
            device_id="light.bathroom",
            controller=self,
            room="bathroom",
        )
        self.redate_circadian()
        self.run_daily(self.redate_circadian, "00:00:01")
        self.listen_state(
            self.handle_dark_outside,
            "binary_sensor.dark_outside",
            new="on",
        )
        self.listen_state(
            self.handle_bright_outside,
            "binary_sensor.dark_outside",
            new="off",
            duration=self.constants["night_to_day_delay"],
        )
        # TODO: https://app.asana.com/0/1207020279479204/1207351651288716/f
        # add to Light definition?
        for room in ("kitchen", "bedroom", "nursery"):
            self.listen_state(
                getattr(self, f"handle_{room}_illuminance_change"),
                f"sensor.{room}_presence_sensor_illuminance",
            )

    def terminate(self):
        """Cancel presence callbacks before termination (auto run by Appdaemon)."""
        for light in self.lights.values():
            light.ignore_vacancy()

    def transition_to_scene(self, scene: str):
        """Change lighting based on the specified scene."""
        self.cancel_timer(self.circadian["timer"])
        if scene == "Night":
            self.start_circadian()
        elif "Day" in scene:
            self.transition_to_day_scene()
        elif scene == "Away (Night)":
            self.transition_to_away_night_scene()
        else:
            getattr(self, f"transition_to_{scene.lower()}_scene")()
        self.log(f"Light scene changed to '{scene}'")

    def transition_to_day_scene(self):
        """Configure lighting for the day scene."""
        light_names = ["office", "bathroom"]
        light_names.extend(
            light_name
            for light_name in ("bedroom", "nursery")
            if not self.is_lighting_sufficient(light_name)
            and not self.control.napping_in(light_name)
        )
        for light_name in light_names:
            self.lights[light_name].set_presence_adjustments(
                occupied=(
                    self.constants["max_brightness"],
                    self.lights[light_name].kelvin_limits["max"],
                ),
                vacating_delay=self.get_setting(f"{light_name}_vacating_delay"),
                # TODO: https://app.asana.com/0/1207020279479204/1207237490859329/f
                # this is always? the same, don't pass as an argument
            )
        for light_name in (
            "entryway",
            "kitchen",
            "kitchen_strip",
            "tv",
            "dining_room",
            "hall",
        ):
            self.lights[light_name].ignore_vacancy()
            self.lights[light_name].turn_off()

    def transition_to_bright_scene(self):
        """Configure lighting for the bright scene."""
        for light in self.lights.values():
            light.ignore_vacancy()
            light.adjust_to_max()

    def transition_to_tv_scene(self):
        """Configure lighting for the tv scene."""
        kelvin = int(float(self.entities.input_number.tv_kelvin.state))
        self.lights["entryway"].set_presence_adjustments(
            occupied=(
                self.get_setting("tv_motion_brightness"),
                kelvin,
            ),
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(self.get_setting("tv_brightness"), kelvin),
            entered=(self.get_setting("tv_motion_brightness"), kelvin),
            occupied=(
                self.constants["max_brightness"],
                self.lights["kitchen"].kelvin_limits["max"],
            ),
            transition_period=self.get_setting("tv_transition_period"),
            vacating_delay=self.get_setting("tv_vacating_delay"),
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=(self.get_setting("tv_motion_brightness"), kelvin),
            occupied=(
                self.constants["max_brightness"],
                self.lights["kitchen_strip"].kelvin_limits["max"],
            ),
            transition_period=self.get_setting("tv_transition_period"),
            vacating_delay=self.get_setting("tv_vacating_delay"),
        )
        self.lights["tv"].adjust(
            self.get_setting("tv_brightness"),
            kelvin,
        )
        if self.control.napping_in_bedroom or self.control.napping_in_nursery:
            self.lights["hall"].turn_off()
        else:
            self.lights["hall"].adjust(self.get_setting("tv_brightness"), kelvin)
        self.lights["dining_room"].set_presence_adjustments(
            entered=(self.get_setting("tv_motion_brightness"), kelvin),
            occupied=(
                self.constants["max_brightness"],
                self.lights["dining_room"].kelvin_limits["max"],
            ),
            transition_period=self.get_setting("tv_transition_period"),
            vacating_delay=self.get_setting("tv_vacating_delay"),
        )
        brightness, kelvin = self.calculate_circadian_brightness_kelvin()
        light_names = ["office", "bathroom"]
        light_names.extend(
            light_name
            for light_name in ("bedroom", "nursery")
            if not self.is_lighting_sufficient(light_name)
            and not self.control.napping_in(light_name)
        )
        for light_name in light_names:
            self.lights[light_name].set_presence_adjustments(
                occupied=(
                    brightness,
                    kelvin,
                ),
                vacating_delay=self.get_setting(f"{light_name}_vacating_delay"),
            )

    def transition_to_sleep_scene(self):
        """Configure lighting for the sleep scene."""
        for light_name in ("entryway", "kitchen"):
            self.lights[light_name].set_presence_adjustments(
                entered=(
                    self.lights[light_name].minimum_brightness,
                    self.lights[light_name].kelvin_limits["min"],
                ),
                occupied=(
                    self.get_setting("sleep_motion_brightness"),
                    self.get_setting("sleep_motion_kelvin"),
                ),
                transition_period=self.get_setting(
                    "sleep_transition_period",
                ),
                vacating_delay=self.get_setting("sleep_vacating_delay"),
            )
        for light_name in ("office", "bathroom"):
            self.lights[light_name].set_presence_adjustments(
                occupied=(
                    self.lights[light_name].minimum_brightness,
                    self.lights[light_name].kelvin_limits["min"],
                ),
                vacating_delay=self.get_setting(
                    "sleep_vacating_delay",
                ),
            )
        for light_name in (
            "kitchen_strip",
            "tv",
            "dining_room",
            "hall",
            "bedroom",
            "nursery",
        ):
            self.lights[light_name].ignore_vacancy()
            self.lights[light_name].turn_off()

    def transition_to_morning_scene(self):
        """Configure lighting for the morning scene."""
        brightness = self.get_setting("morning_brightness")
        kelvin = self.get_setting("morning_kelvin")
        vacating_delay = self.get_setting("morning_vacating_delay")
        self.lights["kitchen"].set_presence_adjustments(
            vacant=(brightness, kelvin),
            occupied=(self.constants["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            occupied=(self.constants["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["office"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.get_setting("office_vacating_delay"),
        )
        for light_name in ("tv", "dining_room", "bathroom", "entryway"):
            self.lights[light_name].set_presence_adjustments(
                occupied=(brightness, kelvin),
                vacating_delay=vacating_delay,
            )
        if not self.is_lighting_sufficient("nursery") and not self.control.napping_in(
            "nursery",
        ):
            self.lights["nursery"].set_presence_adjustments(
                occupied=(brightness, kelvin),
                vacating_delay=vacating_delay,
            )
        for light_name in ("hall", "bedroom"):
            self.lights[light_name].ignore_vacancy()
            self.lights[light_name].turn_off()

    def transition_to_away_night_scene(self):
        """Configure lighting for the Away (Night) scene."""
        for light_name in ("entryway", "kitchen", "office", "bathroom"):
            self.lights[light_name].set_presence_adjustments(
                occupied=(
                    self.constants["max_brightness"],
                    self.lights[light_name].kelvin_limits["max"],
                ),
                vacating_delay=float(
                    self.entities.input_number.night_vacating_delay.state,
                ),
            )
        if self.now_is_between("12:00:00", "23:59:59"):
            self.lights["dining_room"].adjust_to_max()
        else:
            self.lights["dining_room"].ignore_vacancy()
            self.lights["dining_room"].turn_off()
        for light_name in ("kitchen_strip", "tv", "hall", "bedroom", "nursery"):
            self.lights[light_name].ignore_vacancy()
            self.lights[light_name].turn_off()
        if any(
            light.on and not light.control_enabled for light in self.lights.values()
        ):
            self.notify(
                "Some lights are still on because their automatic control was disabled "
                "- enable control or turn off manually if required",
                title="Light Control",
            )

    def start_circadian(self):
        """Schedule a timer to periodically set the lighting appropriately."""
        circadian_progress = self.circadian_progress
        self.circadian_progression(circadian_progress=circadian_progress)
        if circadian_progress not in (0, 1):
            self.circadian["timer"] = self.run_every(
                self.circadian_progression,
                self.datetime() + self.circadian["time_step"],
                self.circadian["time_step"].total_seconds(),
            )
            self.log("Started circadian progression")

    def circadian_progression(self, **kwargs: dict):
        """Calculate appropriate lighting levels and implement."""
        circadian_progress = kwargs.get("circadian_progress")
        if circadian_progress is None:
            circadian_progress = self.circadian_progress
        if circadian_progress in (0, 1):
            self.cancel_timer(self.circadian["timer"])
            next_start = self.circadian["start_time"] + datetime.timedelta(
                days=circadian_progress,
            )
            self.circadian["timer"] = self.run_every(
                self.circadian_progression,
                next_start,
                self.circadian["time_step"].total_seconds(),
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
            vacating_delay=self.get_setting("night_vacating_delay"),
        )
        for light_name in ("kitchen", "dining_room"):
            self.lights[light_name].set_presence_adjustments(
                vacant=(brightness, kelvin),
                entered=(
                    max(brightness, self.get_setting("night_motion_brightness")),
                    kelvin,
                ),
                occupied=(
                    self.constants["max_brightness"],
                    self.get_setting("night_motion_kelvin"),
                ),
                transition_period=self.get_setting("night_transition_period"),
                vacating_delay=self.get_setting("night_vacating_delay"),
            )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=(brightness, kelvin)
            if brightness >= self.get_setting("night_motion_brightness")
            else (self.get_setting("night_motion_brightness"), kelvin),
            occupied=(
                self.constants["max_brightness"],
                self.get_setting("night_motion_kelvin"),
            ),
            transition_period=self.get_setting("night_transition_period"),
            vacating_delay=self.get_setting("night_vacating_delay"),
        )
        self.lights["tv"].adjust(brightness, kelvin)
        if self.control.napping_in_bedroom or self.control.napping_in_nursery:
            self.lights["hall"].turn_off()
        else:
            self.lights["hall"].adjust(brightness, kelvin)
        self.lights["office"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.get_setting("office_vacating_delay"),
        )
        self.lights["bathroom"].set_presence_adjustments(
            occupied=(brightness, kelvin),
            vacating_delay=self.get_setting("night_vacating_delay"),
        )
        if not self.control.napping_in_bedroom:
            self.lights["bedroom"].set_presence_adjustments(
                occupied=(brightness, kelvin),
                vacating_delay=self.get_setting("night_vacating_delay"),
            )
        if not self.control.napping_in_nursery:
            self.lights["nursery"].set_presence_adjustments(
                occupied=(brightness, kelvin),
                vacating_delay=self.get_setting("night_vacating_delay"),
            )
        self.log(
            "Adjusted lighting based on circadian progression to "
            f"brightness: {brightness} and kelvin: {kelvin}",
            level="DEBUG",
        )

    @property
    def circadian_progress(self) -> float:
        """Calculate how far through the circadian rhythm we should be right now."""
        circadian_progress = (self.datetime() - self.circadian["start_time"]) / (
            self.circadian["end_time"] - self.circadian["start_time"]
        )
        if not 0 < circadian_progress < 1:
            circadian_progress = (
                0
                if (
                    self.parse_time(self.get_setting("morning_time"))
                    < self.time()
                    < self.circadian["start_time"].time()
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
            circadian_progress = self.circadian_progress
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

    def redate_circadian(self, **kwargs: dict):
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
                f"(by {time_step.total_seconds() / -60} minutes)",
            )
            raise ValueError
        self.circadian["start_time"] = start_time
        self.circadian["end_time"] = end_time
        self.circadian["time_step"] = time_step
        self.log(
            f"Circadian redated to start at {start_time.time()} with "
            f"time step of {time_step.total_seconds() / 60} minutes",
        )
        if self.control.scene == "Night":
            self.start_circadian()
        elif self.control.scene == "Away (Night)":
            self.lights["dining_room"].ignore_vacancy()
            self.lights["dining_room"].turn_off()

    def is_lighting_sufficient(self, room: str) -> bool:
        """Return if there is enough light to not require further lighting."""
        return (
            float(self.get_state(f"sensor.{room}_presence_sensor_illuminance"))
            - self.lighting_illuminance(room)
            >= self.constants["illuminance"]["auto_off"][room]
        )

    def lighting_illuminance(self, room: str) -> float:
        """Return approximate illuminance of powered lights affecting light sensors."""
        return (
            self.lights[room].brightness
            / self.constants["max_brightness"]
            * self.constants["illuminance"]["lighting_factor"][room]
        )

    def handle_dark_outside(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Change scene appropriately for low outside light levels."""
        del entity, attribute, old, new, kwargs
        if "Day" in self.control.scene:
            self.log("It is now dark outside - changing scene accordingly")
            if self.media.playing:
                self.control.scene = "TV"
            elif self.presence.anyone_home:
                self.control.scene = "Night"
            else:
                self.control.scene = "Away (Night)"

    def handle_bright_outside(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Change scene appropriately for high outside light levels."""
        del entity, attribute, old, new, kwargs
        if self.control.scene not in (
            "Bright",
            "Sleep",
            "Morning",
            "Day",
            "Away (Day)",
        ):
            self.log("It is now bright outside - changing scene accordingly")
            self.control.scene = "Day" if self.presence.anyone_home else "Away (Day)"

    def handle_kitchen_illuminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Change kitchen vacancy lighting based on illuminance levels."""
        del entity, attribute, old, kwargs
        if new == "unavailable":
            self.log("'Kitchen' illuminance is 'unavailable'", level="WARNING")
            return
        if (
            self.control.scene not in ("Morning", "Day")
            or not self.lights["kitchen"].control_enabled
        ):
            return
        if (
            float(new) - self.lighting_illuminance("kitchen")
            >= self.constants["illuminance"]["auto_off"]["kitchen"]
        ):
            for light_name in ("kitchen", "kitchen_strip"):
                self.lights[light_name].ignore_vacancy()
                self.lights[light_name].turn_off()
            self.log(
                f"Kitchen light levels are high ({new}lx), automatic lighting disabled",
            )
        elif (
            not self.lights["kitchen"].ignoring_vacancy
            and float(new) - self.lighting_illuminance("kitchen")
            <= self.constants["illuminance"]["auto_on"]["kitchen"]
        ):
            for light_name in ("kitchen", "kitchen_strip"):
                self.lights[light_name].set_presence_adjustments(
                    occupied=(
                        self.constants["max_brightness"],
                        self.lights[light_name].kelvin_limits["max"],
                    ),
                    vacating_delay=self.get_setting(
                        "morning_vacating_delay",
                    ),
                )
            self.log(
                f"Kitchen light levels are low ({new}lx), automatic lighting enabled",
            )

    def handle_bedroom_illuminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Detect when to change scene from morning to day & set automatic lighting."""
        del entity, attribute, old, kwargs
        if new == "unavailable":
            self.log("'Bedroom' illuminance is 'unavailable'", level="WARNING")
            return
        if (
            self.control.scene == "Morning"
            and float(new) >= self.constants["bedroom_morning_max_illuminance"]
        ):
            self.log(
                f"Bedroom light levels are high ({new}lx), transitioning to day scene",
            )
            self.napping_in_bedroom = False
            self.control.scene = "Day"
        if self.control.scene != "Day" or not self.lights["bedroom"].control_enabled:
            return
        self.handle_room_illuminance_change(float(new), "bedroom")

    def handle_nursery_illuminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Change nursery vacancy lighting based on illuminance levels."""
        del entity, attribute, old, kwargs
        if new == "unavailable":
            self.log("'Nursery' illuminance is 'unavailable'", level="WARNING")
            return
        if (
            self.control.scene not in ("Morning", "Day")
            or not self.lights["nursery"].control_enabled
        ):
            return
        self.handle_room_illuminance_change(float(new), "nursery")

    def handle_room_illuminance_change(self, illuminance: float, room: str):
        """Change room vacancy lighting based on illuminance levels (and napping)."""
        if (
            illuminance - self.lighting_illuminance(room)
            >= self.constants["illuminance"]["auto_off"][room]
        ):
            if not self.lights[room].ignoring_vacancy:
                self.lights[room].ignore_vacancy()
                self.lights[room].turn_off()
                self.log(
                    f"The '{room}'s' light levels are high ({illuminance}lx), "
                    "automatic lighting disabled",
                )
        elif (
            self.lights[room].ignoring_vacancy
            and not self.control.napping_in(room)
            and illuminance - self.lighting_illuminance(room)
            <= self.constants["illuminance"]["auto_on"][room]
        ):
            self.lights[room].set_presence_adjustments(
                occupied=(
                    self.constants["max_brightness"],
                    self.lights[room].kelvin_limits["max"],
                ),
                vacating_delay=self.get_setting(
                    f"{room}_vacating_delay",
                ),
            )
            self.log(
                f"The '{room}'s' light levels are low ({illuminance}lx), "
                "automatic lighting enabled",
            )

    @property
    def lights(self) -> Lights:
        """Override conflicting inherited reference to this app."""
        return self.__lights


class Light(PresenceDevice):
    """Control a light (or a group) and configure responses to environmental changes."""

    def __init__(
        self,
        device_id: str,
        controller: Lights,
        room: str,
        linked_rooms: list[str] = (),
    ):
        """Initialise with a lights's id, room(s), kelvin limits, and controller."""
        super().__init__(
            device_id=device_id,
            controller=controller,
            control_input_boolean_suffix="_light"
            if device_id.startswith("light")
            else "",
            room=room,
            linked_rooms=linked_rooms,
        )
        self.minimum_brightness = self.controller.constants[
            "min_brightness"
            if device_id.endswith("strip")
            or self.get_attribute("supported_color_modes")[0] == "brightness"
            else "restricted_min_brightness"
        ]
        self.kelvin_limits = {
            "max": self.get_attribute("max_color_temp_kelvin"),
            "min": self.get_attribute("min_color_temp_kelvin"),
        }
        self.kelvin_before_off = self.kelvin_limits["min"]
        self.presence_adjustments: dict[str, int] = {}

    @property
    def brightness(self) -> int:
        """Get the brightness of the light from Home Assistant."""
        return self.get_attribute("brightness", 0)

    @brightness.setter
    def brightness(self, value: int):
        """Set and validate light's brightness."""
        if not self.control_enabled:
            return
        value = self.validate_brightness(value)
        if self.brightness == value:
            return
        if value != 0:
            if self.controller.logger.isEnabledFor(logging.DEBUG):
                self.controller.log(
                    f"Setting '{self.device_id}' brightness to {value} "
                    f"(from {self.brightness})",
                    level="DEBUG",
                )
            self.turn_on(brightness=value)
        else:
            self.turn_off()

    def validate_brightness(self, value: int) -> int:
        """Return closest valid value for brightness."""
        if value < self.minimum_brightness:
            return self.minimum_brightness if value > 0 else 0
        if value > self.controller.args["max_brightness"]:
            return self.controller.args["max_brightness"]
        return value

    @property
    def kelvin(self) -> int:
        """Get the colour warmth value of the light from Home Assistant."""
        kelvin = self.get_attribute("color_temp_kelvin")
        return kelvin if kelvin is not None else self.kelvin_before_off

    @kelvin.setter
    def kelvin(self, value: int):
        """Set and validate light's warmth of colour."""
        if not self.control_enabled:
            return
        value = self.validate_kelvin(value)
        if value is None or value == self.kelvin:
            return
        if self.controller.logger.isEnabledFor(logging.DEBUG):
            self.controller.log(
                f"Setting {self.device_id}'s kelvin to {value} (from {self.kelvin})",
                level="DEBUG",
            )
        self.turn_on(color_temp_kelvin=value)

    def validate_kelvin(self, value: int) -> int | None:
        """Return closest valid value for kelvin."""
        if self.kelvin_limits["min"] is None:
            return None
        return self.constants["kelvin_per_step"] * int(
            min(max(value, self.kelvin_limits["min"]), self.kelvin_limits["max"])
            / self.constants["kelvin_per_step"],
        )

    def adjust(self, brightness: int, kelvin: int):
        """Adjust light brightness and kelvin at the same time."""
        if not self.control_enabled:
            return
        brightness = self.validate_brightness(brightness)
        if brightness == 0:
            self.turn_off()
        else:
            kelvin = self.validate_kelvin(kelvin)
            if self.controller.logger.isEnabledFor(logging.DEBUG):
                self.controller.log(
                    f"Adjusting '{self.device_id}' to "
                    f"brightness {brightness} and kelvin {kelvin} "
                    f"(from {self.brightness} and {self.kelvin})",
                    level="DEBUG",
                )
            if kelvin is None or kelvin == self.kelvin:
                self.brightness = brightness
            elif brightness == self.brightness:
                self.kelvin = kelvin
            else:
                self.turn_on(brightness=brightness, color_temp_kelvin=kelvin)

    def adjust_to_max(self):
        """Adjust light brightness and kelvin at the same time to maximum values."""
        self.adjust(self.controller.args["max_brightness"], self.kelvin_limits["max"])

    def turn_on_for_conditions(self):
        """Turn the light to with appropriate parameters for the scene."""
        if not self.ignoring_vacancy:
            self.adjust_for_conditions()
        else:
            self.controller.transition_to_scene(self.controller.control.scene)

    def turn_off(self):
        """Turn light off and record previous kelvin level."""
        if self.control_enabled and self.brightness != 0:
            self.kelvin_before_off = self.kelvin
            if self.controller.logger.isEnabledFor(logging.DEBUG):
                self.controller.log(
                    f"Turning '{self.device_id}' off (previously at"
                    f" {self.brightness} brightness and {self.kelvin} kelvin)",
                    level="DEBUG",
                )
            super().turn_off()

    def set_presence_adjustments(
        self,
        vacant: tuple[int, int] = (0, 0),
        entered: tuple[int, int] = (0, 0),
        occupied: tuple[int, int] = (0, 0),
        transition_period: int = 0,
        vacating_delay: int = 0,
    ):
        """Configure the light to adjust based on presence in the room."""
        self.presence_adjustments["vacant"] = {
            "brightness": vacant[0],
            "kelvin": vacant[1],
        }
        self.presence_adjustments["entered"] = {
            "brightness": entered[0],
            "kelvin": entered[1],
        }
        self.presence_adjustments["occupied"] = {
            "brightness": occupied[0],
            "kelvin": occupied[1],
        }
        self.transition_period = transition_period
        presence = "vacant" if self.vacant else "occupied"
        if (transition_period != 0) ^ (entered != (0, 0)):
            self.controller.log(
                f"'{self.device_id}' set to transition with invalid parameters, "
                "setting to occupied state instead",
                level="WARNING",
            )
        elif self.should_transition_towards_occupied:
            presence = "entered"
            self.start_transition_towards_occupied(self.transition_progress)
        if presence != "entered":
            self.adjust(
                self.presence_adjustments[presence]["brightness"],
                self.presence_adjustments[presence]["kelvin"],
            )
        self.vacating_delay = vacating_delay
        self.monitor_presence()
        if self.controller.logger.isEnabledFor(logging.DEBUG):
            self.controller.log(
                f"Configured '{self.device_id}' with presence '{presence}' and "
                f"presence adjustments: {self.presence_adjustments}",
                level="DEBUG",
            )

    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Adjust to desired light settings for the current presence state."""
        if self.ignoring_vacancy:
            return False
        if check_if_would_adjust_only:
            return True
        if self.transition_timer:
            presence = "entered"
        elif self.vacant:
            presence = "vacant"
        else:
            presence = "occupied"
        self.adjust(
            self.presence_adjustments[presence]["brightness"],
            self.presence_adjustments[presence]["kelvin"],
        )
        self.controller.log(
            f"Lighting '{self.device_id}' adjusted now room is '{presence}'",
            level="DEBUG",
        )
        return True

    def start_transition_towards_occupied(self, progress: float = 0):
        """Calculate the light change required and start the transition."""
        brightness_change = (
            self.presence_adjustments["occupied"]["brightness"]
            - self.presence_adjustments["entered"]["brightness"]
        ) * (1 - progress)
        kelvin_change = (
            self.presence_adjustments["occupied"]["kelvin"]
            - self.presence_adjustments["entered"]["kelvin"]
        ) * (1 - progress)
        if brightness_change == 0 and kelvin_change == 0:
            return
        steps = max(
            abs(brightness_change) / self.constants["brightness_per_step"],
            abs(kelvin_change) / self.constants["kelvin_per_step"],
            1,
        )
        max_steps = self.transition_period * self.constants["max_steps_per_second"]
        steps = min(steps, max_steps)
        brightness_step = brightness_change / steps
        kelvin_step = kelvin_change / steps
        step_time = self.transition_period / steps
        super().start_transition_towards_occupied(
            step_time,
            steps,
            brightness_step=brightness_step,
            kelvin_step=kelvin_step,
        )

    def transition_towards_occupied(self, **kwargs: dict):
        """Step towards occupied lighting settings."""
        if kwargs["timer_id"] != self.transition_timer:
            return
        steps_remaining = kwargs["steps_remaining"] - 1
        if steps_remaining > 0:
            self.adjust(
                self.presence_adjustments["occupied"]["brightness"]
                - kwargs["brightness_step"] * steps_remaining,
                self.presence_adjustments["occupied"]["kelvin"]
                - kwargs["kelvin_step"] * steps_remaining,
            )
        super().transition_towards_occupied(**kwargs)
