"""Coordinates all home automation control.

Schedules scene changes, reacts to user input, reports errors & low batteries.

User defined variables are configued in control.yaml
"""

import datetime
import urllib.request

from app import App, IDs


class Control(App):
    """Controls the scene based on scheduled events, people's presence and input."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.online = False
        self.timers = {
            "morning_time": None,
            "day_time": None,
            "nursery_time": None,
            "bed_time": None,
            "heartbeat": None,
            "heartbeat_fail_count": 0,
            "init_delay": None,
            "living_room_button": None,
            "living_room_button_last_press": 0,
            "nursery_button": None,
            "nursery_button_last_press": 0,
        }
        self.is_all_initialised = False

    def initialize(self):
        """Monitor logs, listen for user input, monitor batteries and set timers.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_log(self.handle_log)
        if self.entities.input_boolean.development_mode.state == "off":
            self.set_production_mode()
        self.call_service("counter/reset", entity_id="counter.warnings")
        self.call_service("counter/reset", entity_id="counter.errors")
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
        for name in ("rachel", "dan"):
            self.listen_event(
                self.handle_bedroom_tuya_button,
                "localtuya_device_dp_triggered",
                device_id=self.constants["button_ids"][name],
            )
        for room in ("nursery", "living_room"):
            self.listen_state(
                self.handle_z_wave_button,
                f"event.{room}_button",
            )
        self.listen_event(self.handle_ifttt, "ifttt_webhook_received")
        for battery in (
            "front_door_camera",
            "back_door_camera",
            "entryway_camera",
            "door_lock",
            "entryway_multisensor",
            "dining_room_multisensor",
            "hall_multisensor",
            "bathroom_multisensor",
            "nursery_temperature_sensor",
            "office_temperature_sensor",
            "dog_bed_area_temperature_sensor",
            "kitchen_door_sensor",
            "dining_room_balcony_door_sensor",
            "bedroom_balcony_door_sensor",
            "living_room_button",
            "nursery_button",
            "dan_s_bedroom_button",
            "rachel_s_bedroom_button",
            "nest_protect_entryway",
            "nest_protect_living_room",
            "nest_protect_garage",
            "toothbrush",
            "side_tap",
            "back_tap",
            "soil_sensor_entryway",
            "soil_sensor_guest_suite",
            "soil_sensor_stairway",
            "soil_sensor_back_deck",
            "soil_sensor_living_room",
            "soil_sensor_dining_room",
            "soil_sensor_bathroom",
            "soil_sensor_office",
        ):
            self.listen_state(
                self.handle_battery_level_change,
                f"sensor.{battery}_battery_level",
            )
        self.set_timer("morning_time")
        self.run_daily(self.handle_day_time, self.constants["day_time"])
        self.set_timer("nursery_time")
        self.set_timer("bed_time")
        self.timers["heartbeat"] = self.run_every(
            self.heartbeat,
            "now",
            self.constants["heartbeat"]["period"],
        )
        self.listen_state(
            self.handle_update_available,
            "update",
            attribute="latest_version",
        )
        self.listen_event(self.all_initialized, "appd_started")
        self.timers["init_delay"] = self.run_in(
            self.assume_all_initialised,
            self.constants["init_delay"],
        )
        # TODO: https://app.asana.com/0/1207020279479204/1203851145721583/f
        # test self.notify("test message", targets="dan", title="test title", critical=True)

    def all_initialized(self, event_name: str, data: dict, **kwargs: dict):
        """Configure all apps with the current scene."""
        del event_name, data, kwargs
        self.log("All apps ready, resetting scene")
        self.is_all_initialised = True
        self.reset_scene(keep_bright=True)
        self.listen_event(
            self.handle_app_reloaded,
            "app_initialized",
            namespace="admin",
        )

    def assume_all_initialised(self, **kwargs: dict):
        """Configure all apps if not already done normally."""
        del kwargs
        if not self.is_all_initialised:
            self.log(
                "Assuming initialisation complete after "
                f"{self.constants['init_delay']} seconds",
            )
            self.all_initialized(None, None)

    def handle_app_reloaded(self, event_name: str, data: dict, **kwargs: dict):
        """Re-link the app and set a timer to initialise it?"""
        del event_name, kwargs
        self.log(f"App added: '{data['app']}'")
        self.reset_scene(keep_bright=True)

    @property
    def scene(self) -> str:
        """Get scene from Home Assistant."""
        return self.get_state("input_select.scene")

    @scene.setter
    def scene(self, new_scene: str):
        """Propagate scene change to other apps and sync scene with Home Assistant."""
        self.log(f"Setting scene to '{new_scene}' (was previously '{self.scene}')")
        self.lights.transition_to_scene(new_scene)
        self.climate.transition_to_scene(new_scene)
        if new_scene == "Sleep" or "Away" in new_scene:
            self.presence.lock_door()
            self.turn_on("switch.entryway_camera_enabled")
            self.turn_on("switch.back_door_camera_enabled")
            self.turn_on("switch.garage_camera_enabled")
            if new_scene == "Sleep":
                self.napping_in_bedroom = True
            else:
                self.turn_on("switch.living_room_camera_enabled")
                self.notify(
                    f"Home set to {new_scene} mode",
                    title="Door Locked",
                )
                self.napping_in_bedroom = False
                self.napping_in_nursery = False
                # TODO: https://app.asana.com/0/1207020279479204/1203851145721583/f
                # clear above notification when not Away?
                self.media.turn_off()
                # TODO: https://app.asana.com/0/1207020279479204/1203851145721573/f
                # media off unless in guest mode
        else:
            self.turn_off("switch.entryway_camera_enabled")
            self.turn_off("switch.living_room_camera_enabled")
            self.turn_off("switch.back_door_camera_enabled")
            self.turn_off("switch.garage_camera_enabled")
            if new_scene == "TV" and not self.media.on:
                self.media.turn_on()
        self.call_service(
            "input_select/select_option",
            entity_id="input_select.scene",
            option=new_scene,
        )

    def reset_scene(self, *, keep_bright: bool = False):
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if keep_bright and self.scene == "Bright":
            self.scene = "Bright"
        elif not self.presence.anyone_home:
            self.scene = (
                "Away (Night)"
                if self.entities.binary_sensor.dark_outside.state == "on"
                else "Away (Day)"
            )
        elif self.entities.binary_sensor.dark_outside.state == "off":
            self.scene = "Day"
        elif self.media.playing:
            self.scene = "TV"
        elif self.scene in ("Morning", "Sleep"):
            self.scene = (
                "Morning"
                if self.now_is_between(
                    self.get_setting("morning_time"),
                    self.constants["day_time"],
                )
                else "Sleep"
            )
        else:
            self.scene = "Night"

    @property
    def napping_in_bedroom(self) -> bool:
        """Get bedroom napping state from Home Assistant."""
        return self.get_state("input_boolean.napping_in_bedroom") == "on"

    @napping_in_bedroom.setter
    def napping_in_bedroom(self, napping: bool):
        """Set bedroom napping state, adjusting lights and climate devices."""
        self.__change_napping_state("bedroom", napping)

    @property
    def napping_in_nursery(self) -> bool:
        """Get nursery napping state from Home Assistant."""
        return self.get_state("input_boolean.napping_in_nursery") == "on"

    @napping_in_nursery.setter
    def napping_in_nursery(self, napping: bool):
        """Set nursery napping state, adjusting lights and climate devices."""
        self.__change_napping_state("nursery", napping)

    def napping_in(self, room: str) -> bool:
        """Get napping state for the given room from Home Assistant."""
        return self.get_state(f"input_boolean.napping_in_{room}") == "on"

    def __change_napping_state(self, room: str, napping: bool):
        """Change device behaviour based on napping state in the specified room."""
        self.log(
            f"Configuring the '{room}' with "
            f"'{'nap' if napping else 'normal'}' settings",
        )
        self.call_service(
            f"input_boolean/turn_{'on' if napping else 'off'}",
            entity_id=f"input_boolean.napping_in_{room}",
        )
        if napping:
            for light in (room, "hall"):
                self.lights.lights[light].ignore_vacancy()
                self.turn_off(f"light.{light}")  # turns off even if control disabled
            self.climate.condition_room_for_sleep(room)
        else:
            self.lights.lights[room].control_enabled = True
            self.lights.transition_to_scene(self.scene)
            self.climate.condition_room_normally(room)

    def set_timer(self, name: str):
        """Set morning or bed timers as specified by the corresponding settings."""
        self.cancel_timer(self.timers[name])
        self.timers[name] = self.run_daily(
            self.handle_morning_time
            if name == "morning_time"
            else self.handle_bed_times,
            self.get_setting(name),
            timer_name=name,
        )

    @property
    def valid_time_settings(self) -> bool:
        """Check if morning and bed times are appropriate."""
        return (
            self.parse_time(self.get_setting("morning_time"))
            < self.parse_time(self.constants["day_time"])
            < self.parse_time(self.get_setting("bed_time"))
        )

    def handle_morning_time(self, **kwargs: dict):
        """Change scene to Morning (callback for daily timer)."""
        del kwargs
        self.log("Morning timer triggered")
        if self.scene == "Sleep":
            self.scene = "Morning"
        else:
            self.log(f"Ignoring morning timer (scene is '{self.scene}', not 'Sleep')")

    def handle_day_time(self, **kwargs: dict):
        """Transition from Morning to Day scene (callback for daily timer)."""
        del kwargs
        self.log("Day timer triggered")
        if self.scene == "Morning":
            self.napping_in_bedroom = False
            self.scene = "Day"
        else:
            self.log(f"Ignoring day timer (scene is '{self.scene}', not 'Morning')")

    def handle_bed_times(self, **kwargs: dict):
        """Adjust climate control when nearing bed times (callback for daily timer)."""
        if "Away" in self.scene:
            self.log(f"Ignoring '{kwargs['timer_name']}' timer ('{self.scene}' scene)")
            return
        self.log(f"{kwargs['timer_name']} triggered")
        self.climate.condition_room_for_sleep(
            "bedroom" if kwargs["timer_name"] == "bed_time" else "nursery",
        )
        self.presence.lock_door()

    @property
    def bed_time(self) -> bool:
        """Return if the time is after bed time (and before midnight)."""
        return self.time() > self.parse_time(self.get_setting("bed_time"))

    def handle_z_wave_button(
        self,
        entity: str,
        attribute: str,
        old,
        new,
        **kwargs: dict,
    ):
        """Handle a Z-Wave button event."""
        del attribute, new, kwargs
        button = entity.removeprefix("event.")
        event = self.get_state(entity, attribute="event_type")
        if old == "unavailable":
            self.log(
                f"Button '{button}' was previously 'unavailable', ignoring '{{event}}'",
                log_level="DEBUG",
            )
            return
        self.cancel_timer(self.timers[button])
        now = self.get_now_ts()
        if event == "KeyPressed":
            if (
                now - self.timers[f"{button}_last_press"]
                < self.constants["button_max_double_press_delay"]
            ):
                getattr(self, f"handle_{button}_double_press")()
            else:
                self.log(
                    f"The '{button}' was pressed once: "
                    "delaying action to detect double press",
                    level="DEBUG",
                )
                self.timers[button] = self.run_in(
                    getattr(self, f"handle_{button}_single_press"),
                    self.constants["button_max_double_press_delay"],
                )
            self.timers[f"{button}_last_press"] = now
        elif event == "KeyHeldDown":
            getattr(self, f"handle_{button}_long_press")()

    def handle_living_room_button_single_press(self, **kwargs: dict):
        """Handle a single press of the living room button."""
        del kwargs
        self.log("Living room button pressed")
        self.log("Enabling automatic control for all lights and climate devices")
        for device_group in (
            self.lights.lights,
            self.climate.aircons,
            self.climate.fans,
            self.climate.heaters,
            self.climate.humidifiers,
        ):
            for device in device_group.values():
                device.control_enabled = True
        self.reset_scene()
        # TODO: enable guest mode
        # TODO: toggle TV if can't get it working automatically?

    def handle_living_room_button_double_press(self):
        """Handle a double press of the living room button."""
        self.log("Living room button double pressed")
        if self.scene != "Bright":
            self.scene = "Bright"
        else:
            self.reset_scene()

    def handle_living_room_button_long_press(self):
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

    def handle_nursery_button_single_press(self, **kwargs: dict):
        """Handle a single press of the nursery button."""
        del kwargs
        self.timers["nursery_button"] = None
        self.log("Nursery button pressed")
        if self.napping_in_nursery:
            self.log("Nursery already configured for napping - setting again anyway")
        self.napping_in_nursery = True

    def handle_nursery_button_double_press(self):
        """Handle a double press of the nursery button."""
        self.log("Nursery button double pressed: turning light on")
        self.napping_in_nursery = False
        self.lights.lights["nursery"].turn_on_for_conditions()
        self.lights.lights["nursery"].control_enabled = True

    def handle_nursery_button_long_press(self):
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
            if devices[0].closer_to_hot_than_cold:
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
        data: dict[str],
        **kwargs: dict,
    ):
        """Handle a bedroom Tuya button event."""
        del event_type, kwargs
        button = (
            "Dan's bedroom button"
            if data["device_id"] == self.constants["button_ids"]["dan"]
            else "Rachel's bedroom button"
        )
        if data["value"] == "single_click":
            self.handle_bedroom_button_single_press(button)
        elif data["value"] == "double_click":
            self.handle_bedroom_button_double_press(button)
        elif data["value"] == "long_press":
            self.handle_bedroom_button_long_press(button)

    def handle_bedroom_button_single_press(self, button: str):
        """Handle a single press of a bedroom button."""
        self.log(f"{button} pressed")
        if self.napping_in_bedroom and self.scene == "Night":
            self.scene = "Sleep"
        else:
            self.napping_in_bedroom = True

    def handle_bedroom_button_double_press(self, button: str):
        """Handle a double press of bedroom button."""
        self.log(f"{button} double pressed: turning light on")
        self.lights.lights["bedroom"].control_enabled = True
        self.napping_in_bedroom = False
        if self.scene == "Sleep":
            self.reset_scene()
            if self.scene == "Sleep":
                self.log("Reset still chose 'Sleep' scene - overriding to 'Night'")
                self.scene = "Night"

    def handle_bedroom_button_long_press(self, button: str):
        """Handle a long press of a bedroom button."""
        self.log(f"{button} held down: adjusting aircon/fan")
        self.climate.humidifiers["bedroom"].control_enabled = True
        aircon = self.climate.aircons["bedroom"]
        fan = self.climate.fans["bedroom"]
        if aircon.on:
            aircon.turn_off()
            aircon.handle_user_adjustment(button)
            if fan.on and not fan.closer_to_hot_than_cold:
                fan.turn_off()
                fan.handle_user_adjustment(button)
        elif fan.on:
            if fan.closer_to_hot_than_cold:
                aircon.turn_on_for_conditions()
                aircon.handle_user_adjustment(button)
            else:
                fan.turn_off()
                fan.handle_user_adjustment(button)
        elif fan.closer_to_hot_than_cold:
            fan.turn_on_for_conditions()
            fan.handle_user_adjustment(button)
            aircon.control_enabled = True
        else:
            aircon.turn_on_for_conditions()
            aircon.handle_user_adjustment(button)

    def handle_ifttt(self, event_name: str, data: dict, **kwargs: dict):
        """Handle commands coming in via IFTTT."""
        del event_name, kwargs
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
            if self.presence.door_locked:
                self.presence.unlock_door()
            else:
                self.presence.lock_door()

    def handle_ui_settings_change(
        self,
        entity: str,
        attribute: str,
        old,
        new,
        **kwargs: dict,
    ):
        """Act on setting changes made by the user through the UI."""
        del attribute, kwargs
        _, setting = self.split_entity(entity)
        user_id = self.get_state(entity, attribute="context")["user_id"]
        is_user = not IDs.is_system(user_id)
        self.log(
            f"'{IDs.get_name(user_id)}' changed UI setting '{setting}' "
            f"to '{new}' from '{old}'",
            level="DEBUG" if not is_user else "INFO",
        )
        if not is_user:
            return
        if setting == "scene":
            self.scene = new
        elif setting == "pets_home_alone":
            if (new == "on") != self.presence.pets_home_alone:
                self.presence.pets_home_alone = new == "on"
        elif "napping_in" in setting:
            self.__change_napping_state(setting.split("_")[-1], new == "on")
        else:
            self.handle_simple_settings_change(setting, new, old)

    def handle_simple_settings_change(self, setting: str, new: str, old: str):
        """Act on changes to settings that can only be made through the UI."""
        if setting == "development_mode":
            self.set_production_mode(new == "off")
        elif setting.startswith("circadian"):
            try:
                self.lights.redate_circadian()
            except ValueError:
                self.revert_setting(f"input_datetime.{setting}", old)
        elif setting.endswith("_time"):
            if self.valid_time_settings:
                self.set_timer(setting)
            else:
                self.revert_setting(f"input_datetime.{setting}", old)
        elif "temperature" in setting:
            self.climate.validate_target_and_trigger(setting)
        elif "door" in setting:
            self.climate.update_door_check_delay(float(new))
        else:
            device_type = setting.split("_")[0]
            if setting.endswith("vacating_delay") and device_type in (
                "aircon",
                "fan",
                "heater",
                "humidifier",
            ):
                self.climate.update_vacating_delays(device_type, float(new))
            else:
                self.lights.transition_to_scene(self.scene)

    def revert_setting(self, setting_id: str, value: str):
        """Revert setting to specified value & notify."""
        if setting_id.startswith("input_datetime"):
            self.call_service(
                "input_datetime/set_datetime",
                entity_id=setting_id,
                time=value,
            )
        else:
            self.call_service(
                "input_number/set_value",
                entity_id=setting_id,
                value=value,
            )
        self.notify(
            f"Invalid value for setting '{setting_id}' - reverted to previous",
            title="Invalid Setting",
        )

    def handle_battery_level_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Notify if a device's battery is low."""
        del attribute, kwargs
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115380/f
        # convert all of these unavailable checks to try float?
        # TODO: https://app.asana.com/0/1207020279479204/1205753645479424/f
        # don't just check for battery level, periodically check for last date of any update
        # TODO: https://app.asana.com/0/1207020279479204/1207033183175577/f
        # notify of low battery level only once per day and only if different to previous (or unavailable/old date)
        if new in ("unavailable", "unknown", None):
            self.log(f"'{entity}' is '{new}'", level="WARNING")
        elif float(new) <= self.constants["notify_battery_level"] and (
            old in ("unavailable", "unknown", None)
            or float(old) >= self.constants["notify_battery_level"]
        ):
            self.notify(f"{entity} is low ({new}%)", title="Low Battery", targets="dan")

    def heartbeat(self, **kwargs: dict):
        """Send a heartbeat then handle if it is received or not."""
        del kwargs
        try:
            urllib.request.urlopen(  # noqa: S310
                self.constants["heartbeat"]["url"],
                timeout=self.constants["heartbeat"]["timeout"],
            )
        except OSError:
            self.timers["heartbeat_fail_count"] += 1
            if (
                self.online
                and self.timers["heartbeat_fail_count"]
                >= self.constants["heartbeat"]["max_fail_count"]
            ):
                self.online = False
                self.log("Heartbeat timed out", level="WARNING")
        else:
            if self.timers["heartbeat_fail_count"] > 0:
                self.log(
                    "Heartbeat sent and recieved after "
                    f"{self.timers['heartbeat_fail_count']} timeout(s)",
                )
            if not self.online:
                self.online = True
                if (
                    self.timers["heartbeat_fail_count"]
                    >= self.constants["heartbeat"]["max_fail_count"]
                ):
                    self.log("Restarting Home Assistant to fix any broken entities")
                    self.cancel_listen_log(self.handle_log)
                    self.call_service("homeassistant/restart")
            self.timers["heartbeat_fail_count"] = 0

    def handle_log(
        self,
        app_name: str,
        timestamp: datetime.datetime,
        level: str,
        log_type: str,
        message: str,
        **kwargs: dict,
    ):
        """Increment counters if logged message is a WARNING or ERROR."""
        del app_name, timestamp, kwargs
        if log_type == "error_log":
            level = "ERROR" if message.startswith("Traceback") else None
        elif log_type == "main_log" and message.endswith("errors.log"):
            level = None
        if level in ("WARNING", "ERROR"):
            self.call_service(
                "counter/increment",
                entity_id=f"counter.{level.lower()}s",
            )

    def handle_update_available(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
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
