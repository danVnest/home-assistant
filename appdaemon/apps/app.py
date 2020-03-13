"""AppDaemon subclass from which all apps inherit API and common functionality from.

This class should be the inherited class of every app.
It inherits methods for interaction with Home Assistant, and includes several useful
utility functions used by multiple or all apps.
"""
from appdaemon.plugins.hass.hassapi import Hass


class App(Hass):
    """Utility functions and methods for Home Assistant interaction."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Start listening to events and monitoring logs."""
        for app in self.args["dependencies"]:
            if getattr(self, app, None) is None:
                setattr(self, app, self.get_app(app))

    def notify(self, message: str, **kwargs):
        """Send a notification to users and log the message."""
        super().notify(message, **kwargs)
        self.log(f"NOTIFICATION: {message}")
