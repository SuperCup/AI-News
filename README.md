# AI News

Daily and weekly AI news collection, static publishing, and WeCom push delivery.

## What It Does

- Runs a daily AI news collection at 09:17 Asia/Shanghai.
- Gives extra weight to top US and China AI companies, with stronger weighting for product launches and real-world applications.
- Balances the 15-item daily list across China, US, and other regions at roughly one third each.
- Publishes a static site grouped by day and week.
- Sends a WeCom push with an overview image plus a clickable news page URL.
- Keeps the web pages card-first, without embedding the overview image in the page body.
- Builds a weekly top-10 impact digest every Sunday evening.

## GitHub Secrets

Set these repository secrets before enabling the workflow:

- `WECHAT_WEBHOOK_URL`: required for Enterprise WeChat pushes.
- `MOONSHOT_API_KEY`: recommended for Kimi-based Chinese summarization, ranking, and weekly impact extraction.
- `OPENAI_API_KEY`: optional fallback for OpenAI or another OpenAI-compatible provider.
- `SEARCH_TAVILY`: optional Tavily search/news enrichment. `TAVILY_API_KEY` is also supported as an alias.
- `SERPER_API_KEY`: optional Google search/news enrichment.
- `BING_SEARCH_API_KEY`: optional Bing News Search enrichment.

## GitHub Variables

Optional repository variables:

- `OPENAI_MODEL`: defaults to `kimi-k2.6`. For DeepSeek, use `deepseek-chat`.
- `OPENAI_BASE_URL`: defaults to `https://api.moonshot.ai/v1` for Kimi. For DeepSeek, use `https://api.deepseek.com`.
- `SITE_BASE_URL`: defaults to `https://supercup.github.io/AI-News`.
- `TAVILY_COMPANY_SEARCH`: optional. Set to `true` only if you want Tavily to query every priority company as well as topic searches.

The pipeline still runs with RSS fallback if search API keys are missing, but coverage and summary quality are better with the keys above.

## Static Site

The generated site is designed for GitHub Pages:

`https://supercup.github.io/AI-News/`

Daily pages are published under:

`https://supercup.github.io/AI-News/daily/YYYY-MM-DD/`

Weekly pages are published under:

`https://supercup.github.io/AI-News/weekly/YYYY-Www/`

## Local Commands

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Run a daily collection without sending:

```bash
python scripts/run_daily.py
```

Run a daily collection with Tavily locally, without sending:

```bash
SEARCH_TAVILY="tvly-..." python scripts/run_daily.py
```

Run a daily collection and send to WeCom:

```bash
WECHAT_WEBHOOK_URL="https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=..." python scripts/run_daily.py --send
```

Render the site from existing data:

```bash
python scripts/render_site.py
```

Build a weekly digest:

```bash
python scripts/run_weekly.py
```

For GitHub push-triggered tests, `[run-daily]` or `[run-weekly]` runs collection without sending to WeCom. Add `[send]` to the commit message only when you intentionally want that push-triggered run to send.

## GitHub Pages

The workflow writes the static site to `docs/`. If `https://supercup.github.io/AI-News/` returns 404 after the first push, enable GitHub Pages once:

1. Open repository `Settings`.
2. Go to `Pages`.
3. Set source to `Deploy from a branch`.
4. Select branch `main` and folder `/docs`.
5. Save.

After that, daily and weekly workflow runs will keep the hosted site updated.
