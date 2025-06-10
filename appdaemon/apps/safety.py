"""Implements home safety automations.

Monitors fire and baby safety sensors,
triggering corresponding alarm routines when appropriate.

User defined variables are configured in safety.yaml
"""

from app import App


class Safety(App):
    """Set up fire and baby safety sensors."""

    def __init__(self, *args, **kwargs):
        """Extend with attribute definitions."""
        super().__init__(*args, **kwargs)
        self.fire_sensors = {"entryway": None, "living_room": None, "garage": None}
        # TODO: https://app.asana.com/0/1207020279479204/1205753645479427/f
        # add , "low_battery", "lost_power", "sock_disconnected", "sock_off") ?

    def initialize(self):
        """Initialise listeners for fire and baby sensors.

        Appdaemon defined init function called once ready after __init__.
        """
        super().initialize()
        for sensor_id in self.fire_sensors:
            self.fire_sensors[sensor_id] = FireSensor(sensor_id, self)


class FireSensor:
    """Monitors smoke, carbon monoxide and heat alarms from a fire sensor."""

    def __init__(self, sensor_id: str, controller: Safety):
        """Start listening to smoke, co and heat status."""
        self.sensor_id = sensor_id
        self.sensor_types = ["smoke", "co", "heat"]
        self.sensor_prefix = "binary_sensor.nest_protect_"
        self.sensor_suffix = "_status"
        self.controller = controller
        for sensor_type in self.sensor_types:
            self.controller.listen_state(
                self.handle_fire,
                f"{self.sensor_prefix}{sensor_id}_{sensor_type}{self.sensor_suffix}",
            )

    def handle_fire(
        self,
        entity: str,
        attribute: str,
        old: str,
        new: str,
        **kwargs: dict,
    ):
        """React when potential fire detected."""
        del attribute, kwargs
        if new == "on":
            self.controller.control.scene = "Bright"
            self.presence.unlock_door()
            self.controller.media.pause()
            alert_type = (
                entity.removeprefix(f"{self.sensor_prefix}{self.sensor_id}_")
                .removesuffix(self.sensor_suffix)
                .replace("_", " ")
                .capitalize()
            )
            self.controller.notify(
                f"{alert_type} detected in the {self.sensor_id.replace('_', ' ')}",
                title="Fire Alarm",
                critical=True,
            )
        elif new == "off" and old == "on":
            self.controller.control.reset_scene()
