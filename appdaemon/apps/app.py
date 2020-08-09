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
        self.control = None
        self.constants = {}

    def initialize(self):
        """Allow easy access to control app (which has access to all other apps)."""
        self.control = self.get_app("Control")

    @property
    def scene(self) -> str:
        """Scene is stored in Home Assistant as an input_select entity."""
        return self.entities.input_select.scene.state

    @scene.setter
    def scene(self, new_scene: str):
        """Call the input_select/select_option service to set the scene."""
        self.log(f"Setting scene to '{new_scene}' (transitioning from '{self.scene}')")
        self.call_service(
            "input_select/select_option",
            entity_id="input_select.scene",
            option=new_scene,
        )

    def notify(self, message: str, **kwargs):
        """Send a notification (title required) to target users (anyone_home or all)."""
        targets = kwargs["targets"] if "targets" in kwargs else "all"
        for person, info in self.entities.person.items():
            if any(
                [
                    targets == "all",
                    targets == "anyone_home" and info["state"] == "home",
                    targets == person,
                ]
            ):
                if person == "dan":
                    mobile_name = "mobile_app_dans_phone"
                    data = {"apns_headers": {"apns-collapse-id": kwargs["title"]}}
                else:
                    mobile_name = "mobile_app_rachel_s_phone"
                    data = {"tag": kwargs["title"]}
                super().notify(
                    message, title=kwargs["title"], name=mobile_name, data=data,
                )
                self.log(f"Notified {targets}: {kwargs['title']}: {message}")
