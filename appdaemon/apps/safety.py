"""Implements home safety automations.

Monitors fire and baby safety sensors, as well as dog water bowl level,
triggering corresponding alarm routines when appropriate.

User defined variables are configured in safety.yaml
"""

from typing import Any, override

from app import App


class Safety(App):
    """Set up safety sensors."""

    @override
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.fire_sensors: dict[str, FireSensor] = {}

    @override
    def initialize(self) -> None:
        """Initialise listeners for safety sensors."""
        super().initialize()
        for room in ("entryway", "living_room", "garage"):
            self.fire_sensors[room] = FireSensor(room, self)
        self.listen_state(
            self.handle_dog_water_bowl_change,
            "binary_sensor.dog_water_bowl",
            duration=60,
        )
        self.dog_water_bowl_empty_reminder_timer = None
        self.notified_of_dog_water_bowl_empty = False

    @property
    def dog_water_bowl_empty(self) -> bool:
        """True if the dog water bowl is empty."""
        return self.entities.binary_sensor.dog_water_bowl.state == "off"

    def handle_dog_water_bowl_change(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """Handle if the dog water bowl is empty or full."""
        del entity, attribute, old, kwargs
        if new == "off":
            self.notify_of_empty_dog_water_bowl()
        else:
            self.notified_of_dog_water_bowl_empty = False
            self.cancel_timer(self.dog_water_bowl_empty_reminder_timer)

    def notify_of_empty_dog_water_bowl(self, **kwargs: Any) -> None:
        """Notify if the dog water bowl is empty and schedule reminders."""
        del kwargs
        self.cancel_timer(self.safety.dog_water_bowl_empty_reminder_timer)
        self.dog_water_bowl_empty_reminder_timer = self.run_in(
            self.notify_of_empty_dog_water_bowl,
            60 * 60,
        )
        if self.control.scene.startswith("Away") and not self.presence.pets_home_alone:
            return
        self.notify(
            "Refill the dog water bowl as soon as possible",
            title="Dog Water Bowl Empty",
            targets="anyone_home",
            critical=(
                self.notified_of_dog_water_bowl_empty
                and self.control.scene != "Sleep"
                and not self.control.napping_in_bedroom
                and not self.control.napping_in_nursery
            ),
        )
        self.notified_of_dog_water_bowl_empty = True


class FireSensor:
    """Monitors smoke, carbon monoxide and heat alarms from a fire sensor."""

    def __init__(self, room: str, controller: Safety) -> None:
        """Start listening to smoke, carbon monoxide, and heat alarms."""
        self.room = room
        self.sensor_types = "smoke", "co", "heat"
        self.sensor_prefix = "binary_sensor.nest_protect_"
        self.sensor_suffix = "_status"
        self.controller = controller
        for sensor_type in self.sensor_types:
            self.controller.listen_state(
                self.handle_fire,
                f"{self.sensor_prefix}{room}_{sensor_type}{self.sensor_suffix}",
            )

    def handle_fire(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: Any,
    ) -> None:
        """React when potential fire detected (or stop the alarm when clear)."""
        del attribute, kwargs
        if new != "on" and not (new == "off" and old == "on"):
            return
        alert_type = (
            entity.removeprefix(f"{self.sensor_prefix}{self.room}_")
            .removesuffix(self.sensor_suffix)
            .replace("_", " ")
            .capitalize()
        )
        if new == "on":
            self.controller.control.scene = "Bright"
            self.controller.presence.unlock_front_door(force=True)
            self.controller.media.pause()
            self.controller.media.mute()
            self.controller.notify(
                f"{alert_type} detected in the {self.room.replace('_', ' ')}",
                title="Fire Alarm",
                critical=True,
            )
        elif new == "off" and old == "on":
            self.controller.control.reset_scene()
            self.controller.notify(
                f"{alert_type} no longer detected in the {self.room.replace('_', ' ')}",
                title="Fire Alarm",
            )
