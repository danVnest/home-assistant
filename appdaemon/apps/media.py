"""Monitors media devices.

Monitors the TV to change the scene appropriately.

User defined variables are configued in media.yaml
"""

import subprocess

from app import App


class Media(App):
    """Listen for TV state changes to set the scene appropriately."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.entity_id = "media_player.tv"
        self.play_state_sensor = "sensor.webostvservice_play_state"
        self.last_play_state = None

    def initialize(self):
        """Start listening to TV states.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        self.listen_state(self.state_change, self.entity_id)
        self.listen_state(
            self.state_change,
            self.play_state_sensor,
            duration=self.args["state_change_delay"],
        )
        # TODO: https://app.asana.com/0/1207020279479204/1207033183115382/f
        # try without state_change_delay, I'd previously removed it!
        for muted in [True, False]:
            self.listen_state(
                self.state_change,
                self.entity_id,
                attribute="is_volume_muted",
                old=not muted,
                new=muted,
                duration=self.args["state_change_delay"],
            )

    @property
    def on(self) -> bool:
        """Check if the TV is currently on or not."""
        return self.get_state(self.entity_id) == "on"

    @property
    def playing(self) -> bool:
        """Check if the TV is currently playing or not."""
        play_state = self.get_state(self.play_state_sensor)
        if self.get_state(self.entity_id, attribute="source") == "PC" and self.pc_on:
            return True
        if play_state == "unavailable":
            return self.last_play_state == "playing"
        return play_state == "playing"

    @property
    def muted(self) -> bool:
        """Check if the TV is currently muted or not."""
        return self.get_state(self.entity_id, attribute="is_volume_muted") is True

    @property
    def pc_on(self) -> bool:
        """Check if the PC is currently on or not."""
        return subprocess.call(["ping", "-c", "1", self.args["pc_ip"]]) == 0  # noqa: S603, S607

    def turn_off(self):
        """Turn the TV off."""
        self.call_service("media_player/turn_off", entity_id=self.entity_id)
        self.log("TV is now off", level="DEBUG")

    def turn_on(self):
        """Turn the TV on."""
        self.call_service("media_player/turn_on", entity_id=self.entity_id)
        self.log("TV is now on", level="DEBUG")

    def pause(self):
        """Pause media being played on the TV."""
        self.call_service("media_player/media_pause", entity_id=self.entity_id)
        self.log("TV media is now paused", level="DEBUG")

    def setup_play_state_sensor(self, kwargs: dict):
        """Start the MQTT app on the TV, enabling play/pause state detection."""
        del kwargs
        if not self.on:
            self.log(
                "TV was turned off before state sensor setup completed",
                level="DEBUG",
            )
            return
        if self.get_state(self.play_state_sensor) in ("unavailable", "unknown"):
            if self.get_state(self.entity_id, attribute="source") != "LG 2 MQTT":
                self.call_service(
                    "media_player/select_source",
                    source="LG 2 MQTT",
                    entity_id=self.entity_id,
                )
            self.run_in(self.setup_play_state_sensor, self.args["setup_check_delay"])
            self.log("Loading app on TV to setup state sensor", level="DEBUG")
        else:
            self.log("Play state sensor setup complete", level="DEBUG")

    def state_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """Handle TV events at night and change the scene."""
        del kwargs
        if entity == self.entity_id and attribute == "state" and new == "on":
            self.setup_play_state_sensor(None)
        elif entity == self.play_state_sensor:
            if new != "unavailable":
                self.last_play_state = new
                if old == "unavailable":
                    old = self.last_play_state
                    # self.call_service("media_player/select_source", source="Home Dashboard", entity_id=self.entity_id)
            else:
                # TODO: https://app.asana.com/0/1207020279479204/1207033183115382/f
                # Firstly, does this matter? Try ignoring as per current setup
                # if it does matter, mark as setup required and trigger __setup_state_change_sensor when not playing
                # BETTER IDEA: recreate LG TV dashboard, no logs, have all streaming app icons (wait for sensor before leaving dashboard)
                # HALT ALL DEVELOPMENT HERE, looks like play state is being released as default functionality
                return
        self.log(f"TV changed from '{old}' to '{new}' ('{entity}' - '{attribute}')")
        if self.on and self.playing and not self.muted:
            if self.control.scene == "Night":
                self.control.scene = "TV"
        elif self.control.scene == "TV":
            if (
                self.control.apps["lights"].lights["bedroom"].brightness == 0
                and self.control.apps["lights"].lights["bedroom"].ignoring_vacancy
            ):
                self.control.scene = "Sleep"
            else:
                self.control.scene = "Night"
