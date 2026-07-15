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

import calendar
import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "history.json"

DEST = "-1257786"

MOSCOW_TZ = timezone(timedelta(hours=3))

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


def parse_history_date(date_str: str) -> datetime | None:
    date_str = (date_str or "").strip()
    try:
        return datetime.strptime(date_str, "%d.%m.%Y %H:%M МСК").replace(tzinfo=MOSCOW_TZ)
    except ValueError:
        pass
    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M UTC").replace(tzinfo=timezone.utc)
        return dt.astimezone(MOSCOW_TZ)
    except ValueError:
        return None


def send_median_report(history: dict, title: str, start_dt: datetime, end_dt: datetime):
    rows = []
    for nm_id, data in history.items():
        if nm_id == "_meta":
            continue
        prices_in_period = [
            pt["price"] for pt in data.get("points", [])
            if (dt := parse_history_date(pt.get("date", ""))) and start_dt <= dt <= end_dt
        ]
        if not prices_in_period:
            continue
        median_price = statistics.median(prices_in_period)
        name = data.get("name") or nm_id
        brand = data.get("brand", "")
        ptype = data.get("type", "own")
        type_label = "🏠 моё" if ptype == "own" else "🔎 конкурент"
        brand_part = f" [{brand}]" if brand else ""
        link = f'<a href="{product_url(nm_id)}">арт. {nm_id}</a>'
        rows.append({
            "median": median_price,
            "text": (
                f"<b>{name}</b>{brand_part} ({type_label}, {link})\n"
                f"Медиана: {median_price:.0f} ₽ (по {len(prices_in_period)} набл.: "
                f"{min(prices_in_period)}–{max(prices_in_period)} ₽)"
            ),
        })

    if not rows:
        return

    rows.sort(key=lambda r: r["median"], reverse=True)
    blocks = [r["text"] for r in rows]
    header = f"📊 <b>{title}</b>"
    send_telegram_blocks(header, blocks)


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    now = datetime.now(MOSCOW_TZ)
    timestamp = now.strftime("%d.%m.%Y %H:%M МСК")

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

    meta = history.setdefault("_meta", {})

    week_id = now.strftime("%Y-W%V")
    if now.weekday() == 6 and meta.get("last_weekly_report") != week_id:
        week_start = (now - timedelta(days=now.weekday())).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        send_median_report(
            history,
            f"Медианная цена за неделю ({week_start.strftime('%d.%m')}–{now.strftime('%d.%m.%Y')})",
            week_start,
            now,
        )
        meta["last_weekly_report"] = week_id
        print("Недельный отчёт с медианой отправлен.")

    month_id = now.strftime("%Y-%m")
    last_day_of_month = calendar.monthrange(now.year, now.month)[1]
    if now.day == last_day_of_month and meta.get("last_monthly_report") != month_id:
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        send_median_report(
            history,
            f"Медианная цена за месяц ({month_start.strftime('%m.%Y')})",
            month_start,
            now,
        )
        meta["last_monthly_report"] = month_id
        print("Месячный отчёт с медианой отправлен.")

    save_json(HISTORY_FILE, history)


if __name__ == "__main__":
    main()
