"""Coordinates all home automation control.

Schedules scene changes, reacts to user input, reports errors & low batteries.

User defined variables are configued in control.yaml
"""
import datetime

import app


class Control(app.App):
    """Controls the scene based on scheduled events, people's presence and input."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.climate = None
        self.lights = None
        self.media = None
        self.presence = None
        self.safety = None
        self.__morning_timer = None

    def initialize(self):
        """Monitor logs then wait until after all other apps are initialised.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.call_service("counter/reset", entity_id="counter.warnings")
        self.call_service("counter/reset", entity_id="counter.errors")
        self.listen_log(self.__handle_log)
        self.listen_event(self.__add_app, "app_initialized", namespace="admin")

    def __add_app(self, event_name: str, data: dict, kwargs: dict):
        """Link the app and check if all are now ready."""
        del event_name, kwargs
        if data["app"] == "Control":
            for app_name in ["Climate", "Lights", "Media", "Presence", "Safety"]:
                setattr(self, app_name.lower(), self.get_app(app_name))
        else:
            if getattr(self, data["app"].lower()) != self.get_app(data["app"]):
                setattr(self, data["app"].lower(), self.get_app(data["app"]))
            else:
                return
        if all([self.climate, self.lights, self.media, self.presence, self.safety]):
            self.__final_initialize()

    def __final_initialize(self):
        """Set scene, listen for user input and monitor batteries."""
        for setting in [
            "input_boolean",
            "input_datetime",
            "input_number",
            "input_select",
        ]:
            self.listen_state(
                self.__handle_settings_change,
                setting,
                duration=self.args["settings_change_delay"],
            )
        self.reset_scene()
        self.__morning_timer = self.run_daily(
            self.__morning, self.get_setting("morning_time")
        )
        self.listen_event(self.__button, "zwave.scene_activated")
        self.listen_event(self.__ifttt, "ifttt_webhook_received")
        for battery in [
            "entryway_protect_battery_health_state",
            "living_room_protect_battery_health_state",
            "garage_protect_battery_health_state",
            "entryway_multisensor_battery_level",
            "kitchen_multisensor_battery_level",
            "switch1_battery_level",
            "switch2_battery_level",
        ]:
            self.listen_state(self.__handle_battery_level_change, f"sensor.{battery}")
        self.log("App 'Control' initialised and linked to all other apps")

    def reset_scene(self):
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if self.scene == "Bright":
            self.log("Scene was set as 'Bright', will not be reset")
        elif not self.presence.anyone_home():
            self.scene = (
                f"Away ({'Day' if self.lights.is_lighting_sufficient() else 'Night'})"
            )
        elif self.lights.is_lighting_sufficient():
            self.scene = "Day"
        elif self.media.is_playing:
            self.scene = "TV"
        elif (
            self.parse_time(self.get_setting("morning_time"))
            < self.time()
            < self.parse_time("12:00:00")
        ):
            self.scene = "Morning"
        elif self.scene == "Sleep":
            self.log("It is night but scene was set as 'Sleep', will not be reset")
        else:
            self.scene = "Night"

    def __morning(self, kwargs: dict):
        """Change scene to Morning (callback for daily timer)."""
        del kwargs
        self.log(f"Morning timer triggered")
        if self.scene == "Sleep":
            self.scene = "Morning"
        else:
            self.log(f"Scene was not changed as it was {self.scene}, not Morning.")

    def __button(self, event_name: str, data: dict, kwargs: dict):
        """Detect and handle when a button is clicked or held."""
        del event_name, kwargs
        room = "bedroom" if data["entity_id"] == "zwave.switch1" else "kitchen"
        if data["scene_data"] == 0:  # clicked
            self.log(f"Button in '{room}' clicked")
            if self.lights.lights[room].brightness == 0:
                brightness, kelvin = self.lights.calculate_circadian_brightness_kelvin()
                self.lights.lights[room].adjust(brightness, kelvin)
            else:
                self.lights.lights[room].turn_off()
        elif data["scene_data"] == 2:  # held
            self.log(f"Button '{data['entity_id']}' held")
            if room == "bedroom":
                self.scene = "Sleep" if self.scene == "Night" else "Night"
            else:
                self.scene = "Night" if self.scene in ("Bright", "TV") else "Bright"

    def __ifttt(self, event_name: str, data: dict, kwargs: dict):
        """Handle commands coming in via IFTTT."""
        del event_name, kwargs
        self.log(f"Received {data} from IFTTT: ")
        if "scene" in data:
            self.scene = data["scene"]
        elif "climate_control" in data:
            self.climate.climate_control = data["climate_control"]
        elif "aircon" in data:
            self.climate.aircon = data["aircon"]

    def get_setting(self, setting_name: str) -> int:
        """Get UI input_number setting values."""
        if setting_name == "morning_time":
            return self.get_state(f"input_datetime.{setting_name}")
        return int(float(self.get_state(f"input_number.{setting_name}")))

    def __handle_settings_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Act on setting changes made by the user through the UI."""
        del attribute, kwargs
        if new == old:
            return
        entity = entity.split(".")
        input_type = entity[0]
        setting = entity[1]
        valid = True
        self.log(f"UI setting '{setting}' changed to {new}")
        if setting == "scene":
            self.lights.transition_to_scene(new)
            self.climate.transition_between_scenes(new, old)
        elif input_type == "input_boolean":
            setattr(self.climate, setting, new == "on")
        elif setting.startswith("circadian"):
            valid = self.lights.redate_circadian(None)
        elif setting == "morning_time":
            valid = self.parse_datetime(
                self.get_state(entity)
            ) < datetime.datetime.combine(self.date(), self.sunrise().time())
            if valid:
                self.cancel_timer(self.__morning_timer)
                self.__morning_timer = self.run_daily(self.__morning, new)
        elif "temperature" in setting:
            self.climate.reset()
        else:
            self.lights.transition_to_scene(self.scene)
        if not valid:
            if input_type == "input_datetime":
                self.call_service(
                    "input_datetime/set_datetime", entity_id=entity, time=old
                )
            else:
                self.call_service("input_number/set_value", entity_id=entity, value=old)
            self.notify(
                f"Reverted invalid change of '{new}' for setting '{setting}'",
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
