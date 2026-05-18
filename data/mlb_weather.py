"""
MLB ballpark weather for total-altering conditions.

Uses OpenWeatherMap free API (1000 req/day).
Set env var: OPENWEATHER_API_KEY

Wind blowing OUT → OVER-friendly for HR/TB; wind blowing IN → UNDER-friendly.
Temperature matters: cold (<50°F) suppresses offense.

get_park_weather(home_team_name: str) -> dict | None
  Returns {temp_f, wind_mph, wind_dir_deg, wind_dir_label, condition,
           over_boost, description}
  over_boost: float probability adjustment (-0.05 to +0.05)
"""

import json
import math
import os
import time
from pathlib import Path

import requests

OPENWEATHER_API_KEY = os.environ.get("OPENWEATHER_API_KEY", "")
CACHE_PATH          = Path("logs/.mlb_weather_cache.json")
CACHE_TTL           = 3600  # 1 hour

# ── Ballpark data ──────────────────────────────────────────────────────────────
# orientation_deg: the direction the outfield faces (where fly balls go).
# Wind blowing IN that direction = headwind (ball carries in, UNDER-friendly).
# Wind blowing OUT from that direction = tailwind (ball carries out, OVER-friendly).
# We compute: wind_component = cos(wind_dir - orientation_deg) * wind_mph
# positive wind_component = wind blowing toward outfield (OVER boost)

MLB_PARKS = {
    # AL East
    "yankees": {
        "city": "Bronx, NY",
        "lat": 40.8296, "lon": -73.9262,
        "orientation_deg": 307,   # CF faces NW
    },
    "red sox": {
        "city": "Boston, MA",
        "lat": 42.3467, "lon": -71.0972,
        "orientation_deg": 65,    # Fenway CF faces NE
    },
    "rays": {
        "city": "St. Petersburg, FL",
        "lat": 27.7682, "lon": -82.6534,
        "orientation_deg": 340,   # Tropicana Field (dome, weather less relevant)
    },
    "blue jays": {
        "city": "Toronto, ON",
        "lat": 43.6414, "lon": -79.3894,
        "orientation_deg": 0,     # Rogers Centre dome
    },
    "orioles": {
        "city": "Baltimore, MD",
        "lat": 39.2839, "lon": -76.6216,
        "orientation_deg": 60,    # Camden Yards CF NE
    },
    # AL Central
    "guardians": {
        "city": "Cleveland, OH",
        "lat": 41.4962, "lon": -81.6852,
        "orientation_deg": 300,
    },
    "white sox": {
        "city": "Chicago, IL",
        "lat": 41.8299, "lon": -87.6338,
        "orientation_deg": 340,
    },
    "royals": {
        "city": "Kansas City, MO",
        "lat": 39.0517, "lon": -94.4803,
        "orientation_deg": 20,
    },
    "tigers": {
        "city": "Detroit, MI",
        "lat": 42.3390, "lon": -83.0485,
        "orientation_deg": 350,
    },
    "twins": {
        "city": "Minneapolis, MN",
        "lat": 44.9817, "lon": -93.2776,
        "orientation_deg": 0,     # Target Field retractable-style
    },
    # AL West
    "astros": {
        "city": "Houston, TX",
        "lat": 29.7573, "lon": -95.3555,
        "orientation_deg": 330,   # Minute Maid dome
    },
    "athletics": {
        "city": "Oakland, CA",
        "lat": 37.7516, "lon": -122.2005,
        "orientation_deg": 270,   # Oakland Coliseum CF W
    },
    "mariners": {
        "city": "Seattle, WA",
        "lat": 47.5914, "lon": -122.3325,
        "orientation_deg": 330,
    },
    "angels": {
        "city": "Anaheim, CA",
        "lat": 33.8003, "lon": -117.8827,
        "orientation_deg": 300,
    },
    "rangers": {
        "city": "Arlington, TX",
        "lat": 32.7473, "lon": -97.0827,
        "orientation_deg": 345,   # Globe Life Field retractable
    },
    # NL East
    "mets": {
        "city": "New York, NY",
        "lat": 40.7571, "lon": -73.8458,
        "orientation_deg": 55,    # Citi Field CF NE
    },
    "braves": {
        "city": "Cumberland, GA",
        "lat": 33.8907, "lon": -84.4677,
        "orientation_deg": 10,
    },
    "phillies": {
        "city": "Philadelphia, PA",
        "lat": 39.9061, "lon": -75.1665,
        "orientation_deg": 330,
    },
    "marlins": {
        "city": "Miami, FL",
        "lat": 25.7781, "lon": -80.2197,
        "orientation_deg": 0,     # LoanDepot Park retractable
    },
    "nationals": {
        "city": "Washington, DC",
        "lat": 38.8730, "lon": -77.0074,
        "orientation_deg": 45,
    },
    # NL Central
    "cubs": {
        "city": "Chicago, IL",
        "lat": 41.9484, "lon": -87.6553,
        "orientation_deg": 60,    # Wrigley Field CF NE, famous wind
    },
    "brewers": {
        "city": "Milwaukee, WI",
        "lat": 43.0280, "lon": -87.9712,
        "orientation_deg": 340,
    },
    "cardinals": {
        "city": "St. Louis, MO",
        "lat": 38.6226, "lon": -90.1928,
        "orientation_deg": 30,
    },
    "reds": {
        "city": "Cincinnati, OH",
        "lat": 39.0979, "lon": -84.5082,
        "orientation_deg": 315,
    },
    "pirates": {
        "city": "Pittsburgh, PA",
        "lat": 40.4469, "lon": -80.0057,
        "orientation_deg": 35,
    },
    # NL West
    "dodgers": {
        "city": "Los Angeles, CA",
        "lat": 34.0739, "lon": -118.2400,
        "orientation_deg": 350,
    },
    "giants": {
        "city": "San Francisco, CA",
        "lat": 37.7786, "lon": -122.3893,
        "orientation_deg": 110,   # Oracle Park CF SE, famous ocean wind IN
    },
    "padres": {
        "city": "San Diego, CA",
        "lat": 32.7076, "lon": -117.1570,
        "orientation_deg": 300,
    },
    "rockies": {
        "city": "Denver, CO",
        "lat": 39.7559, "lon": -104.9942,
        "orientation_deg": 340,   # Coors Field high altitude boosts offense
    },
    "diamondbacks": {
        "city": "Phoenix, AZ",
        "lat": 33.4453, "lon": -112.0667,
        "orientation_deg": 0,     # Chase Field retractable dome
    },
}

WIND_DIRECTION_LABELS = [
    "N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
    "S", "SSW", "SW", "WSW", "W", "WNW", "NW", "NNW",
]


def _deg_to_label(deg: float) -> str:
    idx = int((deg + 11.25) / 22.5) % 16
    return WIND_DIRECTION_LABELS[idx]


def _load_cache() -> dict:
    try:
        if CACHE_PATH.exists():
            return json.loads(CACHE_PATH.read_text())
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(data))


def get_park_weather(home_team_name: str) -> dict | None:
    """
    Fetch current weather at the home team's ballpark.

    Returns:
      {temp_f, wind_mph, wind_dir_deg, wind_dir_label, condition,
       over_boost, description}
    Returns None if OPENWEATHER_API_KEY not set, team not found, or API error.
    """
    if not OPENWEATHER_API_KEY:
        return None

    # Match team name to park entry (last word, case-insensitive)
    team_key = home_team_name.strip().lower().split()[-1] if home_team_name.strip() else ""

    park = None
    matched_key = None
    for k, v in MLB_PARKS.items():
        if k.split()[-1] == team_key or k == team_key:
            park = v
            matched_key = k
            break

    # Also try multi-word match
    if park is None:
        lower_name = home_team_name.strip().lower()
        for k, v in MLB_PARKS.items():
            if k in lower_name or lower_name in k:
                park = v
                matched_key = k
                break

    if park is None:
        return None

    # Check cache (keyed per park per hour)
    hour_key  = f"{matched_key}_{int(time.time() // CACHE_TTL)}"
    cache     = _load_cache()
    if hour_key in cache:
        return cache[hour_key]

    try:
        resp = requests.get(
            "https://api.openweathermap.org/data/2.5/weather",
            params={
                "lat":   park["lat"],
                "lon":   park["lon"],
                "appid": OPENWEATHER_API_KEY,
                "units": "imperial",
            },
            timeout=10,
        )
        resp.raise_for_status()
        w = resp.json()
    except Exception:
        return None

    temp_f       = w.get("main", {}).get("temp", 72)
    wind_mph     = w.get("wind", {}).get("speed", 0)
    wind_dir_deg = w.get("wind", {}).get("deg", 0)
    condition    = w.get("weather", [{}])[0].get("description", "")

    # Compute wind component toward outfield
    orientation_deg = park.get("orientation_deg", 0)
    angle_diff      = math.radians(wind_dir_deg - orientation_deg)
    wind_component  = math.cos(angle_diff) * wind_mph

    # Determine over_boost
    over_boost = 0.0

    if wind_component > 15:
        over_boost += 0.04
    elif wind_component > 8:
        over_boost += 0.02
    elif wind_component < -15:
        over_boost -= 0.04
    elif wind_component < -8:
        over_boost -= 0.02

    # Temperature adjustments
    if temp_f < 35:
        over_boost -= 0.04  # stacked: -0.02 for <50 and -0.02 for <35
    elif temp_f < 45:
        over_boost -= 0.02

    over_boost = max(-0.05, min(0.05, over_boost))

    # Build direction description
    wind_dir_label = _deg_to_label(wind_dir_deg)
    if wind_component > 3:
        wind_desc = f"Wind {wind_mph:.0f}mph out ({wind_dir_label})"
    elif wind_component < -3:
        wind_desc = f"Wind {wind_mph:.0f}mph in ({wind_dir_label})"
    else:
        wind_desc = f"Wind {wind_mph:.0f}mph across ({wind_dir_label})"

    temp_desc = f"{temp_f:.0f}°F"
    description = f"{wind_desc}, {temp_desc}"
    if condition:
        description += f", {condition}"

    result = {
        "temp_f":         round(temp_f, 1),
        "wind_mph":       round(wind_mph, 1),
        "wind_dir_deg":   round(wind_dir_deg, 1),
        "wind_dir_label": wind_dir_label,
        "wind_component": round(wind_component, 1),
        "condition":      condition,
        "over_boost":     round(over_boost, 3),
        "description":    description,
        "park":           matched_key,
        "city":           park.get("city", ""),
    }

    cache[hour_key] = result
    _save_cache(cache)
    return result
