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
        self.__smoke_sensors = {"entryway": None, "living_room": None, "garage": None}

    def initialize(self):
        """Initialise TemperatureMonitor, Aircon units, and event listening.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for sensor_id in self.__smoke_sensors:
            self.__smoke_sensors[sensor_id] = SmokeSensor(sensor_id, self)
        self.listen_state(
            self.__handle_camera_motion, "binary_sensor.doorbell_motion", new="on"
        )

    def __handle_camera_motion(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """Send notification if motion detected when no one is home."""
        del entity, attribute, old, new, kwargs
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
        self.__sensor_id = sensor_id
        self.__controller = controller
        for sensor_type in ["smoke", "co", "heat"]:
            self.__controller.listen_state(
                self.__handle_smoke,
                f"binary_sensor.nest_protect_{sensor_id}_{sensor_type}_status",
                new="on",
            )

    def __handle_smoke(
        self, entity: str, attribute: str, old: str, new: str, kwargs: dict
    ):  # pylint: disable=too-many-arguments
        """React when high smoke level detected."""
        del attribute, new, old, kwargs
        self.__controller.control.scene = "Bright"
        self.__controller.apps["media"].pause()
        if "smoke" in entity:
            sensor_type = "Smoke"
        elif "heat" in entity:
            sensor_type = "Heat"
        else:
            sensor_type = "Carbon monoxide"
        self.__controller.notify(
            f"{sensor_type} detected in {self.__sensor_id.replace('_', ' ')}",
            title="Smoke Alarm",
            critical=True,
        )
