// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightLinksValidator from "starlight-links-validator";
import starlightLlmsTxt from "starlight-llms-txt";

// Project GitHub Pages site: https://home-assistant-libs.github.io/modbus-connection/
// `base` is the repository name, so every internal link is prefixed correctly.
// https://astro.build/config
export default defineConfig({
  site: "https://home-assistant-libs.github.io",
  base: "/modbus-connection",
  // Keep links to retired page URLs working.
  redirects: {
    "/getting-started/connections-and-units/":
      "/modbus-connection/connection/connections-and-units/",
    "/getting-started/connection-parameters/":
      "/modbus-connection/connection/connections-and-units/",
    "/getting-started/operations/": "/modbus-connection/connection/operations/",
    "/getting-started/encoding-decoding/":
      "/modbus-connection/connection/operations/",
    "/reference/exceptions/": "/modbus-connection/connection/reference/",
    "/reference/testing/": "/modbus-connection/patterns/testing/",
    "/modelling/reference/": "/modbus-connection/modelling/components-reference/",
    "/modelling/sunspec-generation/":
      "/modbus-connection/modelling/sunspec-discovery/",
  },
  integrations: [
    starlight({
      title: "modbus-connection",
      description:
        "A small, backend-neutral Modbus connection abstraction and device-modelling framework for Python.",
      plugins: [
        // Fails the build on a broken internal link or heading anchor.
        starlightLinksValidator(),
        starlightLlmsTxt({
          projectName: "modbus-connection",
          description:
            "modbus-connection is a small, backend-neutral Modbus connection abstraction for Python. The top-level package is a pure interface — the ModbusConnection / ModbusUnit Protocols — with interchangeable tmodbus and pymodbus backends. An optional modbus_connection.model framework maps a device's registers and coils to typed Python attributes and reads a device in as few Modbus calls as possible.",
          details: [
            "Important notes for working with modbus-connection:",
            "",
            "- Install with `pip install \"modbus-connection[tmodbus]\"` or `[pymodbus]`; the bare install pulls neither backend.",
            "- A connection is transient and owner-held. Import `ModbusConnection` from the selected backend and construct it with a shared `ModbusTcpParams` / `ModbusUdpParams` / `ModbusTlsParams` / `ModbusSerialParams` dataclass; the first unit operation connects on demand.",
            "- Consumers receive a `ModbusUnit` via `connection.for_unit(unit_id)`.",
            "- The `modbus_connection.model` framework maps registers/coils to typed attributes on a `Component`; `modbus_connection.model.sunspec` adds SunSpec point types with their unimplemented sentinels.",
            "- The library is not Home Assistant specific, but ships helpers and patterns that make it a good fit for Home Assistant integrations.",
          ].join("\n"),
        }),
      ],
      customCss: ["@fontsource-variable/inter", "./src/styles/custom.css"],
      editLink: {
        baseUrl:
          "https://github.com/home-assistant-libs/modbus-connection/edit/main/docs/",
      },
      social: [
        {
          icon: "github",
          label: "GitHub",
          href: "https://github.com/home-assistant-libs/modbus-connection",
        },
      ],
      sidebar: [
        {
          label: "Getting started",
          items: [
            { label: "Introduction", slug: "index" },
            { label: "Installation", slug: "getting-started/installation" },
            { label: "Quickstart", slug: "getting-started/quickstart" },
            { label: "Choosing a backend", slug: "getting-started/backends" },
          ],
        },
        {
          label: "Modbus Connection",
          items: [
            {
              label: "Connections and units",
              slug: "connection/connections-and-units",
            },
            { label: "Modbus operations", slug: "connection/operations" },
            { label: "Reference", slug: "connection/reference" },
          ],
        },
        {
          label: "Device modelling",
          items: [
            { label: "Overview", slug: "modelling/overview" },
            { label: "Built-in fields", slug: "modelling/fields" },
            { label: "Reading a device", slug: "modelling/reading" },
            { label: "Placing a component", slug: "modelling/placement" },
            { label: "Repeating groups", slug: "modelling/repeats" },
            {
              label: "Restricting fields",
              slug: "modelling/restricting-fields",
            },
            { label: "Component groups", slug: "modelling/component-group" },
            { label: "Manual components", slug: "modelling/manual-component" },
            {
              label: "SunSpec",
              items: [
                { label: "SunSpec fields", slug: "modelling/sunspec" },
                {
                  label: "Discovery and generation",
                  slug: "modelling/sunspec-discovery",
                },
              ],
            },
            { label: "Field reference", slug: "modelling/fields-reference" },
            {
              label: "Component reference",
              slug: "modelling/components-reference",
            },
          ],
        },
        {
          label: "Building a library",
          items: [
            { label: "The device object", slug: "patterns/library" },
            { label: "Query helper", slug: "patterns/query-helper" },
            { label: "Testing", slug: "patterns/testing" },
          ],
        },
        {
          label: "Home Assistant",
          items: [
            {
              label: "Integration structure",
              slug: "home-assistant/integration",
            },
            {
              label: "Modbus YAML configuration",
              slug: "home-assistant/yaml-configuration",
            },
          ],
        },
      ],
    }),
  ],
});
