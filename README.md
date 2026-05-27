# AI News

Daily and weekly AI news collection, static publishing, and WeCom push delivery.

## What It Does

- Runs a daily AI news collection at 09:00 Asia/Shanghai.
- Gives extra weight to top US and China AI companies.
- Keeps China-company-related items at no less than one third of the daily list when enough qualified items are available.
- Publishes a static site grouped by day and week.
- Sends a WeCom push with an overview image plus a clickable news page URL.
- Builds a weekly top-10 impact digest every Sunday evening.

## GitHub Secrets

Set these repository secrets before enabling the workflow:

- `WECHAT_WEBHOOK_URL`: required for Enterprise WeChat pushes.
- `OPENAI_API_KEY`: recommended for Chinese summarization, ranking, and weekly impact extraction. If you use an OpenAI-compatible provider, put that provider's API key here.
- `SERPER_API_KEY`: optional Google search/news enrichment.
- `BING_SEARCH_API_KEY`: optional Bing News Search enrichment.

## GitHub Variables

Optional repository variables:

- `OPENAI_MODEL`: defaults to `gpt-5-mini`. For DeepSeek, use `deepseek-chat`.
- `OPENAI_BASE_URL`: optional OpenAI-compatible endpoint. For DeepSeek, use `https://api.deepseek.com`.
- `SITE_BASE_URL`: defaults to `https://supercup.github.io/AI-News`.

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

## GitHub Pages

The workflow writes the static site to `docs/`. If `https://supercup.github.io/AI-News/` returns 404 after the first push, enable GitHub Pages once:

1. Open repository `Settings`.
2. Go to `Pages`.
3. Set source to `Deploy from a branch`.
4. Select branch `main` and folder `/docs`.
5. Save.

After that, daily and weekly workflow runs will keep the hosted site updated.
