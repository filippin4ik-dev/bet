"""Долгоживущие объекты процесса: сканеры и цикл обновления балансов.

Вынесены в отдельный модуль, чтобы до них могли дотянуться и main.py (он
их запускает), и admin_api.py (переключатель лайва читает и меняет
состояние лайв-сканера) — без кругового импорта.

Два независимых сканера: прематч — основной режим, лайв — опциональный,
включается и выключается на ходу из админки (см. Scanner.enabled).
"""
from . import accounts_manager
from .models import KIND_LIVE, KIND_PREMATCH
from .scanner import Scanner

scanner = Scanner(mode=KIND_PREMATCH)
live_scanner = Scanner(mode=KIND_LIVE)
balance_loop = accounts_manager.BalanceRefreshLoop()
