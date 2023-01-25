"""Implements home safety automations.

Monitors smoke sensors and triggers corresponding alarm routines.

User defined variables are configued in safety.yaml
"""
import app


class Safety(app.App):
    """Set up smoke sensors."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        # self.__smoke_sensors = {"entryway": None, "living_room": None, "garage": None}
        # TODO: add Nest Protects when Nest integration is working properly

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for sensor_id in self.__smoke_sensors:
            self.__smoke_sensors[sensor_id] = SmokeSensor(sensor_id, self)
        self.listen_state(
            self.__handle_camera_motion, "binary_sensor.doorbell_motion", new="True"
        )

    def __handle_camera_motion(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """Send notification if motion detected when no one is home."""
        self.log("Person detected by doorbell camera")
        if not self.control.apps["presence"].anyone_home():
            self.notify(
                "Person detected at the front door (currently "
                f"{self.entities.lock.door_lock.state})",
                title="Person Detected",
            )


class SmokeSensor:  # pylint: disable=too-few-public-methods
    """Monitors smoke and carbon monoxide alarms from a sensor."""

    def __init__(self, sensor_id: str, controller: Safety):
        """Start listening to smoke and co status."""
        self.controller = controller
        self.controller.listen_state(
            self.__handle_smoke, f"sensor.{sensor_id}_protect_smoke_status"
        )
        self.controller.listen_state(
            self.__handle_smoke, f"sensor.{sensor_id}_protect_co_status"
        )

    def __handle_smoke(
        self, entity, attribute, old, new, kwargs
    ):  # pylint: disable=too-many-arguments
        """React when high smoke level detected."""
        del attribute, old, kwargs
        self.controller.log(f"{entity}: {new}")
        if new != 0:
            self.controller.apps["media"].pause()
            self.controller.notify(
                f"SMOKE OR CARBON MONOXIDE DETECTED BY: {entity}", title="SMOKE ALARM"
            )
            self.controller.fire_event("SCENE", scene="bright")
