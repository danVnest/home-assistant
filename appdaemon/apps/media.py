"""Monitors media devices.

Monitors the Living Room Apple TV to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import app


class Media(app.App):
    """Listen for media state changes to set the scene appropriately."""

    def initialize(self):
        """Start listening to media states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(
            self.__tv_state_change,
            "media_player.living_room",
            new="playing",
            duration=self.args["steady_state_delay"],
        )
        self.listen_state(
            self.__tv_state_change,
            "media_player.living_room",
            old="playing",
            duration=self.args["steady_state_delay"],
        )

    @property
    def is_playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        return self.entities.media_player.living_room.state == "playing"

    def __tv_state_change(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """Handle TV events at night and change the scene."""
        del entity, attribute, old, kwargs
        if new == "playing" and self.scene == "Night":
            self.scene = "TV"
        elif self.scene == "TV":
            self.scene = "Night"
