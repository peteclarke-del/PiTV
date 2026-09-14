# PiTV web interface

Svelte 5 + Vite front end for the PiTV FastAPI backend. The build output goes to
`../pitv/web/static/` and is served by `pitv web`; the Pi never runs Node.

```sh
npm install        # install pinned dependencies
npm run dev        # dev server on http://localhost:5173, proxies /api to http://127.0.0.1:8080
npm run build      # production build into ../pitv/web/static
```

`npm run check` runs svelte-check.
