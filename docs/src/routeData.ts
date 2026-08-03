import { defineRouteMiddleware } from "@astrojs/starlight/route-data";

/**
 * Announce the version these docs describe on every page.
 *
 * Starlight's `banner` is a per-page frontmatter option, so setting it here
 * saves repeating it on every page and forgetting it on a new one.
 */
export const onRequest = defineRouteMiddleware((context) => {
  context.locals.starlightRoute.entry.data.banner = {
    content:
      'These docs describe the current <strong>alpha</strong>, which is the ' +
      'recommended version. Install it with ' +
      '<a href="/modbus-connection/getting-started/installation/"><code>pip install --pre</code></a>.',
  };
});
