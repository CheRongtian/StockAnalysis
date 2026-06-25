#!/usr/bin/env python3
"""
科技主题强势股扫描脚本。

可选环境变量：
  STOCK_ANALYSIS_SECTOR_LIMIT=3              只跑前 N 个主题，便于试跑
  STOCK_ANALYSIS_MAX_STOCKS_PER_SECTOR=80   每个主题最多处理 N 只股票；0 表示不限制
  STOCK_ANALYSIS_MAX_PAGES=30               HTML 分页兜底时最多抓取页数
  STOCK_ANALYSIS_OUTPUT=/path/to/file.json  自定义输出文件
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import requests
from bs4 import BeautifulSoup


TECH_SECTORS = [
    {
        "name": "小金属",
        "aliases": ["小金属概念", "稀有金属", "稀缺资源"],
    },
    {
        "name": "AI应用",
        "aliases": ["AI应用", "AIGC概念", "ChatGPT概念"],
    },
    {
        "name": "算力",
        "aliases": ["东数西算(算力)", "算力租赁", "算力概念"],
    },
    {
        "name": "芯片",
        "aliases": ["芯片概念", "半导体概念", "集成电路概念"],
    },
    {
        "name": "商业航天",
        "aliases": ["商业航天", "卫星导航", "军工信息化"],
    },
    {
        "name": "机器人",
        "aliases": ["人形机器人", "机器人概念", "工业机器人"],
    },
]


THS_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 Chrome/122"
    ),
    "Referer": "https://q.10jqka.com.cn/",
}
SINA_HEADERS = {"Referer": "https://finance.sina.com.cn"}
QQ_HEADERS = {"Referer": "https://finance.qq.com"}

REQUEST_TIMEOUT_SECONDS = float(os.getenv("STOCK_ANALYSIS_SOURCE_TIMEOUT", "12"))
REQUEST_SLEEP_SECONDS = float(os.getenv("STOCK_ANALYSIS_REQUEST_SLEEP", "0.05"))
MAX_PAGES = int(os.getenv("STOCK_ANALYSIS_MAX_PAGES", "30"))
MAX_STOCKS_PER_SECTOR = int(os.getenv("STOCK_ANALYSIS_MAX_STOCKS_PER_SECTOR", "0"))
SECTOR_LIMIT = int(os.getenv("STOCK_ANALYSIS_SECTOR_LIMIT", "0"))
OUTPUT_FILE = os.getenv("STOCK_ANALYSIS_OUTPUT", "")

SESSION = requests.Session()


@dataclass
class ConceptMatch:
    symbol: str
    code: str | None = None
    source_name: str | None = None


def import_akshare():
    try:
        import akshare as ak  # type: ignore

        return ak
    except Exception as exc:
        print(f"  AkShare 不可用：{exc}")
        return None


def now_beijing() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=8)


def row_get(row: dict[str, Any], columns: Iterable[str]) -> str:
    for col in columns:
        if col not in row:
            continue
        value = row[col]
        if value is None:
            continue
        text = str(value).strip()
        if text and text.lower() not in {"nan", "none", "--"}:
            return text
    return ""


def first_existing_column(columns: Iterable[Any], candidates: Iterable[str]) -> Any | None:
    column_list = list(columns)
    normalized = {str(col).strip().lower(): col for col in column_list}
    for candidate in candidates:
        key = candidate.strip().lower()
        if key in normalized:
            return normalized[key]
    return None


def normalize_concept_name(name: str) -> str:
    text = name.strip().lower()
    text = text.replace("（", "(").replace("）", ")")
    text = re.sub(r"(概念|板块|主题)", "", text)
    text = re.sub(r"[\s_\-·/\\]+", "", text)
    return text


def clean_stock_code(value: Any) -> str:
    text = str(value).strip()
    match = re.search(r"\d{6}", text)
    return match.group(0) if match else ""


def safe_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        number = float(value)
        if number != number:
            return None
        return number
    except Exception:
        return None


def mean(values: list[float]) -> float:
    return sum(values) / len(values)


def extract_ths_code_from_record(record: dict[str, Any]) -> str | None:
    for key in ("代码", "板块代码", "code", "Code", "symbol"):
        raw = str(record.get(key, "")).strip()
        if re.fullmatch(r"\d{6}", raw):
            return raw

    for value in record.values():
        match = re.search(r"/code/(\d{6})", str(value))
        if match:
            return match.group(1)
    return None


def load_ths_concepts(ak: Any) -> list[dict[str, str]]:
    if ak is None or not hasattr(ak, "stock_board_concept_name_ths"):
        return []

    try:
        df = ak.stock_board_concept_name_ths()
    except Exception as exc:
        print(f"  概念列表抓取失败：{exc}")
        return []

    rows = []
    for record in df.to_dict("records"):
        name = row_get(
            record,
            ["概念名称", "板块名称", "名称", "name", "Name", "symbol", "Symbol"],
        )
        if not name:
            continue
        code = extract_ths_code_from_record(record)
        rows.append(
            {
                "name": name,
                "norm": normalize_concept_name(name),
                "code": code or "",
            }
        )
    print(f"  已加载同花顺概念列表：{len(rows)} 个")
    return rows


def resolve_concept(sector: dict[str, Any], concepts: list[dict[str, str]]) -> ConceptMatch:
    aliases = [sector["name"], *sector.get("aliases", [])]
    if not concepts:
        return ConceptMatch(symbol=sector.get("aliases", [sector["name"]])[0])

    by_norm = {item["norm"]: item for item in concepts}
    for alias in aliases:
        norm = normalize_concept_name(alias)
        item = by_norm.get(norm)
        if item:
            return ConceptMatch(symbol=item["name"], code=item["code"] or None, source_name=item["name"])

    for alias in aliases:
        norm = normalize_concept_name(alias)
        candidates = [
            item
            for item in concepts
            if norm and (norm in item["norm"] or item["norm"] in norm)
        ]
        if candidates:
            candidates.sort(key=lambda item: abs(len(item["norm"]) - len(norm)))
            item = candidates[0]
            return ConceptMatch(symbol=item["name"], code=item["code"] or None, source_name=item["name"])

    return ConceptMatch(symbol=sector.get("aliases", [sector["name"]])[0])


def get_market(code: str) -> str:
    if code.startswith("6"):
        return "sh"
    if code.startswith(("4", "8", "9")):
        return "bj"
    return "sz"


def fetch_sector_index(ak: Any, sector: dict[str, Any], match: ConceptMatch) -> tuple[str, list[float], list[dict[str, Any]]]:
    if ak is None or not hasattr(ak, "stock_board_concept_index_ths"):
        return match.symbol, [], []

    today = date.today().strftime("%Y%m%d")
    start = (date.today() - timedelta(days=180)).strftime("%Y%m%d")
    candidates = [match.symbol, *sector.get("aliases", []), sector["name"]]
    seen = set()

    for symbol in candidates:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        try:
            df = ak.stock_board_concept_index_ths(
                symbol=symbol,
                start_date=start,
                end_date=today,
            )
        except Exception:
            continue

        if df is None or len(df) < 22:
            continue

        date_col = first_existing_column(df.columns, ["日期", "date", "Date"])
        open_col = first_existing_column(df.columns, ["开盘价", "开盘", "open", "Open"])
        high_col = first_existing_column(df.columns, ["最高价", "最高", "high", "High"])
        low_col = first_existing_column(df.columns, ["最低价", "最低", "low", "Low"])
        close_col = first_existing_column(df.columns, ["收盘价", "收盘", "close", "Close"])
        if close_col is None:
            continue

        closes = [safe_float(value) for value in df[close_col].tolist()]
        closes = [value for value in closes if value is not None]
        if len(closes) < 22:
            continue

        klines = []
        if all(col is not None for col in (date_col, open_col, high_col, low_col, close_col)):
            for _, row in df.tail(90).iterrows():
                open_value = safe_float(row[open_col])
                high_value = safe_float(row[high_col])
                low_value = safe_float(row[low_col])
                close_value = safe_float(row[close_col])
                if None in (open_value, high_value, low_value, close_value):
                    continue
                klines.append(
                    {
                        "date": str(row[date_col]),
                        "open": round(open_value, 3),
                        "high": round(high_value, 3),
                        "low": round(low_value, 3),
                        "close": round(close_value, 3),
                    }
                )
        return symbol, closes, klines

    return match.symbol, [], []


def dedupe_stocks(stocks: list[dict[str, str]]) -> list[dict[str, str]]:
    seen = set()
    result = []
    for stock in stocks:
        code = clean_stock_code(stock.get("code", ""))
        name = str(stock.get("name", "")).strip()
        if not code or code in seen:
            continue
        seen.add(code)
        result.append({"code": code, "name": name or code})
    return result


def fetch_sector_stocks_ak(ak: Any, sector: dict[str, Any], match: ConceptMatch) -> tuple[str, list[dict[str, str]]]:
    if ak is None or not hasattr(ak, "stock_board_concept_cons_ths"):
        return "", []

    candidates = [match.symbol, *sector.get("aliases", []), sector["name"]]
    seen = set()
    for symbol in candidates:
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        try:
            df = ak.stock_board_concept_cons_ths(symbol=symbol)
        except Exception:
            continue

        if df is None or len(df) == 0:
            continue

        code_col = first_existing_column(df.columns, ["代码", "股票代码", "code", "Code"])
        name_col = first_existing_column(df.columns, ["名称", "股票简称", "股票名称", "name", "Name"])
        if code_col is None or name_col is None:
            continue

        stocks = []
        for _, row in df.iterrows():
            code = clean_stock_code(row[code_col])
            name = str(row[name_col]).strip()
            if code:
                stocks.append({"code": code, "name": name})

        stocks = dedupe_stocks(stocks)
        if stocks:
            return symbol, stocks

    return "", []


def fetch_sector_stocks_html(ths_code: str | None) -> list[dict[str, str]]:
    if not ths_code:
        return []

    stocks = []
    seen = set()
    empty_pages = 0

    for page in range(1, MAX_PAGES + 1):
        url = f"https://q.10jqka.com.cn/gn/detail/code/{ths_code}/page/{page}/"
        try:
            response = SESSION.get(url, headers=THS_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, "html.parser", from_encoding="gbk")
        except Exception as exc:
            print(f"    HTML 分页抓取失败 page={page}: {exc}")
            break

        table = soup.find("table")
        if not table:
            empty_pages += 1
            if empty_pages >= 2:
                break
            continue

        new_count = 0
        for row in table.find_all("tr")[1:]:
            cols = row.find_all("td")
            if len(cols) < 3:
                continue
            code = clean_stock_code(cols[1].get_text(strip=True))
            name = cols[2].get_text(strip=True)
            if code and code not in seen:
                seen.add(code)
                stocks.append({"code": code, "name": name or code})
                new_count += 1

        if new_count == 0:
            break

        print(f"    HTML page={page} 新增 {new_count} 只，累计 {len(stocks)} 只")
        time.sleep(0.25)

    return stocks


def fetch_sector_stocks(ak: Any, sector: dict[str, Any], match: ConceptMatch) -> tuple[str, list[dict[str, str]]]:
    sources = []
    source_symbol, ak_stocks = fetch_sector_stocks_ak(ak, sector, match)
    if ak_stocks:
        sources.append(f"akshare:{source_symbol}({len(ak_stocks)})")

    html_stocks = fetch_sector_stocks_html(match.code)
    if html_stocks:
        sources.append(f"html:{match.code}({len(html_stocks)})")

    stocks = dedupe_stocks(html_stocks + ak_stocks)
    if stocks:
        return "+".join(sources), stocks

    return "none", []


def fetch_stock_kline_qq(code: str, n: int = 60) -> list[dict[str, Any]]:
    market = get_market(code)
    if market == "bj":
        return []

    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={market}{code},day,,,{n},qfq"
    try:
        response = SESSION.get(url, headers=QQ_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        data = response.json()
        key = f"{market}{code}"
        klines = data["data"][key].get("qfqday", [])
    except Exception:
        return []

    rows = []
    for item in klines:
        if not isinstance(item, list) or len(item) < 3:
            continue
        close = safe_float(item[2])
        if close is None:
            continue
        rows.append({"date": str(item[0]).replace("-", ""), "close": close})
    return rows


def fetch_stock_kline_sina(code: str, n: int = 60) -> list[dict[str, Any]]:
    market = get_market(code)
    url = (
        "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
        f"CN_MarketData.getKLineData?symbol={market}{code}&scale=240&ma=no&datalen={n}"
    )
    try:
        response = SESSION.get(url, headers=SINA_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
        data = json.loads(response.text)
    except Exception:
        return []

    rows = []
    for item in data:
        close = safe_float(item.get("close"))
        if close is None:
            continue
        rows.append({"date": str(item.get("day", "")).replace("-", ""), "close": close})
    return rows


def fetch_stock_kline(code: str, n: int = 60) -> list[dict[str, Any]]:
    if get_market(code) == "bj":
        return fetch_stock_kline_sina(code, n)

    rows = fetch_stock_kline_qq(code, n)
    if rows:
        return rows
    return fetch_stock_kline_sina(code, n)


def classify_stock(kline_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if len(kline_rows) < 22:
        return None

    closes = [safe_float(row.get("close")) for row in kline_rows]
    closes = [value for value in closes if value is not None]
    if len(closes) < 22:
        return None

    def day_state(close: float, ma: float) -> str:
        if close > ma * 1.03:
            return "above"
        if close < ma * 0.97:
            return "below"
        return "tangle"

    history = []
    for i in range(max(20, len(closes) - 30), len(closes)):
        ma = mean(closes[i - 20 : i])
        history.append(day_state(closes[i], ma))

    if not history:
        return None

    ma20_today = mean(closes[-20:])
    close_today = closes[-1]
    prev_close = closes[-2] if len(closes) >= 2 else close_today

    deviation = round((close_today - ma20_today) / ma20_today * 100, 2)
    change_pct = round((close_today - prev_close) / prev_close * 100, 2)

    below_streak = 0
    for state in reversed(history):
        if state == "below":
            below_streak += 1
        else:
            break

    above_streak = 0
    for state in reversed(history):
        if state == "above":
            above_streak += 1
        else:
            break

    today_state = history[-1]
    prev_state = history[-2] if len(history) >= 2 else today_state

    if below_streak >= 3:
        status = "excluded"
    elif today_state == "below":
        status = f"below_d{min(below_streak, 2)}"
    elif today_state == "tangle":
        status = "tangle"
    elif today_state == "above":
        if prev_state in ("below", "tangle") and above_streak == 1:
            status = "breakout_up"
        else:
            status = "above"
    else:
        status = "unknown"

    return {
        "status": status,
        "ma20": round(ma20_today, 3),
        "close": round(close_today, 3),
        "change_pct": change_pct,
        "deviation": deviation,
        "above_streak": above_streak,
        "below_streak": below_streak,
    }


def deviation_alert(deviation: float) -> str | None:
    if abs(deviation) >= 50:
        return "strong"
    if abs(deviation) >= 30:
        return "warn"
    return None


def sector_status_from_index(closes: list[float]) -> tuple[str, float | None, float | None, float | None]:
    if len(closes) < 20:
        return "unknown", None, None, None

    ma20 = mean(closes[-20:])
    close = closes[-1]
    deviation = (close - ma20) / ma20 * 100
    if close > ma20 * 1.03:
        status = "bullish"
    elif close < ma20 * 0.97:
        status = "bearish"
    else:
        status = "tangle"
    return status, round(ma20, 2), round(close, 2), round(deviation, 2)


def status_sort_key(stock: dict[str, Any]) -> tuple[int, float]:
    rank = {
        "breakout_up": 0,
        "above": 1,
        "tangle": 2,
        "below_d1": 3,
        "below_d2": 4,
        "excluded": 5,
    }.get(stock.get("status", ""), 9)
    return rank, -float(stock.get("change_pct") or 0)


def compact_alerts(alerts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[str, dict[str, Any]] = {}
    for alert in alerts:
        code = alert["code"]
        if code not in seen:
            seen[code] = dict(alert)
            seen[code]["sectors"] = [alert["sector"]]
            continue

        if alert["sector"] not in seen[code]["sectors"]:
            seen[code]["sectors"].append(alert["sector"])
        if abs(alert["deviation"]) > abs(seen[code]["deviation"]):
            seen[code].update(
                {
                    "deviation": alert["deviation"],
                    "alert": alert["alert"],
                    "close": alert["close"],
                    "ma20": alert["ma20"],
                }
            )

    result = []
    for alert in seen.values():
        alert["sector"] = "·".join(alert["sectors"])
        del alert["sectors"]
        result.append(alert)
    return sorted(result, key=lambda item: abs(item["deviation"]), reverse=True)


def output_path() -> Path:
    if OUTPUT_FILE:
        return Path(OUTPUT_FILE).expanduser()
    return Path(__file__).resolve().parent / "docs" / "data.json"


def main() -> None:
    started_at = now_beijing()
    print(f"=== 科技主题强势股扫描 {started_at.strftime('%Y-%m-%d %H:%M')} 北京时间 ===")

    ak = import_akshare()
    concepts = load_ths_concepts(ak)

    sectors = TECH_SECTORS[:SECTOR_LIMIT] if SECTOR_LIMIT > 0 else TECH_SECTORS
    output = {
        "updated_at": started_at.strftime("%Y-%m-%d %H:%M"),
        "trade_date": started_at.date().strftime("%Y-%m-%d"),
        "config": {
            "sector_limit": SECTOR_LIMIT or None,
            "max_pages": MAX_PAGES,
            "max_stocks_per_sector": MAX_STOCKS_PER_SECTOR or None,
        },
        "sectors": [],
        "deviation_alerts": [],
        "errors": [],
    }

    for sector in sectors:
        print(f"\n-- {sector['name']} --")
        match = resolve_concept(sector, concepts)
        print(f"  匹配概念: {match.symbol}" + (f" code={match.code}" if match.code else ""))

        index_symbol, sector_closes, sector_klines = fetch_sector_index(ak, sector, match)
        sector_status, sector_ma20, sector_close, sector_dev = sector_status_from_index(sector_closes)
        print(f"  板块指数: {sector_status} close={sector_close} ma20={sector_ma20} source={index_symbol}")

        source, stocks_raw = fetch_sector_stocks(ak, sector, match)
        source_count = len(stocks_raw)
        if MAX_STOCKS_PER_SECTOR > 0 and len(stocks_raw) > MAX_STOCKS_PER_SECTOR:
            stocks_to_process = stocks_raw[:MAX_STOCKS_PER_SECTOR]
            truncated = True
        else:
            stocks_to_process = stocks_raw
            truncated = False

        print(
            f"  成分股: {source_count} 只 source={source} "
            f"处理={len(stocks_to_process)} 只"
        )

        stocks_result = []
        for index, stock in enumerate(stocks_to_process, start=1):
            kline = fetch_stock_kline(stock["code"])
            result = classify_stock(kline)
            if result is None:
                continue

            alert = deviation_alert(result["deviation"])
            stock_data = {
                "code": stock["code"],
                "name": stock["name"],
                **result,
                "deviation_alert": alert,
            }
            stocks_result.append(stock_data)

            if alert:
                output["deviation_alerts"].append(
                    {
                        "sector": sector["name"],
                        "code": stock["code"],
                        "name": stock["name"],
                        "deviation": result["deviation"],
                        "alert": alert,
                        "close": result["close"],
                        "ma20": result["ma20"],
                    }
                )

            if index % 20 == 0:
                print(f"    {index}/{len(stocks_to_process)}")
            time.sleep(REQUEST_SLEEP_SECONDS)

        stocks_result.sort(key=status_sort_key)
        status_counts: dict[str, int] = {}
        for stock in stocks_result:
            status_counts[stock["status"]] = status_counts.get(stock["status"], 0) + 1

        output["sectors"].append(
            {
                "name": sector["name"],
                "matched_symbol": match.symbol,
                "matched_code": match.code,
                "index_symbol": index_symbol,
                "member_source": source,
                "status": sector_status,
                "ma20": sector_ma20,
                "close": sector_close,
                "deviation": sector_dev,
                "source_stock_count": source_count,
                "processed_stock_count": len(stocks_to_process),
                "classified_stock_count": len(stocks_result),
                "truncated": truncated,
                "status_counts": status_counts,
                "stocks": stocks_result,
                "klines": sector_klines,
            }
        )
        print(f"  状态分布: {status_counts}")

        if not stocks_raw:
            output["errors"].append(
                {
                    "sector": sector["name"],
                    "message": "未抓到成分股；可能是概念名不匹配或数据源暂不可用",
                }
            )

    output["deviation_alerts"] = compact_alerts(output["deviation_alerts"])

    path = output_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(output, file, ensure_ascii=False, indent=2)

    print(
        f"\n完成: 板块 {len(output['sectors'])} 个, "
        f"乖离预警 {len(output['deviation_alerts'])} 个"
    )
    print(f"输出文件: {path}")


if __name__ == "__main__":
    main()
