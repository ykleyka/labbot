import os
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DB_PATH = os.getenv("DB_PATH", "queue_bot.db")
TZ = ZoneInfo(os.getenv("TZ_NAME", "Europe/Moscow"))
DEFAULT_LESSON_MINUTES = int(os.getenv("LESSON_MINUTES", "90"))
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "credentials.json")
