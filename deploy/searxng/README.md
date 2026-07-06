# Private SearXNG + Tor on a VPS

Run your own **unlimited, keyless** search endpoint on a VPS and reach it from
anywhere over an SSH tunnel. SearXNG's engine requests exit through **Tor**, and
the endpoint is **never exposed publicly** — so nothing sensitive (VPS IP, keys)
ever goes into this repo or any committed file.

## What's here

| File | Purpose | Committed? |
|------|---------|------------|
| `docker-compose.yml` | SearXNG + Tor, SearXNG bound to `127.0.0.1:8080` | yes (no secrets) |
| `settings.yml.template` | SearXNG config; `secret_key` is a placeholder | yes (no secrets) |
| `setup.sh` | Renders `settings.yml` with a random secret, starts the stack | yes |
| `settings.yml` | The rendered config **with the real secret** | **no — gitignored** |

The VPS address lives only in *your* SSH command and the `SEARXNG_URL` env var on
your workstation — never in the repo.

## 1. On the VPS (once)

Needs Docker + the compose plugin.

```bash
# get just this folder (or clone the whole repo)
git clone https://github.com/Underwater-404/web-graph-crawler.git
cd web-graph-crawler/deploy/searxng
./setup.sh
```

`setup.sh` generates a random `secret_key` into `settings.yml` (gitignored) and
starts both containers. SearXNG listens on `127.0.0.1:8080` on the VPS only.

## 2. From your workstation (Kali)

Open an SSH tunnel and point the tool at the local end. The VPS address is only
in this command — nothing is committed.

```bash
ssh -fN -L 8080:127.0.0.1:8080 <vps-user>@<vps-host>
export SEARXNG_URL=http://127.0.0.1:8080

web-graph-crawler --search-provider searxng --dorks dorks.txt \
  --results-per-dork 10 --out data/links.csv
```

`--search-provider searxng` reads `SEARXNG_URL` automatically (or pass
`--searxng-url http://127.0.0.1:8080`).

Verify the endpoint through the tunnel:

```bash
curl -s "http://127.0.0.1:8080/search?q=test&format=json" \
  | grep -o '"url"' | wc -l          # >0 means it works
```

## Tor caveats (be realistic)

- **Google/Bing block most Tor exit IPs**, so over Tor those engines often
  return little. SearXNG still aggregates engines that tolerate Tor (Mojeek,
  Brave, DuckDuckGo, Wikipedia, ...). If you need Google-grade `inurl:`/
  `filetype:` results, keep using the `serper` provider for discovery and use
  this SearXNG box for broader/keyless sweeps.
- Tor is slower — keep `--results-per-dork` modest (e.g. 10) and expect longer
  runs. Timeouts are raised to 15/30s in `settings.yml.template`.
- Want a fresh Tor circuit? `docker compose restart tor`.
- To go direct instead of Tor (faster, uses the VPS IP), delete the `outgoing.proxies`
  block from `settings.yml` and `docker compose restart searxng`.

## Security notes

- SearXNG binds to `127.0.0.1` on the VPS; the only way in is your SSH tunnel.
- `limiter: false` is safe **only because** the instance isn't public. Do not
  publish port 8080.
- The real `secret_key` lives in `settings.yml` on the VPS, which is gitignored —
  it is never committed.
