# Zealy Link Finder

A small X-to-Telegram monitor for new public X posts containing campaign URLs
whose destination is `zealy.io/cw/...`.

## How it works

- Runs every 15 minutes using GitHub Actions.
- Searches X Recent Search with `url:zealy.io has:links`.
- Checks each URL's expanded destination and only alerts for `zealy.io/cw/`.
- Sends the post text, author, date, campaign URL, and original X post URL to Telegram.
- Uses `state.json` / X `since_id` to avoid repeatedly processing old posts.
- The first run establishes a post-ID or timestamp baseline and intentionally sends no historical alerts.

## Important limitations

- X Recent Search covers only the previous 7 days and requires appropriate X API access.
- It can only return posts accessible to your X API plan; it cannot guarantee every public post.
- GitHub scheduled workflows can be delayed. They support a 15-minute schedule, but not exact real-time delivery.
- A public GitHub repository makes the source code public. Never put API tokens in source files.
- The workflow commits `state.json` to the repository so the checkpoint survives between runs. It contains post IDs, not API credentials.

## Setup

1. Rotate/revoke any X or Telegram token previously pasted into a chat. Do not reuse it.
2. Create a **public** GitHub repository and upload these files, including the `.github/workflows/zealy-monitor.yml` workflow.
3. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret** and add:
   - `X_BEARER_TOKEN`: your newly generated X API Bearer Token.
   - `TELEGRAM_BOT_TOKEN`: your newly generated token from `@BotFather`.
   - `TELEGRAM_CHAT_ID`: your Telegram numeric chat ID.
4. In Telegram, open your new bot and press **Start** (or send `/start`).
5. To find your chat ID, open this URL in your own browser after replacing `YOUR_NEW_BOT_TOKEN`:
   `https://api.telegram.org/botYOUR_NEW_BOT_TOKEN/getUpdates`
   Find `message.chat.id` in the response. Do not share this URL or its response with anyone; it contains your bot token.
   If `getUpdates` is empty, send `/start` to the bot again and refresh.
6. In GitHub, open **Settings → Actions → General → Workflow permissions** and allow read and write permissions. The workflow also requests `contents: write`.
7. Open **Actions → Zealy Link Finder → Run workflow** once to establish the baseline. It will not alert on posts already present at setup time.
8. Subsequent runs are scheduled every 15 minutes. Check the Actions tab for logs and failures.

## Local test

Python 3.10+:

```bash
pip install -r requirements.txt
export X_BEARER_TOKEN='your-new-token'
export TELEGRAM_BOT_TOKEN='your-new-token'
export TELEGRAM_CHAT_ID='your-chat-id'
python main.py
```

Do not commit `.env` files or credentials.
