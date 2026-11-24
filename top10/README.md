# Top 10 add-on for Nuvio / Stremio

Adds **Top 10 Movies** and **Top 10 Series** rows with big rank numbers to the home screen. Nothing to host: a
GitHub Action (`.github/workflows/top10.yml`) rebuilds it every day and publishes static files to the
`top10-addon` branch.

## Install

In Nuvio: Settings → Addons → paste one URL → Install.

| Style | URL |
| --- | --- |
| Netflix: outlined number tucked behind the poster, compact square tiles | `https://raw.githubusercontent.com/kazzzzzzzzzzuya/simple-proxy/top10-addon/netflix/manifest.json` |
| Apple TV: white number in the poster's corner | `https://raw.githubusercontent.com/kazzzzzzzzzzuya/simple-proxy/top10-addon/apple/manifest.json` |

Then in Settings → Home Screen (catalogs), move the Top 10 rows to where you want them.

- Both styles also work with **Landscape Posters** on.
- If the numbers don't show, a custom poster URL (RPDB-style rating posters) is replacing them. Turn it off for the
  Home screen. The Netflix style is usually left alone, because those patterns skip wide cards.
- Don't pick the Top 10 rows as **Hero Catalogs**. The hero uses the clean artwork anyway, so it would look fine,
  just repetitive.

## Data

- Default: Cinemeta's popular list (IMDb popularity). No key needed.
- Better: add a repository secret `TMDB_API_KEY` (a TMDB v3 API key or v4 read token) and the lists switch to TMDB
  **trending today**.

Run it now: Actions → Top 10 add-on → Run workflow. Local build: `pip install pillow` then
`BASE_URL=http://localhost:8000 python top10/build.py out`.

Fonts: Inter Display Black (`fonts/OFL.txt`) and League Gothic (`fonts/OFL-LeagueGothic.txt`), both SIL Open Font License 1.1.
