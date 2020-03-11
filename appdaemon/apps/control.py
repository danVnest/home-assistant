"""Coordinates all home automation control.

Reacts to user input from voice commands, buttons, app interaction, etc.
Coordinates all other apps by listening to and firing events.

User defined variables are configued in control.yaml
"""
import datetime

import appdaemon.plugins.hass.hassapi as hass
import yaml


class Control(hass.Hass):
    """Coordinate all home automation based on user input and automated rules."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        self.warning_count = 0
        self.error_count = 0
        self.error_notifier_timer = None
        self.next_notify_error_datetime = None
        self.states = None
        self.scene = None
        self.last_device_date = None
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Start listening to events and monitoring logs.

        Appdaemon defined init function called once ready after __init__.
        """
        self.next_notify_error_datetime = self.datetime()
        self.listen_log(self.handle_log)
        self.states = self.get_saved_states()
        self.scene = self.states["scene"]
        self.last_device_date = self.date()
        self.run_at_sunrise(self.morning, offset=self.args["sunrise_offset"] * 60)
        self.run_at_sunset(self.evening, offset=-self.args["sunset_offset"] * 60)
        self.listen_event(self.voice, "SIRI")
        self.listen_event(self.ifttt, "ifttt_webhook_received")
        self.listen_event(self.button, "zwave.scene_activated")
        self.listen_event(self.apple_tv, "TV")
        self.listen_event(self.everything_initialized, "appd_started")
        self.listen_event(self.handle_new_device, "device_tracker_new_device")
        self.listen_state(self.handle_presence_change, "person")

    def everything_initialized(self, event_name: str, data: dict, kwargs: dict):
        """Configure all other apps with the current scene and other states.

        Appdaemon defined function called once all apps have been initialised.
        """
        del event_name, data, kwargs
        self.log("Initializing all apps")
        self.set_scene(self.detect_scene())
        self.fire_event("SCENE", scene=self.scene)
        if self.states["climate_control_enabled"]:
            self.fire_event("CLIMATE", command="enabled")
        else:
            self.fire_event("CLIMATE", command="disabled")

    def get_saved_states(self) -> list:
        """Get saved states from file."""
        states = {}
        with open(self.args["path_to_saved_states"]) as file:
            states = yaml.load(file, yaml.SafeLoader)
        return states

    def save_state(self, state: str, value):
        """Save state to states file (if changed)."""
        if self.states[state] != value:
            self.states[state] = value
            with open(self.args["path_to_saved_states"], "w") as file:
                yaml.dump(self.states, file)

    def detect_scene(self) -> str:
        """Detect and return scene based on who's home, time, stored scene, etc."""
        self.log("Detecting scene")
        if all(
            person["state"] != "home" for person in self.get_state("person").values()
        ):
            return "away_day" if self.time() < self.evening_time() else "away_night"
        if (
            self.scene == "sleep"
            and self.time()
            < (
                self.sunrise() + datetime.timedelta(minutes=self.args["sunrise_offset"])
            ).time()
        ):
            return "sleep"
        if self.time() < self.evening_time():
            return "day"
        if self.get_state("media_player.living_room") == "playing":
            return "tv"
        return "night"

    def set_scene(self, scene: str):
        """Set the scene by saving the state and notifying other apps (if changed)."""
        if self.scene != scene:
            self.log(f"Setting scene to '{scene}' (transitioning from '{self.scene}')")
            self.scene = scene
            self.save_state("scene", self.scene)
        else:
            self.log(f"Scene '{self.scene}' already set, setting again anyway")
        self.fire_event("SCENE", scene=self.scene)

    def morning(self, kwargs: dict):
        """Change scene to day (callback for run_at_sunrise with offset)."""
        del kwargs
        self.log("Morning triggered")
        self.set_scene(
            "day" if "home" in self.get_state("person").values() else "away_day"
        )

    def evening(self, kwargs: dict):
        """Detect new scene and set it (callback for run_at_sunset with offset)."""
        del kwargs
        self.log("Evening triggered")
        self.set_scene(self.detect_scene())

    def evening_time(self) -> datetime.time:
        """Return the time that day becomes night (as configured relative to sunset)."""
        return (
            self.sunset() - datetime.timedelta(minutes=self.args["sunset_offset"])
        ).time()

    def voice(self, event_name: str, data: dict, kwargs: dict):
        """Handle voice commands from a user."""
        del event_name, kwargs
        self.log(f"Voice command for '{data['type']}' received")
        if data["type"] == "sleep_time":
            self.set_scene("sleep")
        elif data["type"] == "lights_on":
            self.set_scene("night")
        elif data["type"] == "lights_off":
            self.set_scene("day")
        elif data["type"] == "lights_on_bright":
            self.set_scene("bright")
        elif data["type"] == "climate_control_enabled":
            self.save_state("climate_control_enabled", True)
            self.fire_event("CLIMATE", command="enabled")
        elif data["type"] == "climate_control_disabled":
            self.save_state("climate_control_enabled", False)
            self.fire_event("CLIMATE", command="disabled")
        elif data["type"] == "climate_control_on":
            self.fire_event("CLIMATE", command="on")
        elif data["type"] == "climate_control_off":
            self.save_state("climate_control_enabled", False)
            self.fire_event("CLIMATE", command="off")

    def ifttt(self, event_name, data, kwargs):
        """Handle voice commands from a user."""
        if data["type"] == "ok_google":
            self.voice(event_name, {"type": data["command"]}, kwargs)

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
            if self.scene == "night" or self.scene == "tv":
                self.set_scene("sleep")
            else:
                self.set_scene("night")

    def apple_tv(self, event_name: str, data: dict, kwargs: dict):
        """Catch TV playing event at night and change the scene."""
        del event_name, kwargs
        if data["state"] == "playing" and self.scene == "night":
            self.set_scene("tv")
        elif self.scene == "tv":
            self.set_scene("night")

    def handle_new_device(self, event_name: str, data: dict, kwargs: dict):
        """If not home and someone adds a device, notify."""
        del event_name
        self.log(f"New device added: {data}, {kwargs}")
        if self.scene.startswith("away"):
            if self.last_device_date < self.date() - datetime.timedelta(hours=3):
                self.notify(f'A guest has added a device: "{data["host_name"]}"')
                self.last_device_date = self.date()

    def handle_presence_change(
        self, entity: str, attribute: str, old: int, new: int, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Change scene if everyone has left home or if someone has come back."""
        del attribute, old, kwargs
        self.log(f"{entity} is {new}", level="DEBUG")
        if new == "home":
            if self.scene.startswith("away"):
                self.set_scene(self.detect_scene())
        elif (
            self.scene.startswith("away") is False
            and self.get_state(
                f"person.{'rachel' if entity.endswith('dan') else 'dan'}"
            )
            != "home"
        ):
            self.set_scene(
                "away_day" if self.time() < self.evening_time() else "away_night"
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
        """Check if logged message is a WARNING/ERROR, and notify users if appropriate."""
        del app_name, timestamp, kwargs
        new_warning = False
        new_error = False
        if log_type == "error_log":
            if message.startswith("Traceback"):
                new_error = True
        else:
            if level == "WARNING":
                new_warning = True
            elif level == "ERROR":
                new_error = True
        if new_warning or new_error:
            self.warning_count += int(new_warning)
            self.error_count += int(new_error)
            if (
                self.datetime() > self.next_notify_error_datetime
                and self.error_notifier_timer is None
            ):
                self.error_notifier_timer = self.run_in(self.notify_error, 5)

    def notify(self, message: str, **kwargs):
        """Send a notification to users and log the message."""
        super().notify(message, **kwargs)
        self.log(f"NOTIFICATION: {message}")

    def notify_error(self, kwargs: dict):
        """Notify users of errors in a structured and paced way."""
        del kwargs
        title = "Issues Encountered"
        message = (
            f"There have been {self.error_count} errors and {self.warning_count}"
            " warnings logged since the previous issue notification"
        )
        self.call_service(
            "persistent_notification/create", title=title, message=message
        )
        self.next_notify_error_datetime = self.datetime() + datetime.timedelta(
            minutes=self.args["notify_error_delay"]
        )
        self.error_notifier_timer = None
        self.warning_count = 0
        self.error_count = 0
