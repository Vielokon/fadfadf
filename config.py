import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
MOD_GROUP_ID = int(os.getenv("MOD_GROUP_ID", "0"))
UNCHECK_CHANNEL_ID = int(os.getenv("UNCHECK_CHANNEL_ID", "0"))
APPROVED_CHANNEL_ID = int(os.getenv("APPROVED_CHANNEL_ID", "0"))

MEDIA_GROUP_WAIT = float(os.getenv("MEDIA_GROUP_WAIT", "1.2"))
STATE_DIR = os.getenv("STATE_DIR", "storage")
STATE_FILE = os.path.join(STATE_DIR, "bot_state.json")

ENABLE_WEATHER = os.getenv("ENABLE_WEATHER", "false").lower() == "true"
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY", "")
WEATHER_LAT = float(os.getenv("WEATHER_LAT", "59.85278"))
WEATHER_LON = float(os.getenv("WEATHER_LON", "30.35667"))
WEATHER_CITY_LABEL = os.getenv("WEATHER_CITY_LABEL", "СПб")
WEATHER_MIN_C = float(os.getenv("WEATHER_MIN_C", "10"))
WEATHER_MAX_C = float(os.getenv("WEATHER_MAX_C", "20"))

# Ежедневные сообщения
DAILY_ENABLE = os.getenv("DAILY_ENABLE", "false").lower() == "true"
DAILY_MORNING = os.getenv("DAILY_MORNING", "06:00")
DAILY_EVENING = os.getenv("DAILY_EVENING", "02:00")

# энергомодель
ENERGY_OVERHEAD = float(os.getenv("ENERGY_OVERHEAD", "0.07"))
ENERGY_ENCRYPTION_OVERHEAD = float(os.getenv("ENERGY_ENCRYPTION_OVERHEAD", "0.02"))
ENERGY_RETRY_RATE = float(os.getenv("ENERGY_RETRY_RATE", "0.01"))

POWER_PROFILES = {
    "wifi":    {"radio_w": 1.6, "cpu_w": 0.7, "tail_s": 0.8,  "capacity_mbps": 120},
    "lte":     {"radio_w": 3.2, "cpu_w": 0.9, "tail_s": 2.0,  "capacity_mbps": 25},
    "5g":      {"radio_w": 4.6, "cpu_w": 1.2, "tail_s": 3.0,  "capacity_mbps": 220},
    "ethernet":{"radio_w": 0.8, "cpu_w": 0.6, "tail_s": 0.3,  "capacity_mbps": 1000},
}
SERVER_NETWORK_W = 6.0
SERVER_SHARE = 0.25

TIMEZONE = os.getenv("TZ", "Europe/Moscow")