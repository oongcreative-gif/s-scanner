"""S기법 스캐너 - GitHub Actions용

규칙
1. 시총 2조 이상 종목(KOSPI/KOSDAQ, ETF/스팩/우선주 제외)만 본다
2. 엔벨로프 = 20일 이동평균 +-20%  (HTS 엔벨로프 저항선/지지선과 같은 방식)
3. 하단까지 % = 엔벨로프 하단 / 종가 - 1   (HTS의 지지선 괄호 퍼센트와 같은 값, 예: -8.4%)
   종가가 하단 아래로 내려가면 +로 나온다
4. 하단까지 -10% 이내(=하단이 종가보다 10% 안쪽으로 가까움)면 '근접', 당일 저가가 하단 이하면 '닿음'
5. 새로 근접 구간에 들어온 종목은 텔레그램으로 알린다 (어제도 근접이었으면 다시 알리지 않음)

실행하면
- docs/data/list.json        종목 목록(시총 순)과 하단까지 %
- docs/data/charts/코드.json 종목별 차트 데이터
- docs/data/state.json       어제 근접 종목 (중복 알림 방지)
를 갱신한다.

환경변수: (TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, SITE_URL, FORCE, DRY_RUN)
"""
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

import json
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import FinanceDataReader as fdr

ROOT = os.environ.get('SCAN_ROOT') or os.path.dirname(os.path.abspath(__file__))
KST = ZoneInfo('Asia/Seoul')
MAX_WORKERS = 8


def load_json(path, default):
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, indent=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(obj, f, ensure_ascii=False, indent=indent, allow_nan=False)


def reference_date():
    """삼성전자 최신 봉 날짜 = 가장 최근 거래일"""
    start = (datetime.now(KST) - timedelta(days=14)).strftime('%Y-%m-%d')
    return fdr.DataReader('005930', start).index[-1].date()


def send_telegram(text):
    token = os.environ.get('TELEGRAM_BOT_TOKEN')
    chat_id = os.environ.get('TELEGRAM_CHAT_ID')
    if not token or not chat_id:
        print('텔레그램 설정(TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID)이 없어 알림은 건너뜁니다')
        return False
    try:
        r = requests.post(f'https://api.telegram.org/bot{token}/sendMessage',
                          json={'chat_id': chat_id, 'text': text, 'disable_web_page_preview': True},
                          timeout=20)
        if r.status_code != 200:
            print(f'텔레그램 전송 실패: {r.status_code} {r.text[:200]}')
            return False
        return True
    except requests.RequestException as e:
        print(f'텔레그램 전송 실패: {e!r}')
        return False


S_DIR = os.path.join(ROOT, 'docs', 'data')
LIST_JSON = os.path.join(S_DIR, 'list.json')
STATE_JSON = os.path.join(S_DIR, 'state.json')
UNIVERSE_CSV = os.path.join(ROOT, 'data', 's_universe.csv')

ENV_PERIOD = 20           # 이동평균 기간
ENV_PCT = 20.0            # 엔벨로프 폭 (%)
NEAR_PCT = 10.0           # 하단까지 이 % 이내면 근접
MIN_MARCAP = 2_000_000_000_000   # 시총 2조
CHART_BARS = 130          # 사이트 차트에 쓸 봉 수
LOOKBACK_DAYS = 260
MAX_ERROR_RATIO = 0.2


def get_universe():
    try:
        lst = fdr.StockListing('KRX')
        code_col = 'Code' if 'Code' in lst.columns else 'Symbol'
        lst = lst.rename(columns={code_col: 'Code'})
        lst = lst[lst['Market'].astype(str).str.startswith('KOS')]
        lst = lst[~lst['Name'].str.contains(r'스팩|제\d+호|ETN|ETF', regex=True, na=False)]
        lst = lst[~lst['Name'].str.contains(r'우$|우B$|우\(전환\)$', regex=True, na=False)]
        if 'Dept' in lst.columns:
            lst = lst[~lst['Dept'].astype(str).str.contains('관리|SPAC', na=False)]
        print(f"KRX 목록 {len(lst)}종목, 시총 값 있는 종목 {int(lst['Marcap'].notna().sum())}개, "
              f"시총 최대 {lst['Marcap'].max()}")
        lst = lst[lst['Marcap'] >= MIN_MARCAP].sort_values('Marcap', ascending=False)
        if len(lst) == 0:
            raise RuntimeError('시총 2조 이상 종목이 0개입니다 (장중에는 시총 값이 비어 있을 수 있음)')
        uni = lst[['Code', 'Name', 'Market', 'Marcap']].reset_index(drop=True)
        uni['Code'] = uni['Code'].astype(str).str.zfill(6)
        os.makedirs(os.path.dirname(UNIVERSE_CSV), exist_ok=True)
        uni.to_csv(UNIVERSE_CSV, index=False)
        return uni
    except Exception as e:                                  # noqa: BLE001
        print(f'KRX 종목 목록 조회 실패: {e!r}')
        if os.path.exists(UNIVERSE_CSV):
            cached = pd.read_csv(UNIVERSE_CSV, dtype={'Code': str})
            if len(cached):
                print('-> 저장해 둔 data/s_universe.csv 를 대신 사용합니다')
                return cached
        raise


def envelope(df):
    df = df.dropna(subset=['Open', 'High', 'Low', 'Close']).copy()
    df['ma'] = df['Close'].rolling(ENV_PERIOD).mean()
    df['up'] = df['ma'] * (1 + ENV_PCT / 100)
    df['lo'] = df['ma'] * (1 - ENV_PCT / 100)
    return df


def judge(df):
    """마지막 봉 기준 판정 dict (엔벨로프를 못 구하면 None)"""
    if len(df) < ENV_PERIOD + 2 or pd.isna(df['lo'].iloc[-1]):
        return None
    r = df.iloc[-1]
    close = float(r['Close'])
    prev = float(df['Close'].iloc[-2])
    lower = float(r['lo'])
    gap = (lower / close - 1) * 100                       # 하단까지 %
    return {
        'close': close,
        'change_pct': round((close / prev - 1) * 100, 2) if prev else 0.0,
        'lower': round(lower, 2),
        'upper': round(float(r['up']), 2),
        'gap_pct': round(gap, 2),
        'near': bool(gap >= -NEAR_PCT),
        'touch': bool(float(r['Low']) <= lower),
    }


def make_chart(df):
    tail = df.iloc[-CHART_BARS:]

    def col(name, nd=2):
        return [None if pd.isna(x) else round(float(x), nd) for x in tail[name]]

    return {'t': [d.date().isoformat() for d in tail.index],
            'o': col('Open'), 'h': col('High'), 'l': col('Low'), 'c': col('Close'),
            'up': col('up'), 'lo': col('lo')}


def scan_one(code, start, asof):
    last_err = None
    for _ in range(3):
        try:
            raw = fdr.DataReader(code, start)
            if raw is None or raw.empty:
                return 'none', None
            if raw.index[-1].date() != asof:                # 오늘 봉이 없는 종목(거래정지 등)
                return 'none', None
            df = envelope(raw)
            res = judge(df)
            if res is None:
                return 'none', None
            return 'ok', (res, make_chart(df))
        except Exception as e:                              # noqa: BLE001
            last_err = repr(e)
            time.sleep(0.7)
    return 'error', last_err


def build_message(items, asof):
    site = os.environ.get('SITE_URL', '')
    lines = [f'S기법 하단 근접 {len(items)}종목 ({asof})', '']
    for it in items:
        tag = ' · 하단 터치' if it['touch'] else ''
        lines.append(f"{it['name']} ({it['code']}) 종가 {it['close']:,.0f}원 ({it['change_pct']:+.1f}%)")
        lines.append(f"  엔벨로프 하단까지 {it['gap_pct']:+.1f}%{tag}")
    if site:
        lines += ['', site.rstrip('/') + '/']
    return '\n'.join(lines)


def main():
    now = datetime.now(KST)
    force = os.environ.get('FORCE') == '1'
    dry = os.environ.get('DRY_RUN') == '1'
    asof = reference_date()
    if not force:
        if asof != now.date():
            print(f'오늘({now.date()})은 휴장이거나 아직 데이터가 없어요 (최근 거래일 {asof}). 종료합니다.')
            return 0
        if now.hour < 16:
            print('장 마감 후 데이터가 확정되기 전(16시 이전)이라 종료합니다.')
            return 0

    uni = get_universe()
    print(f'기준일 {asof} / 시총 2조 이상 {len(uni)}종목')
    start = (now - timedelta(days=LOOKBACK_DAYS)).strftime('%Y-%m-%d')
    items, charts, errors, first_error = [], {}, 0, None
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futs = {ex.submit(scan_one, r.Code, start, asof): (k, r) for k, r in enumerate(uni.itertuples(), start=1)}
        for f in as_completed(futs):
            rank, r = futs[f]
            status, payload = f.result()
            if status == 'ok':
                res, chart = payload
                items.append({'rank': rank, 'code': r.Code, 'name': r.Name, 'market': r.Market,
                              'marcap': int(r.Marcap), **res})
                charts[r.Code] = {**chart, 'code': r.Code, 'name': r.Name}
            elif status == 'error':
                errors += 1
                first_error = first_error or payload
    items.sort(key=lambda x: x['rank'])
    near = [i for i in items if i['near']]
    print(f'판정 {len(items)}종목, 하단 {NEAR_PCT:.0f}% 이내 {len(near)}종목, 조회 실패 {errors}종목')
    if len(uni) and errors / len(uni) > MAX_ERROR_RATIO:
        print(f'조회 실패가 너무 많아 결과를 저장하지 않습니다. 첫 에러: {first_error}')
        return 1

    for code, ch in charts.items():
        save_json(os.path.join(S_DIR, 'charts', f'{code}.json'), ch)
    save_json(LIST_JSON, {
        'updated_at': now.isoformat(timespec='seconds'), 'asof': asof.isoformat(),
        'params': {'env_period': ENV_PERIOD, 'env_pct': ENV_PCT, 'near_pct': NEAR_PCT, 'min_marcap': MIN_MARCAP},
        'items': items,
    }, indent=1)
    if dry:
        print('DRY_RUN: 상태 저장과 알림은 건너뜁니다')
        return 0

    state = load_json(STATE_JSON, None)
    prev_near = set(state['near']) if state else set()
    fresh = [i for i in near if i['code'] not in prev_near]          # 새로 근접 구간에 들어온 종목
    exit_code = 0
    notified_ok = True
    if fresh:
        fresh.sort(key=lambda x: x['gap_pct'], reverse=True)
        notified_ok = send_telegram(build_message(fresh, asof.isoformat()))
        if notified_ok:
            print(f'텔레그램 알림 {len(fresh)}종목 전송')
        elif os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID'):
            exit_code = 1
    if notified_ok or not (os.environ.get('TELEGRAM_BOT_TOKEN') and os.environ.get('TELEGRAM_CHAT_ID')):
        save_json(STATE_JSON, {'asof': asof.isoformat(), 'near': sorted(i['code'] for i in near)})
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
