"""Coordinates all home automation control.

Schedules scene changes, reacts to user input from buttons, monitors presence and logs.

User defined variables are configued in control.yaml
"""
import datetime

import app


class Control(app.App):
    """Controls the scene based on scheduled events, people's presence and input."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.last_device_date = None
        self.climate = None
        self.lights = None
        self.media = None
        self.safety = None

    def initialize(self):
        """Schedule events, listen for presence changes, user input and log messages.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for app_name in ["climate", "lights", "media", "safety"]:
            setattr(self, app_name, self.get_app(app_name.title()))
        self.listen_log(self.handle_log)
        self.reset_scene()
        self.last_device_date = self.date()
        self.run_at_sunrise(self.morning, offset=self.args["sunrise_offset"] * 60)
        self.run_at_sunset(self.evening, offset=-self.args["sunset_offset"] * 60)
        self.listen_event(self.button, "zwave.scene_activated")
        self.listen_event(self.ifttt, "ifttt_webhook_received")
        self.listen_event(self.handle_new_device, "device_tracker_new_device")
        self.listen_state(self.handle_presence_change, "person")

    def reset_scene(self):
        """Set scene based on who's home, time, stored scene, etc."""
        if not self.anyone_home():
            scene = f"Away ({'Day' if self.time() < self.evening_time() else 'Night'})"
        elif (
            not self.morning_time()
            < self.time()
            < datetime.time(self.args["sleeptime"])
        ):
            scene = "Sleep"
        elif self.time() < self.evening_time():
            scene = "Day"
        elif self.get_state("media_player.living_room") == "playing":
            scene = "TV"
        else:
            scene = "Night"
        if scene != self.scene:
            self.log("Detecting scene to be reset to")
            self.scene = scene

    def morning(self, kwargs: dict):
        """Change scene to day (callback for run_at_sunrise with offset)."""
        del kwargs
        self.log("Morning triggered")
        self.scene = "Day" if self.anyone_home() else "Away (Day)"

    def evening(self, kwargs: dict):
        """Set scene to Night or TV (callback for run_at_sunset with offset)."""
        del kwargs
        self.log("Evening triggered")
        self.scene = (
            "TV" if self.get_state("media_player.living_room") == "playing" else "Night"
        )

    def button(self, event_name: str, data: dict, kwargs: dict):
        """Detect and handle when a button is clicked or held."""
        del event_name, kwargs
        if data["scene_data"] == 0:  # clicked
            self.log(f"Button '{data['entity_id']}' clicked")
            if data["entity_id"] == "zwave.switch1":
                self.toggle("light.bedroom")
            else:
                self.toggle("light.kitchen")
        elif data["scene_data"] == 2:  # held
            self.log(f"Button '{data['entity_id']}' held")
            self.scene = "Sleep" if self.scene == "Night" else "Night"

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

    def handle_new_device(self, event_name: str, data: dict, kwargs: dict):
        """If not home and someone adds a device, notify."""
        del event_name
        self.log(f"New device added: {data}, {kwargs}")
        if "Away" in self.scene:
            if self.last_device_date < self.date() - datetime.timedelta(hours=3):
                self.notify(
                    f'A guest has added a device: "{data["host_name"]}"',
                    title="Guest Device",
                )
                self.last_device_date = self.date()

    def handle_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Change scene if everyone has left home or if someone has come back."""
        del attribute, old, kwargs
        self.log(f"{entity} is {new}")
        if new == "home":
            if "Away" in self.scene:
                self.reset_scene()
        elif (
            "Away" not in self.scene
            and self.get_state(
                f"person.{'rachel' if entity.endswith('dan') else 'dan'}"
            )
            != "home"
        ):
            self.scene = (
                f"Away ({'Day' if self.time() < self.evening_time() else 'Night'})"
            )

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
