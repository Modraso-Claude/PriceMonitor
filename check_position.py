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
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent
PRODUCTS_FILE = BASE_DIR / "products.json"

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


def search_wb(query: str, max_pages: int = 10):
    """Сканирует выдачу постранично, возвращает товары в порядке позиций."""
    all_products = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    }
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
        except Exception as e:
            print(f"[!] Ошибка поиска '{query}', страница {page}: {e}", file=sys.stderr)
            break
        products = data.get("products") or (data.get("data") or {}).get("products") or []
        if not products:
            break
        all_products.extend(products)
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

    tracked_products = load_json(PRODUCTS_FILE, [])
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
            brand = item.get("brand", "") or "—"
            colors = item.get("colors") or []
            color = colors[0].get("name", "") if colors else "—"
            price_kopecks = None
            for size in item.get("sizes") or []:
                pb = size.get("price") or {}
                price_kopecks = pb.get("product") or pb.get("total") or pb.get("basic")
                if price_kopecks:
                    break
            price_str = f"{round(price_kopecks / 100)} ₽" if price_kopecks else "—"
        else:
            brand = label
            color = "—"
            price_str = "—"

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
