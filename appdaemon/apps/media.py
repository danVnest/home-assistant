"""Monitors media devices.

Monitors the TV to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import subprocess
import app


class Media(app.App):
    """Listen for TV state changes to set the scene appropriately."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__entity_id = "media_player.tv"

    def initialize(self):
        """Start listening to TV states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(self.__state_change, self.__entity_id)
        for is_muted in [True, False]:
            self.listen_state(
                self.__state_change,
                self.__entity_id,
                attribute="is_volume_muted",
                old=not is_muted,
                new=is_muted,
            )

    @property
    def is_on(self) -> bool:
        """Check if the TV is currently on or not."""
        return self.get_state(self.__entity_id) == "on"

    @property
    def is_playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        return self.get_state(self.__entity_id) == "on"
        # TODO: update when LG webos supports more than on or off

    @property
    def is_muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.get_state(self.__entity_id, attribute="is_volume_muted") is True

    @property
    def is_pc_on(self) -> bool:
        """Check if the PC is currently on or not."""
        return subprocess.call(["ping", "-c", "1", self.args["pc_ip"]]) == 0

    def standby(self):
        """Turn the TV off."""
        self.call_service("media_player/turn_off", entity_id=self.__entity_id)
        self.log("TV is now on standby", level="DEBUG")

    def turn_on(self):
        """Turn the TV on."""
        self.call_service("media_player/turn_on", entity_id=self.__entity_id)
        self.log("TV is now on", level="DEBUG")

    def pause(self):
        """Pause media being played on the TV."""
        self.call_service("media_player/media_pause", entity_id=self.__entity_id)
        self.call_service(
            "media_player/volume_mute", is_volume_muted=True, entity_id=self.__entity_id
        )
        self.log("TV media is now paused", level="DEBUG")

    def __state_change(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Handle TV events at night and change the scene."""
        del entity, old, kwargs
        self.log(
            f"TV is now {'muted: ' if attribute == 'is_volume_muted' else ''}{new}"
        )
        if self.control.scene == "Night" and self.is_playing and not self.is_muted:
            self.control.scene = "TV"
        elif self.control.scene == "TV" and (not self.is_playing or self.is_muted):
            self.control.scene = "Night"
