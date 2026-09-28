"""Coordinates all home automation control.

Schedules scene changes and handles user input.

User defined variables are configued in control.yaml
"""

import urllib.request
from datetime import datetime
from typing import Any, cast, override

from app import App


class Control(App):
    """Controls the scene based on scheduled events, people's presence and input."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.all_initialized = False
        self.online = False
        self.heartbeat_fail_count = 0
        self.log_listeners: list[str] = []
        self.logging_multiline_error = False
        self.timers: dict[str, str | None] = dict.fromkeys(
            [
                "morning_time",
                "day_time",
                "nursery_time",
                "bed_time",
                "heartbeat",
                "init_delay",
                "living_room_button",
                "nursery_button",
                "dan_s_bedroom_button",
                "rachel_s_bedroom_button",
            ],
            None,
        )
        self.timestamps: dict[str, float] = dict.fromkeys(
            [
                "living_room_button_last_press",
                "nursery_button_last_press",
                "dan_s_bedroom_button_last_press",
                "rachel_s_bedroom_button_last_press",
            ],
            0,
        )

    @override
    def initialize(self) -> None:
        """Set timers and monitor logs, system events, and user input."""
        super().initialize()
        for log_type in ("main_log", "error_log"):
            self.log_listeners.extend(
                self.listen_log(
                    self.increment_log_issue_counter,
                    "WARNING",
                    log=log_type,
                )
                or [],
            )
        if not self.get_boolean_setting("development_mode"):
            self.set_production_mode()
        for setting in [
            "input_boolean.development_mode",
            "input_boolean.pets_home_alone",
            "input_boolean.napping_in_bedroom",
            "input_boolean.napping_in_nursery",
            "input_datetime",
            "input_number",
            "input_select",
        ]:
            self.listen_state(
                self.handle_ui_settings_change,
                setting,
                duration=self.constants["settings_change_delay"],
            )
        for device_id in self.constants["button"]["id"]:
            self.listen_event(
                self.handle_bedroom_tuya_button,
                "localtuya_device_dp_triggered",
                device_id=device_id,
            )
        for room in ("nursery", "living_room"):
            self.listen_state(self.handle_z_wave_button, f"event.{room}_button")
        self.listen_event(self.handle_ifttt, "ifttt_webhook_received")
        self.set_timer("morning_time")
        self.run_daily(self.handle_day_time, self.constants["day_time"])
        self.set_timer("nursery_time")
        self.set_timer("bed_time")
        self.timers["heartbeat"] = self.run_every(
            self.heartbeat,
            "immediate",
            self.constants["heartbeat"]["period"],
        )
        self.listen_state(
            self.handle_update_available,
            "update",
            attribute="latest_version",
        )
        self.listen_event(self.handle_all_initialized, "appd_started")
        self.timers["init_delay"] = self.run_in(
            self.assume_all_initialized,
            self.constants["init_delay"],
        )

    def handle_all_initialized(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Configure all apps with the current scene."""
        del event_type, data, kwargs
        self.log("All apps ready, resetting scene")
        self.all_initialized = True
        self.reset_scene(keep_bright=True)
        self.listen_event(
            self.handle_app_reloaded,
            "app_initialized",
            namespace="admin",
        )

    def assume_all_initialized(self, **kwargs: Any) -> None:
        """Configure all apps if not already done normally."""
        del kwargs
        if not self.all_initialized:
            self.log(
                "Assuming initialisation complete after "
                f"{self.constants['init_delay']} seconds",
                level="WARNING",
            )
            self.handle_all_initialized("assume_all_initialized", {})

    def handle_app_reloaded(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Set a timer to re-initialise the system."""
        del event_type, kwargs
        self.log(f"App added: '{data['app']}'")
        self.cancel_timer(self.timers["init_delay"])
        self.timers["init_delay"] = self.run_in(
            self.assume_all_reloaded,
            self.constants["init_delay"],
        )

    def assume_all_reloaded(self, **kwargs: Any) -> None:
        """Configure all apps if not already done normally."""
        del kwargs
        self.log(
            "Assuming all apps have reloaded after "
            f"{self.constants['init_delay']} seconds",
        )
        self.reset_scene(keep_bright=True)

    @property
    def scene(self) -> str:
        """Name of the current Home Assistant scene."""
        return self.entities.input_select.scene.state

    @scene.setter
    def scene(self, new_scene: str) -> None:
        """Set the Home Assistant scene and propagate the change to all apps."""
        self.log(f"Setting scene to '{new_scene}' (was previously '{self.scene}')")
        self.lights.transition_to_scene(new_scene)
        self.climate.transition_to_scene(new_scene)
        if new_scene == "Sleep" or new_scene.startswith("Away"):
            self.presence.lock_front_door()
            for area in ("entryway", "back_door", "back_deck", "garage"):
                self.turn_on(f"switch.{area}_camera_enabled")
            if new_scene == "Sleep":
                self.napping_in_bedroom = True
            else:
                self.turn_on("switch.living_room_camera_enabled")
                self.notify(f"Home set to {new_scene} mode", title="Door Locked")
                self.napping_in_bedroom = False
                self.napping_in_nursery = False
                # TODO: https://app.asana.com/0/1207020279479204/1203851145721583/f
                # clear above notification when not Away?
                self.media.turn_off()
        else:
            for area in ("entryway", "living_room", "back_door", "back_deck", "garage"):
                self.turn_off(f"switch.{area}_camera_enabled")
            if new_scene == "TV" and not self.media.on:
                self.media.turn_on()
        self.call_service(
            "input_select/select_option",
            entity_id="input_select.scene",
            option=new_scene,
        )

    def reset_scene(self, *, keep_bright: bool = False) -> None:
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if keep_bright and self.scene == "Bright":
            self.scene = "Bright"
        elif not self.presence.anyone_home:
            self.scene = f"Away ({'Night' if self.lights.dark_outside else 'Day'})"
        elif not self.lights.dark_outside:
            self.scene = "Day"
        elif self.media.playing and not self.media.muted:
            self.scene = "TV"
        elif self.scene in ("Morning", "Sleep"):
            self.scene = (
                "Morning"
                if self.now_is_between(
                    self.get_time_setting("morning_time"),
                    self.constants["day_time"],
                )
                else "Sleep"
            )
        else:
            self.scene = "Night"

    @property
    def napping_in_bedroom(self) -> bool:
        """True if nap mode is enabled for the bedroom."""
        return self.get_boolean_setting("napping_in_bedroom")

    @napping_in_bedroom.setter
    def napping_in_bedroom(self, napping: bool) -> None:
        """Set bedroom napping state, adjusting lights and climate devices."""
        self.__change_napping_state("bedroom", napping=napping)

    @property
    def napping_in_nursery(self) -> bool:
        """True if nap mode is enabled for the nursery."""
        return self.get_boolean_setting("napping_in_nursery")

    @napping_in_nursery.setter
    def napping_in_nursery(self, napping: bool) -> None:
        """Set nursery napping state, adjusting lights and climate devices."""
        self.__change_napping_state("nursery", napping=napping)

    def napping_in(self, room: str, *, sustained: bool = False) -> bool:
        """Get napping state for the given room from Home Assistant."""
        input_id = f"input_boolean.napping_in_{room}"
        napping = self.get_state(input_id) == "on"
        if not sustained or not napping:
            return napping
        return (
            self.parse_utc_string(
                cast("str", self.get_state(input_id, attribute="last_changed")),
            )
            + self.constants["napping_sustained_delay"]
            < self.get_now_ts()
        )

    def __change_napping_state(self, room: str, *, napping: bool) -> None:
        """Change device behaviour based on the specified room and napping state."""
        self.log(
            f"Configuring the '{room}' with '{'' if napping else 'non-'}nap' settings",
        )
        self.call_service(
            f"input_boolean/turn_{'on' if napping else 'off'}",
            entity_id=f"input_boolean.napping_in_{room}",
        )
        if napping:
            for light in (room, "hall"):
                self.lights.lights[light].ignore_presence()
                self.turn_off(f"light.{light}")  # turns off even if control disabled
            self.climate.condition_room_for_sleep(room)
        else:
            self.lights.transition_to_scene(self.scene)
            self.climate.condition_room_normally(room)

    def set_timer(self, name: str) -> None:
        """Set morning or bed timers as specified by the corresponding UI settings."""
        self.cancel_timer(self.timers[name])
        self.timers[name] = self.run_daily(
            self.handle_morning_time
            if name == "morning_time"
            else self.handle_bed_times,
            self.get_time_setting(name),
            timer_name=name,
        )

    @property
    def valid_time_settings(self) -> bool:
        """Check if morning and bed times are appropriate."""
        return (
            self.get_time_setting("morning_time")
            < self.parse_time(self.constants["day_time"])
            < self.get_time_setting("bed_time")
        )

    def handle_morning_time(self, **kwargs: Any) -> None:
        """Change scene to Morning (callback for daily timer)."""
        del kwargs
        self.log("Morning timer triggered")
        if self.scene == "Sleep":
            self.scene = "Morning"
        else:
            self.log(f"Ignoring morning timer (scene is '{self.scene}', not 'Sleep')")

    def handle_day_time(self, **kwargs: Any) -> None:
        """Transition from Morning to Day scene (callback for daily timer)."""
        del kwargs
        self.log("Day timer triggered")
        if self.scene == "Morning":
            self.napping_in_bedroom = False
            self.scene = "Day"
        else:
            self.log(f"Ignoring day timer (scene is '{self.scene}', not 'Morning')")

    def handle_bed_times(self, **kwargs: Any) -> None:
        """Adjust climate control when nearing bed times (callback for daily timer)."""
        if self.scene.startswith("Away"):
            self.log(f"Ignoring '{kwargs['timer_name']}' timer ('{self.scene}' scene)")
            return
        self.log(f"{kwargs['timer_name']} triggered")
        self.climate.condition_room_for_sleep(
            "bedroom" if kwargs["timer_name"] == "bed_time" else "nursery",
        )
        self.presence.lock_front_door()

    @property
    def bed_time(self) -> bool:
        """True if the time is after bed time (and before midnight)."""
        return self.time() > self.get_time_setting("bed_time")

    def handle_z_wave_button(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle a Z-Wave button event."""
        del attribute, new, kwargs
        button = entity.removeprefix("event.")
        event = self.get_state(entity, attribute="event_type")
        if old == "unavailable":
            if self.debugging:
                self.log(
                    f"Button '{button}' was previously 'unavailable'"
                    f", ignoring '{{event}}'",
                    level="DEBUG",
                )
            return
        self.cancel_timer(self.timers[button])
        now = self.get_now_ts()
        if event == "KeyPressed":
            if (
                now - self.timestamps[f"{button}_last_press"]
                < self.constants["button"]["max_double_press_delay"]
            ):
                getattr(self, f"handle_{button}_double_press")()
            else:
                if self.debugging:
                    self.log(
                        f"The '{button}' was pressed once: "
                        "delaying action to detect double press",
                        level="DEBUG",
                    )
                self.timers[button] = self.run_in(
                    getattr(self, f"handle_{button}_single_press"),
                    self.constants["button"]["max_double_press_delay"],
                )
            self.timestamps[f"{button}_last_press"] = now
        elif event == "KeyHeldDown":
            getattr(self, f"handle_{button}_long_press")()

    def handle_living_room_button_single_press(self, **kwargs: Any) -> None:
        """Handle a single press of the living room button."""
        del kwargs
        self.log("Living room button pressed")
        self.scene = "Sleep"

    def handle_living_room_button_double_press(self) -> None:
        """Handle a double press of the living room button."""
        self.log("Living room button double pressed")
        self.reset_scene()
        if self.scene in ("Sleep", "Day"):
            self.scene = "Night"

    def handle_living_room_button_long_press(self) -> None:
        """Handle a long press of the living room button."""
        self.log("Living room button held down")
        aircons = (
            self.climate.aircons["living_room"],
            self.climate.aircons["dining_room"],
        )
        should_turn_off = any(aircon.on for aircon in aircons)
        self.log(f"Aircon turning {'off' if should_turn_off else 'on'}")
        for aircon in aircons:
            if should_turn_off:
                aircon.turn_off()
            else:
                aircon.turn_on_for_conditions()
            aircon.handle_user_adjustment("the living room button")

    def handle_nursery_button_single_press(self, **kwargs: Any) -> None:
        """Handle a single press of the nursery button."""
        del kwargs
        self.log("Nursery button pressed")
        if self.napping_in_nursery:
            self.log("Nursery already configured for napping - setting again anyway")
        self.napping_in_nursery = True

    def handle_nursery_button_double_press(self) -> None:
        """Handle a double press of the nursery button."""
        self.log("Nursery button double pressed: turning light on")
        self.napping_in_nursery = False
        light = self.lights.lights["nursery"]
        if light.control_enabled:
            light.turn_on_for_conditions()
        else:
            light.turn_on()

    def handle_nursery_button_long_press(self) -> None:
        """Handle a long press of the nursery button."""
        self.log("Nursery button held down")
        devices = (
            self.climate.heaters["nursery"],
            self.climate.fans["nursery"],
            self.climate.humidifiers["nursery"],
        )
        if any(device.on for device in devices):
            self.log("Turning climate devices off")
            for device in devices:
                device.turn_off()
                device.handle_user_adjustment("the nursery button")
        else:
            self.climate.humidifiers["nursery"].control_enabled = True
            if devices[0].room_closer_to_hot_than_cold:
                device = self.climate.fans["nursery"]
                self.log("Turning fan on because it's hot")
            else:
                device = self.climate.heaters["nursery"]
                self.log("Turning heater on because it's cold")
            device.turn_on_for_conditions()
            device.handle_user_adjustment("the nursery button")

    def handle_bedroom_tuya_button(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Handle a bedroom Tuya button event."""
        del event_type, kwargs
        name: str = self.constants["button"]["id"][data["device_id"]]
        timer_id = f"{name}_s_bedroom_button"
        button = f"{name.capitalize()}'s bedroom button"
        self.cancel_timer(self.timers[timer_id])
        if data["value"] == "single_click":
            now = self.get_now_ts()
            if (
                now - self.timestamps[f"{timer_id}_last_press"]
                < self.constants["button"]["max_double_press_delay"]
            ):
                self.handle_bedroom_button_double_press(button)
            else:
                if self.debugging:
                    self.log(
                        f"{button} was pressed once: "
                        "delaying action to detect double press",
                        level="DEBUG",
                    )
                self.timers[timer_id] = self.run_in(
                    self.handle_bedroom_button_single_press,
                    self.constants["button"]["max_double_press_delay"],
                    button=button,
                )
            self.timestamps[f"{timer_id}_last_press"] = now
        elif data["value"] == "double_click":
            self.handle_bedroom_button_double_press(button)
        elif data["value"] == "long_press":
            self.handle_bedroom_button_long_press(button)

    def handle_bedroom_button_single_press(self, **kwargs: Any) -> None:
        """Handle a single press of a bedroom button."""
        self.log(f"{kwargs['button']} pressed")
        if self.napping_in_bedroom:
            self.scene = "Sleep"
        else:
            self.napping_in_bedroom = True

    def handle_bedroom_button_double_press(self, button: str) -> None:
        """Handle a double press of bedroom button."""
        self.log(f"{button} double pressed: turning light on")
        self.napping_in_bedroom = False
        light = self.lights.lights["bedroom"]
        if light.control_enabled:
            light.turn_on_for_conditions()
        else:
            light.turn_on()
        if self.scene == "Sleep":
            self.reset_scene()
            if self.scene == "Sleep":
                self.log("Reset still chose 'Sleep' scene - overriding to 'Night'")
                self.scene = "Night"

    def handle_bedroom_button_long_press(self, button: str) -> None:
        """Handle a long press of a bedroom button."""
        self.log(f"{button} held down: adjusting aircon/fan")
        self.climate.humidifiers["bedroom"].control_enabled = True
        aircon = self.climate.aircons["bedroom"]
        fan = self.climate.fans["bedroom"]
        if aircon.on:
            aircon.turn_off()
            aircon.handle_user_adjustment(button)
            if fan.on and not fan.room_closer_to_hot_than_cold:
                fan.turn_off()
                fan.handle_user_adjustment(button)
        elif fan.on:
            if fan.room_closer_to_hot_than_cold:
                aircon.turn_on_for_conditions()
                aircon.handle_user_adjustment(button)
            else:
                fan.turn_off()
                fan.handle_user_adjustment(button)
        elif fan.room_closer_to_hot_than_cold:
            fan.turn_on_for_conditions()
            fan.handle_user_adjustment(button)
            aircon.control_enabled = True
        else:
            aircon.turn_on_for_conditions()
            aircon.handle_user_adjustment(button)

    def handle_ifttt(
        self,
        event_type: str,
        data: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        """Handle commands coming in via IFTTT."""
        del event_type, kwargs
        self.log(f"Received '{data}' from IFTTT")
        if "bright" in data:
            self.scene = "Bright"
        elif "sleep" in data:
            self.scene = "Sleep"
        elif "climate_control" in data:
            self.climate.all_climate_control_enabled = (
                not self.climate.all_climate_control_enabled
            )
        elif "aircon" in data:
            self.climate.toggle_airconditioning(user="voice control")
        elif "lock" in data:
            if self.presence.front_door_locked:
                self.presence.unlock_front_door(force=True)
            else:
                self.presence.lock_front_door(force=True)

    def handle_ui_settings_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle setting changes made by the user through the UI."""
        del attribute, kwargs
        _, setting = entity.split(".")
        user_id: str | None = cast("dict", self.get_state(entity, attribute="context"))[
            "user_id"
        ]
        is_user: bool = self.constants["ids"].get(user_id) != "System"
        if is_user or self.debugging:
            self.log(
                f"'{self.constants['ids'].get(user_id)}'"
                f" changed UI setting '{setting}' to '{new}' from '{old}'",
                level="INFO" if is_user else "DEBUG",
            )
        if not is_user:
            return
        if setting == "scene":
            self.scene = new
        elif setting == "pets_home_alone":
            if (new == "on") != self.presence.pets_home_alone:
                self.presence.pets_home_alone = new == "on"
        elif setting.startswith("napping_in"):
            self.__change_napping_state(setting.split("_")[-1], napping=new == "on")
        elif setting == "development_mode":
            if new == "on":
                self.set_production_mode(False)
            else:
                self.call_service("hassio/app_restart", app="a0d7b954_appdaemon")
        else:
            self.handle_simple_settings_change(setting, new, old)

    def handle_simple_settings_change(self, setting: str, new: str, old: str) -> None:
        """Handle changes to settings that can only be made through the UI."""
        if setting.startswith("circadian"):
            try:
                self.lights.redate_circadian()
            except ValueError:
                self.revert_time_setting(setting, old)
        elif setting.endswith("_time"):
            if self.valid_time_settings:
                self.set_timer(setting)
            else:
                self.revert_time_setting(setting, old)
        elif "temperature" in setting:
            self.climate.validate_temperature_setting(setting)
        elif "humidity" in setting:
            self.climate.validate_humidity_setting(setting)
        elif "door" in setting:
            self.climate.update_door_check_delay(float(new))
        else:
            device_type, setting_type = setting.split("_", maxsplit=1)
            if setting_type == "vacating_delay" and device_type in (
                "aircon",
                "fan",
                "heater",
                "humidifier",
            ):
                self.climate.update_vacating_delays(device_type, float(new))
            else:
                self.lights.transition_to_scene(self.scene)

    def revert_time_setting(self, setting_name: str, value: str) -> None:
        """Revert setting to the specified value and notify."""
        self.call_service(
            "input_datetime/set_datetime",
            entity_id=setting_name,
            time=value,
        )
        self.notify(
            f"Invalid time for '{setting_name}' - reverted to previous",
            title="Invalid Setting",
        )

    def heartbeat(self, **kwargs: Any) -> None:
        """Send a heartbeat then handle if it is received or not."""
        del kwargs
        try:
            urllib.request.urlopen(
                f"https://{self.constants['heartbeat']['url']}",
                timeout=self.constants["heartbeat"]["timeout"],
            )
        except OSError:
            self.heartbeat_fail_count += 1
            if (
                self.online
                and self.heartbeat_fail_count
                >= self.constants["heartbeat"]["max_fail_count"]
            ):
                self.online = False
                self.log("Heartbeat timed out", level="WARNING")
        else:
            if self.heartbeat_fail_count > 0:
                self.log(
                    "Heartbeat sent and recieved after "
                    f"{self.heartbeat_fail_count} timeout(s)",
                )
            if not self.online:
                self.online = True
                if (
                    self.heartbeat_fail_count
                    >= self.constants["heartbeat"]["max_fail_count"]
                ):
                    self.log("Restarting Home Assistant to fix any broken entities")
                    if self.log_listeners is not None:
                        for listener in self.log_listeners:
                            self.cancel_listen_log(listener)
                    self.call_service("homeassistant/restart")
            self.heartbeat_fail_count = 0

    def increment_log_issue_counter(
        self,
        app_name: str,
        timestamp: datetime,
        level: str,
        log_type: str,
        message: str,
        **kwargs: dict,
    ) -> None:
        """Increment counters if logged message is a WARNING or ERROR/CRITICAL."""
        del app_name, timestamp, kwargs
        if level == "ERROR" and log_type == "error_log":
            if self.logging_multiline_error:
                if message.endswith("====="):
                    self.logging_multiline_error = False
                return
            if message.startswith("====="):
                self.logging_multiline_error = True
        self.call_service(
            "counter/increment",
            entity_id="counter.warnings" if level == "WARNING" else "counter.errors",
        )

    def handle_update_available(
        self,
        entity: str,
        attribute: str,
        old: str | None,
        new: str | None,
        **kwargs: Any,
    ) -> None:
        """Notify when a system or component update is available."""
        del attribute, kwargs
        if (
            new is None
            or (
                old is None
                and new == self.get_state(entity, attribute="installed_version")
            )
            or self.get_state(entity, attribute="auto_update")
        ):
            return
        self.notify(
            f"{self.get_state(entity, attribute='friendly_name')} available",
            title="Update Available",
            targets="dan",
        )
