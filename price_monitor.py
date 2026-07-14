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

DEST = "-1257786"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def get_price(nm_id: int) -> dict | None:
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

    colors = p.get("colors") or []
    color = colors[0].get("name", "") if colors else ""

    price_kopecks = None
    sizes = p.get("sizes") or []

    for size in sizes:
        price_block = size.get("price") or {}
        candidate = (
            price_block.get("product")
            or price_block.get("total")
            or price_block.get("basic")
        )
        if candidate:
            price_kopecks = candidate
            break

    if price_kopecks is None:
        top_price = p.get("priceU") or p.get("salePriceU")
        if top_price:
            price_kopecks = top_price

    if price_kopecks is None:
        sizes_count = len(sizes)
        in_stock = any((s.get("stocks") for s in sizes)) if sizes else False
        raw_sample = json.dumps(sizes[0], ensure_ascii=False)[:300] if sizes else "нет sizes"
        print(
            f"[!] Не удалось извлечь цену для nm_id={nm_id} ('{name}'). "
            f"sizes_count={sizes_count}, есть_остатки={in_stock}. "
            f"Пример sizes[0]: {raw_sample}",
            file=sys.stderr,
        )
        return None

    return {"price": round(price_kopecks / 100), "name": name, "brand": brand, "color": color}


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def save_json(path: Path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def send_telegram_blocks(header: str, blocks: list[str]):
    """
    Отправляет заголовок + список блоков (каждый — один товар) в Telegram,
    группируя их в сообщения не длиннее ~3500 символов. Разрез всегда
    происходит МЕЖДУ блоками — HTML-теги внутри блока не разрываются.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы — "
              "уведомление не отправлено.", file=sys.stderr)
        return

    limit = 3500
    messages = []
    current = header
    for block in blocks:
        candidate = current + "\n\n" + block
        if len(candidate) > limit and current != header:
            messages.append(current)
            current = header + " (продолжение)\n\n" + block
        else:
            current = candidate
    messages.append(current)

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    for msg in messages:
        try:
            resp = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"},
                timeout=15,
            )
            data = resp.json()
            if not data.get("ok"):
                print(
                    f"[!] Telegram API вернул ошибку: {data.get('description')} "
                    f"(код {data.get('error_code')})",
                    file=sys.stderr,
                )
            else:
                print("Сообщение успешно отправлено в Telegram.")
        except Exception as e:
            print(f"[!] Не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


REFERENCE_NM_ID = "392074718"


def product_url(nm_id: str) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d %H:%M UTC")

    items = []
    error_lines = []

    for product in products:
        nm_id = str(product["nm_id"])
        label = product.get("name") or nm_id
        ptype = product.get("type", "own")

        result = get_price(product["nm_id"])
        if result is None:
            error_lines.append(f"⚠️ <b>{label}</b> (артикул {nm_id}) — не удалось получить цену")
            continue

        price = result["price"]
        wb_name = result["name"] or label
        brand = result["brand"]
        color = result["color"]

        product_history = history.setdefault(
            nm_id, {"name": wb_name, "brand": brand, "color": color, "type": ptype, "points": []}
        )
        product_history["brand"] = brand
        product_history["color"] = color
        points = product_history["points"]
        last_price = points[-1]["price"] if points else None

        points.append({"date": timestamp, "price": price})

        items.append({
            "nm_id": nm_id,
            "price": price,
            "last_price": last_price,
            "wb_name": wb_name,
            "brand": brand,
            "color": color,
            "ptype": ptype,
        })

        if last_price is not None and last_price != price:
            print(f"Изменение цены: {wb_name}: {last_price} -> {price}")

    save_json(HISTORY_FILE, history)

    reference_price = next(
        (it["price"] for it in items if it["nm_id"] == REFERENCE_NM_ID), None
    )

    items.sort(key=lambda it: it["price"], reverse=True)

    report_lines = []
    for it in items:
        nm_id = it["nm_id"]
        price = it["price"]
        last_price = it["last_price"]
        type_label = "🏠 моё" if it["ptype"] == "own" else "🔎 конкурент"
        brand_part = f" [{it['brand']}]" if it["brand"] else ""
        color_part = f", цвет: {it['color']}" if it["color"] else ""
        link = f'<a href="{product_url(nm_id)}">арт. {nm_id}</a>'
        title = f"<b>{it['wb_name']}</b>{brand_part} ({type_label}, {link}{color_part})"

        if last_price is None:
            change_line = f"{price} ₽ (первая запись)"
            marker = "🆕"
        elif last_price == price:
            change_line = f"{price} ₽ (без изменений)"
            marker = "➖"
        else:
            diff = price - last_price
            pct = (diff / last_price) * 100
            marker = "🔺" if diff > 0 else "🔻"
            change_line = f"{last_price} ₽ → {price} ₽ ({diff:+d} ₽, {pct:+.1f}%)"

        vs_reference = ""
        if reference_price is not None and nm_id != REFERENCE_NM_ID:
            diff_ref = price - reference_price
            pct_ref = (diff_ref / reference_price) * 100
            vs_reference = f"\nvs моя цена: {diff_ref:+d} ₽ ({pct_ref:+.1f}%)"

        report_lines.append(f"{marker} {title}\n{change_line}{vs_reference}")

    report_lines.extend(error_lines)

    header = f"💰 <b>Отчёт по ценам Wildberries</b> ({timestamp})"
    send_telegram_blocks(header, report_lines)
    print("Отчёт отправлен.")


if __name__ == "__main__":
    main()
