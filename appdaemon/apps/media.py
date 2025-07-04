"""Controls & monitors media devices.

Loads the TV app launcher on startup, and monitors to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import logging
import subprocess
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
        self.play_state: Entity = self.get_entity("sensor.tv_state")
        self.last_valid_play_state = None

    def initialize(self):
        """Start listening to TV states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(self.handle_state_change, self.device.entity_id)
        self.listen_state(
            self.handle_state_change,
            self.play_state.entity_id,
            duration=self.constants["state_change_delay"],
        )
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115382/f
        # try without state_change_delay, I'd previously removed it!
        for muted in [True, False]:
            self.listen_state(
                self.handle_state_change,
                self.device.entity_id,
                attribute="is_volume_muted",
                old=not muted,
                new=muted,
                duration=self.constants["state_change_delay"],
            )

    @property
    def on(self) -> bool:
        """Check if the TV is currently on or not."""
        return self.device.state == "on"

    @property
    def playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        if not self.on:
            return False
        if self.device.attributes.source == "PC" and self.pc_on:
            return True
        return (
            self.play_state.state == "playing"
            if self.play_state.state != "unavailable"
            else self.last_valid_play_state == "playing"
        )

    @property
    def muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.device.attributes.is_volume_muted is True

    @property
    def pc_on(self) -> bool:
        """Check if the PC is currently on or not."""
        return subprocess.call(["ping", "-c", "1", self.constants["pc_ip"]]) == 0  # noqa: S603, S607

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

    def load_app_launcher_and_state_reporter(self):
        """Start the LG TV app launcher app & media state reporting service."""
        del kwargs
        if not self.on:
            self.log(
                "TV was turned off before state sensor setup completed",
                level="DEBUG",
            )
            return
        self.log(
            "Loading app launcher & media state reporting service on TV",
            level="DEBUG",
        )
        self.device.call_service(
            "select_source",
            source="App Launcher & Media State Reporter",
        )

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
        if entity == self.play_state.entity_id:
            if new not in ("unavailable", "unknown"):
                self.last_valid_play_state = new
            else:
                self.log(
                    "TV state sensor unavailable, using last valid state",
                    level="DEBUG",
                )
        elif attribute == "state" and new == "on":
            self.load_app_launcher_and_state_reporter()
        if self.on and self.playing and not self.muted:
            if self.control.scene == "Night":
                self.control.scene = "TV"
        elif self.control.scene == "TV":
            self.control.scene = (
                "Night"
                if self.entities.binary_sensor.dark_outside.state == "on"
                else "Day"
            )
