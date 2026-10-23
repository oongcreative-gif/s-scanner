# S기법 스캐너

시총 2조 이상 종목의 20일 엔벨로프(±20%) 하단 근접 종목을 매일 평일 18:40(한국시간)에 스캔해서 사이트에 올리고, 새로 하단 10% 이내에 들어온 종목은 텔레그램으로 알려줘요.

- 사이트: GitHub Pages (main / docs)
- 첫 실행: Actions → s_scan → Run workflow, `force` 칸에 `1` (장 마감 후 18:40 이후에 실행)
- Secrets: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
