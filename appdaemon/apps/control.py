"""Coordinates all home automation control.

Schedules scene changes, reacts to user input, reports errors & low batteries.

User defined variables are configued in control.yaml
"""
import datetime
import socket
import urllib.request

import app


class Control(app.App):
    """Controls the scene based on scheduled events, people's presence and input."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.apps = dict.fromkeys(["climate", "lights", "media", "presence", "safety"])
        self.__online = False
        self.__timers = {
            "morning_time": None,
            "bed_time": None,
            "init_delay": None,
            "setting_delay_timers": {},
            "heartbeat": None,
            "heartbeat_fail_count": 0,
        }

    def initialize(self):
        """Monitor logs, listen for user input, monitor batteries and set timers.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for app_name in self.apps:
            self.apps[app_name] = self.get_app(app_name.capitalize())
        self.call_service("counter/reset", entity_id="counter.warnings")
        self.call_service("counter/reset", entity_id="counter.errors")
        self.listen_log(self.__handle_log)
        for setting in [
            "input_boolean",
            "input_datetime",
            "input_number",
            "input_select",
        ]:
            self.listen_state(self.__delay_handle_settings_change, setting)
        self.listen_event(self.__button, "zwave.scene_activated")
        self.listen_event(self.__ifttt, "ifttt_webhook_received")
        for battery in [
            "door_lock_battery_level",
            "entryway_multisensor_battery_level",
            "kitchen_multisensor_battery_level",
            "office_multisensor_battery_level",
            "bedroom_multisensor_battery_level",
            "kitchen_button_battery_level",
            "bedroom_button_battery_level",
            "entryway_protect_battery_health_state",
            "living_room_protect_battery_health_state",
            "garage_protect_battery_health_state",
        ]:
            self.listen_state(self.__handle_battery_level_change, f"sensor.{battery}")
        self.__set_timer("morning_time")
        self.__set_timer("bed_time")
        self.__timers["heartbeat"] = self.run_every(
            self.__heartbeat, "now", self.args["heartbeat_period"],
        )
        self.listen_event(self.__add_app, "app_initialized", namespace="admin")

    def __add_app(self, event_name: str, data: dict, kwargs: dict):
        """Link the app and restart the timer that finalises initialisation."""
        del event_name, kwargs
        self.apps[data["app"].lower()] = self.get_app(data["app"])
        if self.__timers["init_delay"] is not None:
            self.cancel_timer(self.__timers["init_delay"])
        self.__timers["init_delay"] = self.run_in(
            self.__all_initialized, self.args["init_delay"]
        )

    def __all_initialized(self, kwargs: dict = None):
        """Configure all apps with the current scene."""
        del kwargs
        self.log("All apps ready, resetting scene")
        self.reset_scene()

    def reset_scene(self):
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if self.scene == "Bright":
            self.log("Scene was set as 'Bright', will not be reset")
        elif not self.apps["presence"].anyone_home():
            self.scene = f"Away ({'Day' if self.apps['lights'].is_lighting_sufficient() else 'Night'})"
        elif self.apps["lights"].is_lighting_sufficient():
            self.scene = "Day"
        elif self.apps["media"].is_playing:
            self.scene = "TV"
        elif self.scene in ("Morning", "Sleep") and (
            self.parse_time(self.get_setting("morning_time"))
            < self.time()
            < self.parse_time("12:00:00")
        ):
            self.scene = "Morning"
        elif self.scene == "Sleep":
            self.log("It is night but scene was set as 'Sleep', will not be reset")
        else:
            self.scene = "Night"

    def __set_timer(self, name: str):
        """Set morning or bed timer as specified by the corresponding settings."""
        self.cancel_timer(self.__timers[name])
        self.__timers[name] = self.run_daily(
            self.__morning_time if name == "morning_time" else self.__bed_time,
            self.get_setting(name),
        )

    def __are_time_settings_valid(self) -> bool:
        """Check if morning and bed times are appropriate."""
        return (
            self.parse_time(self.get_setting("morning_time"))
            < self.parse_time("12:00:00")
            < self.parse_time(self.get_setting("bed_time"))
        )

    def __morning_time(self, kwargs: dict):
        """Change scene to Morning (callback for daily timer)."""
        del kwargs
        self.log(f"Morning timer triggered")
        if self.scene == "Sleep":
            self.scene = "Morning"
        else:
            self.log(f"Scene was not changed as it was {self.scene}, not Morning.")

    def __bed_time(self, kwargs: dict):
        """Adjust climate control when approaching bed time (callback for daily timer)."""
        del kwargs
        self.log(f"Bed timer triggered")
        self.apps["climate"].reset()
        self.call_service("lock/lock", entity_id="lock.door_lock")

    def is_bed_time(self) -> bool:
        """Return if the time is after bed time (and before midnight)."""
        return self.time() > self.parse_time(self.get_setting("bed_time"))

    def __button(self, event_name: str, data: dict, kwargs: dict):
        """Detect and handle when a button is clicked or held."""
        del event_name, kwargs
        room = data["entity_id"].replace("zwave.", "").replace("_button", "")
        if data["scene_data"] == 0:  # clicked
            self.__button_clicked(room)
        elif data["scene_data"] == 2:  # held
            self.log(f"Button in '{room}' held")
            self.scene = "Bright" if self.scene not in ("Sleep", "Bright") else "Night"

    def __button_clicked(self, room: str):
        """Handle a button click."""
        self.log(f"Button in '{room}' clicked")
        if room == "kitchen":
            if self.scene == "Night":
                if self.apps["lights"].is_lighting_sufficient():
                    self.scene = "Day"
                elif self.apps["media"].is_playing:
                    self.scene = "TV"
                else:
                    self.scene = "Bright"
            else:
                self.scene = "Night"
        elif room == "bedroom":
            if self.scene == "Night":
                self.apps["lights"].alternative_scene = True
                self.scene = "Sleep"
            elif self.scene == "Sleep":
                if self.apps["lights"].lights["bedroom"].brightness == 0:
                    self.scene = "Night"
                else:
                    self.apps["lights"].lights["bedroom"].turn_off()
                    self.log("Bedroom light turned off")
            else:
                adjusting = (
                    self.apps["lights"]
                    .lights["bedroom"]
                    .toggle_presence_adjustments(
                        occupied=(
                            self.apps["lights"].args["max_brightness"],
                            self.apps["lights"].args["max_kelvin"],
                        ),
                        vacating_delay=self.get_setting("bedroom_vacating_delay"),
                    )
                )
                self.log(f"Bedroom light is now adjusting with presence: {adjusting}")

    def __ifttt(self, event_name: str, data: dict, kwargs: dict):
        """Handle commands coming in via IFTTT."""
        del event_name, kwargs
        self.log(f"Received {data} from IFTTT: ")
        if "scene" in data:
            self.scene = data["scene"]
        elif "climate_control" in data:
            self.apps["climate"].climate_control = data["climate_control"]
        elif "aircon" in data:
            self.apps["climate"].aircon = data["aircon"]

    def get_setting(self, setting_name: str) -> int:
        """Get UI input_number setting values."""
        if setting_name.endswith("_time"):
            return self.get_state(f"input_datetime.{setting_name}")
        return int(float(self.get_state(f"input_number.{setting_name}")))

    def __delay_handle_settings_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Delay handling of setting changes made by the user through the UI."""
        del attribute, kwargs
        if entity in self.__timers["setting_delay_timers"]:
            self.cancel_timer(self.__timers["setting_delay_timers"][entity])
            del self.__timers["setting_delay_timers"][entity]
        self.__timers["setting_delay_timers"][entity] = self.run_in(
            self.__handle_settings_change,
            self.args["settings_change_delay"],
            entity=entity,
            new=new,
            old=old,
        )

    def __handle_settings_change(self, kwargs: dict):
        """Act on setting changes made by the user through the UI."""
        (input_type, setting) = kwargs["entity"].split(".")
        self.log(f"UI setting '{setting}' changed to {kwargs['new']}")
        if setting == "scene":
            self.apps["lights"].transition_to_scene(kwargs["new"])
            self.apps["climate"].transition_between_scenes(kwargs["new"], kwargs["old"])
            if kwargs["new"] == "Sleep" or "Away" in kwargs["new"]:
                self.call_service("lock/lock", entity_id="lock.door_lock")
                self.apps["media"].standby()
        elif input_type == "input_boolean":
            setattr(self.apps["climate"], setting, kwargs["new"] == "on")
        elif setting.startswith("circadian"):
            try:
                self.apps["lights"].redate_circadian(None)
            except ValueError:
                self.__revert_setting(kwargs["entity"], kwargs["old"])
        elif setting.endswith("_time"):
            if self.__are_time_settings_valid():
                self.__set_timer(setting)
            else:
                self.__revert_setting(kwargs["entity"], kwargs["old"])
        elif "temperature" in setting:
            self.apps["climate"].reset()
        else:
            self.apps["lights"].transition_to_scene(self.scene)

    def __revert_setting(self, setting_id: str, value: str):
        """Revert setting to specified value & notify."""
        if setting_id.startswith("input_datetime"):
            self.call_service(
                "input_datetime/set_datetime", entity_id=setting_id, time=value,
            )
        else:
            self.call_service(
                "input_number/set_value", entity_id=setting_id, value=value,
            )
        self.notify(
            f"Invalid value for setting '{setting_id}' - reverted to previous",
            title="Invalid Setting",
        )

    def __handle_battery_level_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Notify if a device's battery is low."""
        del attribute, old, kwargs
        if "protect" in entity:
            if new != "Ok":
                self.notify(f"{entity} is low", title="Low Battery", targets="dan")
        elif float(new) <= 20:
            self.notify(f"{entity} is low ({new}%)", title="Low Battery", targets="dan")

    def __heartbeat(self, kwargs: dict):
        """Send a heartbeat then handle if it is received or not."""
        del kwargs
        try:
            urllib.request.urlopen(
                self.args["heartbeat_url"], timeout=self.args["heartbeat_timeout"],
            )
        except socket.error:
            self.__timers["heartbeat_fail_count"] += 1
            if (
                self.__online
                and self.__timers["heartbeat_fail_count"]
                >= self.args["heartbeat_max_fail_count"]
            ):
                self.__online = False
                self.error("Heartbeat timed out")
        else:
            if not self.__online:
                self.__online = True
                self.__timers["heartbeat_fail_count"] = 0
                self.log("Heartbeat sent and received")

    def __handle_log(
        self,
        app_name: str,
        timestamp: datetime.datetime,
        level: str,
        log_type: str,
        message: str,
        kwargs: dict,
    ):  # pylint: disable=too-many-arguments
        """Increment counters if logged message is a WARNING or ERROR."""
        del app_name, timestamp, kwargs
        if log_type == "error_log":
            level = "ERROR" if message.startswith("Traceback") else None
        elif log_type == "main_log" and message.endswith("errors.log"):
            level = None
        if level in ("WARNING", "ERROR"):
            self.call_service(
                "counter/increment", entity_id=f"counter.{level.lower()}s"
            )
