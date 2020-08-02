"""Monitors media devices.

Monitors the Living Room Apple TV to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import app


class Media(app.App):
    """Listen for media state changes to set the scene appropriately."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.entity_id = "media_player.living_room"

    def initialize(self):
        """Start listening to media states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(
            self.__tv_state_change,
            self.entity_id,
            new="playing",
            duration=self.args["steady_state_delay"],
        )
        self.listen_state(
            self.__tv_state_change,
            self.entity_id,
            old="playing",
            duration=self.args["steady_state_delay"],
        )

    @property
    def is_playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        return self.entities.media_player.living_room.state == "playing"

    def standby(self):
        """Turn the TV off."""
        self.call_service("media_player/turn_off", entity_id=self.entity_id)

    def __tv_state_change(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """Handle TV events at night and change the scene."""
        del entity, attribute, old, kwargs
        if new == "playing" and self.scene == "Night":
            self.scene = "TV"
        elif self.scene == "TV":
            self.scene = "Night"
