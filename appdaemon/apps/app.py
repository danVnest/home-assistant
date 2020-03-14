"""AppDaemon subclass from which all apps inherit API and common functionality from.

This class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps.
"""
import appdaemon.plugins.hass.hassapi as hass


class App(hass.Hass):
    """Utility functions and methods for Home Assistant interaction."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Start listening to events and monitoring logs."""
        if "dependencies" in self.args:
            for app in self.args["dependencies"]:
                if getattr(self, app, None) is None:
                    setattr(self, app, self.get_app(app))

    @property
    def scene(self) -> str:
        """Scene is stored in Home Assistant as a sensor."""
        return self.get_state("input_select.scene")

    @scene.setter
    def scene(self, new_scene: str):
        """"""
        self.log(f"Setting scene to '{new_scene}' (transitioning from '{self.scene}')")
        self.call_service(
            "input_select/select_option",
            entity_id="input_select.scene",
            option=new_scene,
        )

    def notify(self, message: str, **kwargs):
        """Send a notification to users and log the message."""
        super().notify(message, **kwargs)
        self.log(f"NOTIFICATION: {message}")

    def anyone_home(self, **kwargs) -> bool:
        """Check if anyone is home."""
        return any(
            person["state"] == "home" for person in self.get_state("person").values()
        )
