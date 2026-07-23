import os

# Токен бота, полученный от @BotFather
BOT_TOKEN = os.getenv("BOT  _TOKEN", "8691454968:AAEGtXcesWp97_PkFj6rGjBHTgV4pvTVl0s")

# ID администраторов, которые будут получать заявки.
# Узнать свой ID можно у бота @userinfobot
# Можно указать несколько через запятую в переменной окружения: "111111,222222"
_admin_ids_raw = os.getenv("ADMIN_IDS", "7041564361")
ADMIN_IDS = [int(x.strip()) for x in _admin_ids_raw.split(",") if x.strip()]
