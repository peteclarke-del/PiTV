# PiTV web interface

Svelte 5 and Vite front end for the PiTV FastAPI backend, hash-routed (`#/`, `#/guide`,
`#/remote`, `#/admin/...`). The build output goes to `../pitv/web/static/` and is served by
`pitv web`; the built files are committed so the Pi never runs Node.

```sh
npm install        # install pinned dependencies
npm run dev        # dev server on http://localhost:5173, proxies /api to http://127.0.0.1:8080
npm run build      # production build into ../pitv/web/static (commit the result)
npm run check      # svelte-check
```

For `npm run dev` start the backend first, for example `setup/dev.sh start` from the
repository root, which serves the API on port 8080.
