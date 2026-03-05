import fs from "node:fs";
import path from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { fileURLToPath } from "node:url";

import cors from "cors";
import { getAddress } from "ethers";
import express from "express";

const execFileAsync = promisify(execFile);

const __filename = fileURLToPath(import.meta.url);
const __dirname = path.dirname(__filename);

const DEFAULT_PORT = 8787;
const DEFAULT_LIMIT = 1000;
const MAX_LIMIT = 5000;
const CHAIN_ID = 1;
const LOGO_CACHE_VERSION = "v2";
const defaultDbPath = path.resolve(__dirname, "../../tidal.db");
const dbPath = process.env.TIDAL_DB_PATH
  ? path.resolve(process.cwd(), process.env.TIDAL_DB_PATH)
  : defaultDbPath;
const defaultAuctionMapPath = path.join(path.dirname(dbPath), "strategy_auction_map.json");
const auctionCachePathEnv = process.env.TIDAL_AUCTION_CACHE_PATH || process.env.AUCTION_CACHE_PATH;
const auctionMapPath = auctionCachePathEnv
  ? path.resolve(process.cwd(), auctionCachePathEnv)
  : defaultAuctionMapPath;
const logoCacheRoot = path.resolve(__dirname, "../.cache/token-logos", LOGO_CACHE_VERSION);
const logoCacheTtlMs = 24 * 60 * 60 * 1000;
const logoNegativeCacheTtlMs = 6 * 60 * 60 * 1000;
const logoCandidates = ["logo-32.png", "logo-128.png", "logo.png", "logo.svg"];
const logoMemoryCache = new Map();

if (!fs.existsSync(dbPath)) {
  throw new Error(`Database not found: ${dbPath}`);
}
fs.mkdirSync(logoCacheRoot, { recursive: true });

function checksumOrOriginal(address) {
  if (!address) {
    return null;
  }

  try {
    return getAddress(address);
  } catch {
    return address;
  }
}

function asBoolean(value) {
  return value === 1 || value === true;
}

function normalizeLimit(rawLimit) {
  const parsed = Number.parseInt(String(rawLimit ?? DEFAULT_LIMIT), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return DEFAULT_LIMIT;
  }
  return Math.min(parsed, MAX_LIMIT);
}

function normalizeToken(rawToken) {
  if (typeof rawToken !== "string") {
    return "";
  }

  const token = rawToken.trim();
  if (!token) {
    return "";
  }

  if (!/^0x[a-fA-F0-9]{40}$/.test(token)) {
    return null;
  }

  return token;
}

function getLogoMemoryEntry(key) {
  const entry = logoMemoryCache.get(key);
  if (!entry) {
    return null;
  }
  if (entry.expiresAt < Date.now()) {
    logoMemoryCache.delete(key);
    return null;
  }
  return entry;
}

function setLogoMemoryEntry(key, value, ttlMs) {
  logoMemoryCache.set(key, {
    ...value,
    expiresAt: Date.now() + ttlMs,
  });
}

async function runJsonQuery(sql) {
  const { stdout } = await execFileAsync("sqlite3", ["-json", dbPath, sql], {
    maxBuffer: 20 * 1024 * 1024,
  });

  const payload = stdout.trim();
  if (!payload) {
    return [];
  }

  return JSON.parse(payload);
}

function normalizeAddressCandidate(value) {
  if (typeof value !== "string") {
    return null;
  }
  const trimmed = value.trim();
  if (!/^0x[a-fA-F0-9]{40}$/.test(trimmed)) {
    return null;
  }
  return trimmed.toLowerCase();
}

function loadStrategyAuctionMap() {
  if (!fs.existsSync(auctionMapPath)) {
    return new Map();
  }

  try {
    const raw = fs.readFileSync(auctionMapPath, "utf-8");
    const payload = JSON.parse(raw);
    const strategyToAuction = payload?.strategyToAuction;
    if (!strategyToAuction || typeof strategyToAuction !== "object") {
      return new Map();
    }

    const next = new Map();
    for (const [strategyAddress, auctionAddress] of Object.entries(strategyToAuction)) {
      const normalizedStrategy = normalizeAddressCandidate(strategyAddress);
      if (!normalizedStrategy) {
        continue;
      }
      const normalizedAuction = normalizeAddressCandidate(auctionAddress);
      next.set(normalizedStrategy, normalizedAuction);
    }

    return next;
  } catch {
    return new Map();
  }
}

function readLogoFileIfFresh(filePath) {
  if (!fs.existsSync(filePath)) {
    return null;
  }
  const stat = fs.statSync(filePath);
  if (Date.now() - stat.mtimeMs > logoCacheTtlMs) {
    return null;
  }
  return filePath;
}

function getDiskPaths(chainId, address) {
  const chainDir = path.join(logoCacheRoot, String(chainId));
  fs.mkdirSync(chainDir, { recursive: true });
  const base = path.join(chainDir, address.toLowerCase());
  return {
    chainDir,
    negativePath: `${base}.miss`,
    png32Path: `${base}.logo-32.png`,
    png128Path: `${base}.logo-128.png`,
    pngPath: `${base}.logo.png`,
    svgPath: `${base}.logo.svg`,
  };
}

async function fetchAndCacheLogo(chainId, address, paths) {
  const lowerAddress = address.toLowerCase();
  for (const candidate of logoCandidates) {
    const sourceUrl = `https://assets.smold.app/api/token/${chainId}/${lowerAddress}/${candidate}`;
    const response = await fetch(sourceUrl);
    if (!response.ok) {
      continue;
    }

    const contentType = response.headers.get("content-type") || "";
    if (!contentType.startsWith("image/")) {
      continue;
    }

    const arrayBuffer = await response.arrayBuffer();
    const buffer = Buffer.from(arrayBuffer);
    const targetPath =
      candidate === "logo-32.png"
        ? paths.png32Path
        : candidate === "logo-128.png"
          ? paths.png128Path
          : candidate === "logo.png"
            ? paths.pngPath
            : paths.svgPath;
    fs.writeFileSync(targetPath, buffer);
    return { path: targetPath, contentType };
  }

  if (chainId === 1) {
    const checksumAddress = checksumOrOriginal(address) || address;
    const fallbackUrls = [
      `https://cdn.jsdelivr.net/gh/trustwallet/assets@master/blockchains/ethereum/assets/${checksumAddress}/logo.png`,
      `https://raw.githubusercontent.com/trustwallet/assets/master/blockchains/ethereum/assets/${checksumAddress}/logo.png`,
    ];

    for (const sourceUrl of fallbackUrls) {
      const response = await fetch(sourceUrl);
      if (!response.ok) {
        continue;
      }

      const contentType = response.headers.get("content-type") || "";
      if (!contentType.startsWith("image/")) {
        continue;
      }

      const arrayBuffer = await response.arrayBuffer();
      const buffer = Buffer.from(arrayBuffer);
      fs.writeFileSync(paths.pngPath, buffer);
      return { path: paths.pngPath, contentType };
    }
  }

  fs.writeFileSync(paths.negativePath, String(Date.now()));
  return null;
}

async function resolveLogo(chainId, address) {
  const key = `${chainId}:${address.toLowerCase()}`;
  const cached = getLogoMemoryEntry(key);
  if (cached?.type === "hit") {
    return cached.value;
  }
  if (cached?.type === "miss") {
    return null;
  }

  const paths = getDiskPaths(chainId, address);
  const diskCandidates = [
    paths.png32Path,
    paths.png128Path,
    paths.pngPath,
    paths.svgPath,
  ];
  for (const filePath of diskCandidates) {
    const freshPath = readLogoFileIfFresh(filePath);
    if (!freshPath) {
      continue;
    }
    const contentType = freshPath.endsWith(".svg") ? "image/svg+xml" : "image/png";
    const hit = { path: freshPath, contentType };
    setLogoMemoryEntry(key, { type: "hit", value: hit }, logoCacheTtlMs);
    return hit;
  }

  if (fs.existsSync(paths.negativePath)) {
    const stat = fs.statSync(paths.negativePath);
    if (Date.now() - stat.mtimeMs < logoNegativeCacheTtlMs) {
      setLogoMemoryEntry(key, { type: "miss" }, logoNegativeCacheTtlMs);
      return null;
    }
  }

  const fetched = await fetchAndCacheLogo(chainId, address, paths);
  if (fetched) {
    setLogoMemoryEntry(key, { type: "hit", value: fetched }, logoCacheTtlMs);
    return fetched;
  }

  setLogoMemoryEntry(key, { type: "miss" }, logoNegativeCacheTtlMs);
  return null;
}

const app = express();
app.use(cors());

app.get("/api/health", (_req, res) => {
  res.json({ status: "ok", dbPath });
});

app.get("/api/summary", async (_req, res) => {
  try {
    const rows = await runJsonQuery(`
      SELECT
        COUNT(*) AS row_count,
        COUNT(DISTINCT strategy_address) AS strategy_count,
        COUNT(DISTINCT token_address) AS token_count,
        MAX(scanned_at) AS latest_scan_at
      FROM strategy_token_balances_latest;
    `);

    const summary = rows[0] || {};

    res.json({
      rowCount: Number(summary.row_count || 0),
      strategyCount: Number(summary.strategy_count || 0),
      tokenCount: Number(summary.token_count || 0),
      latestScanAt: summary.latest_scan_at || null,
    });
  } catch (error) {
    res.status(500).json({ error: "summary_query_failed", message: String(error) });
  }
});

app.get("/api/tokens", async (_req, res) => {
  try {
    const rows = await runJsonQuery(`
      SELECT
        b.token_address,
        COALESCE(t.symbol, '') AS token_symbol,
        COUNT(*) AS strategy_count,
        MAX(b.scanned_at) AS latest_scan_at
      FROM strategy_token_balances_latest b
      LEFT JOIN tokens t ON t.address = b.token_address
      GROUP BY b.token_address, t.symbol
      ORDER BY strategy_count DESC, token_symbol ASC, b.token_address ASC;
    `);

    res.json({
      tokens: rows.map((row) => ({
        tokenAddress: checksumOrOriginal(row.token_address),
        tokenSymbol: row.token_symbol || "UNKNOWN",
        strategyCount: Number(row.strategy_count || 0),
        latestScanAt: row.latest_scan_at || null,
      })),
    });
  } catch (error) {
    res.status(500).json({ error: "tokens_query_failed", message: String(error) });
  }
});

app.get("/api/balances", async (req, res) => {
  const token = normalizeToken(req.query.token);
  const limit = normalizeLimit(req.query.limit);

  if (token === null) {
    res.status(400).json({ error: "invalid_token", message: "token must be a 0x-prefixed 40-byte address" });
    return;
  }

  const whereClause = token
    ? `WHERE lower(b.token_address) = lower('${token}')`
    : "";

  try {
    const rows = await runJsonQuery(`
      SELECT
        b.strategy_address,
        b.token_address,
        b.raw_balance,
        b.normalized_balance,
        b.block_number,
        b.scanned_at,
        t.symbol AS token_symbol,
        t.name AS token_name,
        t.decimals AS token_decimals,
        s.name AS strategy_name,
        s.vault_address,
        s.active
      FROM strategy_token_balances_latest b
      LEFT JOIN tokens t ON lower(t.address) = lower(b.token_address)
      LEFT JOIN strategies s ON lower(s.address) = lower(b.strategy_address)
      ${whereClause}
      ORDER BY CAST(b.normalized_balance AS REAL) DESC, b.strategy_address ASC
      LIMIT ${limit};
    `);

    res.json({
      count: rows.length,
      rows: rows.map((row) => ({
        strategyAddress: checksumOrOriginal(row.strategy_address),
        strategyName: row.strategy_name || null,
        vaultAddress: checksumOrOriginal(row.vault_address),
        tokenAddress: checksumOrOriginal(row.token_address),
        tokenSymbol: row.token_symbol || "UNKNOWN",
        tokenName: row.token_name || null,
        tokenDecimals: row.token_decimals == null ? null : Number(row.token_decimals),
        rawBalance: row.raw_balance,
        normalizedBalance: row.normalized_balance,
        blockNumber: row.block_number == null ? null : Number(row.block_number),
        scannedAt: row.scanned_at,
        active: asBoolean(row.active),
      })),
    });
  } catch (error) {
    res.status(500).json({ error: "balances_query_failed", message: String(error) });
  }
});

app.get("/api/strategy-balances", async (req, res) => {
  const limit = normalizeLimit(req.query.limit);
  const strategyAuctionMap = loadStrategyAuctionMap();

  try {
    const rows = await runJsonQuery(`
      SELECT
        b.strategy_address,
        b.token_address,
        b.normalized_balance,
        b.scanned_at,
        t.symbol AS token_symbol,
        t.name AS token_name,
        t.price_usd AS token_price_usd,
        s.name AS strategy_name,
        s.vault_address,
        v.name AS vault_name,
        v.symbol AS vault_symbol,
        s.active
      FROM strategy_token_balances_latest b
      LEFT JOIN tokens t ON lower(t.address) = lower(b.token_address)
      LEFT JOIN strategies s ON lower(s.address) = lower(b.strategy_address)
      LEFT JOIN vaults v ON lower(v.address) = lower(s.vault_address)
      ORDER BY b.strategy_address ASC, CAST(b.normalized_balance AS REAL) DESC;
    `);

    const grouped = new Map();
    for (const row of rows) {
      const key = String(row.strategy_address || "").toLowerCase();
      if (!grouped.has(key)) {
        const auctionAddress = strategyAuctionMap.get(key) || null;
        grouped.set(key, {
          strategyAddress: checksumOrOriginal(row.strategy_address),
          strategyName: row.strategy_name || null,
          vaultAddress: checksumOrOriginal(row.vault_address),
          vaultName: row.vault_name || null,
          vaultSymbol: row.vault_symbol || null,
          auctionAddress: checksumOrOriginal(auctionAddress),
          active: asBoolean(row.active),
          scannedAt: row.scanned_at || null,
          balances: [],
          totalBalance: 0,
          topBalance: 0,
        });
      }

      const target = grouped.get(key);
      const numericBalance = Number.parseFloat(String(row.normalized_balance || "0"));
      const safeNumeric = Number.isFinite(numericBalance) ? numericBalance : 0;
      target.totalBalance += safeNumeric;
      if (safeNumeric > target.topBalance) {
        target.topBalance = safeNumeric;
      }
      if (row.scanned_at && (!target.scannedAt || row.scanned_at > target.scannedAt)) {
        target.scannedAt = row.scanned_at;
      }

      target.balances.push({
        tokenAddress: checksumOrOriginal(row.token_address),
        tokenSymbol: row.token_symbol || "UNKNOWN",
        tokenName: row.token_name || null,
        normalizedBalance: String(row.normalized_balance || "0"),
        tokenPriceUsd: row.token_price_usd == null ? null : String(row.token_price_usd).trim() || null,
      });
    }

    const strategies = Array.from(grouped.values())
      .sort((a, b) => b.topBalance - a.topBalance)
      .slice(0, limit);

    res.json({
      count: strategies.length,
      rows: strategies,
    });
  } catch (error) {
    res.status(500).json({ error: "strategy_balances_query_failed", message: String(error) });
  }
});

app.get("/api/token-logo/:address", async (req, res) => {
  const token = normalizeToken(req.params.address);
  if (token === null || token === "") {
    res.status(400).json({ error: "invalid_token", message: "token must be a 0x-prefixed 40-byte address" });
    return;
  }

  const chainIdRaw = req.query.chainId;
  const chainId = Number.isFinite(Number(chainIdRaw)) ? Number(chainIdRaw) : CHAIN_ID;

  try {
    const resolved = await resolveLogo(chainId, token);
    if (!resolved) {
      res.status(404).json({ error: "logo_not_found" });
      return;
    }

    res.setHeader("Content-Type", resolved.contentType);
    res.setHeader("Cache-Control", "public, max-age=86400, stale-while-revalidate=604800");
    res.sendFile(resolved.path);
  } catch (error) {
    res.status(500).json({ error: "token_logo_fetch_failed", message: String(error) });
  }
});

const distPath = path.resolve(__dirname, "../dist");
if (fs.existsSync(distPath)) {
  app.use(express.static(distPath));

  app.get(/^\/(?!api).*/, (_req, res) => {
    res.sendFile(path.join(distPath, "index.html"));
  });
}

const port = Number.parseInt(process.env.PORT ?? `${DEFAULT_PORT}`, 10);
app.listen(port, () => {
  process.stdout.write(`Tidal UI API listening on http://localhost:${port}\n`);
  process.stdout.write(`Reading database at ${dbPath}\n`);
  process.stdout.write(`Reading auction mapping cache at ${auctionMapPath}\n`);
});
