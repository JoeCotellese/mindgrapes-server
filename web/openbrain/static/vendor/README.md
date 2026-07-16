<!-- ABOUTME: Provenance + upgrade procedure for the self-hosted vendor assets. -->
<!-- ABOUTME: Covers htmx and Bulma; both are pinned, prebuilt, and served by whitenoise. -->

# Vendored front-end assets

These files are checked in deliberately rather than pulled from a CDN. The app
runs on the tailnet and the public internet with no build toolchain; vendoring
keeps every page working offline and removes an external dependency at request
time. Each is loaded from templates via `{% static 'vendor/<file>' %}` and
served by whitenoise.

| File             | Library | Version | Source                                                        |
| ---------------- | ------- | ------- | ------------------------------------------------------------- |
| `htmx.min.js`    | htmx    | 2.0.4   | `https://cdn.jsdelivr.net/npm/htmx.org@2.0.4/dist/htmx.min.js` |
| `bulma.min.css`  | Bulma   | 1.0.4   | `https://cdn.jsdelivr.net/npm/bulma@1.0.4/css/bulma.min.css`   |

## Upgrade procedure

1. Pick the new version from the library's releases (Bulma: stay on the latest
   stable 1.x line; htmx: latest stable 2.x).
2. Re-fetch the prebuilt minified file over the same jsdelivr path, swapping the
   version, e.g.:

   ```bash
   curl -fsSL -o bulma.min.css \
     "https://cdn.jsdelivr.net/npm/bulma@<version>/css/bulma.min.css"
   ```

3. Update the version in the table above.
4. Run the web test suite (`cd web && uv run pytest`) — `test_styling.py`
   asserts the Bulma file is present and defines its core selectors, so a
   bad fetch fails loudly.
5. Eyeball the rendered pages (`make dev-up`) before committing — Bulma majors
   can rename classes; htmx majors can change attribute semantics.

## Why not a Sass build for Bulma

Bulma supports a Sass customization pipeline, but we ship the prebuilt
stylesheet to avoid adding a Node/Sass build step to a Django app that
otherwise has none. A custom theme is intentionally out of scope (#98).
