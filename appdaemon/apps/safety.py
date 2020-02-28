"""Implements home safety automations.

Monitors smoke sensors and triggers corresponding alarm routines.

User defined variables are configued in safety.yaml
"""
import appdaemon.plugins.hass.hassapi as hass


class Safety(hass.Hass):
    """Set up smoke sensors."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        self.smoke_sensors = {"entryway": None, "living_room": None, "garage": None}
        super().__init__(*args, **kwargs)

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        for sensor_id in self.smoke_sensors:
            self.smoke_sensors[sensor_id] = SmokeSensor(sensor_id, self)

    def notify(self, message: str, **kwargs):
        """Send a notification to users and log the message."""
        super().notify(message, title="Safety")
        self.log(f"NOTIFICATION: {message}")


class SmokeSensor:  # pylint: disable=too-few-public-methods
    """Monitors smoke and carbon monoxide alarms from a sensor."""

    def __init__(self, sensor_id: str, controller: Safety):
        """Start listening to smoke and co status."""
        self.controller = controller
        self.controller.listen_state(
            self.smoke, f"sensor.{sensor_id}_protect_smoke_status"
        )
        self.controller.listen_state(
            self.smoke, f"sensor.{sensor_id}_protect_co_status"
        )

    def smoke(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """React when high smoke level detected."""
        del attribute, old, kwargs
        self.controller.log(f"{entity}: {new}")
        if new != 0:
            self.controller.notify(f"SMOKE OR CARBON MONOXIDE DETECTED BY: {entity}")
            self.controller.fire_event("SCENE", scene="bright")
