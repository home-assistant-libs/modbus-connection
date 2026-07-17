// @ts-check
import { defineConfig } from "astro/config";
import starlight from "@astrojs/starlight";
import starlightLlmsTxt from "starlight-llms-txt";

// Project GitHub Pages site: https://home-assistant-libs.github.io/modbus-connection/
// `base` is the repository name, so every internal link is prefixed correctly.
// https://astro.build/config
export default defineConfig({
  site: "https://home-assistant-libs.github.io",
  base: "/modbus-connection",
  integrations: [
    starlight({
      title: "modbus-connection",
      description:
        "A small, backend-neutral Modbus connection abstraction and device-modelling framework for Python.",
      plugins: [
        starlightLlmsTxt({
          projectName: "modbus-connection",
          description:
            "modbus-connection is a small, backend-neutral Modbus connection abstraction for Python. The top-level package is a pure interface — the ModbusConnection base class, the ModbusUnit Protocol, and shared params dataclasses — with interchangeable tmodbus and pymodbus backends, each exporting a concrete ModbusConnection plus eager connect_tcp/connect_udp/connect_tls/connect_serial factories. An optional modbus_connection.model framework maps a device's registers and coils to typed Python attributes and reads a device in as few Modbus calls as possible.",
          details: [
            "Important notes for working with modbus-connection:",
            "",
            "- Install with `pip install \"modbus-connection[tmodbus]\"` or `[pymodbus]`; the bare install pulls no backend.",
            "- A connection is owner-held with no `connect()` on the object. Each backend exports a `ModbusConnection` (`modbus_connection.tmodbus.ModbusConnection` / `modbus_connection.pymodbus.ModbusConnection`) built from the shared `ModbusTcpParams` / `ModbusUdpParams` / `ModbusTlsParams` / `ModbusSerialParams` dataclasses. Direct construction does no I/O (connects on the first request); the `connect_*` factories connect immediately and raise on an unreachable device. Either way it reconnects on demand, requests interrupted by a drop are retried once, and after `close()` requests raise `ClientClosedError`. tmodbus covers TCP (socket/rtu), TLS, and serial; pymodbus additionally covers UDP and ASCII-over-TCP.",
            "- Consumers receive a `ModbusUnit` via `connection.for_unit(unit_id)` — a stateless per-unit handle. Every method raises on failure; it never returns `None`.",
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
            {
              label: "Connections and units",
              slug: "getting-started/connections-and-units",
            },
          ],
        },
        {
          label: "Device modelling",
          items: [
            { label: "Overview", slug: "modelling/overview" },
            { label: "Built-in fields", slug: "modelling/fields" },
            { label: "SunSpec", slug: "modelling/sunspec" },
            { label: "Repeated sub-units", slug: "modelling/repeats" },
            { label: "Component groups", slug: "modelling/component-group" },
            { label: "Manual components", slug: "modelling/manual-component" },
          ],
        },
        {
          label: "Building a library",
          items: [
            { label: "Library entrypoint", slug: "patterns/library" },
            { label: "Query helper", slug: "patterns/query-helper" },
          ],
        },
        {
          label: "Home Assistant",
          items: [
            {
              label: "Modbus YAML configuration",
              slug: "home-assistant/yaml-configuration",
            },
            {
              label: "Integration structure",
              slug: "home-assistant/integration",
            },
          ],
        },
        {
          label: "Reference",
          items: [
            { label: "Exceptions", slug: "reference/exceptions" },
            { label: "Testing", slug: "reference/testing" },
          ],
        },
      ],
    }),
  ],
});
