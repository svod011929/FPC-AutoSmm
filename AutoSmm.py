"""
AutoSmm Plugin v1.5 - Улучшенная версия
Плагин автонакрутки для FunPay Cardinal с улучшенной надежностью

Основные улучшения:
- Валидация всех входных данных
- Улучшенная обработка ошибок
- Потокобезопасная работа с файлами
- Кэширование настроек
- Подробное логирование
- Retry механизм для API
- Защита от race conditions
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional, List, Dict, Tuple, Any
import requests
import telebot
from telebot import types
from telebot.types import InlineKeyboardMarkup, InlineKeyboardButton

from cardinal import Cardinal
if TYPE_CHECKING:
    from cardinal import Cardinal

from FunPayAPI.updater.events import *
from FunPayAPI.types import MessageTypes
from locales.localizer import Localizer
from tg_bot.utils import load_authorized_users

# ====================
# КОНСТАНТЫ И НАСТРОЙКИ
# ====================

pending_confirmations = {}
logger = logging.getLogger("FPC.AutoSmm")
localizer = Localizer()
_ = localizer.translate

LOGGER_PREFIX = "AutoSmm Plugin"
NAME = "AUTOSMM"
VERSION = "1.5.0"
CREDITS = "@klaymov (improved)"
UUID = "7aa412ab-0840-455d-9513-6f51bf83d43b"
SETTINGS_PAGE = False
DESCRIPTION = "Улучшенный плагин автонакрутки с валидацией и защитой от ошибок"

# Пути к файлам
STORAGE_PATH = f"storage/plugins/{UUID}"
ORDERS_FILE = f"{STORAGE_PATH}/orders.json"
PAYORDERS_FILE = f"{STORAGE_PATH}/payorders.json"
SETTINGS_FILE = f"{STORAGE_PATH}/settings.json"
CASHLIST_FILE = f"{STORAGE_PATH}/cashlist.json"
REFILL_FILE = f"{STORAGE_PATH}/refill.json"

# Настройки по умолчанию
DEFAULT_SETTINGS = {
    "api_url": "",
    "api_key": "",
    "api_url_2": "",
    "api_key_2": "",
    "set_alert_neworder": True,
    "set_alert_errororder": True,
    "set_alert_smmbalance_new": False,
    "set_alert_smmbalance": True,
    "set_refund_smm": True,
    "set_start_mess": True,
    "set_auto_refill": False,
    "set_tg_private": False,
    "set_recreated_order": False,
    "api_timeout": 30,
    "check_interval": 60,
    "max_retries": 3
}

# ====================
# УТИЛИТЫ И ВАЛИДАТОРЫ
# ====================

class FileLocker:
    """Потокобезопасная работа с файлами"""
    _locks = {
        'orders': threading.Lock(),
        'payorders': threading.Lock(),
        'settings': threading.Lock(),
        'cashlist': threading.Lock(),
        'refill': threading.Lock()
    }
    
    @classmethod
    def get_lock(cls, file_type: str) -> threading.Lock:
        return cls._locks.get(file_type, threading.Lock())


class Validator:
    """Валидаторы для входных данных"""
    
    @staticmethod
    def validate_url(url: str) -> Tuple[bool, Optional[str]]:
        """Проверка URL"""
        if not url or not isinstance(url, str):
            return False, "URL не может быть пустым"
        
        url = url.strip()
        url_pattern = re.compile(
            r'^https?://'
            r'(?:(?:[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?\.)+[A-Z]{2,6}\.?|'
            r'localhost|'
            r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})'
            r'(?::\d+)?'
            r'(?:/?|[/?]\S+)$',
            re.IGNORECASE
        )
        
        if not url_pattern.match(url):
            return False, "Некорректный формат URL"
        
        return True, None
    
    @staticmethod
    def validate_api_key(api_key: str) -> Tuple[bool, Optional[str]]:
        """Проверка API ключа"""
        if not api_key or not isinstance(api_key, str):
            return False, "API ключ не может быть пустым"
        
        api_key = api_key.strip()
        
        if len(api_key) < 10:
            return False, "API ключ слишком короткий"
        
        if not re.match(r'^[a-zA-Z0-9_-]+$', api_key):
            return False, "API ключ содержит недопустимые символы"
        
        return True, None
    
    @staticmethod
    def validate_service_id(service_id: Any) -> Tuple[bool, Optional[str]]:
        """Проверка ID сервиса"""
        try:
            sid = int(service_id)
            if sid <= 0:
                return False, "ID сервиса должен быть положительным"
            if sid > 999999:
                return False, "ID сервиса слишком большой"
            return True, None
        except (ValueError, TypeError):
            return False, "ID сервиса должен быть числом"
    
    @staticmethod
    def validate_quantity(quantity: Any) -> Tuple[bool, Optional[str]]:
        """Проверка количества"""
        try:
            qty = int(quantity)
            if qty <= 0:
                return False, "Количество должно быть больше нуля"
            if qty > 10000000:
                return False, "Количество слишком большое"
            return True, None
        except (ValueError, TypeError):
            return False, "Количество должно быть числом"


class SettingsCache:
    """Кэш настроек для избежания частого чтения файла"""
    _cache = None
    _last_update = 0
    _cache_ttl = 60  # секунды
    _lock = threading.Lock()
    
    @classmethod
    def get_settings(cls) -> Dict:
        """Получить настройки с кэшированием"""
        with cls._lock:
            current_time = time.time()
            if cls._cache is None or (current_time - cls._last_update) > cls._cache_ttl:
                cls._cache = load_settings()
                cls._last_update = current_time
            return cls._cache.copy()
    
    @classmethod
    def invalidate(cls):
        """Сбросить кэш"""
        with cls._lock:
            cls._cache = None
            cls._last_update = 0


# ====================
# РАБОТА С ФАЙЛАМИ (улучшенная)
# ====================

def ensure_storage_exists():
    """Создание директории хранилища"""
    try:
        os.makedirs(STORAGE_PATH, exist_ok=True)
    except Exception as e:
        logger.error(f"Не удалось создать директорию хранилища: {e}")


def load_json_safe(filepath: str, default: Any, lock_type: str) -> Any:
    """Безопасная загрузка JSON с блокировкой"""
    with FileLocker.get_lock(lock_type):
        if not os.path.exists(filepath):
            return default
        
        try:
            with open(filepath, "r", encoding='utf-8') as file:
                return json.load(file)
        except json.JSONDecodeError as e:
            logger.error(f"Ошибка декодирования JSON из {filepath}: {e}")
            # Создаем бэкап поврежденного файла
            try:
                backup_path = f"{filepath}.corrupted_{int(time.time())}"
                os.rename(filepath, backup_path)
                logger.warning(f"Поврежденный файл сохранен как {backup_path}")
            except:
                pass
            return default
        except Exception as e:
            logger.error(f"Ошибка чтения {filepath}: {e}")
            return default


def save_json_safe(filepath: str, data: Any, lock_type: str) -> bool:
    """Безопасное сохранение JSON с блокировкой и атомарной записью"""
    with FileLocker.get_lock(lock_type):
        ensure_storage_exists()
        temp_filepath = f"{filepath}.tmp"
        
        try:
            # Запись во временный файл
            with open(temp_filepath, "w", encoding='utf-8') as file:
                json.dump(data, file, indent=4, ensure_ascii=False)
            
            # Атомарная замена
            os.replace(temp_filepath, filepath)
            return True
        except Exception as e:
            logger.error(f"Ошибка сохранения {filepath}: {e}")
            # Удаляем временный файл при ошибке
            if os.path.exists(temp_filepath):
                try:
                    os.remove(temp_filepath)
                except:
                    pass
            return False


def load_orders() -> dict:
    """Загрузка заказов"""
    return load_json_safe(ORDERS_FILE, {}, 'orders')


def save_orders(orders: dict) -> bool:
    """Сохранение заказов"""
    return save_json_safe(ORDERS_FILE, orders, 'orders')


def load_payorders() -> List[Dict]:
    """Загрузка оплаченных заказов"""
    return load_json_safe(PAYORDERS_FILE, [], 'payorders')


def save_payorders(orders: List[Dict]) -> bool:
    """Сохранение оплаченных заказов"""
    return save_json_safe(PAYORDERS_FILE, orders, 'payorders')


def load_cashlist() -> dict:
    """Загрузка кэшлиста"""
    return load_json_safe(CASHLIST_FILE, {}, 'cashlist')


def save_cashlist(orders: dict) -> bool:
    """Сохранение кэшлиста"""
    return save_json_safe(CASHLIST_FILE, orders, 'cashlist')


def load_refill() -> dict:
    """Загрузка рефиллов"""
    return load_json_safe(REFILL_FILE, {}, 'refill')


def save_refill(orders: dict) -> bool:
    """Сохранение рефиллов"""
    return save_json_safe(REFILL_FILE, orders, 'refill')


def load_settings() -> dict:
    """Загрузка настроек"""
    settings = load_json_safe(SETTINGS_FILE, None, 'settings')
    if settings is None:
        settings = DEFAULT_SETTINGS.copy()
        save_settings(settings)
    else:
        # Добавляем новые настройки если их нет
        updated = False
        for key, value in DEFAULT_SETTINGS.items():
            if key not in settings:
                settings[key] = value
                updated = True
        if updated:
            save_settings(settings)
    
    return settings


def save_settings(settings: dict) -> bool:
    """Сохранение настроек"""
    result = save_json_safe(SETTINGS_FILE, settings, 'settings')
    if result:
        SettingsCache.invalidate()
    return result


def get_api_url(type_api=None) -> str:
    """Получить API URL с валидацией"""
    settings = SettingsCache.get_settings()
    key = "api_url_2" if type_api else "api_url"
    url = settings.get(key, "")
    
    if url:
        is_valid, error = Validator.validate_url(url)
        if not is_valid:
            logger.warning(f"Некорректный {key}: {error}")
            return ""
    
    return url


def get_api_key(type_api=None) -> str:
    """Получить API ключ с валидацией"""
    settings = SettingsCache.get_settings()
    key = "api_key_2" if type_api else "api_key"
    api_key = settings.get(key, "")
    
    if api_key:
        is_valid, error = Validator.validate_api_key(api_key)
        if not is_valid:
            logger.warning(f"Некорректный {key}: {error}")
            return ""
    
    return api_key


# ====================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ====================

def extract_links(text: str) -> List[str]:
    """Извлечение ссылок из текста"""
    if not text:
        return []
    
    link_pattern = r'http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\\(\\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+'
    links = re.findall(link_pattern, text)
    return links


def find_order_by_buyer(orders: List[Dict], buyer: str) -> Optional[Dict]:
    """Поиск заказа по имени покупателя"""
    if not buyer or not orders:
        return None
    
    for order in orders:
        if order.get('buyer') == buyer:
            return order
    return None


def validate_telegram_link(link: str, allow_private: bool = False) -> Tuple[bool, Optional[str]]:
    """Валидация Telegram ссылки"""
    if not link:
        return False, "Ссылка не может быть пустой"
    
    if "t.me" not in link.lower() and "telegram.me" not in link.lower():
        return True, None  # Не Telegram ссылка
    
    if not allow_private and ("/c/" in link or "+" in link):
        return False, "Закрытые каналы/группы не поддерживаются"
    
    return True, None


# ====================
# SMM API КЛИЕНТ (улучшенный)
# ====================

class SocTypeAPI:
    """Клиент для работы с SMM API с retry механизмом"""
    
    @staticmethod
    def _make_request_with_retry(url: str, max_retries: int = 3, timeout: int = 30) -> Optional[Dict]:
        """HTTP запрос с повторными попытками"""
        for attempt in range(max_retries):
            try:
                response = requests.get(url, timeout=timeout)
                response.raise_for_status()
                return response.json()
            except requests.exceptions.Timeout:
                logger.warning(f"Timeout при запросе (попытка {attempt + 1}/{max_retries})")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)  # Экспоненциальная задержка
                continue
            except requests.exceptions.RequestException as e:
                logger.error(f"Ошибка HTTP запроса: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                continue
            except json.JSONDecodeError as e:
                logger.error(f"Ошибка декодирования JSON: {e}")
                return None
        
        return None
    
    @staticmethod
    def create_order(service_id: int, link: str, quantity: int, api_url: str, api_key: str):
        """Создание заказа с валидацией"""
        # Валидация входных данных
        is_valid, error = Validator.validate_service_id(service_id)
        if not is_valid:
            return f"Ошибка валидации service_id: {error}"
        
        is_valid, error = Validator.validate_quantity(quantity)
        if not is_valid:
            return f"Ошибка валидации quantity: {error}"
        
        if not link:
            return "Ошибка: ссылка не может быть пустой"
        
        # Формирование URL
        try:
            url = f"{api_url}?action=add&service={service_id}&link={link}&quantity={quantity}&key={api_key}"
            logger.info(f"Создание заказа: service={service_id}, quantity={quantity}")
            
            response = SocTypeAPI._make_request_with_retry(url)
            
            if not response:
                return "Ошибка подключения к API"
            
            if "order" in response:
                logger.info(f"Заказ создан успешно: {response['order']}")
                return response["order"]
            elif "error" in response:
                logger.error(f"API вернул ошибку: {response['error']}")
                return response["error"]
            else:
                logger.error(f"Неожиданный ответ API: {response}")
                return "Неизвестная ошибка API"
                
        except Exception as e:
            logger.error(f"Исключение при создании заказа: {e}", exc_info=True)
            return f"Ошибка: {str(e)}"
    
    @staticmethod
    def get_order_status(order_id: int, api_url: str, api_key: str) -> Optional[dict]:
        """Получение статуса заказа"""
        try:
            url = f"{api_url}?action=status&order={order_id}&key={api_key}"
            response = SocTypeAPI._make_request_with_retry(url)
            
            if response and "error" not in response:
                return response
            else:
                logger.warning(f"Ошибка получения статуса заказа {order_id}")
                return None
                
        except Exception as e:
            logger.error(f"Исключение при получении статуса: {e}")
            return None
    
    @staticmethod
    def refill_order(order_id: int, api_url: str, api_key: str) -> Optional[str]:
        """Рефилл заказа"""
        try:
            url = f"{api_url}?action=refill&order={order_id}&key={api_key}"
            response = SocTypeAPI._make_request_with_retry(url)
            
            if response and "refill" in response:
                return response["refill"]
            return None
            
        except Exception as e:
            logger.error(f"Ошибка рефилла: {e}")
            return None
    
    @staticmethod
    def get_balance(api_url: str, api_key: str) -> Tuple[Optional[float], Optional[str]]:
        """Получение баланса"""
        try:
            url = f"{api_url}?action=balance&key={api_key}"
            response = SocTypeAPI._make_request_with_retry(url)
            
            if not response:
                return None, None
            
            balance_str = response.get('balance', '')
            pattern = r'\d+\.\d+'
            match = re.search(pattern, str(balance_str))
            
            if match:
                balance = float(match.group())
                currency = response.get('currency', 'USD')
                return balance, currency
            
            return None, None
            
        except Exception as e:
            logger.error(f"Ошибка получения баланса: {e}")
            return None, None
    
    @staticmethod
    def cancel_order(order_id: int, api_url: str, api_key: str) -> Optional[str]:
        """Отмена заказа"""
        try:
            url = f"{api_url}?action=cancel&order={order_id}&key={api_key}"
            response = SocTypeAPI._make_request_with_retry(url)
            
            if response and "cancel" in response:
                return response["cancel"]
            return None
            
        except Exception as e:
            logger.error(f"Ошибка отмены: {e}")
            return None


# ====================
# ОБРАБОТЧИКИ СОБЫТИЙ
# ====================

def bind_to_new_order(c: Cardinal, e: NewOrderEvent) -> None:
    """Обработка нового заказа"""
    try:
        _element_data = e.order
        _order_id = _element_data.id
        
        logger.info(f"Получен новый заказ #{_order_id}")
        
        # Получаем полные данные заказа
        try:
            _element_full_data = c.account.get_order(_order_id)
            _full_disc = _element_full_data.full_description
            _buyer_uz = _element_full_data.buyer_username
        except Exception as ex:
            logger.error(f"Не удалось получить данные заказа #{_order_id}: {ex}")
            return
        
        # Уведомление о балансе (если включено)
        settings = SettingsCache.get_settings()
        if settings.get("set_alert_smmbalance_new", False):
            try:
                send_smm_balance_info(c)
            except Exception as ex:
                logger.error(f"Ошибка отправки баланса: {ex}")
        
        # Извлечение параметров из описания
        match_id = re.search(r'ID:\s*(\d+)', _full_disc)
        match_oid = re.search(r'ID2:\s*(\d+)', _full_disc)
        match_quan = re.search(r'#Quan:\s*(\d+)', _full_disc)
        
        if match_id:
            id_value = match_id.group(1)
            quan_value = int(match_quan.group(1)) if match_quan else 1
            
            # Валидация параметров
            is_valid, error = Validator.validate_service_id(id_value)
            if not is_valid:
                logger.error(f"Некорректный service_id в заказе #{_order_id}: {error}")
                return
            
            is_valid, error = Validator.validate_quantity(quan_value)
            if not is_valid:
                logger.error(f"Некорректный quantity в заказе #{_order_id}: {error}")
                return
            
            order_handler(c, e, id_value, quan_value, _buyer_uz, 'API_1')
            
        elif match_oid:
            id_value = match_oid.group(1)
            quan_value = int(match_quan.group(1)) if match_quan else 1
            
            # Валидация параметров
            is_valid, error = Validator.validate_service_id(id_value)
            if not is_valid:
                logger.error(f"Некорректный service_id в заказе #{_order_id}: {error}")
                return
            
            order_handler(c, e, id_value, quan_value, _buyer_uz, 'API_2')
        else:
            logger.info(f"Заказ #{_order_id} не предназначен для автонакрутки")
            
    except Exception as ex:
        logger.error(f"Критическая ошибка в bind_to_new_order: {ex}", exc_info=True)


def order_handler(c: Cardinal, e: NewOrderEvent, id_value: str, quan_value: int, buyer_uz: str, type_api: str = 'API_1') -> None:
    """Обработчик заказа"""
    try:
        orders_data = load_payorders()
        order_ = e.order
        orderID = order_.id
        orderAmount = order_.amount * quan_value
        orderPrice = order_.price
        orderCurrency = order_.currency
        url = ""
        
        current_datetime = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        
        current_order_data = {
            'OrderID': str(orderID),
            'Amount': int(orderAmount),
            'OrderPrice': orderPrice,
            'OrderCurrency': f"{orderCurrency}",
            'Order': f"{str(order_)}",
            'service_id': int(id_value),
            'buyer': str(buyer_uz),
            'url': str(url),
            'NewUser': True,
            'chat_id': "",
            'OrderDateTime': current_datetime,
            'api_type': type_api
        }
        
        orders_data.append(current_order_data)
        
        if save_payorders(orders_data):
            logger.info(f"Заказ #{orderID} добавлен в список обработки")
            handle_order(c, current_order_data, [])
        else:
            logger.error(f"Не удалось сохранить заказ #{orderID}")
            
    except Exception as ex:
        logger.error(f"Ошибка в order_handler: {ex}", exc_info=True)


logger.info(f"$MAGENTA{LOGGER_PREFIX} v{VERSION} успешно запущен.$RESET")


def msg_hook(c: Cardinal, e: NewMessageEvent) -> None:
    """Обработка сообщений"""
    try:
        orders_data = load_payorders()
        
        msg = e.message
        msgname = msg.chat_name
        message_text = msg.text.strip() if msg.text else ""
        
        # Проверка системных сообщений
        if msg.type != MessageTypes.NON_SYSTEM:
            return
        
        # Игнорируем свои сообщения
        if msg.author_id == c.account.id:
            return
        
        # Проверка на возврат средств
        if "вернул деньги покупателю" in message_text:
            order = find_order_by_buyer(orders_data, msgname)
            if order:
                try:
                    orders_data.remove(order)
                    save_payorders(orders_data)
                    logger.info(f"Заказ отменен: {order.get('OrderID')}")
                except Exception as e:
                    logger.error(f"Ошибка при возврате: {e}")
            return
        
        # Поиск заказа по покупателю
        order = find_order_by_buyer(orders_data, msgname)
        
        # Получаем API данные
        try:
            api_key = get_api_key() if order and order.get('api_type') == 'API_1' else get_api_key(order.get('api_type') if order else None)
            api_url = get_api_url() if order and order.get('api_type') == 'API_1' else get_api_url(order.get('api_type') if order else None)
        except Exception as e:
            logger.error(f"Ошибка получения API данных: {e}")
            api_key = get_api_key()
            api_url = get_api_url()
        
        # Обработка подтверждения
        if msg.chat_id in pending_confirmations:
            if message_text in ["+", "-"]:
                confirm_order(c, msg.chat_id, message_text, api_url, api_key)
            elif "http" in message_text:
                order = pending_confirmations.get(msg.chat_id)
                if order:
                    order['chat_id'] = msg.chat_id
                    links = extract_links(message_text)
                    handle_order(c, order, links)
            else:
                c.send_message(msg.chat_id, "⚪️ Пожалуйста, отправьте +, если всё верно, или -, для возврата средств.")
            return
        
        # Обработка заказа от покупателя
        if order:
            logger.info(f"Обработка сообщения от {msgname} для заказа #{order.get('OrderID')}")
            order['chat_id'] = msg.chat_id
            links = extract_links(message_text)
            handle_order(c, order, links)
        else:
            logger.debug(f"Пользователь {msgname} не имеет активных заказов")
        
        # Команды проверки статуса
        command_parts = message_text.split()
        
        if len(command_parts) >= 2 and command_parts[0] == "#статус":
            try:
                smm_order_id = command_parts[1]
                status = SocTypeAPI.get_order_status(int(smm_order_id), api_url, api_key)
                if status:
                    start_count = status.get('start_count', 0)
                    display_start_count = "*" if start_count == 0 else str(start_count)
                    
                    status_text = f"📈 Статус заказа: {smm_order_id}\n"
                    status_text += f"⠀∟📊 Статус: {status.get('status', 'Unknown')}\n"
                    status_text += f"⠀∟🔢 Было: {display_start_count}\n"
                    status_text += f"⠀∟👀 Остаток выполнения: {status.get('remains', 'N/A')}"
                    c.send_message(msg.chat_id, status_text)
                else:
                    c.send_message(msg.chat_id, "🔴 Не удалось получить статус заказа.")
            except Exception as e:
                logger.error(f"Ошибка получения статуса: {e}")
                c.send_message(msg.chat_id, "🔴 Ошибка при получении статуса.")
        
        elif len(command_parts) >= 2 and command_parts[0] == "#инфо":
            try:
                smm_order_id = command_parts[1]
                status = SocTypeAPI.get_order_status(int(smm_order_id), get_api_url('API_2'), get_api_key('API_2'))
                if status:
                    start_count = status.get('start_count', 0)
                    display_start_count = "*" if start_count == 0 else str(start_count)
                    
                    status_text = f"📈 Статус заказа: {smm_order_id}\n"
                    status_text += f"⠀∟📊 Статус: {status.get('status', 'Unknown')}\n"
                    status_text += f"⠀∟🔢 Было: {display_start_count}\n"
                    status_text += f"⠀∟👀 Остаток выполнения: {status.get('remains', 'N/A')}"
                    c.send_message(msg.chat_id, status_text)
                else:
                    c.send_message(msg.chat_id, "🔴 Не удалось получить статус заказа.")
            except Exception as e:
                logger.error(f"Ошибка получения статуса (API 2): {e}")
                c.send_message(msg.chat_id, "🔴 Ошибка при получении статуса.")
        
        elif len(command_parts) >= 2 and command_parts[0] == "#рефилл":
            try:
                smm_order_id = command_parts[1]
                refill_result = SocTypeAPI.refill_order(int(smm_order_id), api_url, api_key)
                if refill_result is not None:
                    c.send_message(msg.chat_id, f"✅ Запрос на рефилл отправлен!")
                else:
                    c.send_message(msg.chat_id, f"🔴 Ошибка при выполнении рефилла.\n⚠️ Возможно, рефилл еще недоступен!")
            except Exception as e:
                logger.error(f"Ошибка рефилла: {e}")
                c.send_message(msg.chat_id, "🔴 Ошибка при выполнении рефилла.")
                
    except Exception as ex:
        logger.error(f"Критическая ошибка в msg_hook: {ex}", exc_info=True)


def handle_order(c: Cardinal, order: Dict, links: List[str]) -> None:
    """Обработка заказа с ссылкой"""
    try:
        settings = SettingsCache.get_settings()
        
        if links:
            link = links[0]
            orders_data = load_payorders()
            
            # Валидация Telegram ссылки
            allow_private = settings.get("set_tg_private", False)
            is_valid, error = validate_telegram_link(link, allow_private)
            
            if not is_valid:
                c.send_message(order['chat_id'], f"❌ {error}")
                return
            
            order['url'] = link
            link_display = link.replace("https://", "").replace("http://", "")
            
            confirmation_text = f"""📋 Пожалуйста, проверьте детали вашего заказа:
🛒 Лот: {order.get('Order', 'N/A')}
🔢 Количество: {order.get('Amount', 'N/A')} шт
🔗 Ссылка: {link_display}

✅ Если всё верно, отправьте: +
❌ Для возврата средств, отправьте: -
🔄 Или отправьте новую ссылку для обновления."""
            
            c.send_message(order['chat_id'], confirmation_text)
            pending_confirmations[order['chat_id']] = order
            
            # Обновляем заказ в списке
            existing_order = next((o for o in orders_data if o.get('OrderID') == order.get('OrderID')), None)
            
            if existing_order:
                existing_order.update(order)
            else:
                orders_data.append(order)
            
            save_payorders(orders_data)
            logger.info(f"Заказ #{order.get('OrderID')} обновлен с URL")
            
    except Exception as ex:
        logger.error(f"Ошибка в handle_order: {ex}", exc_info=True)


def confirm_order(c: Cardinal, chat_id: int, text: str, api_url: str, api_key: str) -> None:
    """Подтверждение заказа"""
    try:
        orders_data = load_payorders()
        settings = SettingsCache.get_settings()
        
        if chat_id not in pending_confirmations:
            logger.warning(f"Подтверждение для несуществующего заказа (chat_id: {chat_id})")
            return
        
        order = pending_confirmations.pop(chat_id)
        
        if text.strip() == "+":
            logger.info(f"Создание заказа в SMM для #{order.get('OrderID')}")
            
            try:
                smm_order_id = SocTypeAPI.create_order(
                    order['service_id'],
                    order['url'],
                    order['Amount'],
                    api_url,
                    api_key
                )
            except Exception as e:
                logger.error(f"Исключение при создании заказа в SMM: {e}", exc_info=True)
                smm_order_id = f"Ошибка: {str(e)}"
            
            # Проверка успешности создания
            if isinstance(smm_order_id, (int, str)) and str(smm_order_id).isdigit():
                try:
                    orders = load_orders()
                    orders[str(smm_order_id)] = {
                        "service_id": order['service_id'],
                        "chat_id": order['chat_id'],
                        "order_id": order['OrderID'],
                        "order_url": order['url'],
                        "order_amount": order['Amount'],
                        "partial_amount": 0,
                        "orderdatetime": order['OrderDateTime'],
                        "status": "pending"
                    }
                    save_orders(orders)
                    
                    # Уведомление об успехе
                    if settings.get("set_alert_neworder", False):
                        try:
                            send_order_info(c, order, int(smm_order_id), api_url, api_key)
                        except Exception as e:
                            logger.error(f"Ошибка отправки уведомления: {e}")
                    
                    status_cmd = 'статус' if order.get('api_type') == 'API_1' else 'инфо'
                    success_message = f"""📊 Ваш заказ СОЗДАН и отправлен SMM сервису!
🆔 ID заказа: {smm_order_id}

📋 Доступные команды:
⠀∟📗 Узнать статус заказа: #{status_cmd} {smm_order_id}
⠀∟📙 Рефилл (если доступно): #рефилл {smm_order_id}

⌛ Время выполнения: от нескольких минут до 48 часов. В редких случаях возможны задержки."""
                    
                    c.send_message(order['chat_id'], success_message)
                    logger.info(f"Заказ #{order.get('OrderID')} успешно создан в SMM: {smm_order_id}")
                    
                except Exception as e:
                    logger.error(f"Ошибка сохранения заказа: {e}", exc_info=True)
            else:
                # Ошибка создания заказа
                error_message = f"❌ Ошибка при создании заказа: {smm_order_id}"
                c.send_message(order['chat_id'], error_message)
                logger.error(f"Не удалось создать заказ #{order.get('OrderID')}: {smm_order_id}")
                
                # Уведомление об ошибке
                if settings.get("set_alert_errororder", False):
                    try:
                        send_order_error_info(c, smm_order_id, order)
                    except Exception as e:
                        logger.error(f"Ошибка отправки уведомления об ошибке: {e}")
                
                # Уведомление о балансе
                if settings.get("set_alert_smmbalance", False):
                    try:
                        send_smm_balance_info(c)
                    except Exception as e:
                        logger.error(f"Ошибка отправки баланса: {e}")
                
                # Автовозврат
                if settings.get("set_refund_smm", False):
                    try:
                        c.account.refund(order['OrderID'])
                        logger.info(f"Выполнен автовозврат для заказа #{order.get('OrderID')}")
                    except Exception as e:
                        logger.error(f"Ошибка автовозврата: {e}")
        
        elif text.strip() == "-":
            c.send_message(chat_id, "❌ Заказ отменен.\n")
            logger.info(f"Заказ #{order.get('OrderID')} отменен пользователем")
            
            try:
                c.account.refund(order['OrderID'])
            except Exception as e:
                logger.error(f"Ошибка возврата средств: {e}")
            
            try:
                orders_data.remove(order)
                save_payorders(orders_data)
            except Exception as e:
                logger.error(f"Ошибка удаления заказа из списка: {e}")
                
    except Exception as ex:
        logger.error(f"Критическая ошибка в confirm_order: {ex}", exc_info=True)


# ====================
# УВЕДОМЛЕНИЯ В TELEGRAM
# ====================

def send_order_info(c: Cardinal, order: Dict, smm_order_id: int, api_url: str, api_key: str) -> None:
    """Уведомление о новом заказе"""
    try:
        def getcoingaterate(fromcurrency='USD', tocurrency='RUB'):
            try:
                url = f'https://api.coingate.com/v2/rates/merchant/{fromcurrency}/{tocurrency}'
                response = requests.get(url, timeout=10)
                response.raise_for_status()
                return float(response.text)
            except Exception as e:
                logger.error(f"Ошибка получения курса валют: {e}")
                return 1.0
        
        fp_balance = c.get_balance()
        
        # Получение данных о заказе из SMM
        status_info = SocTypeAPI.get_order_status(smm_order_id, api_url, api_key)
        if not status_info:
            logger.warning(f"Не удалось получить данные заказа {smm_order_id} для уведомления")
            return
        
        price_smm_order = float(status_info.get('charge', 0))
        currency = status_info.get('currency', 'USD')
        
        # Получение баланса SMM
        smm_balance_info = SocTypeAPI.get_balance(api_url, api_key)
        balance, smm_currency = smm_balance_info if smm_balance_info else (0, currency)
        
        # Конвертация валют
        fp_currency = order.get('OrderCurrency', '₽')
        if fp_currency == '₽':
            if currency == 'USD':
                price_smm_order = price_smm_order * getcoingaterate('USD', 'RUB')
        elif fp_currency == '$':
            if currency == 'RUB':
                price_smm_order = price_smm_order * getcoingaterate('RUB', 'USD')
        
        # Расчет прибыли
        sum_order = float(order.get('OrderPrice', 0)) - price_smm_order
        sum_order_6com = sum_order * 0.94
        sum_order_3com = sum_order * 0.97
        
        order_info = (
            f"✅ Создан заказ `{NAME}`: `{order.get('Order', 'N/A')}`\n\n"
            f"🙍‍♂️ Покупатель: `{order.get('buyer', 'N/A')}`\n\n"
            f"💵 Сумма заказа: `{order.get('OrderPrice', 0)} {fp_currency}`\n"
            f"💵 Потрачено: `{price_smm_order:.2f} {currency}`\n"
            f"💵 Прибыль: `{sum_order:.2f}`\n"
            f"💵 Прибыль с комиссией: `{sum_order_6com:.2f} (6%) / {sum_order_3com:.2f} (3%)`\n"
            f"💰 Остаток на балансе: `{balance:.2f} {smm_currency}`\n"
            f"💰 Баланс на FunPay: `{fp_balance.total_rub}₽, {fp_balance.available_usd}$, {fp_balance.total_eur}€`\n\n"
            f"📇 ID заказа на FunPay: `{order.get('OrderID', 'N/A')}`\n"
            f"🆔 ID заказа на сайте: `{smm_order_id}`\n"
            f"🔍 Сервис ID: `{order.get('service_id', 'N/A')}`\n"
            f"🔢 Кол-во: `{order.get('Amount', 'N/A')}`\n"
            f"🔗 Ссылка: {order.get('url', 'N/A').replace('https://', '').replace('http://', '')}\n\n"
        )
        
        button = InlineKeyboardButton(
            text="🌐 Открыть страницу заказа",
            url=f"https://funpay.com/orders/{order.get('OrderID')}/"
        )
        keyboard = InlineKeyboardMarkup().add(button)
        
        users = load_authorized_users()
        if not users:
            logger.warning("Нет авторизованных пользователей для уведомления")
            return
        
        for user_id in users:
            try:
                c.telegram.bot.send_message(
                    user_id,
                    order_info,
                    parse_mode='Markdown',
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления пользователю {user_id}: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в send_order_info: {e}", exc_info=True)


def send_order_error_info(c: Cardinal, text: str, order: Dict) -> None:
    """Уведомление об ошибке"""
    try:
        error_text = (
            f"❌ Ошибка при создании заказа `{NAME} #{order.get('OrderID')}`: `{text}`\n\n"
        )
        
        button = InlineKeyboardButton(
            text="🌐 Открыть страницу заказа",
            url=f"https://funpay.com/orders/{order.get('OrderID')}/"
        )
        keyboard = InlineKeyboardMarkup().add(button)
        
        users = load_authorized_users()
        if not users:
            return
        
        for user_id in users:
            try:
                c.telegram.bot.send_message(
                    user_id,
                    error_text,
                    parse_mode='Markdown',
                    reply_markup=keyboard,
                    disable_web_page_preview=True
                )
            except Exception as e:
                logger.error(f"Ошибка отправки уведомления об ошибке: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в send_order_error_info: {e}")


def send_smm_balance_info(c: Cardinal) -> None:
    """Уведомление о балансе"""
    try:
        fp_balance = c.get_balance()
        
        # Пытаемся получить баланс первого API
        api_url = get_api_url()
        api_key = get_api_key()
        smm_balance_info = SocTypeAPI.get_balance(api_url, api_key)
        balance, currency = smm_balance_info if smm_balance_info else (0, 'N/A')
        
        # Пытаемся получить баланс второго API
        api_url_2 = get_api_url('2')
        api_key_2 = get_api_key('2')
        
        if api_url_2 and api_key_2:
            smm_balance_info_2 = SocTypeAPI.get_balance(api_url_2, api_key_2)
            balance_2, currency_2 = smm_balance_info_2 if smm_balance_info_2 else (0, 'N/A')
            
            text_balance = (
                f"💰 Баланс {api_url.replace('https://', '').replace('/api/v2/', '').replace('/api/v2', '')}: `{balance:.2f} {currency}`\n"
                f"💰 Баланс {api_url_2.replace('https://', '').replace('/api/v2/', '').replace('/api/v2', '')}: `{balance_2:.2f} {currency_2}`\n"
                f"💰 Баланс на FunPay: `{fp_balance.total_rub}₽, {fp_balance.available_usd}$, {fp_balance.total_eur}€`"
            )
        else:
            text_balance = (
                f"💰 Баланс сайта: `{balance:.2f} {currency}`\n"
                f"💰 Баланс на FunPay: `{fp_balance.total_rub}₽, {fp_balance.available_usd}$, {fp_balance.total_eur}€`"
            )
        
        users = load_authorized_users()
        if not users:
            return
        
        for user_id in users:
            try:
                c.telegram.bot.send_message(
                    user_id,
                    text_balance,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Ошибка отправки баланса: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в send_smm_balance_info: {e}")


def send_smm_start_info(c: Cardinal) -> None:
    """Уведомление при старте"""
    try:
        text_start = (
            f"✅ Авто-накрутка инициализирована!\n\n"
            f"ℹ️ Версия: `{VERSION}`\n"
            f"⚙️ Настройки: /autosmm\n\n"
            f"*ℹ️ Авто-накрутка by @klaymov (improved)*"
        )
        
        users = load_authorized_users()
        if not users:
            return
        
        for user_id in users:
            try:
                c.telegram.bot.send_message(
                    user_id,
                    text_start,
                    parse_mode='Markdown'
                )
            except Exception as e:
                logger.error(f"Ошибка отправки стартового сообщения: {e}")
                
    except Exception as e:
        logger.error(f"Ошибка в send_smm_start_info: {e}")


# ====================
# ЧЕКЕР ЗАКАЗОВ
# ====================

def checkbox(cardinal: Cardinal):
    """Запуск чекера в отдельном потоке"""
    try:
        threading.Thread(target=process_orders, args=[cardinal], daemon=True).start()
        logger.info("Чекер заказов запущен")
    except Exception as e:
        logger.error(f"Ошибка запуска чекера: {e}")


def process_orders(c: Cardinal):
    """Проверка статусов заказов"""
    settings = SettingsCache.get_settings()
    check_interval = settings.get("check_interval", 60)
    
    while True:
        try:
            logger.info("Проверка статусов заказов...")
            api_url = get_api_url()
            api_key = get_api_key()
            
            if not api_url or not api_key:
                logger.warning("API не настроен, пропускаем проверку")
                time.sleep(check_interval)
                continue
            
            def check_order_status(order_id: str) -> Optional[dict]:
                """Проверка статуса одного заказа"""
                try:
                    return SocTypeAPI.get_order_status(int(order_id), api_url, api_key)
                except Exception as e:
                    logger.error(f"Ошибка проверки статуса заказа {order_id}: {e}")
                    return None
            
            def send_completion_message(c: Cardinal, order_id: str):
                """Отправка сообщения о завершении"""
                try:
                    orders = load_orders()
                    if order_id not in orders:
                        return
                    
                    order_info = orders[order_id]
                    chat_id = order_info.get("chat_id")
                    fp_order_id = order_info.get("order_id")
                    
                    if not chat_id:
                        logger.warning(f"Нет chat_id для заказа {order_id}")
                        return
                    
                    message_text = (
                        f"✅ Заказ #{fp_order_id} выполнен!\n"
                        f"Пожалуйста, перейдите по ссылке https://funpay.com/orders/{fp_order_id}/ "
                        f"и нажмите кнопку «Подтвердить выполнение заказа»."
                    )
                    c.send_message(chat_id, message_text)
                    logger.info(f"Отправлено уведомление о завершении заказа {order_id}")
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления о завершении: {e}")
            
            def send_canceled_message(c: Cardinal, order_id: str):
                """Отправка сообщения об отмене"""
                try:
                    orders = load_orders()
                    if order_id not in orders:
                        return
                    
                    order_info = orders[order_id]
                    chat_id = order_info.get("chat_id")
                    fp_order_id = order_info.get("order_id")
                    
                    if not chat_id:
                        return
                    
                    message_text = f"❌ Заказ #{fp_order_id} отменён!"
                    c.send_message(chat_id, message_text)
                    
                    # Попытка возврата средств
                    try:
                        c.account.refund(fp_order_id)
                        logger.info(f"Выполнен возврат средств для заказа {order_id}")
                    except Exception as e:
                        logger.error(f"Ошибка возврата средств: {e}")
                        
                except Exception as e:
                    logger.error(f"Ошибка отправки уведомления об отмене: {e}")
            
            def send_partial_message(c: Cardinal, order_id: str):
                """Обработка частично выполненного заказа"""
                try:
                    settings = SettingsCache.get_settings()
                    orders = load_orders()
                    
                    if order_id not in orders:
                        return
                    
                    order_info = orders[order_id]
                    cashlist = load_cashlist()
                    chat_id = order_info.get("chat_id")
                    partial_amount = int(order_info.get('partial_amount', 0))
                    
                    if partial_amount <= 0:
                        logger.warning(f"Некорректное partial_amount для заказа {order_id}")
                        return
                    
                    new_service_id = order_info.get('service_id')
                    new_link = order_info.get('order_url')
                    order_fid = order_info.get('order_id')
                    orderdatetime = order_info.get('orderdatetime')
                    
                    # Пересоздание заказа если включено
                    if settings.get("set_recreated_order", False):
                        try:
                            smm_order_id = SocTypeAPI.create_order(
                                new_service_id,
                                new_link,
                                partial_amount,
                                api_url,
                                api_key
                            )
                            
                            if isinstance(smm_order_id, (int, str)) and str(smm_order_id).isdigit():
                                cashlist[str(smm_order_id)] = {
                                    "service_id": new_service_id,
                                    "chat_id": chat_id,
                                    "order_id": order_fid,
                                    "order_url": new_link,
                                    "order_amount": partial_amount,
                                    "partial_amount": 0,
                                    "orderdatetime": orderdatetime,
                                    "status": "new"
                                }
                                save_cashlist(cashlist)
                                
                                message = f"""📈 Ваш заказ #{order_fid} был пересоздан!
🆔 Новый ID заказа: {smm_order_id}
⏳ Остаток выполнения: {partial_amount}"""
                                c.send_message(chat_id, message)
                                logger.info(f"Заказ {order_id} пересоздан как {smm_order_id}")
                        except Exception as e:
                            logger.error(f"Ошибка пересоздания заказа: {e}")
                    else:
                        message = f"""🔴 Заказ #{order_fid} был приостановлен!
⏳ Остаток выполнения: {partial_amount}"""
                        c.send_message(chat_id, message)
                        
                except Exception as e:
                    logger.error(f"Ошибка обработки Partial заказа: {e}")
            
            # Основная логика проверки
            orders = load_orders()
            updated_orders = {}
            completed_orders = []
            canceled_orders = []
            partial_orders = []
            
            for order_id, order_info in orders.items():
                try:
                    order_status = check_order_status(order_id)
                    
                    if order_status:
                        status = order_status.get("status", "unknown")
                        remains = int(order_status.get("remains", 0))
                        
                        updated_orders[order_id] = {
                            "service_id": order_info.get('service_id'),
                            "chat_id": order_info.get("chat_id"),
                            "order_id": order_info.get("order_id"),
                            "order_url": order_info.get('order_url'),
                            "order_amount": order_info.get('order_amount'),
                            "partial_amount": remains,
                            "orderdatetime": order_info.get('orderdatetime'),
                            "status": status
                        }
                        
                        # Сортировка по статусам
                        if status == "Completed":
                            completed_orders.append(order_id)
                            send_completion_message(c, order_id)
                        elif status == "Canceled":
                            canceled_orders.append(order_id)
                            send_canceled_message(c, order_id)
                        elif status == "Partial":
                            partial_orders.append(order_id)
                            send_partial_message(c, order_id)
                    else:
                        # Статус не получен, оставляем заказ как есть
                        updated_orders[order_id] = order_info
                        
                except Exception as e:
                    logger.error(f"Ошибка обработки заказа {order_id}: {e}")
                    updated_orders[order_id] = order_info
            
            # Удаление завершенных/отмененных заказов
            for order_id in completed_orders + canceled_orders + partial_orders:
                if order_id in updated_orders:
                    del updated_orders[order_id]
            
            # Добавление заказов из кэшлиста
            cashlist = load_cashlist()
            for order_id, order_info in cashlist.items():
                if order_id not in updated_orders:
                    updated_orders[order_id] = order_info
            
            # Сохранение обновленных заказов
            save_orders(updated_orders)
            
            # Очистка кэшлиста
            if cashlist:
                save_cashlist({})
            
            logger.info(f"Проверка завершена. Активных заказов: {len(updated_orders)}")
            
        except Exception as e:
            logger.error(f"Критическая ошибка в process_orders: {e}", exc_info=True)
        
        # Пауза перед следующей проверкой
        time.sleep(check_interval)


# ====================
# TELEGRAM КОМАНДЫ
# ====================

def init_commands(cardinal: Cardinal, *args):
    """Инициализация команд"""
    try:
        # Стартовое сообщение
        settings = SettingsCache.get_settings()
        if settings.get("set_start_mess", False):
            send_smm_start_info(cardinal)
        
        if not cardinal.telegram:
            logger.warning("Telegram бот не настроен")
            return
        
        tg = cardinal.telegram
        bot = tg.bot
        
        # Команда проверки баланса
        def send_smm_balance_command(m: types.Message):
            try:
                send_smm_balance_info(cardinal)
            except Exception as e:
                logger.error(f"Ошибка команды check_balance: {e}")
                bot.reply_to(m, "❌ Ошибка получения баланса")
        
        # Главное меню настроек
        settings_smm_keyboard = InlineKeyboardMarkup(row_width=1)
        set_api = InlineKeyboardButton("🔗 API URL", callback_data='set_api')
        set_api_key = InlineKeyboardButton("🔐 API KEY", callback_data='set_api_key')
        set_api_2 = InlineKeyboardButton("🔗 API URL 2", callback_data='set_api_2')
        set_api_key_2 = InlineKeyboardButton("🔐 API KEY 2", callback_data='set_api_key_2')
        set_usersm_settings = InlineKeyboardButton("🛠 Настройки", callback_data='set_usersm_settings')
        pay_orders = InlineKeyboardButton("📝 Оплаченные заказы", callback_data='pay_orders')
        active_orders = InlineKeyboardButton("📋 Активные заказы", callback_data='active_orders')
        settings_smm_keyboard.row(set_api, set_api_key)
        settings_smm_keyboard.row(set_api_2, set_api_key_2)
        settings_smm_keyboard.add(set_usersm_settings, pay_orders, active_orders)
        
        def update_alerts_keyboard():
            """Обновление клавиатуры настроек"""
            settings = SettingsCache.get_settings()
            alerts_smm_keyboard = InlineKeyboardMarkup(row_width=1)
            
            # Генерация кнопок на основе настроек
            buttons = []
            
            for key, label in [
                ("set_alert_neworder", "Увед. о созданном заказе"),
                ("set_alert_errororder", "Увед. при ошибке создания"),
                ("set_alert_smmbalance_new", "Увед. о балансе смм до создания"),
                ("set_alert_smmbalance", "Увед. о балансе смм после создания"),
                ("set_refund_smm", "Автовозврат"),
                ("set_start_mess", "Сообщение при запуске FPC"),
                ("set_tg_private", "Закрытые ТГ каналы/группы"),
                ("set_recreated_order", "Пересоздание заказа"),
            ]:
                icon = "🔔" if settings.get(key, False) and "alert" in key else ("🟢" if settings.get(key, False) else "🔴")
                if "alert" in key and not settings.get(key, False):
                    icon = "🔕"
                
                button = InlineKeyboardButton(f"{icon} {label}", callback_data=key)
                buttons.append(button)
            
            for button in buttons:
                alerts_smm_keyboard.add(button)
            
            set_back_butt = InlineKeyboardButton("⬅️ Назад", callback_data='set_back_butt')
            alerts_smm_keyboard.add(set_back_butt)
            
            return alerts_smm_keyboard
        
        # Обработчик команды /autosmm
        def send_settings(m: types.Message):
            try:
                bot.reply_to(m, "API 1: `ID:`\nAPI 2: `ID2:`\n\n⚙️ AutoSmm:", reply_markup=settings_smm_keyboard, parse_mode='Markdown')
            except Exception as e:
                logger.error(f"Ошибка send_settings: {e}")
        
        # Обработчик callback кнопок
        def edit(call: telebot.types.CallbackQuery):
            try:
                settings = SettingsCache.get_settings()
                
                if call.data == 'set_usersm_settings':
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="🛠 Настройки:",
                        reply_markup=update_alerts_keyboard()
                    )
                
                # Переключение настроек
                elif call.data in [
                    'set_alert_neworder', 'set_alert_errororder',
                    'set_alert_smmbalance_new', 'set_alert_smmbalance',
                    'set_refund_smm', 'set_start_mess', 'set_auto_refill',
                    'set_tg_private', 'set_recreated_order'
                ]:
                    settings[call.data] = not settings.get(call.data, False)
                    save_settings(settings)
                    
                    bot.edit_message_reply_markup(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        reply_markup=update_alerts_keyboard()
                    )
                
                elif call.data == 'set_back_butt':
                    bot.edit_message_text(
                        chat_id=call.message.chat.id,
                        message_id=call.message.message_id,
                        text="API 1: `ID:`\nAPI 2: `ID2:`\n\n⚙️ AutoSmm:",
                        parse_mode='Markdown',
                        reply_markup=settings_smm_keyboard
                    )
                
                # Настройка API
                elif call.data in ['set_api', 'set_api_key', 'set_api_2', 'set_api_key_2']:
                    back_button = InlineKeyboardButton("❌ Отмена", callback_data='delete_back_butt')
                    kb = InlineKeyboardMarkup().add(back_button)
                    
                    setting_map = {
                        'set_api': ('api_url', 'URL'),
                        'set_api_key': ('api_key', 'API KEY'),
                        'set_api_2': ('api_url_2', 'URL 2'),
                        'set_api_key_2': ('api_key_2', 'API KEY 2')
                    }
                    
                    setting_key, label = setting_map[call.data]
                    current_value = settings.get(setting_key, 'Не установлено')
                    
                    result = bot.send_message(
                        call.message.chat.id,
                        f'**Текущее значение {label}:** {current_value}\n\n*⬇️ Введите новое значение ⬇️*',
                        parse_mode='Markdown',
                        reply_markup=kb
                    )
                    
                    tg.set_state(
                        chat_id=call.message.chat.id,
                        message_id=result.id,
                        user_id=call.from_user.id,
                        state=f"setting_{setting_key}"
                    )
                
                elif call.data == 'delete_back_butt':
                    bot.delete_message(call.message.chat.id, call.message.message_id)
                    tg.clear_state(call.message.chat.id, call.from_user.id)
                
                # Просмотр заказов
                elif call.data == 'pay_orders':
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data='delete_back_butt')
                    kb = InlineKeyboardMarkup().add(back_button)
                    
                    orders_data = load_payorders()
                    if not orders_data:
                        orders_text = "📝 Оплаченные заказы отсутствуют."
                    else:
                        orders_text = "📝 Оплаченные заказы:\n\n"
                        for order in orders_data[:10]:  # Ограничиваем 10 заказами
                            orders_text += f"🆔 ID: {order.get('OrderID', 'N/A')}\n"
                            orders_text += f"⠀∟📋 Название: {order.get('Order', 'N/A')}\n"
                            orders_text += f"⠀∟🔢 Кол-во: {order.get('Amount', 'N/A')}\n"
                            orders_text += f"⠀∟👤 Покупатель: {order.get('buyer', 'N/A')}\n"
                            orders_text += f"⠀∟📅 Дата: {order.get('OrderDateTime', 'N/A')}\n"
                            orders_text += f"⠀∟🔗 Ссылка: {order.get('url', 'N/A')}\n\n"
                        
                        if len(orders_data) > 10:
                            orders_text += f"... и еще {len(orders_data) - 10} заказов"
                    
                    bot.send_message(call.message.chat.id, orders_text, reply_markup=kb)
                    bot.answer_callback_query(call.id)
                
                elif call.data == 'active_orders':
                    back_button = InlineKeyboardButton("⬅️ Назад", callback_data='delete_back_butt')
                    kb = InlineKeyboardMarkup().add(back_button)
                    
                    orders_data = load_orders()
                    if not orders_data:
                        orders_text = "📋 Активные заказы отсутствуют."
                    else:
                        orders_text = "📋 Активные заказы:\n\n"
                        for order_id, order in list(orders_data.items())[:10]:
                            orders_text += f"🆔 ID: {order_id}\n"
                            orders_text += f"⠀∟🔢 Кол-во: {order.get('order_amount', 'N/A')}\n"
                            orders_text += f"⠀∟📅 Дата: {order.get('orderdatetime', 'N/A')}\n"
                            orders_text += f"⠀∟📋 Статус: {order.get('status', 'N/A')}\n\n"
                        
                        if len(orders_data) > 10:
                            orders_text += f"... и еще {len(orders_data) - 10} заказов"
                    
                    bot.send_message(call.message.chat.id, orders_text, reply_markup=kb)
                    bot.answer_callback_query(call.id)
                    
            except Exception as e:
                logger.error(f"Ошибка в обработчике callback: {e}", exc_info=True)
                try:
                    bot.answer_callback_query(call.id, "❌ Произошла ошибка")
                except:
                    pass
        
        # Обработчик текстового ввода
        def handle_text_input(message: telebot.types.Message):
            try:
                state_data = tg.get_state(message.chat.id, message.from_user.id)
                if not state_data or 'state' not in state_data:
                    return
                
                state = state_data['state']
                input_text = message.text.strip()
                
                # Маппинг состояний
                state_map = {
                    'setting_api_url': ('api_url', 'URL', Validator.validate_url),
                    'setting_api_key': ('api_key', 'API KEY', Validator.validate_api_key),
                    'setting_api_url_2': ('api_url_2', 'URL 2', Validator.validate_url),
                    'setting_api_key_2': ('api_key_2', 'API KEY 2', Validator.validate_api_key)
                }
                
                if state in state_map:
                    setting_key, label, validator = state_map[state]
                    
                    # Валидация
                    is_valid, error = validator(input_text)
                    if not is_valid:
                        bot.send_message(
                            message.from_user.id,
                            f"❌ Ошибка валидации: {error}\nПопробуйте еще раз."
                        )
                        return
                    
                    # Сохранение
                    settings = load_settings()
                    settings[setting_key] = input_text
                    
                    if save_settings(settings):
                        bot.send_message(
                            message.from_user.id,
                            f"✅ Успех: {label} обновлён\n‼️ Рекомендуется выполнить /restart"
                        )
                        logger.info(f"{label} обновлён на: {input_text[:20]}...")
                        
                        # Удаление сообщений
                        try:
                            bot.delete_message(message.chat.id, message.message_id)
                            bot.delete_message(message.chat.id, message.message_id - 1)
                        except:
                            pass
                    else:
                        bot.send_message(
                            message.from_user.id,
                            f"❌ Ошибка сохранения настроек"
                        )
                    
                    tg.clear_state(message.chat.id, message.from_user.id)
                    
            except Exception as e:
                logger.error(f"Ошибка обработки текстового ввода: {e}", exc_info=True)
                try:
                    bot.send_message(message.from_user.id, "❌ Произошла ошибка")
                    tg.clear_state(message.chat.id, message.from_user.id)
                except:
                    pass
        
        # Регистрация обработчиков
        tg.cbq_handler(edit, lambda c: c.data in [
            'set_api', 'set_api_key', 'set_api_2', 'set_api_key_2',
            'set_usersm_settings', 'set_back_butt',
            'set_alert_neworder', 'set_alert_errororder',
            'set_alert_smmbalance_new', 'set_alert_smmbalance',
            'set_refund_smm', 'set_auto_refill', 'set_start_mess',
            'set_tg_private', 'pay_orders', 'active_orders',
            'set_recreated_order', 'delete_back_butt'
        ])
        
        tg.msg_handler(
            handle_text_input,
            func=lambda m: tg.check_state(m.chat.id, m.from_user.id, "setting_api_url") or
                          tg.check_state(m.chat.id, m.from_user.id, "setting_api_key") or
                          tg.check_state(m.chat.id, m.from_user.id, "setting_api_url_2") or
                          tg.check_state(m.chat.id, m.from_user.id, "setting_api_key_2")
        )
        
        tg.msg_handler(send_settings, commands=["autosmm"])
        tg.msg_handler(send_smm_balance_command, commands=["check_balance"])
        
        cardinal.add_telegram_commands(UUID, [
            ("autosmm", f"настройки {NAME}", True),
            ("check_balance", f"баланс {NAME}", True)
        ])
        
        logger.info("Telegram команды успешно инициализированы")
        
    except Exception as e:
        logger.error(f"Ошибка инициализации команд: {e}", exc_info=True)


# ====================
# ПРИВЯЗКА К СОБЫТИЯМ
# ====================

BIND_TO_PRE_INIT = [init_commands]
BIND_TO_POST_INIT = [checkbox]
BIND_TO_NEW_ORDER = [bind_to_new_order]
BIND_TO_NEW_MESSAGE = [msg_hook]
BIND_TO_DELETE = None
