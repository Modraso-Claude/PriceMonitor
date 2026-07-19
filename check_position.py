"""
Проверяет позиции отслеживаемых товаров в поисковой выдаче Wildberries
по ОДНОМУ поисковому запросу и отправляет результат в Telegram.

В отличие от price_monitor.py — запускается НЕ по расписанию, а по
требованию: бот на PythonAnywhere дёргает GitHub Actions через API
(workflow_dispatch) при нажатии кнопки в Telegram, передавая нужный
запрос как input. Сам поиск идёт отсюда, потому что у GitHub Actions
неограниченный интернет — search.wb.ru заблокирован на бесплатном
тарифе PythonAnywhere, а здесь такой проблемы нет.

Переменные окружения:
  SEARCH_QUERY        — какой запрос проверять (передаётся из workflow input)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID — те же секреты, что у price_monitor.py
"""

import json
import os
import sys
import time
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"
HISTORY_FILE = BASE_DIR / "history.json"

DEST = "-1257786"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
QUERY = os.environ.get("SEARCH_QUERY", "").strip()


def product_url(nm_id: str) -> str:
    return f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"


def load_json(path: Path, default):
    if path.exists():
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


def search_wb(query: str, max_pages: int = 5):
    """
    Сканирует выдачу постранично. Не останавливается после первой же
    пустой/ошибочной страницы (WB иногда отдаёт временный сбой на
    отдельной странице) — сдаётся только после двух неудач подряд.
    """
    all_products = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
        "Referer": "https://www.wildberries.ru/",
        "Accept": "application/json",
    }
    consecutive_failures = 0
    for page in range(1, max_pages + 1):
        url = "https://search.wb.ru/exactmatch/ru/common/v18/search"
        params = {
            "appType": 1, "curr": "rub", "dest": DEST, "lang": "ru",
            "page": page, "query": query, "resultset": "catalog",
            "sort": "popular", "spp": 30,
        }
        try:
            resp = requests.get(url, params=params, headers=headers, timeout=15)
            resp.raise_for_status()
            data = resp.json()
            products = data.get("products") or (data.get("data") or {}).get("products") or []
        except requests.exceptions.HTTPError as e:
            is_rate_limited = e.response is not None and e.response.status_code == 429
            print(f"[!] Ошибка поиска '{query}', страница {page}: {e}", file=sys.stderr)
            if is_rate_limited:
                print("[i] Похоже на ограничение частоты запросов — пауза подольше перед следующей попыткой", file=sys.stderr)
                time.sleep(2.5)
            products = []
        except Exception as e:
            print(f"[!] Ошибка поиска '{query}', страница {page}: {e}", file=sys.stderr)
            products = []

        if not products:
            consecutive_failures += 1
            print(f"[i] Страница {page} пустая (подряд неудач: {consecutive_failures})", file=sys.stderr)
            if consecutive_failures >= 2:
                break
            time.sleep(0.7)
            continue

        consecutive_failures = 0
        all_products.extend(products)
        time.sleep(0.3)
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

    tracked_products_raw = load_json(PRODUCTS_FILE, [])
    seen_ids = set()
    tracked_products = []
    for p in tracked_products_raw:
        nid = str(p.get("nm_id"))
        if nid in seen_ids:
            continue
        seen_ids.add(nid)
        tracked_products.append(p)

    history = load_json(HISTORY_FILE, {})
    results = search_wb(QUERY)

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
            # Товар нашёлся в этой выдаче — берём свежие данные прямо из неё
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
            # Товар не найден в просканированной части выдачи — этот конкретный
            # запрос ничего о нём не знает. Подставляем последние известные
            # данные из history.json (их накапливает price_monitor.py дважды
            # в день), а не пустое "н/д".
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
