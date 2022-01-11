"""Monitors media devices.

Monitors the TV to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import app


class Media(app.App):
    """Listen for media state changes to set the scene appropriately."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.entity_id = "media_player.tv"

    def initialize(self):
        """Start listening to media states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(
            self.__tv_state_change,
            self.entity_id,
            new="on",
            duration=self.args["steady_state_delay"],
        )
        self.listen_state(
            self.__tv_state_change,
            self.entity_id,
            old="on",
            duration=self.args["steady_state_delay"],
        )
        self.listen_state(
            self.__tv_state_change,
            self.entity_id,
            attribute="is_volume_muted",
        )

    @property
    def is_playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        return (
            self.get_state(self.entity_id) == "on"
        )  # FIXME: LG webos currently only detects on or off

    @property
    def is_muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.get_state(self.entity_id, attribute="is_volume_muted")

    def standby(self):
        """Turn the TV off."""
        self.call_service("media_player/turn_off", entity_id=self.entity_id)
        self.log("TV is now on standby", level="DEBUG")

    def turn_on(self):
        """Turn the TV on."""
        self.call_service("media_player/turn_on", entity_id=self.entity_id)
        self.log("TV is now on", level="DEBUG")

    def pause(self):
        """Pause media being played on the TV."""
        self.call_service("media_player/media_pause", entity_id=self.entity_id)
        self.log("TV media is now paused", level="DEBUG")

    def __tv_state_change(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """Handle TV events at night and change the scene."""
        del entity, attribute, old, kwargs
        if new == "on":
            self.call_service(
                "media_player/select_source", source="Netflix", entity_id=self.entity_id
            )
        if self.scene == "Night" and self.is_playing and not self.is_muted:
            self.scene = "TV"
        elif self.scene == "TV":
            self.scene = "Night"
