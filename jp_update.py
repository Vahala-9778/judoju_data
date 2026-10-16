"""일본 시세 받기 — GitHub Actions에서 평일마다 실행된다.

만드는 파일(out/ → 저장소의 'data' 릴리스에 올라감):
  jp_listing.csv   JPX 상장 종목표: code,name,market,sector33,sector17,size
  jp_prices.parquet  대상 종목 일봉 5년: date,code,close,adj_close,volume
  jp_shares.csv    주식 수·시총: code,shares,mcap,src
  report.txt       무엇이 되고 안 됐는지 기록(저장소에도 커밋)

대상(UNIVERSE 환경변수): topix500(기본) = TOPIX Core30+Large70+Mid400 / topix1000 = +Small 1 / prime = 프라임 전체
"""
import io, os, re, sys, time, traceback
from pathlib import Path

import pandas as pd
import requests

OUT = Path("out")
OUT.mkdir(exist_ok=True)
UNIVERSE = os.environ.get("UNIVERSE", "topix500")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/129.0 Safari/537.36"}
REPORT = []


def log(s=""):
    print(s, flush=True)
    REPORT.append(str(s))


def save_report():
    txt = "\n".join(REPORT) + "\n"
    (OUT / "report.txt").write_text(txt, encoding="utf-8")
    Path("report.txt").write_text(txt, encoding="utf-8")


# ---------------------------------------------------------------- 1. 종목표
JPX_XLS = "https://www.jpx.co.jp/markets/statistics-equities/misc/tvdivq0000001vg2-att/data_j.xls"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/misc/01.html"
COLS = {"コード": "code", "銘柄名": "name", "市場・商品区分": "market",
        "33業種区分": "sector33", "17業種区分": "sector17", "規模区分": "size"}


def jpx_listing():
    t = time.time()
    r = requests.get(JPX_XLS, headers=UA, timeout=60)
    if r.status_code != 200 or len(r.content) < 100_000:
        page = requests.get(JPX_PAGE, headers=UA, timeout=60).text
        m = re.search(r'href="([^"]+data_j\.xls)"', page)
        if not m:
            raise RuntimeError(f"종목표 주소를 못 찾음(상태 {r.status_code})")
        r = requests.get("https://www.jpx.co.jp" + m.group(1), headers=UA, timeout=60)
    r.raise_for_status()
    df = pd.read_excel(io.BytesIO(r.content), dtype=str)
    df = df.rename(columns=COLS)[list(COLS.values())]
    df["code"] = df["code"].str.strip()
    for c in df.columns:
        df[c] = df[c].fillna("").str.strip()
    log(f"JPX 종목표: {len(df):,}종목, {len(r.content):,}바이트, {time.time() - t:.1f}초")
    log("  시장: " + ", ".join(f"{k} {v}" for k, v in df["market"].value_counts().head(6).items()))
    log("  규모: " + ", ".join(f"{k} {v}" for k, v in df["size"].value_counts().items()))
    log(f"  33업종 {df['sector33'].nunique()}개")
    return df


def pick_universe(lst):
    stock = lst[lst["market"].str.contains("内国株式")]
    if UNIVERSE == "prime":
        u = stock[stock["market"].str.startswith("プライム")]
    elif UNIVERSE == "topix1000":
        u = stock[stock["size"].str.contains("Core30|Large70|Mid400|Small 1")]
    else:
        u = stock[stock["size"].str.contains("Core30|Large70|Mid400")]
    log(f"대상({UNIVERSE}): {len(u)}종목")
    return u["code"].tolist()


# ---------------------------------------------------------------- 2. 시세
def yahoo_prices(codes, period="5y"):
    import yfinance as yf
    log(f"yfinance {yf.__version__}")
    frames, t = [], time.time()

    def grab(batch):
        df = yf.download([f"{c}.T" for c in batch], period=period, interval="1d",
                         auto_adjust=False, group_by="column", threads=True, progress=False)
        if df is None or df.empty:
            return pd.DataFrame()
        if not isinstance(df.columns, pd.MultiIndex):  # 한 종목만 받은 경우
            df.columns = pd.MultiIndex.from_product([df.columns, [f"{batch[0]}.T"]])
        long = df.stack(level=1, future_stack=True).reset_index()
        long.columns = [str(c) for c in long.columns]
        long = long.rename(columns={"Date": "date", "Ticker": "code", "Close": "close",
                                    "Adj Close": "adj_close", "Volume": "volume"})
        if "code" not in long.columns:
            long = long.rename(columns={long.columns[1]: "code"})
        long = long[["date", "code", "close", "adj_close", "volume"]].dropna(subset=["close"])
        long["code"] = long["code"].str.replace(".T", "", regex=False)
        return long

    for i in range(0, len(codes), 100):
        batch = codes[i:i + 100]
        try:
            frames.append(grab(batch))
        except Exception as e:
            log(f"  묶음 {i}~ 실패: {type(e).__name__}: {e}")
        time.sleep(2)
    px = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()
    got = set(px["code"]) if len(px) else set()
    miss = [c for c in codes if c not in got]
    if miss and len(miss) < len(codes):
        log(f"  빠진 {len(miss)}종목 다시 시도")
        time.sleep(5)
        try:
            again = grab(miss)
            px = pd.concat([px, again], ignore_index=True)
        except Exception as e:
            log(f"  재시도 실패: {e}")
    if len(px):
        px["date"] = pd.to_datetime(px["date"]).dt.tz_localize(None).dt.normalize()
        px["volume"] = px["volume"].fillna(0).astype("int64")
        px = px.drop_duplicates(["date", "code"], keep="last").sort_values(["code", "date"])
    n = px["code"].nunique() if len(px) else 0
    log(f"야후 일봉: {n}/{len(codes)}종목, {len(px):,}행, {time.time() - t:.0f}초")
    if n:
        last = px["date"].max()
        log(f"  기간 {px['date'].min().date()} ~ {last.date()}, 마지막 날 종목 수 "
            f"{(px['date'] == last).sum()}")
        s = px[px["code"] == "7203"].tail(2)
        if len(s):
            log("  예시 7203(도요타): " + s[["date", "close", "adj_close", "volume"]]
                .to_string(index=False, header=False).replace("\n", " | "))
    miss = sorted(set(codes) - (set(px["code"]) if len(px) else set()))
    if miss:
        log(f"  못 받은 종목 {len(miss)}개: {' '.join(miss[:20])}")
    return px


# ---------------------------------------------------------------- 3. 주식 수
def tv_caps():
    body = {"filter": [{"left": "type", "operation": "equal", "right": "stock"}],
            "markets": ["japan"], "options": {"lang": "en"},
            "columns": ["name", "close", "market_cap_basic", "sector", "industry"],
            "sort": {"sortBy": "market_cap_basic", "sortOrder": "desc"}, "range": [0, 5000]}
    t = time.time()
    r = requests.post("https://scanner.tradingview.com/japan/scan", json=body,
                      headers=UA, timeout=60)
    r.raise_for_status()
    rows = []
    for it in r.json().get("data", []):
        exch, code = it["s"].split(":", 1)
        if exch != "TSE":
            continue
        name, close, mcap, sec, ind = it["d"]
        if close and mcap:
            rows.append({"code": code, "shares": mcap / close, "mcap": mcap, "src": "tradingview",
                         "tv_sector": sec, "tv_industry": ind})
    df = pd.DataFrame(rows)
    log(f"트레이딩뷰 시총: {len(df)}종목, {time.time() - t:.1f}초")
    return df


def yahoo_shares(codes):
    import yfinance as yf
    rows, t = [], time.time()
    for c in codes:
        try:
            fi = yf.Ticker(f"{c}.T").fast_info
            sh = fi.get("shares")
            if sh:
                rows.append({"code": c, "shares": float(sh), "mcap": fi.get("marketCap"),
                             "src": "yahoo"})
        except Exception:
            pass
    log(f"야후 주식 수: {len(rows)}/{len(codes)}종목, {time.time() - t:.0f}초")
    return pd.DataFrame(rows)


def step(name, fn, *a):
    log(f"\n## {name}")
    try:
        return fn(*a)
    except Exception as e:
        log(f"실패: {type(e).__name__}: {e}")
        log("  " + traceback.format_exc(limit=3).strip().splitlines()[-2].strip())
        return None


if __name__ == "__main__":
    log(f"일본 데이터 갱신 {time.strftime('%Y-%m-%d %H:%M UTC', time.gmtime())}"
        f" / 파이썬 {sys.version.split()[0]} / 대상 {UNIVERSE}")
    lst = step("1. JPX 상장 종목표", jpx_listing)
    if lst is not None:
        lst.to_csv(OUT / "jp_listing.csv", index=False, encoding="utf-8")
        codes = pick_universe(lst)
    else:
        codes = ["7203", "6758", "8306", "9984", "6861", "8035", "9432", "4063", "6501", "7974"]
        log(f"종목표 없이 시험용 {len(codes)}종목만 받음")
    px = step("2. 야후 일봉 5년", yahoo_prices, codes)
    if px is not None and len(px):
        px.to_parquet(OUT / "jp_prices.parquet", index=False)
    caps = step("3. 트레이딩뷰 시총", tv_caps)
    sh = caps[caps["code"].isin(codes)] if caps is not None and len(caps) else pd.DataFrame()
    miss = sorted(set(codes) - set(sh["code"] if len(sh) else []))
    if miss:
        more = step(f"4. 야후 주식 수(빠진 {len(miss)}종목)", yahoo_shares, miss)
        if more is not None and len(more):
            sh = pd.concat([sh, more], ignore_index=True)
    if len(sh):
        sh.to_csv(OUT / "jp_shares.csv", index=False, encoding="utf-8")
        log(f"\n주식 수 확보: {sh['code'].nunique()}/{len(codes)}종목")
    log("\n파일: " + ", ".join(f"{p.name} {p.stat().st_size // 1024:,}KB"
                              for p in sorted(OUT.iterdir()) if p.name != "report.txt"))
    save_report()
