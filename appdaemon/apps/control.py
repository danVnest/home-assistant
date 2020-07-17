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

    def initialize(self):
        """Monitor logs then wait until after all other apps are initialised.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.call_service("counter/reset", entity_id="counter.warnings")
        self.call_service("counter/reset", entity_id="counter.errors")
        self.listen_log(self.handle_log)
        self.listen_event(self.everything_initialized, "appd_started")

    def everything_initialized(self, event_name: str, data: dict, kwargs: dict):
        """Link all other apps, set scene, listen for user input and monitor batteries."""
        del event_name, data, kwargs
        for app_name in ["climate", "lights", "media", "presence", "safety"]:
            setattr(self, app_name, self.get_app(app_name.title()))
        self.reset_scene()
        self.run_daily(self.morning, self.args["morning_time"])
        self.listen_event(self.button, "zwave.scene_activated")
        self.listen_event(self.ifttt, "ifttt_webhook_received")
        for battery in [
            "entryway_protect_battery_health_state",
            "living_room_protect_battery_health_state",
            "garage_protect_battery_health_state",
            "entryway_multisensor_battery_level",
            "kitchen_multisensor_battery_level",
            "switch1_battery_level",
            "switch2_battery_level",
        ]:
            self.listen_state(self.handle_battery_level_change, f"sensor.{battery}")

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
            self.parse_time(self.args["morning_time"])
            < self.time()
            < self.parse_time("12:00:00")
        ):
            self.scene = "Morning"
        elif self.scene == "Sleep":
            self.log("It is night but scene was set as 'Sleep', will not be reset")
        else:
            self.scene = "Night"

    def morning(self, kwargs: dict):
        """Change scene to Morning (callback for daily timer)."""
        del kwargs
        self.log(f"Morning timer triggered")
        if self.scene == "Sleep":
            self.scene = "Morning"
        else:
            self.log(f"Scene was not changed as it was {self.scene}, not Morning.")

    def button(self, event_name: str, data: dict, kwargs: dict):
        """Detect and handle when a button is clicked or held."""
        del event_name, kwargs
        room = "bedroom" if data["entity_id"] == "zwave.switch1" else "kitchen"
        if data["scene_data"] == 0:  # clicked
            self.log(f"Button in '{room}' clicked")
            if self.lights.lights[room].brightness == 0:
                brightness, kelvin = self.lights.calculate_circadian_brightness_kelvin()
                self.lights.lights[room].adjust(brightness, kelvin)
            else:
                self.lights.lights[room].brightness = 0
        elif data["scene_data"] == 2:  # held
            self.log(f"Button '{data['entity_id']}' held")
            if room == "bedroom":
                self.scene = "Sleep" if self.scene == "Night" else "Night"
            else:
                self.scene = "Night" if self.scene in ("Bright", "TV") else "Bright"

    def ifttt(self, event_name: str, data: dict, kwargs: dict):
        """Handle commands coming in via IFTTT."""
        del event_name, kwargs
        self.log(f"Received {data} from IFTTT: ")
        if "scene" in data:
            self.scene = data["scene"]
        elif "climate_control" in data:
            self.climate.climate_control = data["climate_control"]
        elif "aircon" in data:
            self.climate.aircon = data["aircon"]

    def handle_battery_level_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Notify if a device's battery is low."""
        del attribute, old, kwargs
        if "protect" in entity:
            if new != "Ok":
                self.notify(f"{entity} is low", title="Low Battery", targets="dan")
        elif float(new) <= 20:
            self.notify(f"{entity} is low ({new}%)", title="Low Battery", targets="dan")

    def handle_log(
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
