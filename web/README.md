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

The admin is split into a PiTV section and a pitv_content section (see
[docs/PLAN.md](../docs/PLAN.md) section 6.3) so it is clear which app a setting affects.
pitv_content's pages reach its API through PiTV: `/api/sources` for its sources and
`/api/content/tool/api/*` for everything else (`src/lib/toolapi.js`).

Shared building blocks, so a page is mostly its columns and fields:

- `components/DataTable.svelte` (with `Pager.svelte` and `lib/table.js`): every table of
  records. Columns are `{key, label, title?, get?, cell?, class?, sortable?}`; `cell` is a
  snippet declared at the top level of the page and receives the row and the column.
- `components/SchemaForm.svelte` and `SettingField.svelte`: settings forms for both apps from
  their schemas, with `lib/settingTypes.js` cleaning each value type before it is saved.
- `components/Tabs.svelte` and `LevelSwitch.svelte`, with `lib/prefs.svelte.js`: the
  familiarity level (Basic, Standard, Advanced) and per-table preferences, kept in
  localStorage and never required: without storage they last until the page is reloaded.
