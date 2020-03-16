"""Monitors media devices.

Fires an event to signify the Living Room Apple TV toggling between playing and
not playing.

User defined variables are configued in media.yaml
"""

import app


class Media(app.App):
    """Detect media state changes and fire corresponding events."""

    def initialize(self):
        """Start listening to media states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(
            self.tv_state_change,
            "media_player.living_room",
            new="playing",
            duration=self.args["steady_state_delay"],
        )
        self.listen_state(
            self.tv_state_change,
            "media_player.living_room",
            old="playing",
            duration=self.args["steady_state_delay"],
        )
        # self.listen_event(self.scene_change, 'SCENE')

    def tv_state_change(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """Handle TV events at night and change the scene."""
        del entity, attribute, old, kwargs
        if new == "playing" and self.scene == "night":
            self.scene = "tv"
        elif self.scene == "tv":
            self.scene = "night"
