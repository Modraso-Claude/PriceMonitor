"""
Мониторинг цен товаров на Wildberries (свои + конкуренты) через Playwright.

ПОЧЕМУ PLAYWRIGHT, А НЕ ПРОСТОЙ HTTP-ЗАПРОС:
Мы последовательно упёрлись в несколько уровней защиты WB:
  1. card.wb.ru / search.wb.ru — блокировка по IP дата-центров (таймауты)
  2. www.wildberries.ru/__internal/... — 403 на обычный requests
  3. то же самое с cookies и браузерными заголовками — снова 403
  4. то же самое с curl_cffi (имитация TLS-отпечатка Chrome) — снова 403
Значит, WB требует чего-то, что умеет только настоящий браузерный движок
(скорее всего, выполнения JS-челленджа).

ПОДХОД: запускаем настоящий Chromium, открываем в нём обычную страницу
товара — ровно так, как это делает покупатель. Страница сама запрашивает
у сервера свои данные (цену, бренд, цвет), а мы просто перехватываем этот
ответ через обработчик response. Ничего не подделываем — забираем то, что
браузер и так легально получил.

ЦЕНА ВОПРОСА: это медленнее (~3-8 сек на товар вместо 0.5), поэтому на
24 товара уйдёт 2-4 минуты вместо 20 секунд. Для двух запусков в день
это укладывается в бесплатные лимиты GitHub Actions с запасом.
"""

import calendar
import json
import os
import statistics
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "history.json"

MOSCOW_TZ = timezone(timedelta(hours=3))

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

REFERENCE_NM_ID = "392074718"


def product_url(nm_id: str) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def extract_price_data(payload: dict) -> dict | None:
    """Достаёт цену/бренд/цвет из JSON-ответа карточки товара."""
    products = payload.get("products") or (payload.get("data") or {}).get("products")
    if not products:
        return None

    p = products[0]
    name = p.get("name", "")
    brand = p.get("brand", "") or ""
    colors = p.get("colors") or []
    color = colors[0].get("name", "") if colors else ""

    price_kopecks = None
    for size in p.get("sizes") or []:
        pb = size.get("price") or {}
        candidate = pb.get("product") or pb.get("total") or pb.get("basic")
        if candidate:
            price_kopecks = candidate
            break

    if price_kopecks is None:
        price_kopecks = p.get("priceU") or p.get("salePriceU")

    if price_kopecks is None:
        return None

    return {
        "price": round(price_kopecks / 100),
        "name": name,
        "brand": brand,
        "color": color,
    }


def read_price_from_page(page, nm_id: str) -> dict | None:
    """
    Запасной способ: читает цену, бренд и название прямо из разметки
    страницы — если перехватить ответ API не удалось. Цена на странице
    отображается покупателю, значит она есть в HTML.
    """
    try:
        page.wait_for_selector(".product-page__price-block", timeout=8000)
    except Exception:
        pass

    try:
        data = page.evaluate("""() => {
            const clean = (s) => (s || '').replace(/[^0-9]/g, '');
            const priceEl =
                document.querySelector('.price-block__wallet-price') ||
                document.querySelector('.price-block__final-price') ||
                document.querySelector('ins.price-block__final-price');
            const brandEl =
                document.querySelector('.product-page__header-brand') ||
                document.querySelector('[class*="brand"]');
            const nameEl =
                document.querySelector('.product-page__title') ||
                document.querySelector('h1');
            return {
                priceRaw: priceEl ? clean(priceEl.textContent) : '',
                brand: brandEl ? brandEl.textContent.trim() : '',
                name: nameEl ? nameEl.textContent.trim() : '',
            };
        }""")
    except Exception as e:
        print(f"[!] Не удалось прочитать страницу nm_id={nm_id}: {e}", file=sys.stderr)
        return None

    price_raw = (data or {}).get("priceRaw") or ""
    if not price_raw.isdigit():
        return None

    return {
        "price": int(price_raw),
        "name": (data.get("name") or "").strip(),
        "brand": (data.get("brand") or "").strip(),
        "color": "",
    }


def fetch_prices_via_browser(nm_ids: list[str]) -> dict:
    """
    Открывает страницу каждого товара в настоящем браузере и перехватывает
    ответ API, который страница загружает для себя. Возвращает словарь
    {nm_id: {price, name, brand, color}} только для успешно полученных.
    """
    results = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            args=["--disable-blink-features=AutomationControlled"]
        )
        context = browser.new_context(
            locale="ru-RU",
            timezone_id="Europe/Moscow",
            viewport={"width": 1440, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        # Один раз заходим на главную — получаем cookies как обычный посетитель
        try:
            page.goto("https://www.wildberries.ru/", timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"[!] Не удалось открыть главную страницу: {e}", file=sys.stderr)

        for idx, nm_id in enumerate(nm_ids):
            captured = {}
            seen_urls = []

            def handle_response(response, _captured=captured, _seen=seen_urls):
                url = response.url
                # Собираем список всех похожих на данные запросов —
                # пригодится для диагностики, если ничего не поймаем
                if any(k in url for k in ("card", "detail", "nm=", "product")):
                    _seen.append(url[:160])
                # Широкий фильтр: любой ответ, где есть и признак карточки,
                # и наш артикул — структура путей WB периодически меняется
                if ("detail" in url or "cards" in url) and str(nm_id) in url:
                    try:
                        _captured["payload"] = response.json()
                    except Exception:
                        pass

            page.on("response", handle_response)

            try:
                page.goto(product_url(nm_id), timeout=45000, wait_until="domcontentloaded")
                # Ждём, пока страница подтянет свои данные
                for _ in range(24):
                    if "payload" in captured:
                        break
                    page.wait_for_timeout(500)
            except Exception as e:
                print(f"[!] Ошибка загрузки страницы nm_id={nm_id}: {e}", file=sys.stderr)

            page.remove_listener("response", handle_response)

            payload = captured.get("payload")
            if not payload:
                # Запасной вариант: цена видна на самой странице —
                # читаем её прямо из разметки
                fallback = read_price_from_page(page, nm_id)
                if fallback:
                    results[nm_id] = fallback
                    print(f"OK (со страницы) nm_id={nm_id}: {fallback['price']} ₽ ({fallback['brand']})")
                    continue

                print(f"[!] Не перехватили данные карточки для nm_id={nm_id}", file=sys.stderr)
                # Для первого товара печатаем, что вообще пролетало —
                # по этому списку можно подобрать правильный фильтр
                if idx == 0:
                    print("[i] Запросы, которые делала страница (первые 25):", file=sys.stderr)
                    for u in seen_urls[:25]:
                        print(f"    {u}", file=sys.stderr)
                    if not seen_urls:
                        print("    (ничего похожего на данные не поймано вообще)", file=sys.stderr)
                    # Что реально показал браузер вместо страницы товара
                    try:
                        print(f"[i] Итоговый URL: {page.url}", file=sys.stderr)
                        print(f"[i] Заголовок страницы: {page.title()}", file=sys.stderr)
                        body_text = page.evaluate(
                            "() => (document.body ? document.body.innerText : '').slice(0, 600)"
                        )
                        print(f"[i] Текст страницы (начало):\n{body_text}", file=sys.stderr)
                    except Exception as diag_err:
                        print(f"[i] Не удалось снять диагностику страницы: {diag_err}", file=sys.stderr)
                continue

            data = extract_price_data(payload)
            if data is None:
                print(f"[!] Не удалось извлечь цену из ответа для nm_id={nm_id}", file=sys.stderr)
                continue

            results[nm_id] = data
            print(f"OK nm_id={nm_id}: {data['price']} ₽ ({data['brand']})")

        context.close()
        browser.close()

    return results


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
        print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы.", file=sys.stderr)
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
                print(f"[!] Telegram API вернул ошибку: {data.get('description')}", file=sys.stderr)
            else:
                print("Сообщение успешно отправлено в Telegram.")
        except Exception as e:
            print(f"[!] Не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


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
    send_telegram_blocks(f"📊 <b>{title}</b>", [r["text"] for r in rows])


def main():
    products = load_json(PRODUCTS_FILE, [])
    history = load_json(HISTORY_FILE, {})

    now = datetime.now(MOSCOW_TZ)
    timestamp = now.strftime("%d.%m.%Y %H:%M МСК")

    # Убираем дубли артикулов, сохраняя порядок
    seen = set()
    unique_products = []
    for p in products:
        nid = str(p.get("nm_id"))
        if nid in seen:
            continue
        seen.add(nid)
        unique_products.append(p)

    nm_ids = [str(p["nm_id"]) for p in unique_products]
    print(f"Запускаю браузер для {len(nm_ids)} товаров...")
    fetched = fetch_prices_via_browser(nm_ids)
    print(f"Успешно получено: {len(fetched)} из {len(nm_ids)}")

    items = []
    error_lines = []

    for product in unique_products:
        nm_id = str(product["nm_id"])
        label = product.get("name") or nm_id
        ptype = product.get("type", "own")

        result = fetched.get(nm_id)
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
            "nm_id": nm_id, "price": price, "last_price": last_price,
            "wb_name": wb_name, "brand": brand, "color": color, "ptype": ptype,
        })

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

    send_telegram_blocks(f"💰 <b>Отчёт по ценам Wildberries</b> ({timestamp})", report_lines)
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
            week_start, now,
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
            month_start, now,
        )
        meta["last_monthly_report"] = month_id
        print("Месячный отчёт с медианой отправлен.")

    save_json(HISTORY_FILE, history)


if __name__ == "__main__":
    main()
