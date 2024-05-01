"""Implements home safety automations.

Monitors fire and baby safety sensors,
triggering corresponding alarm routines when appropriate.

User defined variables are configured in safety.yaml
"""

import app


class Safety(app.App):
    """Set up fire and baby safety sensors."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.__fire_sensors = {"entryway": None, "living_room": None, "garage": None}
        self.__owlet_sensors = (
            "low_heart_rate",
            "high_heart_rate",
            "low_oxygen",
            "high_oxygen",
        )
        self.__owlet_sensor_prefix = "binary_sensor.owlet_"
        self.__owlet_sensor_suffix = "_alert"

    def initialize(self):
        """Initialise listeners for fire and baby sensors.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for sensor_id in self.__fire_sensors:
            self.__fire_sensors[sensor_id] = FireSensor(sensor_id, self)
        for owlet_sensor in self.__owlet_sensors:
            self.listen_state(
                self.__handle_owlet_alert,
                f"{self.__owlet_sensor_prefix}{owlet_sensor}{self.__owlet_sensor_suffix}",
            )

    def __handle_owlet_alert(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """React when an Owlet alert is triggered or ends."""
        del attribute, kwargs
        if new == "on":
            self.control.scene = "Bright"
            self.apps["media"].pause()
            alert_type = (
                entity.removeprefix(self.__owlet_sensor_prefix)
                .removesuffix(self.__owlet_sensor_suffix)
                .replace("_", " ")
            )
            self.notify(
                f"Check Wren - {alert_type} detected",
                title="Owlet Alarm",
                critical=True,
            )
        elif new == "off" and old != "unavailable":
            self.control.reset_scene()


class FireSensor:
    """Monitors smoke, carbon monoxide and heat alarms from a fire sensor."""

    def __init__(self, sensor_id: str, controller: Safety):
        """Start listening to smoke, co and heat status."""
        self.__sensor_id = sensor_id
        self.__sensor_types = ["smoke", "co", "heat"]
        self.__sensor_prefix = "binary_sensor.nest_protect_"
        self.__sensor_suffix = "_status"
        self.__controller = controller
        for sensor_type in self.__sensor_types:
            self.__controller.listen_state(
                self.__handle_fire,
                f"{self.__sensor_prefix}{sensor_id}_{sensor_type}{self.__sensor_suffix}",
            )

    def __handle_fire(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        kwargs: dict,
    ):
        """React when potential fire detected."""
        del attribute, kwargs
        if new == "on":
            self.__controller.control.scene = "Bright"
            self.__controller.apps["media"].pause()
            alert_type = (
                entity.removeprefix(f"{self.__sensor_prefix}{self.__sensor_id}_")
                .removesuffix(self.__sensor_suffix)
                .replace("_", " ")
                .capitalize()
            )
            self.__controller.notify(
                f"{alert_type} detected in the {self.__sensor_id.replace('_', ' ')}",
                title="Fire Alarm",
                critical=True,
            )
        elif new == "off" and old != "unavailable":
            self.__controller.control.reset_scene()
