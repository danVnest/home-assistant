"""Coordinates lighting based primarily on scenes.

Includes circadian adjustments to brightness and kelvin settings to ease into sleep,
localised adjustments based on presence in the room, and low/high ambient illuminance.

User defined variables are configued in lights.yaml
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from math import ceil
from typing import Any, cast, override

from app import App
from presence import PresenceDevice


class Lights(App):
    """Control lights based on user input, illuminance, scene changes, and time."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__lights: dict[str, Light] = {}
        self.circadian_timer: str | None = None
        self.last_low_illuminance_time = dict.fromkeys(
            ["kitchen", "bedroom", "nursery"],
            self.datetime(),
        )
        self.high_illuminance_auto_off_delay = timedelta(
            minutes=self.constants["illuminance"]["auto_off_delay"],
        )

    @override
    def initialize(self) -> None:
        """Initialise lights and monitor ambient illuminance."""
        super().initialize()
        self.configure_circadian()
        self.lights["entryway"] = Light(
            device_id="group.entryway_lights",
            controller=self,
            room="entryway",
            linked_rooms=("front_door",),
        )
        self.lights["kitchen"] = Light(
            device_id="light.kitchen",
            controller=self,
            room="kitchen",
            linked_rooms=("back_deck",),
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
        for room in ("office", "bedroom", "nursery", "bathroom"):
            self.lights[room] = Light(
                device_id=f"light.{room}",
                controller=self,
                room=room,
            )
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
        for room in ("kitchen", "bedroom", "nursery"):
            self.last_low_illuminance_time[room] = (
                self.datetime() - self.high_illuminance_auto_off_delay
            )
            self.listen_state(
                getattr(self, f"handle_{room}_illuminance_change"),
                f"sensor.{room}_presence_sensor_illuminance_filtered",
            )

    def terminate(self) -> None:
        """Cancel presence callbacks before termination (auto run by Appdaemon)."""
        for light in self.lights.values():
            light.ignore_presence()

    def transition_to_scene(self, scene: str) -> None:
        """Change lighting based on the specified scene."""
        self.cancel_timer(self.circadian_timer)
        if scene == "Night":
            self.start_circadian()
        elif "Day" in scene:
            self.transition_to_day_scene()
        elif scene == "Away (Night)":
            self.transition_to_away_night_scene()
        else:
            getattr(self, f"transition_to_{scene.lower()}_scene")()
        self.log(f"Light scene changed to '{scene}'")

    def transition_to_day_scene(self) -> None:
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
                occupied=LightState(
                    self.constants["max_brightness"],
                    self.lights[light_name].kelvin_limits["max"],
                ),
                vacating_delay=self.get_integer_setting(f"{light_name}_vacating_delay"),
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
            self.lights[light_name].turn_off_and_ignore_presence()

    def transition_to_bright_scene(self) -> None:
        """Configure lighting for the Bright scene."""
        for light in self.lights.values():
            light.ignore_vacancy()
            light.adjust_to_max()

    def transition_to_tv_scene(self) -> None:
        """Configure lighting for the TV scene."""
        kelvin = self.get_integer_setting("tv_kelvin")
        self.lights["entryway"].set_presence_adjustments(
            occupied=LightState(
                self.get_integer_setting("tv_motion_brightness"),
                kelvin,
            ),
        )
        self.lights["kitchen"].set_presence_adjustments(
            vacant=LightState(self.get_integer_setting("tv_brightness"), kelvin),
            entered=LightState(
                self.get_integer_setting("tv_motion_brightness"),
                kelvin,
            ),
            occupied=LightState(
                self.constants["max_brightness"],
                self.lights["kitchen"].kelvin_limits["max"],
            ),
            transition_period=self.get_integer_setting("tv_transition_period"),
            vacating_delay=self.get_integer_setting("tv_vacating_delay"),
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=LightState(
                self.get_integer_setting("tv_motion_brightness"),
                kelvin,
            ),
            occupied=LightState(
                self.constants["max_brightness"],
                self.lights["kitchen_strip"].kelvin_limits["max"],
            ),
            transition_period=self.get_integer_setting("tv_transition_period"),
            vacating_delay=self.get_integer_setting("tv_vacating_delay"),
        )
        light_names = ["tv"]
        if self.control.napping_in_bedroom or self.control.napping_in_nursery:
            self.lights["hall"].turn_off_and_ignore_presence()
        else:
            light_names.append("hall")
        for light_name in light_names:
            self.lights[light_name].adjust(
                self.get_integer_setting("tv_brightness"),
                kelvin,
            )
        self.lights["dining_room"].set_presence_adjustments(
            entered=LightState(
                self.get_integer_setting("tv_motion_brightness"),
                kelvin,
            ),
            occupied=LightState(
                self.constants["max_brightness"],
                self.lights["dining_room"].kelvin_limits["max"],
            ),
            transition_period=self.get_integer_setting("tv_transition_period"),
            vacating_delay=self.get_integer_setting("tv_vacating_delay"),
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
                occupied=LightState(
                    brightness,
                    kelvin,
                ),
                vacating_delay=self.get_integer_setting(f"{light_name}_vacating_delay"),
            )

    def transition_to_sleep_scene(self) -> None:
        """Configure lighting for the Sleep scene."""
        for light_name in ("entryway", "kitchen"):
            self.lights[light_name].set_presence_adjustments(
                entered=LightState(
                    self.lights[light_name].min_brightness,
                    self.lights[light_name].kelvin_limits["min"],
                ),
                occupied=LightState(
                    self.get_integer_setting("sleep_motion_brightness"),
                    self.get_integer_setting("sleep_motion_kelvin"),
                ),
                transition_period=self.get_integer_setting(
                    "sleep_transition_period",
                ),
                vacating_delay=self.get_integer_setting("sleep_vacating_delay"),
            )
        for light_name in ("office", "bathroom"):
            self.lights[light_name].set_presence_adjustments(
                occupied=LightState(
                    self.lights[light_name].min_brightness,
                    self.lights[light_name].kelvin_limits["min"],
                ),
                vacating_delay=self.get_integer_setting(
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
            self.lights[light_name].turn_off_and_ignore_presence()

    def transition_to_morning_scene(self) -> None:
        """Configure lighting for the Morning scene."""
        brightness = self.get_integer_setting("morning_brightness")
        kelvin = self.get_integer_setting("morning_kelvin")
        vacating_delay = self.get_integer_setting("morning_vacating_delay")
        self.lights["kitchen"].set_presence_adjustments(
            vacant=LightState(brightness, kelvin),
            occupied=LightState(self.constants["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["kitchen_strip"].set_presence_adjustments(
            occupied=LightState(self.constants["max_brightness"], kelvin),
            vacating_delay=vacating_delay,
        )
        self.lights["office"].set_presence_adjustments(
            occupied=LightState(brightness, kelvin),
            vacating_delay=self.get_integer_setting("office_vacating_delay"),
        )
        for light_name in ("tv", "dining_room", "bathroom", "entryway"):
            self.lights[light_name].set_presence_adjustments(
                occupied=LightState(brightness, kelvin),
                vacating_delay=vacating_delay,
            )
        if not self.is_lighting_sufficient("nursery") and not self.control.napping_in(
            "nursery",
        ):
            self.lights["nursery"].set_presence_adjustments(
                occupied=LightState(brightness, kelvin),
                vacating_delay=vacating_delay,
            )
        for light_name in ("hall", "bedroom"):
            self.lights[light_name].turn_off_and_ignore_presence()

    def transition_to_away_night_scene(self) -> None:
        """Configure lighting for the Away (Night) scene."""
        for light_name in ("entryway", "kitchen", "office", "bathroom"):
            self.lights[light_name].set_presence_adjustments(
                occupied=LightState(
                    self.constants["max_brightness"],
                    self.lights[light_name].kelvin_limits["max"],
                ),
                vacating_delay=self.get_integer_setting("night_vacating_delay"),
            )
        if self.now_is_between("12:00:00", "23:59:59"):
            self.lights["dining_room"].adjust_to_max()
        else:
            self.lights["dining_room"].turn_off_and_ignore_presence()
        for light_name in ("kitchen_strip", "tv", "hall", "bedroom", "nursery"):
            self.lights[light_name].turn_off_and_ignore_presence()
        if any(
            light.on and not light.control_enabled for light in self.lights.values()
        ):
            self.notify(
                "Some lights are still on because their automatic control was disabled "
                "- enable control or turn off manually if required",
                title="Light Control",
            )

    def start_circadian(self) -> None:
        """Schedule a timer to periodically set the lighting appropriately."""
        circadian_progress = self.circadian_progress
        self.circadian_progression(circadian_progress=circadian_progress)
        if circadian_progress not in (0, 1):
            self.circadian_timer = self.run_every(
                self.circadian_progression,
                self.datetime() + self.circadian_time_step,
                self.circadian_time_step.total_seconds(),
            )
            self.log("Started circadian progression")

    def circadian_progression(self, **kwargs: Any) -> None:
        """Calculate appropriate lighting levels and implement."""
        circadian_progress = kwargs.get("circadian_progress")
        if circadian_progress is None:
            circadian_progress = self.circadian_progress
        if circadian_progress in (0, 1):
            self.cancel_timer(self.circadian_timer)
            next_start = self.circadian_start_time + timedelta(days=circadian_progress)
            self.circadian_timer = self.run_every(
                self.circadian_progression,
                next_start,
                self.circadian_time_step.total_seconds(),
            )
            if self.debugging:
                self.log(
                    f"Set circadian progression to commence at {next_start}",
                    level="DEBUG",
                )
        brightness, kelvin = self.calculate_circadian_brightness_kelvin(
            circadian_progress,
        )
        self.lights["entryway"].set_presence_adjustments(
            occupied=LightState(brightness, kelvin),
            vacating_delay=self.get_integer_setting("night_vacating_delay"),
        )
        for light_name in ("kitchen", "dining_room"):
            self.lights[light_name].set_presence_adjustments(
                vacant=LightState(brightness, kelvin),
                entered=LightState(
                    max(
                        brightness,
                        self.get_integer_setting("night_motion_brightness"),
                    ),
                    kelvin,
                ),
                occupied=LightState(
                    self.constants["max_brightness"],
                    self.get_integer_setting("night_motion_kelvin"),
                ),
                transition_period=self.get_integer_setting("night_transition_period"),
                vacating_delay=self.get_integer_setting("night_vacating_delay"),
            )
        self.lights["kitchen_strip"].set_presence_adjustments(
            entered=LightState(brightness, kelvin)
            if brightness >= self.get_integer_setting("night_motion_brightness")
            else LightState(
                self.get_integer_setting("night_motion_brightness"),
                kelvin,
            ),
            occupied=LightState(
                self.constants["max_brightness"],
                self.get_integer_setting("night_motion_kelvin"),
            ),
            transition_period=self.get_integer_setting("night_transition_period"),
            vacating_delay=self.get_integer_setting("night_vacating_delay"),
        )
        self.lights["tv"].adjust(brightness, kelvin)
        if self.control.napping_in_bedroom or self.control.napping_in_nursery:
            self.lights["hall"].turn_off_and_ignore_presence()
        else:
            self.lights["hall"].adjust(brightness, kelvin)
        self.lights["office"].set_presence_adjustments(
            occupied=LightState(brightness, kelvin),
            vacating_delay=self.get_integer_setting("office_vacating_delay"),
        )
        self.lights["bathroom"].set_presence_adjustments(
            occupied=LightState(brightness, kelvin),
            vacating_delay=self.get_integer_setting("night_vacating_delay"),
        )
        if not self.control.napping_in_bedroom:
            self.lights["bedroom"].set_presence_adjustments(
                occupied=LightState(brightness, kelvin),
                vacating_delay=self.get_integer_setting("night_vacating_delay"),
            )
        if not self.control.napping_in_nursery:
            self.lights["nursery"].set_presence_adjustments(
                occupied=LightState(brightness, kelvin),
                vacating_delay=self.get_integer_setting("night_vacating_delay"),
            )
        if self.debugging:
            self.log(
                "Adjusted lighting based on circadian progression to "
                f"{brightness = } and {kelvin = }",
                level="DEBUG",
            )

    @property
    def circadian_progress(self) -> float:
        """How far through the circadian rhythm we should be right now (0 -> 1)."""
        circadian_progress = (self.datetime() - self.circadian_start_time) / (
            self.circadian_end_time - self.circadian_start_time
        )
        if not 0 < circadian_progress < 1:
            circadian_progress = (
                0
                if (
                    self.get_time_setting("morning_time")
                    < self.time()
                    < self.circadian_start_time.time()
                )
                else 1
            )
        if self.debugging:
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
            round(
                self.get_integer_setting("initial_circadian_brightness")
                + (
                    self.get_integer_setting("final_circadian_brightness")
                    - self.get_integer_setting("initial_circadian_brightness")
                )
                * circadian_progress,
            ),
            round(
                self.get_integer_setting("initial_circadian_kelvin")
                + (
                    self.get_integer_setting("final_circadian_kelvin")
                    - self.get_integer_setting("initial_circadian_kelvin")
                )
                * circadian_progress,
            ),
        )

    def configure_circadian(self) -> None:
        """Configure the start, end, and step times for lighting adjustment today."""
        start_time = self.sunset(today=True) + timedelta(
            hours=self.get_number_setting("circadian_initial_sunset_offset"),
        )
        end_time = self.get_datetime_setting("circadian_end_time")
        time_step: timedelta = (end_time - start_time) / max(
            abs(
                self.get_integer_setting("initial_circadian_brightness")
                - self.get_integer_setting("final_circadian_brightness"),
            )
            / min(self.constants["brightness_per_step"].values()),
            abs(
                self.get_integer_setting("initial_circadian_kelvin")
                - self.get_integer_setting("final_circadian_kelvin"),
            )
            / min(
                value
                for value in self.constants["kelvin_per_step"].values()
                if value is not None
            ),
        )
        if time_step.total_seconds() < 0:
            self.error(
                "Circadian end time is before start time "
                f"(by {time_step.total_seconds() / -60} minutes)",
            )
            raise ValueError
        min_seconds_per_step = 1 / self.constants["max_steps_per_second"]
        if time_step.total_seconds() < min_seconds_per_step:
            time_step = timedelta(seconds=min_seconds_per_step)
        self.circadian_start_time = start_time
        self.circadian_end_time = end_time
        self.circadian_time_step = time_step
        self.log(
            f"Circadian set to start at {start_time.time()} with "
            f"time step of {time_step.total_seconds() / 60} minutes",
        )

    def redate_circadian(self, **kwargs: Any) -> None:
        """Adjust circadian lighting timing for today."""
        del kwargs
        self.configure_circadian()
        if self.control.scene == "Night":
            self.start_circadian()
        elif self.control.scene == "Away (Night)":
            self.lights["dining_room"].turn_off_and_ignore_presence()

    def is_lighting_sufficient(self, room: str) -> bool:
        """Return if there is enough light to not require further lighting."""
        return (
            float(
                cast(
                    "str",
                    self.get_state(f"sensor.{room}_presence_sensor_illuminance"),
                ),
            )
            - self.lighting_illuminance(room)
            >= self.constants["illuminance"]["auto_threshold"][room]
        )

    def lighting_illuminance(self, room: str) -> float:
        """Return approximate illuminance of powered lights affecting light sensors."""
        return (
            self.lights[room].brightness
            / self.constants["max_brightness"]
            * self.constants["illuminance"]["lighting_factor"][room]
        )

    @property
    def dark_outside(self) -> bool:
        """True if it is currently dark outside."""
        return self.entities.binary_sensor.dark_outside.state == "on"

    def handle_dark_outside(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
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
        **kwargs: Any,
    ) -> None:
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
        **kwargs: Any,
    ) -> None:
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
            > self.constants["illuminance"]["auto_threshold"]["kitchen"]
        ):
            if (
                not self.lights["kitchen"].ignoring_vacancy
                and self.datetime()
                > self.last_low_illuminance_time["kitchen"]
                + self.high_illuminance_auto_off_delay
            ):
                for light_name in ("kitchen", "kitchen_strip"):
                    self.lights[light_name].turn_off_and_ignore_presence()
                self.log(
                    f"The 'kitchen' light level is high ({float(new):.0f}lx), "
                    "automatic lighting disabled",
                )
        else:
            self.last_low_illuminance_time["kitchen"] = self.datetime()
            if self.lights["kitchen"].ignoring_vacancy:
                for light_name in ("kitchen", "kitchen_strip"):
                    self.lights[light_name].set_presence_adjustments(
                        occupied=LightState(
                            self.constants["max_brightness"],
                            self.lights[light_name].kelvin_limits["max"],
                        ),
                        vacating_delay=self.get_integer_setting(
                            "morning_vacating_delay",
                        ),
                    )
                self.log(
                    f"The 'kitchen' light level is low ({float(new):.0f}lx), "
                    "automatic lighting enabled",
                )

    def handle_bedroom_illuminance_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Detect when to change scene from morning to day & set automatic lighting."""
        del entity, attribute, old, kwargs
        if new == "unavailable":
            self.log("'Bedroom' illuminance is 'unavailable'", level="WARNING")
            return
        if (
            self.control.scene == "Morning"
            and float(new) >= self.constants["illuminance"]["bedroom_morning_max"]
        ):
            self.log(
                f"The 'bedroom' light level is high ({float(new):.0f}lx), "
                "transitioning to day scene",
            )
            self.control.napping_in_bedroom = False
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
        **kwargs: Any,
    ) -> None:
        """Change occupied nursery lighting based on illuminance levels."""
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

    def handle_room_illuminance_change(self, illuminance: float, room: str) -> None:
        """Change occupied room lighting based on illuminance levels (and napping)."""
        if (
            illuminance - self.lighting_illuminance(room)
            > self.constants["illuminance"]["auto_threshold"][room]
        ):
            if (
                not self.lights[room].ignoring_vacancy
                and self.datetime()
                > self.last_low_illuminance_time[room]
                + self.high_illuminance_auto_off_delay
            ):
                self.lights[room].turn_off_and_ignore_presence()
                self.log(
                    f"The '{room}' light level is high ({illuminance:.0f}lx), "
                    "automatic lighting disabled",
                )
        else:
            self.last_low_illuminance_time[room] = self.datetime()
            if self.lights[room].ignoring_vacancy and not self.control.napping_in(room):
                self.lights[room].set_presence_adjustments(
                    occupied=LightState(
                        self.constants["max_brightness"],
                        self.lights[room].kelvin_limits["max"],
                    ),
                    vacating_delay=self.get_integer_setting(
                        f"{room}_vacating_delay",
                    ),
                )
                self.log(
                    f"The '{room}' light level is low ({illuminance:.0f}lx), "
                    "automatic lighting enabled",
                )

    @override
    @property
    def lights(self) -> dict[str, Light]:
        """Dictionary of all lights controlled by the Lights app."""
        return self.__lights


@dataclass
class LightState:
    """Light state as defined by its brightness and kelvin values."""

    brightness: int = 0
    kelvin: int | None = None


@dataclass
class PresenceLightStates:
    """Light states for each possible room presence state."""

    vacant: LightState
    entered: LightState
    occupied: LightState

    def __getitem__(self, presence: str) -> LightState:
        """Get the desired light state for a given presence."""
        return getattr(self, presence, LightState(0, None))


class Light(PresenceDevice):
    """Control a light (or a group) and configure responses to environmental changes."""

    @override
    def __init__(
        self,
        device_id: str,
        controller: Lights,
        room: str,
        linked_rooms: tuple[str, ...] = (),
        control_input_boolean_suffix: str = "",
    ) -> None:
        """Initialise with a lights' id, room(s), kelvin limits, and controller."""
        super().__init__(
            device_id,
            controller,
            room,
            linked_rooms,
            "_light" if device_id.startswith("light") else control_input_boolean_suffix,
        )
        self.controller: Lights = self.controller
        if device_id.endswith("strip"):
            light_type = "strip"
        elif (
            self.device.attributes.get("supported_color_modes", [None])[0]
            == "brightness"
        ):
            light_type = "fan"
        else:
            light_type = "bulb"
        self.brightness_per_step: float = self.constants["brightness_per_step"][
            light_type
        ]
        self.min_brightness = max(int(self.constants["min_brightness"][light_type]), 1)
        device = self.device
        self.kelvin_limits: dict[str, int | None] = {
            "max": device.attributes.get("max_color_temp_kelvin"),
            "min": device.attributes.get("min_color_temp_kelvin"),
        }
        self.kelvin_per_step: float | None = self.constants["kelvin_per_step"][
            light_type
        ]
        self.kelvin_before_off: int | None = self.kelvin_limits["min"]
        off = LightState()
        self.presence_states = PresenceLightStates(off, off, off)

    @property
    def brightness(self) -> int:
        """Actual brightness of the light(s), or 0 if off."""
        if not self.on:
            return 0
        device = self.device
        return max(int(device.attributes.get("brightness") or 0), self.min_brightness)

    @brightness.setter
    def brightness(self, value: int) -> None:
        """Validate and set the light's brightness."""
        if not (self.control_enabled and self.available):
            return
        value = self.validate_brightness(value)
        if self.brightness == value:
            return
        if value != 0:
            if self.debugging:
                self.log(
                    f"Setting brightness to {value} (from {self.brightness})",
                    level="DEBUG",
                )
            self.turn_on(brightness=value)
        else:
            self.turn_off()

    def validate_brightness(self, value: int) -> int:
        """Get the closest valid value for the light's brightness."""
        current_brightness = self.brightness
        if value == current_brightness:
            return value
        if value <= 0:
            return 0
        if value <= self.min_brightness:
            return self.min_brightness
        if value >= self.constants["max_brightness"]:
            return self.constants["max_brightness"]
        if (
            value - self.brightness_per_step / 2
            < current_brightness
            < value + self.brightness_per_step / 2
        ):
            return current_brightness
        return value  # brightness will change and HA will round to nearest step

    @property
    def kelvin(self) -> int | None:
        """Colour temperature value of the light."""
        device = self.device
        kelvin = device.attributes.get("color_temp_kelvin", self.kelvin_before_off)
        return int(kelvin) if kelvin else None

    @kelvin.setter
    def kelvin(self, value: int | None) -> None:
        """Set and validate the light's colour temperature value."""
        if not (self.control_enabled and self.available):
            return
        value = self.validate_kelvin(value)
        if value is None or value == self.kelvin:
            return
        if self.debugging:
            self.log(f"Setting kelvin to {value} (from {self.kelvin})", level="DEBUG")
        self.turn_on(color_temp_kelvin=value)

    def validate_kelvin(self, value: int | None) -> int | None:
        """Get the closest valid value for the light's kelvin."""
        if self.kelvin_per_step is None:
            return None
        current_kelvin = self.kelvin
        if value == current_kelvin or value is None:
            return value
        if self.kelvin_limits["min"] is not None and value <= self.kelvin_limits["min"]:
            return self.kelvin_limits["min"]
        if self.kelvin_limits["max"] is not None and value >= self.kelvin_limits["max"]:
            return self.kelvin_limits["max"]
        if current_kelvin is not None and (
            value - self.kelvin_per_step / 2
            < current_kelvin
            < value + self.kelvin_per_step / 2
        ):
            return current_kelvin
        return value  # kelvin will change and Home Assistant will round to nearest step

    def adjust(self, brightness: int, kelvin: int | None) -> None:
        """Adjust the light's brightness and kelvin at the same time."""
        if not (self.control_enabled and self.available):
            return
        if kelvin is None:
            kelvin = self.kelvin
        brightness = self.validate_brightness(brightness)
        if brightness == 0:
            self.turn_off()
            return
        kelvin = self.validate_kelvin(kelvin)
        if self.debugging:
            self.log(
                f"Adjusting to {brightness = } and {kelvin = } "
                f"(from {self.brightness} and {self.kelvin})",
                level="DEBUG",
            )
        if kelvin is None or kelvin == self.kelvin:
            self.brightness = brightness
        elif brightness == self.brightness:
            self.kelvin = kelvin
        else:
            self.turn_on(brightness=brightness, color_temp_kelvin=kelvin)

    def adjust_state(self, state: LightState) -> None:
        """Adjust the light's brightness and kelvin at the same time."""
        self.adjust(state.brightness, state.kelvin)

    def adjust_to_max(self) -> None:
        """Adjust light brightness and kelvin at the same time to maximum values."""
        self.adjust(
            self.constants["max_brightness"],
            self.kelvin_limits["max"],
        )

    @override
    def turn_on_for_conditions(self) -> None:
        """Turn the light on with appropriate brightness and kelvin for the scene."""
        if not self.ignoring_vacancy:
            self.adjust_for_conditions()
        else:
            self.controller.transition_to_scene(self.controller.control.scene)
        if not self.on:
            self.turn_on()

    @override
    def turn_off(self) -> None:
        """Turn the light off and store the previous kelvin level."""
        if self.control_enabled and self.on:
            self.kelvin_before_off = self.kelvin
            if self.debugging:
                self.log(
                    f"Turning off (previously at "
                    f"{self.brightness = } and {self.kelvin = })",
                    level="DEBUG",
                )
            super().turn_off()

    def turn_off_and_ignore_presence(self) -> None:
        """Turn the light off and keep it off regardless of presence in the room."""
        self.ignore_presence()
        self.turn_off()

    def set_presence_adjustments(
        self,
        vacant: LightState | None = None,
        entered: LightState | None = None,
        occupied: LightState | None = None,
        transition_period: float = 0,
        vacating_delay: float = 0,
    ) -> None:
        """Configure the light to adjust appropriately when room presence changes."""
        off = LightState()
        self.presence_states = PresenceLightStates(
            vacant or off,
            entered or off,
            occupied or off,
        )
        self.transition_period = transition_period
        presence = "vacant" if self.vacant else "occupied"
        if (transition_period != 0) ^ (self.presence_states.entered != off):
            self.log(
                "Set to transition with invalid parameters, "
                "setting to occupied state instead",
                level="WARNING",
            )
        elif self.should_transition_towards_occupied:
            presence = "entered"
            self.start_transition_towards_occupied(progress=self.transition_progress)
        if presence != "entered":
            self.adjust_state(self.presence_states[presence])
        self.vacating_delay = vacating_delay
        self.monitor_presence()
        if self.debugging:
            self.log(
                f"Configured with room '{presence}' and {self.presence_states = }",
                level="DEBUG",
            )

    @override
    def adjust_for_conditions(
        self,
        *,
        check_if_would_adjust_only: bool = False,
    ) -> bool:
        """Adjust to desired light settings for the current presence state."""
        if not (self.control_enabled and self.available) or self.ignoring_vacancy:
            return False
        if check_if_would_adjust_only:
            return True
        if self.transition_timer:
            presence = "entered"
        elif self.vacant:
            presence = "vacant"
        else:
            presence = "occupied"
        self.adjust_state(self.presence_states[presence])
        if self.debugging:
            self.log(f"Lighting adjusted now room is '{presence}'", level="DEBUG")
        return True

    @override
    def start_transition_towards_occupied(
        self,
        step_time: float = 0,
        steps_remaining: int = 0,
        progress: float = 0,
        **kwargs: Any,
    ) -> None:
        """Calculate the light change required and start the transition."""
        brightness_change = (
            self.presence_states.occupied.brightness
            - self.presence_states.entered.brightness
        ) * (1 - progress)
        kelvin_change = (
            (
                (
                    self.presence_states.occupied.kelvin
                    - self.presence_states.entered.kelvin
                )
                * (1 - progress)
            )
            if self.kelvin_per_step is not None
            and self.presence_states.occupied.kelvin is not None
            and self.presence_states.entered.kelvin is not None
            else 0
        )
        if brightness_change == 0 and kelvin_change == 0:
            return
        steps = ceil(
            min(
                max(
                    abs(brightness_change) / self.brightness_per_step,
                    abs(kelvin_change) / self.kelvin_per_step
                    if self.kelvin_per_step is not None
                    else 0,
                    1,
                ),
                self.transition_period * self.constants["max_steps_per_second"],
            ),
        )
        super().start_transition_towards_occupied(
            self.transition_period / steps,
            steps,
            brightness_step=brightness_change / steps,
            kelvin_step=kelvin_change / steps,
        )

    @override
    def transition_towards_occupied(self, **kwargs: Any) -> None:
        """Step towards occupied lighting settings."""
        if kwargs["timer_id"] != self.transition_timer:
            return
        steps_remaining = kwargs["steps_remaining"] - 1
        if steps_remaining > 0:
            self.adjust(
                round(
                    self.presence_states.occupied.brightness
                    - kwargs["brightness_step"] * steps_remaining,
                ),
                round(
                    self.presence_states.occupied.kelvin
                    - kwargs["kelvin_step"] * steps_remaining,
                ),
            )
        super().transition_towards_occupied(**kwargs)
