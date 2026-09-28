"""Controls & monitors media devices.

Loads the TV app launcher on startup, and monitors to change the scene appropriately.

User defined variables are configued in media.yaml
"""

from typing import TYPE_CHECKING, Any, override

from app import App

if TYPE_CHECKING:
    from appdaemon.entity import Entity


class Media(App):
    """Listen for TV state changes to load the app launcher & set the scene."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.device: Entity = self.get_entity("media_player.tv")

    @override
    def initialize(self) -> None:
        """Start listening to TV states."""
        super().initialize()
        self.listen_state(self.handle_state_change, self.device.entity_id)
        self.listen_state(
            self.handle_state_change,
            self.device.entity_id,
            attribute="is_volume_muted",
        )
        self.listen_state(
            self.handle_state_change,
            "binary_sensor.tv_playing",
            new="on",
            duration=self.constants["tv_playing_delay"],
        )
        self.listen_state(
            self.handle_state_change,
            "binary_sensor.tv_playing",
            old="on",
        )

    @property
    def on(self) -> bool:
        """True if the TV is on."""
        return self.device.state == "on"

    @property
    def playing(self) -> bool:
        """True if the TV is playing."""
        return self.on and self.entities.binary_sensor.tv_playing.state == "on"

    @property
    def muted(self) -> bool:
        """True if the TV is muted."""
        return self.device.attributes.get("is_volume_muted") is True

    @override
    def turn_off(self) -> None:  # ty: ignore[invalid-method-override]
        """Turn the TV off."""
        self.device.turn_off()
        self.log("TV is now off", level="DEBUG")

    @override
    def turn_on(self) -> None:  # ty: ignore[invalid-method-override]
        """Turn the TV on."""
        self.device.turn_on()
        self.log("TV is now on", level="DEBUG")

    def pause(self) -> None:
        """Pause media being played on the TV."""
        if self.playing:
            self.device.call_service("media_pause")
            self.log("TV media is now paused", level="DEBUG")

    def mute(self) -> None:
        """Mute the TV."""
        if self.on and not self.muted:
            self.device.call_service("volume_mute", is_volume_muted=True)
            self.log("TV is now muted", level="DEBUG")

    def handle_state_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle TV events to adjust the scene and load appropriately on startup."""
        del kwargs
        if self.debugging:
            self.log(
                f"TV changed from '{old}' to '{new}' ('{entity}' - '{attribute}')",
                level="DEBUG",
            )
        if entity == self.device.entity_id and attribute == "state" and new == "on":
            self.device.call_service(
                "select_source",
                source="App Launcher & Media State Reporter",
            )
        elif self.playing and not self.muted:
            if self.control.scene == "Night":
                self.control.scene = "TV"
        elif self.control.scene == "TV":
            self.control.scene = "Night" if self.lights.dark_outside else "Day"
