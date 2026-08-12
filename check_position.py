"""
Проверяет позиции отслеживаемых товаров в поисковой выдаче Wildberries
по ОДНОМУ поисковому запросу и отправляет результат в Telegram.

Работает через Playwright по той же причине, что и price_monitor.py:
обычные HTTP-запросы (в т.ч. с cookies, браузерными заголовками и
имитацией TLS-отпечатка через curl_cffi) получают от WB 403. Настоящий
браузер открывает страницу поиска как обычный посетитель, а мы
перехватываем ответы API, которые страница загружает сама.

Переменные окружения:
  SEARCH_QUERY        — какой запрос проверять (из workflow input)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
"""

import json
import os
import sys
from pathlib import Path
from urllib.parse import quote

import requests
from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "history.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
QUERY = os.environ.get("SEARCH_QUERY", "").strip()

# Сколько страниц выдачи просматривать (на странице обычно ~100 товаров)
MAX_PAGES = 4


def product_url(nm_id: str) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def search_via_browser(query: str) -> list:
    """
    Открывает страницы поиска в настоящем браузере и собирает товары в
    порядке их показа, перехватывая ответы поискового API.
    """
    all_products = []

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

        try:
            page.goto("https://www.wildberries.ru/", timeout=45000, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
        except Exception as e:
            print(f"[!] Не удалось открыть главную страницу: {e}", file=sys.stderr)

        for page_num in range(1, MAX_PAGES + 1):
            captured = []

            def handle_response(response, _captured=captured):
                url = response.url
                if "search" in url and ("exactmatch" in url or "catalog" in url):
                    try:
                        payload = response.json()
                    except Exception:
                        return
                    products = (
                        payload.get("products")
                        or (payload.get("data") or {}).get("products")
                        or []
                    )
                    if products:
                        _captured.append(products)

            page.on("response", handle_response)

            search_url = (
                f"https://www.wildberries.ru/catalog/0/search.aspx"
                f"?search={quote(query)}&page={page_num}&sort=popular"
            )
            try:
                page.goto(search_url, timeout=45000, wait_until="domcontentloaded")
                for _ in range(24):
                    if captured:
                        break
                    page.wait_for_timeout(500)
                # Немного прокручиваем — WB догружает часть карточек лениво
                page.mouse.wheel(0, 4000)
                page.wait_for_timeout(1500)
            except Exception as e:
                print(f"[!] Ошибка загрузки страницы поиска {page_num}: {e}", file=sys.stderr)

            page.remove_listener("response", handle_response)

            if not captured:
                print(f"[i] Страница {page_num}: ничего не перехвачено, останавливаюсь", file=sys.stderr)
                break

            page_products = []
            for chunk in captured:
                page_products.extend(chunk)
            print(f"Страница {page_num}: получено {len(page_products)} товаров")
            all_products.extend(page_products)

        context.close()
        browser.close()

    return all_products


def send_telegram_blocks(header: str, blocks: list[str]):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID не заданы", file=sys.stderr)
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
                url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}, timeout=15
            )
            data = resp.json()
            if not data.get("ok"):
                print(f"[!] Telegram API вернул ошибку: {data.get('description')}", file=sys.stderr)
            else:
                print("Отчёт по позициям отправлен в Telegram.")
        except Exception as e:
            print(f"[!] Не удалось отправить сообщение в Telegram: {e}", file=sys.stderr)


def main():
    if not QUERY:
        print("[!] SEARCH_QUERY не задан — нечего проверять", file=sys.stderr)
        return

    tracked_raw = load_json(PRODUCTS_FILE, [])
    seen_ids = set()
    tracked_products = []
    for p in tracked_raw:
        nid = str(p.get("nm_id"))
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        tracked_products.append(p)

    history = load_json(HISTORY_FILE, {})

    print(f"Ищу «{QUERY}» через браузер...")
    results = search_via_browser(QUERY)
    print(f"Всего собрано {len(results)} позиций выдачи")

    position_by_id = {}
    info_by_id = {}
    for idx, p in enumerate(results):
        pid = str(p.get("id"))
        position_by_id.setdefault(pid, idx + 1)
        info_by_id[pid] = p

    rows = []
    for product in tracked_products:
        nm_id = str(product["nm_id"])
        label = product.get("name") or nm_id
        ptype = product.get("type", "own")
        pos = position_by_id.get(nm_id)
        item = info_by_id.get(nm_id)

        if item:
            brand = item.get("brand", "") or label
            colors = item.get("colors") or []
            color = colors[0].get("name", "") if colors else "н/д"
            price_kopecks = None
            for size in item.get("sizes") or []:
                pb = size.get("price") or {}
                price_kopecks = pb.get("product") or pb.get("total") or pb.get("basic")
                if price_kopecks:
                    break
            price_str = f"{round(price_kopecks / 100)} ₽" if price_kopecks else "н/д"
        else:
            hist_entry = history.get(nm_id, {})
            brand = hist_entry.get("brand") or label
            color = hist_entry.get("color") or "н/д"
            hist_points = hist_entry.get("points") or []
            if hist_points:
                price_str = f"{hist_points[-1]['price']} ₽ (посл. известная)"
            else:
                price_str = "н/д"

        type_label = "🏠" if ptype == "own" else "🔎"
        link = f'<a href="{product_url(nm_id)}">арт. {nm_id}</a>'
        place_str = f"<b>{pos}</b>" if pos else f"<b>вне топ-{len(results)}</b>"

        rows.append({
            "pos": pos if pos is not None else float("inf"),
            "text": f"{type_label} {brand} — {color} — {link} — {price_str} — Место: {place_str}",
        })

    rows.sort(key=lambda r: r["pos"])
    header = f"📍 <b>Позиции по запросу</b> «{QUERY}» (просмотрено {len(results)} товаров в выдаче)"
    send_telegram_blocks(header, [r["text"] for r in rows])


if __name__ == "__main__":
    main()
