"""Coordinates all home automation control.

Schedules scene changes, reacts to user input, reports errors & low batteries.

User defined variables are configued in control.yaml
"""

import datetime
import urllib.request

from app import App


class Control(App):
    """Controls the scene based on scheduled events, people's presence and input."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.apps = dict.fromkeys(["climate", "lights", "media", "presence", "safety"])
        self.online = False
        self.timers = {
            "morning_time": None,
            "day_time": None,
            "bed_time": None,
            "heartbeat": None,
            "heartbeat_fail_count": 0,
            "init_delay": None,
        }
        self.is_all_initialised = False
        self.__scene = None
        self.pre_sleep_scene = False

    def initialize(self):
        """Monitor logs, listen for user input, monitor batteries and set timers.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_log(self.handle_log)
        if self.entities.input_boolean.development_mode.state == "off":
            self.set_production_mode()
        self.__scene = self.entities.input_select.scene.state
        for app_name in self.apps:
            self.apps[app_name] = self.get_app(app_name.capitalize())
        self.call_service("counter/reset", entity_id="counter.warnings")
        self.call_service("counter/reset", entity_id="counter.errors")
        for setting in [
            "group.climate_control",
            "input_boolean",
            "input_datetime",
            "input_number",
            "input_select",
        ]:
            self.listen_state(
                self.handle_settings_change,
                setting,
                duration=self.args["settings_change_delay"],
            )
        self.listen_event(self.button, "zwave_js_value_notification")
        self.listen_event(self.handle_ifttt, "ifttt_webhook_received")
        for battery in (
            "front_door_camera",
            "back_door_camera",
            "entryway_camera",
            "door_lock",
            "kitchen_door_sensor",
            "entryway_multisensor",
            "dining_room_multisensor",
            "hall_multisensor",
            "bathroom_multisensor",
            "kitchen_door_sensor",
            "kitchen_button",
            "bedroom_button",
            "nest_protect_entryway",
            "nest_protect_living_room",
            "nest_protect_garage",
            "owlet",
            "toothbrush",
            "soil_sensor_vegetable_garden_sun",
            "soil_sensor_vegetable_garden_shade",
            "soil_sensor_front_deck",
            "soil_sensor_back_deck",
            "soil_sensor_carport_pots",
            "soil_sensor_dining_room",
            "soil_sensor_living_room",
            "soil_sensor_office",
        ):
            self.listen_state(
                self.handle_battery_level_change,
                f"sensor.{battery}_battery_level",
            )
        self.set_timer("morning_time")
        self.run_daily(self.handle_day_time, self.args["day_time"])
        self.set_timer("bed_time")
        self.timers["heartbeat"] = self.run_every(
            self.heartbeat,
            "now",
            self.args["heartbeat_period"],
        )
        for system_component in [
            "update.home_assistant_core_update",
            "update.home_assistant_supervisor_update",
            "update.home_assistant_operating_system_update",
        ]:
            self.listen_state(
                self.handle_system_update_available,
                system_component,
                attribute="latest_version",
            )
        self.listen_state(
            self.handle_hacs_update_available,
            "sensor.hacs",
            attribute="repositories",
        )
        self.listen_event(self.all_initialized, "appd_started")
        self.timers["init_delay"] = self.run_in(
            self.assume_all_initialised,
            self.args["init_delay"],
        )
        # TODO: https://app.asana.com/0/1207020279479204/1203851145721583/f
        # test self.notify("test message", targets="dan", title="test title", critical=True)

    def all_initialized(self, event_name: str, data: dict, **kwargs: dict):
        """Configure all apps with the current scene."""
        del event_name, data, kwargs
        self.log("All apps ready, resetting scene")
        self.is_all_initialised = True
        for app_name in self.apps:
            self.apps[app_name] = self.get_app(app_name.capitalize())
        self.reset_scene()
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
                f"{self.args['init_delay']} seconds",
            )
            self.all_initialized(None, None)

    def handle_app_reloaded(self, event_name: str, data: dict, **kwargs: dict):
        """Re-link the app and set a timer to initialise it."""
        del event_name, kwargs
        self.apps[data["app"].lower()] = self.get_app(data["app"])
        self.log(f"App added: '{data['app']}'")
        self.reset_scene()

    @property
    def scene(self) -> str:
        """Access scene setting (all apps) from here rather than Home Assistant."""
        return self.__scene

    @scene.setter
    def scene(self, new_scene: str):
        """Propagate scene change to other apps and sync scene with Home Assistant."""
        # TODO: this should check if new_scene is different to old scene
        # TODO: figure out where scene needs to be reset and why (current reason this isn't checked)
        old_scene = self.__scene
        self.__scene = new_scene
        self.log(f"Setting scene to '{new_scene}' (transitioning from '{old_scene}')")
        self.apps["lights"].transition_to_scene(new_scene)
        self.apps["climate"].transition_between_scenes(new_scene, old_scene)
        if new_scene == "Sleep" or "Away" in new_scene:
            self.call_service("lock/lock", entity_id="lock.door_lock")
            self.turn_on("switch.entryway_camera_enabled")
            self.turn_on("switch.back_door_camera_enabled")
            if "Away" in new_scene:
                self.notify(
                    f"Home set to {new_scene} mode",
                    title="Door Locked",
                )
                # TODO: https://app.asana.com/0/1207020279479204/1203851145721583/f
                # clear above notification when not Away?
                self.apps["media"].turn_off()
                # TODO: https://app.asana.com/0/1207020279479204/1203851145721573/f
                # media off unless in guest mode
        else:
            self.turn_off("switch.entryway_camera_enabled")
            self.turn_off("switch.back_door_camera_enabled")
            if new_scene == "TV" and not self.apps["media"].on:
                self.apps["media"].turn_on()
        self.call_service(
            "input_select/select_option",
            entity_id="input_select.scene",
            option=new_scene,
        )

    def reset_scene(self):
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if self.scene == "Bright":
            self.scene = "Bright"
        elif not self.apps["presence"].anyone_home:
            self.scene = (
                "Away (Night)"
                if self.entities.binary_sensor.dark_outside.state == "on"
                else "Away (Day)"
            )
        elif self.entities.binary_sensor.dark_outside.state == "off":
            self.scene = "Day"
        elif self.apps["media"].playing:
            self.scene = "TV"
        elif self.scene in ("Morning", "Sleep"):
            self.scene = (
                "Morning"
                if (
                    self.parse_time(self.get_setting("morning_time"))
                    < self.time()
                    < self.parse_time(self.args["day_time"])
                )
                else "Sleep"
            )
        else:
            self.scene = "Night"

    def set_timer(self, name: str):
        """Set morning or bed timer as specified by the corresponding settings."""
        self.cancel_timer(self.timers[name])
        self.timers[name] = self.run_daily(
            self.handle_morning_time
            if name == "morning_time"
            else self.handle_bed_time,
            self.get_setting(name),
        )

    @property
    def valid_time_settings(self) -> bool:
        """Check if morning and bed times are appropriate."""
        return (
            self.parse_time(self.get_setting("morning_time"))
            < self.parse_time(self.args["day_time"])
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
            self.scene = "Day"
        else:
            self.log(f"Ignoring day timer (scene is '{self.scene}', not 'Morning')")

    def handle_bed_time(self, **kwargs: dict):
        """Adjust climate control when nearing bed time (callback for daily timer)."""
        del kwargs
        self.log("Bed timer triggered")
        self.apps["climate"].pre_condition_bedrooms()
        self.call_service("lock/lock", entity_id="lock.door_lock")

    @property
    def bed_time(self) -> bool:
        """Return if the time is after bed time (and before midnight)."""
        return self.time() > self.parse_time(self.get_setting("bed_time"))

    def button(self, event_name: str, data: dict, **kwargs: dict):
        """Detect and handle when a button is clicked or held."""
        del event_name, kwargs
        room = (
            "bedroom"
            if data["node_id"] == self.args["bedroom_button_node_id"]
            else "kitchen"
        )
        if data["value"] == "KeyPressed":
            self.handle_button_click(room)
        elif data["value"] == "KeyHeldDown":
            self.log(f"Button in '{room}' held")
            self.apps["climate"].aircon = not self.apps["climate"].aircon
            # TODO: fix so only the button's room aircon is turned on/off

    def handle_button_click(self, room: str):
        """Handle a button click."""
        self.log(f"Button in '{room}' clicked")
        if room == "kitchen":
            if self.scene == "Night":
                if self.entities.binary_sensor.dark_outside.state == "off":
                    self.scene = "Day"
                elif self.apps["media"].playing:
                    self.scene = "TV"
                else:
                    self.scene = "Bright"
            else:
                self.scene = "Night"
        elif room == "bedroom":
            if self.scene == "Morning":
                self.scene = "Day"
                self.apps["lights"].lights["bedroom"].adjust_to_max()
                self.log("Bedroom light turned on")
            elif self.scene == "Night":
                self.pre_sleep_scene = True
                self.scene = "Sleep"
                self.log("Bedroom light kept on (pre-sleep)")
            elif (
                self.scene == "Sleep"
                and self.apps["lights"].lights["bedroom"].brightness != 0
            ):
                self.apps["lights"].lights["bedroom"].turn_off()
                self.log("Pre-sleep transitioned to Sleep - bedroom light turned off")
            elif self.scene == "TV":
                self.apps["lights"].lights["bedroom"].ignore_presence()
                self.apps["lights"].lights["bedroom"].turn_off()
                self.log("TV is on - bedroom light turned off but scene remains 'TV'")
            else:
                self.scene = "Night"

    def handle_ifttt(self, event_name: str, data: dict, **kwargs: dict):
        """Handle commands coming in via IFTTT."""
        del event_name, kwargs
        self.log(f"Received '{data}' from IFTTT")
        if "bright" in data:
            self.scene = "Bright"
        elif "sleep" in data:
            self.scene = "Sleep"
        elif "climate_control" in data:
            self.apps["climate"].climate_control_enabled = not self.apps[
                "climate"
            ].climate_control_enabled
        elif "aircon" in data:
            self.apps["climate"].aircon = not self.apps["climate"].aircon
            # TODO: how to handle individual aircons?
        elif "lock" in data:
            command = (
                "unlock" if self.entities.lock.door_lock.state == "locked" else "lock"
            )
            self.call_service(f"lock/{command}", entity_id="lock.door_lock")

    def get_setting(self, setting_name: str) -> int:
        """Get UI input_number setting values."""
        if setting_name.endswith("_time"):
            return self.get_state(f"input_datetime.{setting_name}")
        return int(float(self.get_state(f"input_number.{setting_name}")))

    def handle_settings_change(
        self,
        entity: str,
        attribute: str,
        old,
        new,
        **kwargs: dict,
    ):
        """Act on setting changes made by the user through the UI."""
        del attribute, kwargs
        setting = entity.split(".")[1]
        self.log(f"UI setting '{setting}' changed to '{new}' from '{old}'")
        if setting == "scene":
            if new != self.scene:
                self.scene = new
        elif "climate_control" in setting:
            if setting == "climate_control" and (
                (new == "on") != self.apps["climate"].climate_control_enabled
            ):
                self.apps["climate"].climate_control_enabled = new == "on"
        elif setting == "pets_home_alone":
            if (new == "on") != self.apps["presence"].pets_home_alone:
                self.log(f"UI setting '{setting}' changed to '{new}'")
                self.apps["presence"].pets_home_alone = new == "on"
        else:
            self.handle_simple_settings_change(setting, new, old)

    def handle_simple_settings_change(self, setting: str, new: str, old: str):
        """Act on changes to settings that can only be made through the UI."""
        if setting == "development_mode":
            self.set_production_mode(new == "off")
        elif setting.startswith("circadian"):
            try:
                self.apps["lights"].redate_circadian()
            except ValueError:
                self.revert_setting(f"input_datetime.{setting}", old)
        elif setting.endswith("_time"):
            if self.valid_time_settings:
                self.set_timer(setting)
            else:
                self.revert_setting(f"input_datetime.{setting}", old)
        elif "temperature" in setting:
            self.apps["climate"].validate_target_and_trigger(setting)
        elif "door" in setting:
            self.apps["climate"].update_door_check_delay(float(new))
        elif setting == "aircon_vacating_delay":
            self.apps["climate"].update_aircon_vacating_delays(float(new))
        elif setting == "fan_vacating_delay":
            self.apps["climate"].update_fan_vacating_delays(float(new))
        elif setting == "heater_vacating_delay":
            self.apps["climate"].update_heater_vacating_delays(float(new))
        else:
            self.apps["lights"].transition_to_scene(self.scene)

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
        elif float(new) <= self.args["notify_battery_level"] and (
            old in ("unavailable", "unknown", None)
            or float(old) >= self.args["notify_battery_level"]
        ):
            self.notify(f"{entity} is low ({new}%)", title="Low Battery", targets="dan")

    def heartbeat(self, **kwargs: dict):
        """Send a heartbeat then handle if it is received or not."""
        del kwargs
        try:
            urllib.request.urlopen(  # noqa: S310
                self.args["heartbeat_url"],
                timeout=self.args["heartbeat_timeout"],
            )
        except OSError:
            self.timers["heartbeat_fail_count"] += 1
            if (
                self.online
                and self.timers["heartbeat_fail_count"]
                >= self.args["heartbeat_max_fail_count"]
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
                    >= self.args["heartbeat_max_fail_count"]
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

    def handle_system_update_available(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Notify when a system update is available."""
        del attribute, old, new, kwargs
        self.notify(
            f"{self.get_state(entity, attribute='title')} update available",
            title="System Update Available",
            targets="dan",
        )

    def handle_hacs_update_available(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Notify when a HACS update is available."""
        del entity, attribute, kwargs
        count = len(new)
        if count != 0 and count > len(old):
            self.notify(
                f"{count} HACS update{'s' if count != 1 else ''} available",
                title="System Update Available",
                targets="dan",
            )
