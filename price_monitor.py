"""
Мониторинг цен товаров на Wildberries (свои + конкуренты).

Логика:
1. Читаем список товаров из products.json (nm_id — это артикул WB,
   он же число в ссылке товара: wildberries.ru/catalog/<nm_id>/detail.aspx)
2. Для каждого товара получаем текущую цену через публичный JSON-эндпоинт
   витрины WB (тот же, что использует сам сайт для отображения цены).
3. Сравниваем с последней сохранённой ценой в history.json.
4. Если цена изменилась — отправляем уведомление в Telegram и
   дописываем новую точку в историю.

ВАЖНО: card.wb.ru — не официальный документированный API, а публичный
эндпоинт витрины. WB может менять его формат без предупреждения.
Если скрипт перестанет находить цену — см. раздел "Если сломалось"
в README.md.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "history.json"

# Регион для расчёта цены (влияет на скидки/логистику). -1257786 = усреднённый
# по РФ вариант, которым часто пользуются парсеры. При необходимости
# замените на код своего региона.
DEST = "-1257786"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def get_price(nm_id: int) -> dict | None:
    """
    Возвращает цену товара по артикулу через публичный API карточки WB.
    Возвращает словарь {"price": int, "price_full": int, "name": str}
    в рублях, либо None, если товар не найден / эндпоинт не ответил.
    """
    url = "https://card.wb.ru/cards/v4/detail"
    params = {
        "appType": 1,
        "curr": "rub",
        "dest": DEST,
        "spp": 30,
        "hide_dtype": 13,
        "ab_testing": "false",
        "lang": "ru",
        "nm": nm_id,
    }
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }

    try:
        resp = requests.get(url, params=params, headers=headers, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"[!] Ошибка запроса для nm_id={nm_id}: {e}", file=sys.stderr)
        return None

    products = data.get("products") or (data.get("data") or {}).get("products")
    if not products:
        print(f"[!] Товар nm_id={nm_id} не найден в ответе API", file=sys.stderr)
        return None

    p = products[0]
    name = p.get("name", "")

    # Цена в копейках. Пробуем несколько возможных мест, т.к. структура
    # WB отличается в зависимости от типа товара и периодически меняется.
    price_kopecks = None
    sizes = p.get("sizes") or []

    if sizes:
        price_block = sizes[0].get("price") or {}
        price_kopecks = price_block.get("product") or price_block.get("total")

    if price_kopecks is None:
        # Запасной вариант: цена на уровне самого товара, без размеров
        # (актуально, если sizes пуст — например, товар закончился, но
        # цена в карточке всё ещё отдаётся)
        top_price = p.get("priceU") or p.get("salePriceU")
        if top_price:
            price_kopecks = top_price

    if price_kopecks is None:
        # Диагностика: показываем, что реально пришло, чтобы можно было
        # быстро найти правильное поле вручную
        sizes_count = len(sizes)
        in_stock = any((s.get("stocks") for s in sizes)) if sizes else False
        print(
            f"[!] Не удалось извлечь цену для nm_id={nm_id} ('{name}'). "
            f"sizes_count={sizes_count}, есть_остатки={in_stock}. "
            f"Похоже, товар закончился на складах либо изменился формат "
            f"ответа WB — проверьте вручную.",
            file=sys.stderr,
        )
        return None

    return {"price": round(price_kopecks / 100), "name": name}


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram(text: str):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — "
              "уведомление не отправлено:", text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=15,
        )
    except Exception as e:
        print(f"[!] Не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    changes = []

    for product in products:
        nm_id = str(product["nm_id"])
        label = product.get("name") or nm_id
        ptype = product.get("type", "own")

        result = get_price(product["nm_id"])
        if result is None:
            continue

        price = result["price"]
        wb_name = result["name"] or label

        product_history = history.setdefault(nm_id, {"name": wb_name, "type": ptype, "points": []})
        points = product_history["points"]
        last_price = points[-1]["price"] if points else None

        # Записываем точку раз в день (перезаписываем, если уже был запуск сегодня)
        if points and points[-1]["date"] == today:
            points[-1]["price"] = price
        else:
            points.append({"date": today, "price": price})

        if last_price is not None and last_price != price:
            diff = price - last_price
            arrow = "🔺" if diff > 0 else "🔻"
            pct = (diff / last_price) * 100
            changes.append(
                f"{arrow} <b>{wb_name}</b> ({ptype})\n"
                f"{last_price} ₽ → {price} ₽ ({diff:+d} ₽, {pct:+.1f}%)\n"
                f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
            )
            print(f"Изменение цены: {wb_name}: {last_price} -> {price}")
        else:
            print(f"{wb_name}: {price} ₽ (без изменений)" if last_price else
                  f"{wb_name}: {price} ₽ (первая запись)")

    save_json(HISTORY_FILE, history)

    if changes:
        message = "💰 <b>Изменения цен на Wildberries</b>\n\n" + "\n\n".join(changes)
        send_telegram(message)
    else:
        print("Изменений цен не обнаружено.")


if __name__ == "__main__":
    main()
