"""Example plugin: weather forecast. Shows a data source, a tool and a trigger."""

from __future__ import annotations

from typing import Any

from assistant.plugins.sdk import Item, Plugin, PluginError, tool

URL = "https://api.open-meteo.com/v1/forecast"
DAILY = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
# WMO weather interpretation codes, grouped.
CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "fog", 48: "fog",
    51: "drizzle", 53: "drizzle", 55: "drizzle", 61: "rain", 63: "rain", 65: "heavy rain",
    71: "snow", 73: "snow", 75: "heavy snow", 80: "showers", 81: "showers", 82: "heavy showers",
    95: "thunderstorm", 96: "thunderstorm", 99: "thunderstorm",
}


class WeatherPlugin(Plugin):
    async def _forecast(self, days: int) -> list[dict[str, Any]]:
        config = self.ctx.config
        response = await self.ctx.http(
            "GET", URL,
            params={
                "latitude": config["latitude"], "longitude": config["longitude"],
                "daily": DAILY, "forecast_days": days, "timezone": str(self.ctx.timezone),
            },
        )
        if response.status_code != 200:
            raise PluginError(f"Open-Meteo returned HTTP {response.status_code}")
        daily = response.json()["daily"]
        return [
            {
                "date": day,
                "summary": CODES.get(daily["weather_code"][i], "unknown"),
                "high_c": daily["temperature_2m_max"][i],
                "low_c": daily["temperature_2m_min"][i],
                "rain_chance_percent": daily["precipitation_probability_max"][i],
            }
            for i, day in enumerate(daily["time"])
        ]

    async def sync(self) -> None:
        days = await self._forecast(3)
        place = self.ctx.config["location_name"]
        await self.ctx.upsert_items(
            [
                Item(
                    external_id=f"forecast:{d['date']}",
                    type="item",
                    title=f"Weather in {place} on {d['date']}: {d['summary']}",
                    body=f"High {d['high_c']}°C, low {d['low_c']}°C, "
                         f"{d['rain_chance_percent']}% chance of rain.",
                    data={"kind": "weather", **d},
                )
                for d in days
            ]
        )
        today = days[0]
        threshold = self.ctx.config["rain_alert_percent"]
        if (today["rain_chance_percent"] or 0) >= threshold and await self.ctx.kv_get("alerted") != today["date"]:
            await self.ctx.kv_set("alerted", today["date"])  # once per day
            await self.ctx.emit("rain_expected", today)

    @tool(
        "Get the weather forecast for the user's location.",
        {"type": "object", "properties": {"days": {"type": "integer", "minimum": 1, "maximum": 7}}},
        untrusted=False,
    )
    async def forecast(self, days: int = 3) -> Any:
        return {
            "location": self.ctx.config["location_name"],
            "days": await self._forecast(max(1, min(int(days), 7))),
        }
