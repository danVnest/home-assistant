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
        self.__play_state_sensor = "sensor.webostvservice_play_state"
        self.__last_play_state = None

    def initialize(self):
        """Start listening to TV states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(self.__state_change, self.__entity_id)
        self.listen_state(
            self.__state_change,
            self.__play_state_sensor,
            duration=self.args["state_change_delay"],
        )
        for is_muted in [True, False]:
            self.listen_state(
                self.__state_change,
                self.__entity_id,
                attribute="is_volume_muted",
                old=not is_muted,
                new=is_muted,
                duration=self.args["state_change_delay"],
            )

    @property
    def is_on(self) -> bool:
        """Check if the TV is currently on or not."""
        return self.get_state(self.__entity_id) == "on"

    @property
    def is_playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        play_state = self.get_state(self.__play_state_sensor)
        if (
            self.get_state(self.__entity_id, attribute="source") == "PC"
            and self.is_pc_on
        ):
            return True
        if play_state == "unavailable":
            return self.__last_play_state == "playing"
        return play_state == "playing"

    @property
    def is_muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.get_state(self.__entity_id, attribute="is_volume_muted") is True

    @property
    def is_pc_on(self) -> bool:
        """Check if the PC is currently on or not."""
        return subprocess.call(["ping", "-c", "1", self.args["pc_ip"]]) == 0  # noqa: S603, S607

    def turn_off(self):
        """Turn the TV off."""
        self.call_service("media_player/turn_off", entity_id=self.__entity_id)
        self.log("TV is now off", level="DEBUG")

    def turn_on(self):
        """Turn the TV on."""
        self.call_service("media_player/turn_on", entity_id=self.__entity_id)
        self.log("TV is now on", level="DEBUG")

    def pause(self):
        """Pause media being played on the TV."""
        self.call_service("media_player/media_pause", entity_id=self.__entity_id)
        self.log("TV media is now paused", level="DEBUG")

    def __setup_play_state_sensor(self, kwargs: dict):
        """Start the MQTT app on the TV, enabling play/pause state detection."""
        del kwargs
        if not self.is_on:
            self.log(
                "TV was turned off before state sensor setup completed",
                level="DEBUG",
            )
            return
        if self.get_state(self.__play_state_sensor) in ("unavailable", "unknown"):
            if self.get_state(self.__entity_id, attribute="source") != "LG 2 MQTT":
                self.call_service(
                    "media_player/select_source",
                    source="LG 2 MQTT",
                    entity_id=self.__entity_id,
                )
            self.run_in(self.__setup_play_state_sensor, self.args["setup_check_delay"])
            self.log("Loading app on TV to setup state sensor", level="DEBUG")
        else:
            self.log("Play state sensor setup complete", level="DEBUG")

    def __state_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Handle TV events at night and change the scene."""
        del kwargs
        if entity == self.__entity_id and attribute == "state" and new == "on":
            self.__setup_play_state_sensor(None)
        elif entity == self.__play_state_sensor:
            if new != "unavailable":
                self.__last_play_state = new
                if old == "unavailable":
                    old = self.__last_play_state
            else:
                return
        self.log(f"TV changed from '{old}' to '{new}' ('{entity}' - '{attribute}')")
        if self.is_on and self.is_playing and not self.is_muted:
            if self.control.scene == "Night":
                self.control.scene = "TV"
        elif self.control.scene == "TV":
            if (
                self.apps["lights"].lights["bedroom"].brightness == 0
                and self.apps["lights"].lights["bedroom"].is_ignoring_presence()
            ):
                self.control.scene = "Sleep"
            else:
                self.control.scene = "Night"
