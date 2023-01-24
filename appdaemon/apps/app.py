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
        """Scene is stored in the control app and synced with Home Assistant."""
        return self.control.scene

    @scene.setter
    def scene(self, new_scene: str):
        """Use the control app to change the scene and sync with Home Assistant."""
        self.control.scene = new_scene

    def cancel_timer(self, handle):
        """Cancel timer after checking it is valid and running."""
        if self.timer_running(handle):
            super().cancel_timer(handle)

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
                    mobile_name = "mobile_app_daniels_phone"
                    data = {"apns_headers": {"apns-collapse-id": kwargs["title"]}}
                else:
                    mobile_name = "mobile_app_rachel_s_phone"
                    data = {"tag": kwargs["title"]}
                super().notify(
                    message,
                    title=kwargs["title"],
                    name=mobile_name,
                    data=data,
                )
                self.log(f"Notified {targets}: {kwargs['title']}: {message}")
