"""Controls & monitors media devices.

Loads the TV app launcher on startup, and monitors to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import logging
from typing import TYPE_CHECKING

from app import App

if TYPE_CHECKING:
    from appdaemon.entity import Entity


class Media(App):
    """Listen for TV state changes to load the app launcher & set the scene."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.device: Entity = self.get_entity("media_player.tv")

    def initialize(self):
        """Start listening to TV states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(self.handle_state_change, self.device.entity_id)
        for state in [True, False]:
            self.listen_state(
                self.handle_state_change,
                "binary_sensor.tv_playing",
                old="off" if state else "on",
                new="on" if state else "off",
            )
            self.listen_state(
                self.handle_state_change,
                self.device.entity_id,
                attribute="is_volume_muted",
                old=not state,
                new=state,
            )

    @property
    def on(self) -> bool:
        """Check if the TV is currently on or not."""
        return self.device.state == "on"

    @property
    def playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        return self.entities.binary_sensor.tv_playing.state == "on"

    @property
    def muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.device.attributes.get("is_volume_muted") is True

    def turn_off(self):
        """Turn the TV off."""
        self.device.turn_off()
        self.log("TV is now off", level="DEBUG")

    def turn_on(self):
        """Turn the TV on."""
        self.device.turn_on()
        self.log("TV is now on", level="DEBUG")

    def pause(self):
        """Pause media being played on the TV."""
        self.device.call_service("media_pause")
        self.log("TV media is now paused", level="DEBUG")

    def handle_state_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """Handle TV events to adjust the scene and load appropriately on startup."""
        del kwargs
        if self.logger.isEnabledFor(logging.DEBUG):
            self.log(
                f"TV changed from '{old}' to '{new}' ('{entity}' - '{attribute}')",
                level="DEBUG",
            )
        if (
            entity == self.device.entity_id
            and attribute == "state"
            and new == "on"
            and self.entities.sensor.tv_state.state == "unavailable"
        ):
            self.device.call_service(
                "select_source",
                source="App Launcher & Media State Reporter",
            )
        if self.on and self.playing and not self.muted:
            if self.control.scene == "Night":
                self.control.scene = "TV"
        elif self.control.scene == "TV":
            self.control.scene = (
                "Night"
                if self.entities.binary_sensor.dark_outside.state == "on"
                else "Day"
            )
