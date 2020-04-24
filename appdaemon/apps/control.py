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
        self.listen_event(self.button, "zwave.scene_activated")
        self.listen_event(self.ifttt, "ifttt_webhook_received")
        self.listen_event(self.handle_new_device, "device_tracker_new_device")
        self.listen_state(self.handle_presence_change, "person")
        self.listen_state(
            self.handle_light_change, "sensor.kitchen_multisensor_luminance"
        )
        for battery in [
            "entryway_protect_battery_health_state",
            "living_room_protect_battery_health_state",
            "garage_protect_battery_health_state",
            "kitchen_multisensor_battery_level",
            "multisensor_battery_level",
            "switch1_battery_level",
            "switch2_battery_level",
        ]:
            self.listen_state(self.handle_battery_level_change, f"sensor.{battery}")

    def reset_scene(self):
        """Set scene based on who's home, time, stored scene, etc."""
        self.log("Detecting current appropriate scene")
        if self.scene == "Bright":
            self.log("Scene was set as 'Bright', will not be reset")
        elif not self.anyone_home():
            self.scene = (
                f"Away ({'Day' if self.is_ambient_light_sufficient() else 'Night'})"
            )
        elif self.is_ambient_light_sufficient():
            self.scene = "Day"
        elif self.get_state("media_player.living_room") == "playing":
            self.scene = "TV"
        elif self.scene == "Sleep":
            self.log("It is night but scene was set as 'Sleep', will not be reset")
        else:
            self.scene = "Night"

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
                f"Away ({'Day' if self.is_ambient_light_sufficient() else 'Night'})"
            )

    def handle_light_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Change scene to day or night based on luminance levels."""
        del entity, attribute, old, kwargs
        if "Day" in self.scene:
            if float(new) <= self.args["day_min_luminance"]:
                self.log(f"Light levels are low ({new}%) transitioning to night scene")
                if self.get_state("media_player.living_room") == "playing":
                    self.scene = "TV"
                elif self.anyone_home():
                    self.scene = "Night"
                else:
                    self.scene = "Away (Night)"
        elif self.scene != "Bright":
            if float(new) >= self.args["night_max_luminance"]:
                self.log(f"Light levels are high ({new}%), transitioning to day scene")
                self.scene = "Day" if self.anyone_home() else "Away (Day)"

    def is_ambient_light_sufficient(self) -> bool:
        """Return if there is enough ambient light to not require further lighting."""
        return (
            float(self.get_state("sensor.kitchen_multisensor_luminance"))
            >= self.control.args["night_max_luminance"]
        )

    def handle_battery_level_change(
        self, entity: str, attribute: str, old: bool, new: bool, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Notify if a device's battery is low."""
        del attribute, old, kwargs
        if "protect" in entity:
            if new != "Ok":
                low_message = f"{entity} is low"
        elif float(new) < 20:
            low_message = f"{entity} is low ({new}%)"
        if low_message is not None:
            self.notify(low_message, title="Low Battery", targets="dan")

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
