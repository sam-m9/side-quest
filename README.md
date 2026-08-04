# Side Quest

A self-contained, offline-first PWA for tracking events around town. Everything
(app code, styles, images, seed data) lives in a single `public/index.html`, so
there is no build step and no framework to install.

## How it's deployed

The app runs as a **Cloudflare Worker serving static assets** — no server code.
It's wired to this Git repo through Cloudflare's built-in Git integration
("Workers Builds"), so **every push to the production branch redeploys the site
automatically**. You never need to install or run `wrangler` locally.

Live URL: https://side-quest.samarthmaira9.workers.dev/

### Layout

```
public/            <- everything here is served at the site root
  index.html       <- the entire app
  manifest.json    <- PWA manifest (Add to Home Screen)
  _headers         <- cache rules (app shell = no-cache so updates land instantly)
  icons/           <- home-screen / favicon icons
wrangler.jsonc     <- static-assets config Cloudflare reads on each build
seed-events.json   <- reference copy of the events bundled into the app
```

### The config that makes it work (`wrangler.jsonc`)

```jsonc
{
  "name": "side-quest",
  "compatibility_date": "2025-06-01",
  "assets": {
    "directory": "./public",
    "not_found_handling": "single-page-application"
  }
}
```

`name` matches the existing Worker (`side-quest`) so deploys keep the same URL.

## Connecting the repo to Cloudflare (one-time)

If the Worker isn't connected to this repo yet:

1. Cloudflare dashboard → **Workers & Pages** → open the **side-quest** Worker.
2. **Settings → Build** → **Connect** → pick GitHub repo `sam-m9/side-quest`.
3. Production branch: `main`. Build command: leave empty. Deploy command:
   `npx wrangler deploy` (the default). Save.
4. Cloudflare runs the first build and every subsequent push auto-deploys.

## Making updates

Edit `public/index.html` (or any file under `public/`), commit, and push to the
production branch. Cloudflare rebuilds and the change is live in ~30–60s. On
phones with the app added to the home screen, relaunching while online pulls the
new version (the app shell is served `no-cache`).

## Events data

The app stores events in the browser's `localStorage`. On a fresh install
(including a new "Add to Home Screen"), it seeds itself with the events in
`SEED_EVENTS` inside `index.html`. `seed-events.json` is a version-controlled
copy of that same data; it can also be re-imported anytime via the app's
**Import** button.
