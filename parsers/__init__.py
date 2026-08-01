"""
LeadScout AI — Пакет парсеров.
Экспортирует ALL_PARSERS — список инстансов всех подключённых парсеров.
Для добавления нового парсера: создайте класс и добавьте его инстанс в ALL_PARSERS.
"""

from parsers.habr_freelance import HabrFreelanceParser
from parsers.freelancehunt_parser import FreelancehuntParser
from parsers.fl_ru_parser import FlRuParser
from parsers.vollna_parser import VollnaParser
from parsers.oneclancer_parser import OneCLancerParser
from parsers.freelancer_parser import FreelancerParser
from parsers.weblancer_parser import WeblancerParser
from parsers.kwork_parser import KworkParser
from parsers.telegram_parser import TelegramParser

ALL_PARSERS = [
    HabrFreelanceParser(),       # Заглушка (Хабр Фриланс закрыт)
    FreelancehuntParser(),       # REST API (требует токен)
    FlRuParser(),                # RSS (работает)
    VollnaParser(),              # API-агрегатор (заглушка)
    OneCLancerParser(),          # RSS (работает)
    FreelancerParser(),          # Scraper (работает)
    WeblancerParser(),           # Scraper (работает)
    KworkParser(),               # Scraper из stateData (работает)
    TelegramParser(),            # Userbot MTProto (опционально, требует авторизации)
]
