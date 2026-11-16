# Earth Anomaly Scout

A small bot that watches free Earth-science feeds, throws out the noise, has an AI (via OpenRouter)
score what's left, and sends you a daily shortlist of video-worthy stories. Big breaking quakes or
volcanoes ping you right away.

- One container, one SQLite file, standard-library Python. No MinIO, no n8n.
- Expected cost: about $1–5/month in OpenRouter usage. Feeds and notifications are free.

## How it works

1. **Every 3 hours** it reads the feeds in `feeds.json` (USGS quakes, GDACS, Smithsonian volcano report,
   NSIDC, ScienceDaily, Phys.org, NASA Earth Observatory).
2. **Skips anything it has already seen** (SQLite remembers every item).
3. **Hard rules** drop most items for free:
   - Quakes: M6.0+, or M5.0+ in North America, or shallow M5.5+, or a "doublet" (two M5.5+ within 300 km / 72 h).
   - GDACS: orange/red alerts, any volcano, or anything in North America.
   - Volcano report: only real activity changes (eruption, alert level, ash, swarm…).
   - Science news: must mention an Earth topic AND a hook word ("unexpected", "beneath", "record"…).
4. **AI scores the survivors** in batches: hook, how explainable it is with real data, and North America
   relevance (Canada gets a bonus). It also suggests a title.
5. **Daily digest** at 7:00 your time: top 5 stories scoring 6+, each with title idea, why, scores and the
   original source link (feed that link to your fact-check step).
6. **Breaking alert**: a quake/volcano/disaster with a strong hook and solid data (8.5+ on those two,
   location ignored) pings you immediately. A mid-Atlantic doublet still counts.
7. If a feed URL breaks, the digest tells you which one so you can fix `feeds.json`.

## Deploy on Coolify (about 10 minutes, from your laptop)

1. Push this folder to a **private** GitHub repo.
2. Coolify → New Resource → your repo → build pack **Docker Compose**.
3. In the resource's **Environment Variables**, add the values from `.env.example`:
   - `OPENROUTER_API_KEY` — create at openrouter.ai → Keys. Add a few dollars of credit.
   - `SCOUT_MODEL` — pick a cheap, fast model on openrouter.ai/models and paste its id exactly.
   - `SCOUT_FALLBACK_MODEL` — optional, a cheap model from a different provider.
   - Notifications, pick one:
     - **Telegram:** message @BotFather → `/newbot` → copy the token. Send your bot any message, then open
       `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
     - **ntfy:** install the ntfy app, subscribe to a long random topic name, set
       `NTFY_URL=https://ntfy.sh/<that-topic>` (or your own ntfy server).
4. Deploy. The `scout-data` volume keeps the SQLite file across redeploys.
5. In Coolify's terminal for the container, check it:
   ```
   python scout.py test-notify   # should buzz your phone
   python scout.py scan          # one full run now
   python scout.py stats         # what got stored/scored
   python scout.py digest        # force today's digest
   ```

## Tuning

- **Too much noise?** Raise `DIGEST_MIN_SCORE` to 7, or tighten the hook/topic word lists at the top of `scout.py`.
- **Too quiet?** Lower it to 5, or add feeds to `feeds.json` (`kind` = `science` for keyword filtering,
  `trusted` to always pass, `rss` or `usgs` as `format`). Set `"enabled": false` to switch one off.
- **Compare models:** run a week on one model, switch `SCOUT_MODEL`, compare which picks you'd actually make.
- **Costs:** each scan sends at most 60 items to the AI (`AI_MAX_ITEMS_PER_RUN`), in batches of 20.

## Notes

- The feed URLs weren't live-tested from the build environment. If one is wrong, the digest footer
  will flag it on day one.
- The AI only ranks and suggests titles. Facts for scripts should always come from the source link.
