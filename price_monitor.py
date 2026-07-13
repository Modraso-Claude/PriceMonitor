"""
Мониторинг цен товаров на Wildberries (свои + конкуренты).

Логика:
1. Читаем список товаров из products.json (nm_id — это артикул WB,
   он же число в ссылке товара: wildberries.ru/catalog/<nm_id>/detail.aspx)
2. Для каждого товара получаем текущую цену и бренд через публичный
   JSON-эндпоинт витрины WB (тот же, что использует сам сайт).
3. Сравниваем с последней сохранённой ценой в history.json.
4. Раз в запуск отправляем в Telegram ПОЛНЫЙ отчёт по всем товарам —
   с текущей ценой, брендом и процентом изменения относительно
   предыдущей проверки (даже если изменений не было).

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
    Возвращает данные товара по артикулу через публичный API карточки WB.
    Возвращает {"price": int, "name": str, "brand": str} в рублях,
    либо None, если товар не найден / эндпоинт не ответил.
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
    brand = p.get("brand", "") or ""

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

    return {"price": round(price_kopecks / 100), "name": name, "brand": brand}


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
        # Telegram режет длинные сообщения на 4096 символов — при большом
        # числе товаров разбиваем отчёт на части.
        chunks = [text[i:i + 3500] for i in range(0, len(text), 3500)] or [text]
        for chunk in chunks:
            requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": chunk, "parse_mode": "HTML"},
                timeout=15,
            )
    except Exception as e:
        print(f"[!] Не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    report_lines = []

    for product in products:
        nm_id = str(product["nm_id"])
        label = product.get("name") or nm_id
        ptype = product.get("type", "own")

        result = get_price(product["nm_id"])
        if result is None:
            report_lines.append(f"⚠️ <b>{label}</b> — не удалось получить цену")
            continue

        price = result["price"]
        wb_name = result["name"] or label
        brand = result["brand"]

        product_history = history.setdefault(
            nm_id, {"name": wb_name, "brand": brand, "type": ptype, "points": []}
        )
        product_history["brand"] = brand  # обновляем на случай, если раньше не было
        points = product_history["points"]
        last_price = points[-1]["price"] if points else None

        points.append({"date": timestamp, "price": price})

        type_label = "🏠 моё" if ptype == "own" else "🔎 конкурент"
        brand_part = f" [{brand}]" if brand else ""
        title = f"<b>{wb_name}</b>{brand_part} ({type_label})"

        if last_price is None:
            report_lines.append(f"🆕 {title}\n{price} ₽ (первая запись)")
        elif last_price == price:
            report_lines.append(f"➖ {title}\n{price} ₽ (без изменений)")
        else:
            diff = price - last_price
            pct = (diff / last_price) * 100
            arrow = "🔺" if diff > 0 else "🔻"
            report_lines.append(
                f"{arrow} {title}\n"
                f"{last_price} ₽ → {price} ₽ ({diff:+d} ₽, {pct:+.1f}%)"
            )
            print(f"Изменение цены: {wb_name}: {last_price} -> {price}")

    save_json(HISTORY_FILE, history)

    message = f"💰 <b>Отчёт по ценам Wildberries</b> ({timestamp})\n\n" + "\n\n".join(report_lines)
    send_telegram(message)
    print("Отчёт отправлен.")


if __name__ == "__main__":
    main()
