"""Sensor platform for apparent_temperature."""

import logging
import math
from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
)
from homeassistant.components.climate import (
    DOMAIN as CLIMATE_DOMAIN,
)
from homeassistant.components.group import expand_entity_ids
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.components.weather import (
    ATTR_WEATHER_HUMIDITY,
    ATTR_WEATHER_TEMPERATURE,
    ATTR_WEATHER_TEMPERATURE_UNIT,
    ATTR_WEATHER_WIND_SPEED,
    ATTR_WEATHER_WIND_SPEED_UNIT,
)
from homeassistant.components.weather import (
    DOMAIN as WEATHER_DOMAIN,
)
from homeassistant.const import (
    ATTR_DEVICE_CLASS,
    ATTR_UNIT_OF_MEASUREMENT,
    CONF_NAME,
    CONF_SOURCE,
    CONF_UNIQUE_ID,
    EVENT_HOMEASSISTANT_START,
    PERCENTAGE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfSpeed,
    UnitOfTemperature,
)
from homeassistant.core import (
    Event,
    HomeAssistant,
    State,
    callback,
    split_entity_id,
)
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.typing import ConfigType, DiscoveryInfoType, UndefinedType
from homeassistant.util.unit_conversion import SpeedConverter, TemperatureConverter

from .const import (
    ATTR_HUMIDITY_SOURCE,
    ATTR_HUMIDITY_SOURCE_VALUE,
    ATTR_TEMPERATURE_SOURCE,
    ATTR_TEMPERATURE_SOURCE_VALUE,
    ATTR_WIND_SPEED_SOURCE,
    ATTR_WIND_SPEED_SOURCE_VALUE,
    STARTUP_MESSAGE,
)

_LOGGER = logging.getLogger(__name__)

PLATFORM_SCHEMA = cv.PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_SOURCE): cv.entity_ids,
        vol.Optional(CONF_NAME): cv.string,
        vol.Optional(CONF_UNIQUE_ID): cv.string,
    },
)


# pylint: disable=unused-argument
async def async_setup_platform(
    hass: HomeAssistant,
    config: ConfigType,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # noqa: ARG001
) -> None:
    """Set up the Car Wash sensor."""
    # Print startup message
    _LOGGER.info(STARTUP_MESSAGE)

    async_add_entities(
        [
            ApparentTemperatureSensor(
                config.get(CONF_UNIQUE_ID),
                config.get(CONF_NAME),
                expand_entity_ids(hass, config.get(CONF_SOURCE)),
            ),
        ],
    )


class ApparentTemperatureSensor(SensorEntity):
    """Apparent Temperature Sensor class."""

    _attr_has_entity_name = True
    _attr_icon = "mdi:thermometer-lines"
    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_should_poll = False
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_suggested_display_precision = 1

    def __init__(
        self,
        unique_id: str | None,
        name: str | None,
        sources: list[str],
    ) -> None:
        """Class initialization."""
        self._attr_unique_id = unique_id
        self._attr_native_value = None

        self._name = name
        self._sources = sources

        self._temp = None
        self._humd = None
        self._wind = None
        self._temp_val = None
        self._humd_val = None
        self._wind_val = None

    @staticmethod
    def _compose_name(source_name: str) -> str:
        """Compose entity name based on source entity name."""
        tpos = source_name.rfind("temperature")
        return (
            source_name + " Apparent Temperature"
            if tpos < 0
            else source_name[:tpos] + "Apparent " + source_name[tpos:]
        )

    @property
    def name(self) -> str | UndefinedType | None:
        """Return the name of the sensor."""
        if self._name:
            return self._name

        return self._compose_name(split_entity_id(self._sources[0])[1])

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return entity specific state attributes."""
        return {
            ATTR_TEMPERATURE_SOURCE: self._temp,
            ATTR_TEMPERATURE_SOURCE_VALUE: self._temp_val,
            ATTR_HUMIDITY_SOURCE: self._humd,
            ATTR_HUMIDITY_SOURCE_VALUE: self._humd_val,
            ATTR_WIND_SPEED_SOURCE: self._wind,
            ATTR_WIND_SPEED_SOURCE_VALUE: self._wind_val,
        }

    def _setup_sources(self) -> list[str]:
        """Set sources for entity and return list of sources to track."""
        entities = set()
        for entity_id in self._sources:
            state: State = self.hass.states.get(entity_id)
            domain = split_entity_id(state.entity_id)[0]
            device_class = state.attributes.get(ATTR_DEVICE_CLASS)
            unit_of_measurement = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

            if domain == WEATHER_DOMAIN:
                self._temp = entity_id
                self._humd = entity_id
                self._wind = entity_id
                entities.add(entity_id)
            elif domain == CLIMATE_DOMAIN:
                self._temp = entity_id
                self._humd = entity_id
                entities.add(entity_id)
            elif (
                device_class == SensorDeviceClass.TEMPERATURE
                or unit_of_measurement in UnitOfTemperature
            ):
                self._temp = entity_id
                entities.add(entity_id)
            elif (
                device_class == SensorDeviceClass.HUMIDITY
                or unit_of_measurement == PERCENTAGE
            ):
                self._humd = entity_id
                entities.add(entity_id)
            elif unit_of_measurement in UnitOfSpeed:
                self._wind = entity_id
                entities.add(entity_id)
            elif entity_id.find("temperature") >= 0:
                self._temp = entity_id
                entities.add(entity_id)
            elif entity_id.find("humidity") >= 0:
                self._humd = entity_id
                entities.add(entity_id)
            elif entity_id.find("wind") >= 0:
                self._wind = entity_id
                entities.add(entity_id)

        return list(entities)

    async def async_added_to_hass(self) -> None:
        """Register callbacks."""

        # pylint: disable=unused-argument
        @callback
        def sensor_state_listener(event: Event) -> None:  # noqa: ARG001
            """Handle device state changes."""
            self.async_schedule_update_ha_state(force_refresh=True)

        # pylint: disable=unused-argument
        @callback
        def sensor_startup(event: Event) -> None:  # noqa: ARG001
            """Update entity on startup."""
            async_track_state_change_event(
                self.hass,
                self._setup_sources(),
                sensor_state_listener,
            )

            self.async_schedule_update_ha_state(
                force_refresh=True,
            )  # Force first update

        self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_START, sensor_startup)

    @staticmethod
    def _has_state(state: str | None) -> bool:
        """Return True if state has any value."""
        return state is not None and state not in [
            STATE_UNKNOWN,
            STATE_UNAVAILABLE,
            "None",
            "",
        ]

    def _get_temperature(self, entity_id: str | None) -> float | None:
        """Get temperature value (in °C) from entity."""
        if entity_id is None:
            return None
        state: State = self.hass.states.get(entity_id)
        if state is None:
            return None

        domain = split_entity_id(state.entity_id)[0]
        if domain == WEATHER_DOMAIN:
            temperature = state.attributes.get(ATTR_WEATHER_TEMPERATURE)
            entity_unit = state.attributes.get(ATTR_WEATHER_TEMPERATURE_UNIT)
        elif domain == CLIMATE_DOMAIN:
            temperature = state.attributes.get(ATTR_CURRENT_TEMPERATURE)
            entity_unit = state.attributes.get(ATTR_WEATHER_TEMPERATURE_UNIT)
        else:
            temperature = state.state
            entity_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if not self._has_state(temperature):
            return None

        try:
            temperature = TemperatureConverter.convert(
                float(temperature),
                entity_unit,
                UnitOfTemperature.CELSIUS,
            )
        except ValueError:
            _LOGGER.exception('Could not convert value "%s" to float', state)
            return None

        return float(temperature)

    def _get_humidity(self, entity_id: str | None) -> float | None:
        """Get humidity value from entity."""
        if entity_id is None:
            return None
        state: State = self.hass.states.get(entity_id)
        if state is None:
            return None

        domain = split_entity_id(state.entity_id)[0]
        if domain == WEATHER_DOMAIN:
            humidity = state.attributes.get(ATTR_WEATHER_HUMIDITY)
        elif domain == CLIMATE_DOMAIN:
            humidity = state.attributes.get(ATTR_CURRENT_HUMIDITY)
        else:
            humidity = state.state

        if not self._has_state(humidity):
            return None

        return float(humidity)

    def _get_wind_speed(self, entity_id: str | None) -> float | None:
        """Get wind speed value from entity."""
        if entity_id is None:
            return 0.0
        state: State = self.hass.states.get(entity_id)
        if state is None:
            return 0.0

        domain = split_entity_id(state.entity_id)[0]
        if domain == WEATHER_DOMAIN:
            wind_speed = state.attributes.get(ATTR_WEATHER_WIND_SPEED)
            entity_unit = state.attributes.get(ATTR_WEATHER_WIND_SPEED_UNIT)
        else:
            wind_speed = state.state
            entity_unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)

        if not self._has_state(wind_speed):
            return None

        try:
            wind_speed = SpeedConverter.convert(
                float(wind_speed),
                entity_unit,
                UnitOfSpeed.METERS_PER_SECOND,
            )
        except ValueError:
            _LOGGER.exception('Could not convert value "%s" to float', state)
            return None

        return float(wind_speed)

    async def async_update(self) -> None:
        """Update sensor state."""
        self._temp_val = temp = self._get_temperature(self._temp)  # °C
        self._humd_val = humd = self._get_humidity(self._humd)  # %
        self._wind_val = wind = self._get_wind_speed(self._wind)  # m/s

        _LOGGER.debug("Temp: %s °C  Hum: %s %%  Wind: %s m/s", temp, humd, wind)

        if temp is None or humd is None:
            _LOGGER.warning(
                "Can't calculate sensor value: some sources are unavailable.",
            )
            self._attr_native_value = None
            return

        if wind is None:
            _LOGGER.warning(
                "Can't get wind speed value. Wind speed will be ignored in calculation.",
            )
            wind = 0

        e_value = humd * 0.06105 * math.exp((17.27 * temp) / (237.7 + temp))
        self._attr_native_value = temp + 0.33 * e_value - 0.7 * wind - 4
        _LOGGER.debug(
            "New sensor state is %s %s",
            self._attr_native_value,
            self._attr_native_unit_of_measurement,
        )
