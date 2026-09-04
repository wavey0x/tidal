import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, readdirSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createServer } from "vite";

test("production build cannot embed legacy browser operator credentials", () => {
  const outputDir = mkdtempSync(join(tmpdir(), "tidal-browser-build-"));
  const key = "tidal-test-browser-key-must-not-ship";
  const token = "tidal-test-legacy-token-must-not-ship";
  try {
    execFileSync("npm", ["run", "build", "--", "--outDir", outputDir, "--emptyOutDir"], {
      cwd: fileURLToPath(new URL("..", import.meta.url)),
      env: { ...process.env, VITE_TIDAL_API_KEY: key, VITE_TIDAL_API_TOKEN: token },
      stdio: "pipe",
    });
    const assetsDir = join(outputDir, "assets");
    const scripts = readdirSync(assetsDir).filter((name) => name.endsWith(".js"));
    assert.ok(scripts.length > 0);
    for (const name of scripts) {
      const source = readFileSync(join(assetsDir, name), "utf8");
      assert.equal(source.includes(key) || source.includes(token), false, "operator key embedded in browser build");
      assert.equal(source.includes("Authorization"), false, "browser build injects an authorization header");
    }
  } finally {
    rmSync(outputDir, { recursive: true, force: true });
  }
});

test("development modules expose only public URL configuration, not legacy credentials", async () => {
  const values = {
    VITE_TIDAL_API_KEY: "tidal-test-dev-key-must-not-ship",
    VITE_TIDAL_API_TOKEN: "tidal-test-dev-token-must-not-ship",
    VITE_UNRELATED_SECRET: "tidal-test-other-secret-must-not-ship",
    VITE_TIDAL_API_BASE_URL: "https://primary.example.test/api",
    VITE_FACTORY_DASHBOARD_API_BASE_URL: "https://legacy.example.test/api",
    TIDAL_API_PROXY_TARGET: "http://primary.example.test",
    FACTORY_DASHBOARD_API_PROXY_TARGET: "http://legacy.example.test",
  };
  const previous = Object.fromEntries(Object.keys(values).map((key) => [key, process.env[key]]));
  Object.assign(process.env, values);
  let server;
  try {
    server = await createServer({
      root: fileURLToPath(new URL("..", import.meta.url)),
      envFile: false,
      server: { middlewareMode: true, hmr: false, preTransformRequests: false },
      optimizeDeps: { noDiscovery: true, include: [] },
    });
    const transformed = await server.transformRequest("/src/App.jsx");
    assert.ok(transformed);
    for (const key of ["VITE_TIDAL_API_KEY", "VITE_TIDAL_API_TOKEN", "VITE_UNRELATED_SECRET"]) {
      assert.equal(transformed.code.includes(values[key]), false, `${key} embedded in development output`);
      assert.equal(key in server.config.env, false);
    }
    for (const key of ["VITE_TIDAL_API_BASE_URL", "VITE_FACTORY_DASHBOARD_API_BASE_URL"]) {
      assert.ok(transformed.code.includes(values[key]), `${key} public URL missing`);
    }
    assert.equal(server.config.server.proxy["/api"].target, values.TIDAL_API_PROXY_TARGET);
    await server.close();
    server = undefined;
    process.env.TIDAL_API_PROXY_TARGET = "";
    server = await createServer({
      root: fileURLToPath(new URL("..", import.meta.url)),
      envFile: false,
      server: { middlewareMode: true, hmr: false, preTransformRequests: false },
      optimizeDeps: { noDiscovery: true, include: [] },
    });
    assert.equal(server.config.server.proxy["/api"].target, values.FACTORY_DASHBOARD_API_PROXY_TARGET);
  } finally {
    await server?.close();
    for (const [key, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
