# modbus-connection documentation

The modbus-connection documentation site, built with [Astro
Starlight](https://starlight.astro.build/).

## Local development

```bash
cd docs
npm install
npm run dev      # serve at http://localhost:4321/modbus-connection
npm run build    # build static site to ./dist
```

## Structure

```
docs/
├── astro.config.mjs              # site + sidebar configuration
├── src/
│   ├── content.config.ts         # Starlight content collection
│   ├── styles/custom.css         # accent + typography
│   └── content/docs/
│       ├── index.mdx             # landing page / introduction
│       ├── getting-started/      # installation, connections and units
│       ├── modelling/            # the device-modelling framework
│       ├── patterns/             # library entrypoint, query helper
│       ├── home-assistant/       # HA YAML config, custom integrations
│       └── reference/            # exceptions, testing
```

The site is published to GitHub Pages by
[`.github/workflows/docs.yaml`](../.github/workflows/docs.yaml) on every push to
`main`.
