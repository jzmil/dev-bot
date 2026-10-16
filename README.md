# Roblox Developer Discord Tracker

This free tracker checks the public Roblox games of three creators every five minutes and posts new games and later updates to a Discord channel.

| Creator | Roblox ID | Type |
| --- | ---: | --- |
| Tracked user 1 | `7627847305` | User |
| xFrozen x Dudes | `719390069` | Group |
| Tracked user 2 | `71552399` | User |

It uses Roblox's public games and thumbnail APIs, Python, GitHub Actions, and a Discord webhook. The webhook URL is read only from the `DISCORD_WEBHOOK_URL` Actions secret; never put it in a file or commit it.

## Setup

1. In Discord, open the destination channel's **Edit Channel → Integrations → Webhooks**, create a webhook, and copy its URL.
2. In GitHub, open **Settings → Secrets and variables → Actions → New repository secret**. Name it `DISCORD_WEBHOOK_URL` and paste the webhook URL as its value. Do not share or commit the URL.
3. In the repository's **Actions** tab, enable GitHub Actions if prompted. Run **Roblox developer tracker → Run workflow** once, or wait for its next scheduled run.
4. The first successful run records the current games as a baseline and posts one “tracker is online” message. It does not flood the channel with alerts for existing games. Later scans notify for newly listed games and when Roblox reports a newer update timestamp.

The workflow needs repository **Actions → General → Workflow permissions** set to **Read and write permissions** so it can commit `state.json`. The workflow also declares `contents: write`; repository policy can override that permission.

## State and reliability

`state.json` stores the last successful snapshot and is committed by the workflow. The next scan therefore survives fresh GitHub runners. It commits when tracked games change, and at most once per month otherwise, so changing player counts do not create a commit every five minutes. Monthly commits also keep the public repository active for GitHub's scheduled workflow policy. A failed Roblox scan or Discord delivery fails the job before replacing the saved snapshot, so the next scheduled run can try again. If Discord accepts a message but GitHub cannot save the state afterward, a repeat alert can occur; webhooks do not provide an exactly-once delivery guarantee.

Runs are serialized so two scans do not overwrite each other's state. Scheduled scans can be delayed during GitHub load. GitHub may automatically disable scheduled workflows in public repositories after 60 days without repository activity; make a repository change and re-enable the workflow if that happens.

## Cost

The repository is currently public. GitHub-hosted Actions usage is free for public repositories, and the Roblox and Discord APIs used here do not require paid plans. The webhook is a secret and is not included in this public source. If you make the repository private, GitHub-hosted jobs count toward the account's private-repository Actions minutes; this five-minute schedule can exceed the included monthly allowance because GitHub rounds each job up to a full minute.

## Run manually

```bash
python -m pip install -r requirements.txt
DISCORD_WEBHOOK_URL="your-webhook-url" python roblox_tracker.py
```

To inspect the current Roblox data without sending a message or changing state:

```bash
python roblox_tracker.py --dry-run
```
