import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Big from "big.js";
import { keccak_256 } from "js-sha3";
import { useLiveData } from "./useLiveData";
import { useStableRowOrder } from "./useStableRowOrder";
import { useDialogFocus } from "./useDialogFocus";

const ALL_TOKENS = "__all__";
const MIN_USD_VISIBLE = new Big("0.01");
const THEME_SEQUENCE = ["light", "dark"];
const THEME_STORAGE_KEY = "tidal_theme_preference";
const LEGACY_THEME_STORAGE_KEY = "factory_dashboard_theme_preference";
const API_BASE_URL = (
  import.meta.env.VITE_TIDAL_API_BASE_URL
  || import.meta.env.VITE_FACTORY_DASHBOARD_API_BASE_URL
  || "/api/v1/tidal"
).replace(/\/$/, "");
const ETHERSCAN_TX_URL = "https://etherscan.io/tx/";
const ETHERSCAN_ADDRESS_URL = "https://etherscan.io/address/";
const COW_EXPLORER_URL = "https://explorer.cow.fi/address/";
const AUCTIONSCAN_BASE_URL = "https://auctionscan.info";
const AUCTIONSCAN_ICON_SRC = "/auctionscan-favicon.svg";
const DEFAULT_CHAIN_ID = 1;
const FAILED_STATUSES = new Set(["REVERTED", "ERROR", "ESTIMATE_FAILED"]);
const FAINT_STATUSES = new Set(["DRY_RUN", "SUBMITTED", "USER_SKIPPED", "SKIP"]);
const KICK_LOG_PAGE_SIZE = 25;
const ADDRESS_PATTERN = /^0x[a-fA-F0-9]{40}$/;
// Match the scanner-staleness threshold in config/server.yaml.
const SCAN_STALE_AFTER_MS = 90 * 60 * 1000;

function apiUrl(path) {
  return `${API_BASE_URL}${path}`;
}

async function apiFetch(path, options = {}) {
  return fetch(apiUrl(path), options);
}

function parseLocation() {
  const path = window.location.pathname.replace(/^\/+/, "");
  const params = new URLSearchParams(window.location.search);
  const offsetValue = Number.parseInt(params.get("offset") || "0", 10);
  let page = "strategies";
  if (path === "logs" || path === "kicklog") {
    page = "kicks";
  } else if (path === "fee-burner") {
    page = "fee-burner";
  } else if (path === "alerts") {
    page = "alerts";
  }
  return {
    page,
    runId: params.get("run_id") || null,
    kickId: params.get("kick_id") || null,
    logsOffset: Number.isFinite(offsetValue) && offsetValue >= 0 ? offsetValue : 0,
    logsStatus: params.get("status") || "all",
    logsQuery: params.get("q") || "",
  };
}

function navigateTo(page, params) {
  const slug = page === "kicks"
    ? "logs"
    : page === "fee-burner"
      ? "fee-burner"
      : page === "alerts"
        ? "alerts"
        : "strategies";
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params || {})) {
    if (value == null || value === "") {
      continue;
    }
    search.set(key, String(value));
  }
  const qs = search.size ? `?${search.toString()}` : "";
  window.history.pushState(null, "", `/${slug}${qs}`);
}

function getTokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || ALL_TOKENS;
}

function checksumAddress(address) {
  if (typeof address !== "string") {
    return address || null;
  }

  const trimmed = address.trim();
  if (!ADDRESS_PATTERN.test(trimmed)) {
    return trimmed;
  }

  const lower = trimmed.toLowerCase().slice(2);
  const hash = keccak_256(lower);
  let output = "0x";

  for (let index = 0; index < lower.length; index += 1) {
    output += Number.parseInt(hash[index], 16) >= 8 ? lower[index].toUpperCase() : lower[index];
  }

  return output;
}

function shortenAddress(address) {
  const formatted = checksumAddress(address);
  if (!formatted || formatted.length < 13) {
    return formatted || "—";
  }
  return `${formatted.slice(0, 6)}...${formatted.slice(-4)}`;
}

function truncateMiddle(value, maxLength = 18) {
  if (!value || value.length <= maxLength) {
    return value || "—";
  }

  const ellipsis = "...";
  const visibleChars = maxLength - ellipsis.length;
  const frontChars = Math.ceil(visibleChars / 2);
  const backChars = Math.floor(visibleChars / 2);
  return `${value.slice(0, frontChars)}${ellipsis}${value.slice(-backChars)}`;
}

function formatStrategyDisplayName(name) {
  if (!name) {
    return "Unnamed Strategy";
  }

  let output = name;
  if (output.startsWith("Strategy")) {
    output = output.slice("Strategy".length);
  }
  output = output.replaceAll("Curve.fi Factory Crypto Pool:", "");
  output = output.replaceAll("Curve.fi Crypto Pool:", "");
  output = output.replaceAll("Boosted", "");
  output = output.replaceAll("Factory", "");
  output = output.replace(/-{2,}/g, "-").trim();
  output = output.replace(/^-+/, "").replace(/-+$/, "");
  return output || name;
}

function isRabbyProvider(provider, info = null) {
  return Boolean(provider?.isRabby || info?.rdns === "io.rabby");
}

async function getEthereumProvider() {
  if (typeof window === "undefined") {
    return null;
  }

  const { ethereum } = window;
  if (!ethereum) {
    return null;
  }

  const seenProviders = new Set();
  const candidates = [];

  function addProvider(provider, info = null) {
    if (!provider || typeof provider.request !== "function" || seenProviders.has(provider)) {
      return;
    }
    seenProviders.add(provider);
    candidates.push({ provider, info });
  }

  if (Array.isArray(ethereum.providers)) {
    for (const provider of ethereum.providers) {
      addProvider(provider);
    }
  }

  addProvider(ethereum);

  if (typeof window.addEventListener === "function" && typeof window.dispatchEvent === "function") {
    const announcedProviders = await new Promise((resolve) => {
      const detected = [];

      function handleAnnounce(event) {
        const provider = event?.detail?.provider;
        const info = event?.detail?.info || null;
        if (!provider || typeof provider.request !== "function") {
          return;
        }
        detected.push({ provider, info });
      }

      window.addEventListener("eip6963:announceProvider", handleAnnounce);
      window.dispatchEvent(new Event("eip6963:requestProvider"));
      window.setTimeout(() => {
        window.removeEventListener("eip6963:announceProvider", handleAnnounce);
        resolve(detected);
      }, 120);
    });

    for (const announced of announcedProviders) {
      addProvider(announced.provider, announced.info);
    }
  }

  const rabbyCandidate = candidates.find(({ provider, info }) => isRabbyProvider(provider, info));
  if (rabbyCandidate) {
    return rabbyCandidate.provider;
  }

  return candidates[0]?.provider || null;
}

function normalizeChainIdValue(value) {
  if (value == null) {
    return null;
  }

  if (typeof value === "number" && Number.isFinite(value)) {
    return value;
  }

  const normalized = String(value).trim();
  if (!normalized) {
    return null;
  }

  const parsed = normalized.startsWith("0x") ? Number.parseInt(normalized, 16) : Number.parseInt(normalized, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function toHexChainId(chainId) {
  return `0x${Number(chainId).toString(16)}`;
}

function formatDeployConfirmation(spec) {
  const lines = [
    `Deploy auction for ${spec.strategyName || shortenAddress(spec.strategyAddress)}?`,
    "",
    `Factory${spec.factoryVersion ? ` (${spec.factoryVersion})` : ""}: ${shortenAddress(spec.factoryAddress)}`,
    `Receiver: ${shortenAddress(spec.receiverAddress || spec.strategyAddress)}`,
    `Want: ${spec.wantSymbol || shortenAddress(spec.wantAddress)}`,
  ];

  if (spec.inference?.sellTokenAddress) {
    lines.push(
      `Inference token: ${spec.inference.sellTokenSymbol || shortenAddress(spec.inference.sellTokenAddress)}`,
    );
  }
  if (spec.startingPrice) {
    lines.push(`Starting price: ${spec.startingPriceDisplay || spec.startingPrice} ${spec.wantSymbol || "want"}`);
    lines.push(`Raw startingPrice: ${spec.startingPrice}`);
  }
  if (spec.startPriceBufferBps != null) {
    lines.push(`Start-price buffer: +${(Number(spec.startPriceBufferBps) / 100).toFixed(1)}%`);
  }
  if (spec.predictedAuctionAddress) {
    lines.push(`Predicted auction: ${shortenAddress(spec.predictedAuctionAddress)}`);
  }

  lines.push("", "Queue this transaction in your connected wallet?");
  return lines.join("\n");
}

async function waitForTransactionReceipt(provider, txHash, chainId, attempts = 60, delayMs = 2000) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    if (chainId != null && normalizeChainIdValue(await provider.request({ method: "eth_chainId" })) !== chainId) {
      throw new Error(`Switch your wallet to chain ${chainId}, then check again.`);
    }
    const receipt = await provider.request({
      method: "eth_getTransactionReceipt",
      params: [txHash],
    });
    if (receipt) {
      return receipt;
    }
    if (attempt + 1 < attempts) {
      await new Promise((resolve) => window.setTimeout(resolve, delayMs));
    }
  }
  return null;
}

function normalizeReceiptStatus(value) {
  if (typeof value !== "number" && (typeof value !== "string" || !/^(0x[0-9a-f]+|[0-9]+)$/i.test(value))) return null;
  const normalized = Number(value);
  if (normalized === 0 || normalized === 1) {
    return normalized;
  }
  return null;
}

function hexToNumber(value) {
  if (value == null) {
    return null;
  }
  const normalized = String(value);
  const parsed = normalized.startsWith("0x")
    ? Number.parseInt(normalized, 16)
    : Number.parseInt(normalized, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatDeployError(error) {
  const code = error?.code ?? error?.cause?.code;
  if (code === 4001) {
    return "Wallet request rejected";
  }
  if (code === 4902) {
    return "Ethereum mainnet is not configured in this wallet";
  }

  const messages = [
    error?.data?.message,
    error?.cause?.message,
    error?.message,
    typeof error === "string" ? error : null,
  ];

  for (const rawMessage of messages) {
    if (!rawMessage) {
      continue;
    }

    let message = String(rawMessage).trim();
    message = message.replace(/^Internal JSON-RPC error\.?\s*/i, "").trim();
    message = message.replace(/^Error:\s*/i, "").trim();
    if (message) {
      return message;
    }
  }

  return "Unable to queue deployment transaction";
}

function normalizeKick(kick) {
  const chainId = normalizeChainIdValue(kick.chainId) ?? DEFAULT_CHAIN_ID;
  const auctionAddress = kick.auctionAddress || null;
  const auctionScanRoundId = kick.auctionScanRoundId ?? null;
  const auctionScanLinkable = isAuctionScanLinkableKick(kick);
  return {
    ...kick,
    chainId,
    operationType: canonicalOperationType(kick.operationType || "kick"),
    sourceType: kick.sourceType || (kick.strategyAddress ? "strategy" : null),
    sourceAddress: kick.sourceAddress || kick.strategyAddress || null,
    sourceName: kick.sourceName || kick.strategyName || null,
    auctionScanRoundId,
    auctionScanMatchedAt: kick.auctionScanMatchedAt || null,
    auctionScanLastCheckedAt: kick.auctionScanLastCheckedAt || null,
    auctionScanResolved: Boolean(kick.auctionScanResolved ?? (auctionScanRoundId != null)),
    auctionScanEligible: kick.auctionScanEligible ?? undefined,
    auctionScanTxUrl: auctionScanLinkable ? (kick.auctionScanTxUrl || buildAuctionScanTxUrl(kick.txHash)) : null,
    auctionScanAuctionUrl:
      auctionScanLinkable ? (kick.auctionScanAuctionUrl || buildAuctionScanAuctionUrl(chainId, auctionAddress)) : null,
    auctionScanRoundUrl:
      auctionScanLinkable ? (kick.auctionScanRoundUrl || buildAuctionScanRoundUrl(chainId, auctionAddress, auctionScanRoundId)) : null,
    auctionScanResolving: Boolean(kick.auctionScanResolving),
    auctionScanResolveError: kick.auctionScanResolveError || "",
  };
}

function isAuctionScanLinkableKick(kick) {
  return Boolean(kick?.txHash && kick.status === "CONFIRMED");
}

function buildAuctionScanTxUrl(txHash) {
  if (!txHash) {
    return null;
  }
  const normalized = txHash.startsWith("0x") ? txHash : `0x${txHash}`;
  return `${AUCTIONSCAN_BASE_URL}/tx/${normalized}`;
}

function buildAuctionScanAuctionUrl(chainId, auctionAddress) {
  if (!chainId || !auctionAddress) {
    return null;
  }
  return `${AUCTIONSCAN_BASE_URL}/auction/${chainId}/${auctionAddress}`;
}

function buildAuctionScanRoundUrl(chainId, auctionAddress, roundId) {
  if (!chainId || !auctionAddress || roundId == null) {
    return null;
  }
  return `${AUCTIONSCAN_BASE_URL}/round/${chainId}/${auctionAddress}/${roundId}`;
}

function canonicalOperationType(value) {
  const normalized = String(value || "kick").trim().replaceAll("-", "_");
  if (!normalized) return "kick";
  if (normalized === "settle") return "resolve_auction";
  if (normalized === "sweep" || normalized === "sweep_and_settle") return "sweep_auction";
  return normalized;
}

const OPERATION_METADATA = {
  kick: {
    operationType: "kick",
    rowVerb: "KICK",
    detailLabel: "Kick",
    primaryTokenLabel: "Sell",
    secondaryTokenLabel: "Buy",
    showUsd: true,
    showKickPricing: true,
  },
  enable_tokens: {
    operationType: "enable_tokens",
    rowVerb: "ENABLE",
    detailLabel: "Enable Tokens",
    primaryTokenLabel: "Token",
    secondaryTokenLabel: "Auction want",
    showUsd: false,
    showKickPricing: false,
  },
  resolve_auction: {
    operationType: "resolve_auction",
    rowVerb: "SETTLE",
    detailLabel: "Settle",
    primaryTokenLabel: "Token",
    secondaryTokenLabel: "Auction want",
    showUsd: false,
    showKickPricing: false,
  },
  sweep_auction: {
    operationType: "sweep_auction",
    rowVerb: "SWEEP",
    detailLabel: "Sweep",
    primaryTokenLabel: "Token",
    secondaryTokenLabel: "Auction want",
    showUsd: false,
    showKickPricing: false,
  },
};

function getOperationMeta(operationType) {
  const canonical = canonicalOperationType(operationType);
  return OPERATION_METADATA[canonical] || {
    operationType: canonical,
    rowVerb: canonical.replaceAll("_", " ").toUpperCase(),
    detailLabel: canonical.replaceAll("_", " "),
    primaryTokenLabel: "Token",
    secondaryTokenLabel: "Auction want",
    showUsd: false,
    showKickPricing: false,
  };
}

function formatKickPairLabel(kick) {
  const meta = getOperationMeta(kick.operationType);
  const tokenLabel = kick.tokenSymbol || "?";
  if (meta.operationType === "kick") {
    return `${meta.rowVerb} ${tokenLabel} -> ${kick.wantSymbol || "?"}`;
  }
  return `${meta.rowVerb} ${tokenLabel}`;
}

function normalizeDashboardRow(row) {
  const sourceType = row.sourceType || (row.strategyAddress ? "strategy" : "fee_burner");
  const sourceAddress = row.sourceAddress || row.strategyAddress || null;
  const sourceName = row.sourceName || row.strategyName || null;
  const contextType =
    row.contextType || (row.vaultAddress || row.vaultName || row.vaultSymbol ? "vault" : null);
  const contextAddress = row.contextAddress || row.vaultAddress || null;
  const contextName = row.contextName || row.vaultName || null;
  const contextSymbol = row.contextSymbol || row.vaultSymbol || null;

  return {
    ...row,
    sourceType,
    sourceAddress,
    sourceName,
    contextType,
    contextAddress,
    contextName,
    contextSymbol,
    kicks: Array.isArray(row.kicks) ? row.kicks.map(normalizeKick) : [],
  };
}

function withGrouping(value) {
  const [integer, decimal] = value.split(".");
  const grouped = integer.replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  return decimal ? `${grouped}.${decimal}` : grouped;
}

function formatBalance(value) {
  if (!value) {
    return "0.00";
  }

  try {
    return withGrouping(new Big(value).toFixed(2));
  } catch {
    return "0.00";
  }
}

function getAuctionSellTokenTooltip(balance) {
  switch (balance.auctionSellTokenStatus) {
    case "disabled":
      return "Balance present, but token is not enabled in this auction";
    case "unknown":
      return balance.auctionSellTokenStatusError
        || "Auction enabled-token status unavailable from the latest scan";
    default:
      return "";
  }
}

function getKickPrepareTooltip(balance) {
  if (
    balance.kickPrepareStatus !== "PAUSED"
    || balance.kickPrepareReason !== "AUCTION_PRICE_GRANULARITY"
  ) {
    return "";
  }
  return (
    "Kick paused: the sell token is above the USD threshold, but auction v1.0.4 "
    + "rounding means the current market price would not be reached before the auction "
    + "ends. The kick will wait for a larger sell amount."
  );
}

function parseBig(value) {
  if (value == null) {
    return null;
  }

  const normalized = String(value).trim();
  if (!normalized) {
    return null;
  }

  try {
    return new Big(normalized);
  } catch {
    return null;
  }
}

function formatTimestamp(value) {
  if (!value) {
    return "—";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }
  return date.toLocaleString();
}

function formatUtcTimestamp(value) {
  if (!value) return "—";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString(undefined, {
    timeZone: "UTC",
    timeZoneName: "short",
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatRelativeTimestamp(value, nowMs) {
  if (!value) {
    return "—";
  }

  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return value;
  }

  const diffSeconds = Math.floor((nowMs - date.getTime()) / 1000);
  const absSeconds = Math.abs(diffSeconds);

  if (absSeconds < 60) {
    return diffSeconds >= -5 ? "just now" : "in a moment";
  }

  const units = [
    { label: "year", seconds: 365 * 24 * 60 * 60 },
    { label: "month", seconds: 30 * 24 * 60 * 60 },
    { label: "week", seconds: 7 * 24 * 60 * 60 },
    { label: "day", seconds: 24 * 60 * 60 },
    { label: "hour", seconds: 60 * 60 },
    { label: "minute", seconds: 60 },
  ];

  for (const unit of units) {
    if (absSeconds >= unit.seconds) {
      const count = Math.floor(absSeconds / unit.seconds);
      const suffix = count === 1 ? unit.label : `${unit.label}s`;
      return diffSeconds >= 0 ? `${count} ${suffix} ago` : `in ${count} ${suffix}`;
    }
  }

  return "just now";
}

function resolveSystemTheme() {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return "light";
  }
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function getStoredThemePreference() {
  if (typeof window === "undefined") {
    return null;
  }
  const stored = window.localStorage.getItem(THEME_STORAGE_KEY)
    || window.localStorage.getItem(LEGACY_THEME_STORAGE_KEY);
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return null;
}

function useMediaQuery(query) {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches);
  useEffect(() => {
    const mql = window.matchMedia(query);
    const onChange = (e) => setMatches(e.matches);
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, [query]);
  return matches;
}

function SkeletonRows() {
  return [...Array(10)].map((_, index) => (
    <tr key={`skeleton-${index}`} className="strategy-skeleton ledger-skeleton">
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
    </tr>
  ));
}

function CopyIconButton({ valueToCopy, title, ariaLabel, className = "" }) {
  const [copied, setCopied] = useState(false);
  const resetTimerRef = useRef(null);

  useEffect(() => {
    return () => {
      if (resetTimerRef.current) {
        window.clearTimeout(resetTimerRef.current);
      }
    };
  }, []);

  async function onCopy(event) {
    event.stopPropagation();
    if (!valueToCopy || !navigator.clipboard) {
      return;
    }

    try {
      await navigator.clipboard.writeText(valueToCopy);
      setCopied(true);

      if (resetTimerRef.current) {
        window.clearTimeout(resetTimerRef.current);
      }
      resetTimerRef.current = window.setTimeout(() => {
        setCopied(false);
      }, 1500);
    } catch {
      // Ignore clipboard failures in unsupported browser contexts.
    }
  }

  return (
    <button
      type="button"
      className={`copy-trigger ${copied ? "is-copied" : ""} ${className}`.trim()}
      title={title}
      aria-label={ariaLabel}
      onClick={onCopy}
    >
      <span className="copy-icon" aria-hidden="true">
        <svg className="copy-glyph" viewBox="0 0 16 16">
          <rect className="copy-back" x="3" y="5.5" width="7" height="9" rx="1.5" />
          <rect className="copy-front" x="6" y="2.5" width="7" height="9" rx="1.5" />
        </svg>
        <svg className="check-glyph" viewBox="0 0 16 16">
          <path d="M3 8.5L6.5 12L13 4.5" />
        </svg>
      </span>
    </button>
  );
}

function AddressCopy({ address }) {
  const formattedAddress = checksumAddress(address);

  if (!formattedAddress) {
    return <span className="row-secondary mono">—</span>;
  }

  return (
    <span className="address-copy" title={formattedAddress}>
      <span className="mono address-value">{shortenAddress(formattedAddress)}</span>
      <CopyIconButton
        valueToCopy={formattedAddress}
        title={`Copy address ${formattedAddress}`}
        ariaLabel={`Copy address ${formattedAddress}`}
      />
    </span>
  );
}

function AddressLinkCopy({
  address,
  label = null,
  title = null,
  copyTitle = null,
  copyAriaLabel = null,
  onClick = null,
}) {
  const formattedAddress = checksumAddress(address);

  if (!formattedAddress) {
    return <span className="row-secondary mono">—</span>;
  }

  return (
    <span className="address-copy" title={title || formattedAddress}>
      <a
        className="etherscan-link mono address-value"
        href={`${ETHERSCAN_ADDRESS_URL}${formattedAddress}`}
        title={formattedAddress}
        target="_blank"
        rel="noopener noreferrer"
        onClick={onClick}
      >
        {label || shortenAddress(formattedAddress)}
      </a>
      <CopyIconButton
        valueToCopy={formattedAddress}
        title={copyTitle || `Copy address ${formattedAddress}`}
        ariaLabel={copyAriaLabel || `Copy address ${formattedAddress}`}
      />
    </span>
  );
}

function WantTokenValue({ address, symbol }) {
  const formattedAddress = checksumAddress(address);

  if (!formattedAddress) {
    return <span className="row-secondary mono">{symbol || "Unknown want"}</span>;
  }

  return (
    <span className="address-copy want-token-value" title={formattedAddress}>
      <span className="mono address-value">{symbol || shortenAddress(formattedAddress)}</span>
      <CopyIconButton
        valueToCopy={formattedAddress}
        title={`Copy address ${formattedAddress}`}
        ariaLabel={`Copy address ${formattedAddress}`}
      />
    </span>
  );
}

function EntityIdentity({ primary, primaryTitle, secondary, address, onOpen, expanded = false }) {
  return (
    <div className="entity-cell">
      <div className="row-primary" title={primaryTitle || (typeof primary === "string" ? primary : undefined)}>
        {onOpen ? (
          <button type="button" className="entity-open" onClick={onOpen} aria-expanded={expanded}
            aria-label={`${expanded ? "Hide" : "Show"} details for ${primary}`}>
            {primary || "—"}
          </button>
        ) : primary || "—"}
      </div>
      {secondary ? <div className="entity-secondary mono">{secondary}</div> : null}
      <AddressLinkCopy address={address} />
    </div>
  );
}

function EtherscanTxLink({ txHash, compact = false, concise = false }) {
  if (!txHash) return <span className="row-secondary">No transaction</span>;
  const normalized = txHash.startsWith("0x") ? txHash : `0x${txHash}`;
  return (
    <a
      className={`etherscan-link transaction-link mono${concise ? " is-concise" : ""}`}
      href={`${ETHERSCAN_TX_URL}${normalized}`}
      title={normalized}
      aria-label={`View transaction ${normalized} on Etherscan`}
      target="_blank"
      rel="noopener noreferrer"
    >
      <span>{compact ? <span className="transaction-prefix">tx </span> : null}{normalized.slice(0, concise ? 4 : 6)}{concise ? "…" : "..."}{normalized.slice(-4)}</span>
      {!concise ? <OutboundLinkGlyph /> : null}
    </a>
  );
}

function getAuctionScanHref(kick) {
  if (!isAuctionScanLinkableKick(kick)) {
    return null;
  }

  return (
    kick.auctionScanTxUrl
    || buildAuctionScanTxUrl(kick.txHash)
    || kick.auctionScanRoundUrl
    || kick.auctionScanAuctionUrl
    || null
  );
}

function AuctionScanFavicon({ className = "" }) {
  return (
    <img
      src={AUCTIONSCAN_ICON_SRC}
      alt=""
      aria-hidden="true"
      className={`auctionscan-favicon ${className}`.trim()}
    />
  );
}

function OutboundLinkGlyph({ className = "" }) {
  return (
    <svg
      className={`outbound-link-glyph ${className}`.trim()}
      viewBox="0 0 16 16"
      fill="none"
      aria-hidden="true"
    >
      <path d="M6 4h6v6" />
      <path d="M10.5 5.5 4 12" />
    </svg>
  );
}

function AuctionScanTextLink({ kick }) {
  const href = getAuctionScanHref(kick);
  if (!href) {
    return null;
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="kick-external-link"
      onClick={(event) => event.stopPropagation()}
    >
      <span>view on</span>
      <AuctionScanFavicon />
      <span>auctionscan.info</span>
    </a>
  );
}

function AuctionScanIconLink({
  kick,
  className = "",
  iconClassName = "",
  glyphClassName = "",
}) {
  const href = getAuctionScanHref(kick);
  if (!href) {
    return null;
  }

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className={`kick-auctionscan-link ${className}`.trim()}
      title="View on AuctionScan"
      aria-label="View on AuctionScan"
      onClick={(event) => event.stopPropagation()}
    >
      <AuctionScanFavicon className={`kick-auctionscan-link-icon ${iconClassName}`.trim()} />
      <OutboundLinkGlyph className={`kick-auctionscan-link-glyph ${glyphClassName}`.trim()} />
    </a>
  );
}

function KickHistoryAuctionScanLink({ kick }) {
  return (
    <AuctionScanIconLink
      kick={kick}
      className="kick-history-auctionscan"
      iconClassName="kick-history-auctionscan-icon"
      glyphClassName="kick-history-auctionscan-glyph"
    />
  );
}

function MissingAuctionAction({ deployState, onDeploy, onCheck }) {
  const status = deployState?.status || "idle";
  const txHash = deployState?.txHash || null;
  const error = deployState?.error || "";
  const isBusy = status === "preparing" || status === "wallet";
  const txStatusLabel = { confirmed: "confirmed", reverted: "failed", checking: "checking…" }[status] || "pending";

  return (
    <div className="auction-missing-state" onClick={(event) => event.stopPropagation()}>
      {txHash ? (
        <div className="auction-action-status">
          <span className={`row-secondary mono ${status === "confirmed" ? "deployment-confirmed" : ""}`}>{txStatusLabel}</span>
          <span className="kick-separator mono">·</span>
          <span onClick={(event) => event.stopPropagation()}>
            <EtherscanTxLink txHash={txHash} />
          </span>
        </div>
      ) : (
        <button
          type="button"
          className="auction-action-link"
          onClick={(event) => {
            event.stopPropagation();
            onDeploy();
          }}
          disabled={isBusy}
        >
          {status === "wallet" ? (
            <span className="mono">confirm in wallet…</span>
          ) : status === "preparing" ? (
            <span className="mono">preparing…</span>
          ) : (
            <>
              <span className="deploy-cta">Deploy auction</span>
              <span className="deploy-plus" aria-hidden="true">+</span>
            </>
          )}
        </button>
      )}
      {!txHash && !isBusy ? <span className="row-secondary deployment-hint">Not deployed</span> : null}
      {txHash && (status === "pending" || status === "checking") ? (
        <button type="button" className="auction-action-link" onClick={onCheck} disabled={status === "checking"}>
          Check again
        </button>
      ) : null}
      {status === "confirmed" ? <div className="row-secondary">Waiting for scanner mapping.</div> : null}
      {error ? <div className="auction-action-error">{error}</div> : null}
    </div>
  );
}

function DeployConfirmModal({ payload, onConfirm, onCancel }) {
  const dialogRef = useRef(null);
  const cancelRef = useRef(null);
  const titleId = useId();
  useDialogFocus(dialogRef, onCancel, cancelRef);

  const spec = payload || {};
  const warnings = Array.isArray(spec.warnings) ? spec.warnings.filter(Boolean) : [];
  const receiverAddress = spec.receiverAddress || spec.strategyAddress || null;
  const rows = [
    [
      spec.factoryVersion ? `Factory (${spec.factoryVersion})` : "Factory",
      <AddressLinkCopy address={spec.factoryAddress} />,
    ],
    ["Receiver", <AddressLinkCopy address={receiverAddress} />],
    ["Want", <AddressLinkCopy address={spec.wantAddress} label={spec.wantSymbol || shortenAddress(spec.wantAddress)} />],
  ];
  if (spec.inference?.sellTokenAddress) {
    rows.push([
      "Inference token",
      <AddressLinkCopy
        address={spec.inference.sellTokenAddress}
        label={spec.inference.sellTokenSymbol || shortenAddress(spec.inference.sellTokenAddress)}
      />,
    ]);
  }
  if (spec.startingPrice) {
    rows.push([
      "Starting price",
      `${spec.startingPriceDisplay || spec.startingPrice} ${spec.wantSymbol || "want"}`,
    ]);
    rows.push(["Raw startingPrice", spec.startingPrice]);
  }
  if (spec.startPriceBufferBps != null) {
    rows.push(["Start-price buffer", `+${(Number(spec.startPriceBufferBps) / 100).toFixed(1)}%`]);
  }
  if (spec.predictedAuctionAddress) {
    rows.push(["Predicted auction", <AddressLinkCopy address={spec.predictedAuctionAddress} />]);
  }

  return createPortal(
    <div className="deploy-modal-backdrop" onMouseDown={onCancel}>
      <div ref={dialogRef} className="deploy-modal" role="dialog" aria-modal="true"
        aria-labelledby={titleId} tabIndex={-1} onMouseDown={(e) => e.stopPropagation()}>
        <div id={titleId} className="deploy-modal-title">
          Deploy auction for {spec.strategyName || shortenAddress(spec.strategyAddress)}?
        </div>
        <dl className="deploy-modal-details">
          {rows.map(([label, value]) => (
            <div key={label} className="deploy-modal-row">
              <dt>{label}</dt>
              <dd className={typeof value === "string" || typeof value === "number" ? "mono" : "deploy-modal-value"}>
                {value}
              </dd>
            </div>
          ))}
        </dl>
        {warnings.length ? (
          <div className="deploy-modal-warnings" role="status" aria-live="polite">
            {warnings.map((warning) => (
              <p key={warning} className="deploy-modal-warning">
                {warning}
              </p>
            ))}
          </div>
        ) : null}
        <div className="deploy-modal-actions">
          <button ref={cancelRef} type="button" className="deploy-modal-btn deploy-modal-btn-cancel" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" className="deploy-modal-btn deploy-modal-btn-confirm" onClick={onConfirm}>
            Confirm
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}

function AuctionAddressCell({
  address,
  version,
  wantAddress,
  wantSymbol,
  emptyContent = null,
}) {
  return (
    <div className="auction-value-slot">
      {address ? (
        <>
          <span className="auction-title-row">
            <WantTokenValue address={wantAddress} symbol={wantSymbol} />
            {version ? <span className="auction-version-badge mono">v{String(version).replace(/^v/, "")}</span> : null}
          </span>
          <span className="auction-address-row"><AddressLinkCopy address={address} /></span>
        </>
      ) : null}
      {!address ? (emptyContent || <span className="row-secondary mono">—</span>) : null}
    </div>
  );
}

function KickHistoryCell({
  kicks,
  nowMs,
  isExpanded,
  onToggleExpand,
  fallbackAuctionAddress = null,
  emptyContent = null,
}) {
  const hasKicks = kicks && kicks.length > 0;
  const hasToggle = kicks && kicks.length > 1 && typeof onToggleExpand === "function";

  if (!hasKicks) {
    return emptyContent || <span className="row-secondary">None recorded</span>;
  }

  const visibleKicks = (isExpanded ? kicks : kicks.slice(0, 1)).slice(0, 5);
  const displayKick = (kick) => (
    kick.auctionAddress || !fallbackAuctionAddress
      ? kick
      : { ...kick, auctionAddress: fallbackAuctionAddress }
  );

  return (
    <div className={`kick-history${isExpanded ? " is-expanded" : ""}`} onClick={(event) => event.stopPropagation()}>
      <div className="kick-history-list">
        {visibleKicks.map((kick, index) => (
          <div key={kick.txHash || index} className="kick-row">
            <KickRow kick={displayKick(kick)} nowMs={nowMs} compact={isExpanded} toggle={!isExpanded && index === 0 && hasToggle ? (
              <button type="button" className="history-toggle-button" onClick={onToggleExpand}
                aria-expanded={isExpanded}
                aria-label={isExpanded ? "Collapse kick history" : "Expand kick history"}
                title={isExpanded ? "Collapse history" : "Show earlier activity"}>
                <Chevron expanded={isExpanded} />
                <span>+{Math.min(kicks.length, 5) - 1}</span>
              </button>
            ) : null} />
          </div>
        ))}
      </div>
      {isExpanded && hasToggle ? (
        <button type="button" className="history-toggle-button history-collapse" onClick={onToggleExpand}
          aria-expanded="true" aria-label="Collapse kick history" title="Collapse history">
          <Chevron expanded />
        </button>
      ) : null}
    </div>
  );
}

function Chevron({ expanded = false }) {
  return <span className={`chevron-toggle ${expanded ? "is-expanded" : ""}`} aria-hidden="true">
    <svg viewBox="0 0 12 12"><path d="m4 2 4 4-4 4" /></svg>
  </span>;
}

function KickRow({ kick, nowMs, toggle, compact = false }) {
  const relativeTime = formatRelativeTimestamp(kick.createdAt, nowMs);
  const displayTime = compact ? relativeTime.replace(/ (year|month|week|day|hour|minute)s?\b/g,
    (_, unit) => ({ year: "y", month: "mo", week: "w", day: "d", hour: "h", minute: "m" })[unit])
    .replace(/ ago$/, "").replace(/^just now$/, "now").replace(/^in a moment$/, "soon").replace(/^in /, "+") : relativeTime;
  return (
    <div className="kick-row-inner">
      <div className="activity-time-row">
        <time className="kick-time" dateTime={kick.createdAt} title={formatUtcTimestamp(kick.createdAt)} aria-label={relativeTime}>
          {displayTime}
        </time>
        {toggle}
      </div>
      <div className="activity-links"><EtherscanTxLink txHash={kick.txHash} concise={compact} /><KickHistoryAuctionScanLink kick={kick} /></div>
    </div>
  );
}

function ThemeSwitch({ themePreference, resolvedTheme, onCycle }) {
  const currentTheme = themePreference || resolvedTheme;
  const nextTheme = THEME_SEQUENCE[(THEME_SEQUENCE.indexOf(currentTheme) + 1) % THEME_SEQUENCE.length];
  const title = themePreference
    ? `Theme: ${themePreference}. Click to switch to ${nextTheme}.`
    : `Theme: system (${resolvedTheme}). Click to switch to ${nextTheme}.`;

  return (
    <button
      type="button"
      className="theme-switch"
      onClick={onCycle}
      title={title}
      aria-label={title}
    >
      <span className="theme-icon-wrap" aria-hidden="true">
        <svg className={`theme-icon sun ${resolvedTheme === "light" ? "is-visible" : ""}`} viewBox="0 0 16 16">
          <circle cx="8" cy="8" r="3" />
          <path d="M8 1.6V3.2M8 12.8v1.6M1.6 8H3.2M12.8 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M12.6 3.4l-1.1 1.1M4.5 11.5l-1.1 1.1" />
        </svg>
        <svg className={`theme-icon moon ${resolvedTheme === "dark" ? "is-visible" : ""}`} viewBox="0 0 16 16">
          <path d="M10.8 1.8a5.9 5.9 0 1 0 3.4 10.7A6.3 6.3 0 0 1 10.8 1.8Z" />
        </svg>
      </span>
      {!themePreference ? <span className="theme-auto-dot" aria-hidden="true" /> : null}
    </button>
  );
}

function TabBar({ activePage, onChangePage, alertCount = 0 }) {
  return (
    <nav className="tab-bar" role="tablist">
      <button
        type="button"
        role="tab"
        aria-selected={activePage === "strategies"}
        className={`tab-item ${activePage === "strategies" ? "is-active" : ""}`}
        onClick={() => onChangePage("strategies")}
      >
        Strategies
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activePage === "fee-burner"}
        className={`tab-item ${activePage === "fee-burner" ? "is-active" : ""}`}
        onClick={() => onChangePage("fee-burner")}
      >
        Fee Burner
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activePage === "kicks"}
        className={`tab-item ${activePage === "kicks" ? "is-active" : ""}`}
        onClick={() => onChangePage("kicks")}
      >
        Logs
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={activePage === "alerts"}
        className={`tab-item ${activePage === "alerts" ? "is-active" : ""}`}
        onClick={() => onChangePage("alerts")}
      >
        Alerts
        {alertCount > 0 ? (
          <span className="alert-nav-badge" aria-label={`${alertCount} alerts need action`}>
            {alertCount}
          </span>
        ) : null}
      </button>
    </nav>
  );
}

function formatKickStatusLabel(status) {
  return String(status || "UNKNOWN").replaceAll("_", " ");
}

function StatusBadge({ status }) {
  let className = "status-badge";
  if (status === "CONFIRMED") {
    className += " status-confirmed";
  } else if (FAILED_STATUSES.has(status)) {
    className += " status-error";
  } else if (FAINT_STATUSES.has(status)) {
    className += " status-faint";
  }

  return <span className={className}>{formatKickStatusLabel(status)}</span>;
}

function formatProviderAmount(amountOut, decimals, status) {
  if (amountOut == null) return status || "—";
  if (decimals != null) {
    try {
      return formatBalance(new Big(String(amountOut)).div(new Big(10).pow(decimals)).toString());
    } catch { /* fall through */ }
  }
  return String(amountOut);
}

function DetailField({ label, children, error = false }) {
  return <div className="kick-detail-item">
    <dt className="kick-detail-label">{label}</dt>
    <dd className={`kick-detail-value${error ? " error-text" : ""}`}>{children}</dd>
  </div>;
}

function DetailGroup({ title, children }) {
  return <section className="log-detail-group">
    <h3>{title}</h3>
    <dl className="kick-detail-grid">{children}</dl>
  </section>;
}

function KickDetailContent({ kick }) {
  const [showRelativeTimestamp, setShowRelativeTimestamp] = useState(false);
  const operationMeta = getOperationMeta(kick.operationType);
  let quote = null;
  try { quote = kick.quoteResponseJson ? JSON.parse(kick.quoteResponseJson) : null; } catch { /* Preserve the event even if quote diagnostics are malformed. */ }
  const providers = quote?.providers && typeof quote.providers === "object" ? Object.entries(quote.providers) : [];
  const summary = quote?.summary;
  const decimals = quote?.tokenOutDecimals ?? quote?.token_out?.decimals ?? null;
  const providerAmount = (amount, status) => {
    const value = formatProviderAmount(amount, decimals, status);
    return amount != null ? `${value} ${decimals == null ? "raw units" : kick.wantSymbol || "output tokens"}${status ? ` · ${status}` : ""}` : value;
  };
  const actionId = kick.runId?.startsWith("api-action:");
  const identifier = actionId ? kick.runId.slice("api-action:".length) || kick.runId : kick.runId !== "api-prepare" ? kick.runId : null;
  const bpsToPercent = (bps) => `${(Number(bps) / 100).toFixed(2)}%`;
  const hasDiagnostics = kick.stuckAbortReason || kick.errorMessage || providers.length || summary || quote?.requestUrl;
  const tokenValue = (address, symbol) => address ? <WantTokenValue address={address} symbol={symbol} /> : symbol || "—";

  return <div className="log-detail-content">
    <DetailGroup title="Execution">
      <DetailField label="Operation">{operationMeta.detailLabel}</DetailField>
      <DetailField label="Timestamp">
        <button type="button" className="timestamp-toggle" onClick={() => setShowRelativeTimestamp(value => !value)}
          title={kick.createdAt || undefined} aria-label="Toggle timestamp format">
          {showRelativeTimestamp ? formatRelativeTimestamp(kick.createdAt, Date.now()) : formatUtcTimestamp(kick.createdAt)}
        </button>
      </DetailField>
      <DetailField label="Source"><EntityIdentity primary={kick.sourceName || "Unknown source"} address={kick.sourceAddress} /></DetailField>
      <DetailField label={operationMeta.primaryTokenLabel}>{tokenValue(kick.tokenAddress, kick.tokenSymbol)}</DetailField>
      <DetailField label={operationMeta.secondaryTokenLabel}>{tokenValue(kick.wantAddress, kick.wantSymbol)}</DetailField>
      {kick.normalizedBalance != null ? <DetailField label="Balance">{formatBalance(kick.normalizedBalance)} {kick.tokenSymbol}</DetailField> : null}
      {kick.txHash ? <DetailField label="Transaction"><EtherscanTxLink txHash={kick.txHash} /></DetailField> : null}
      {kick.blockNumber != null ? <DetailField label="Block">{kick.blockNumber}</DetailField> : null}
      {kick.gasUsed != null ? <DetailField label="Gas used">{Number(kick.gasUsed).toLocaleString()}</DetailField> : null}
      {kick.gasPriceGwei != null ? <DetailField label="Gas price">{kick.gasPriceGwei} gwei</DetailField> : null}
      {identifier ? <DetailField label={actionId ? "Action ID" : "Run ID"}>{identifier}</DetailField> : null}
    </DetailGroup>
    {operationMeta.showKickPricing ? <DetailGroup title="Pricing">
      <DetailField label="Start quote">{kick.startingPriceDisplay || kick.startingPrice || "—"}
        {!kick.startingPriceDisplay && kick.startPriceBufferBps != null ? ` (+${bpsToPercent(kick.startPriceBufferBps)} buffer)` : ""}</DetailField>
      <DetailField label="Minimum quote">{kick.minimumQuote ?? "—"}{kick.minPriceBufferBps != null ? ` (-${bpsToPercent(kick.minPriceBufferBps)} buffer)` : ""}</DetailField>
      <DetailField label="Minimum price · scaled">{kick.minimumPrice ?? "—"}</DetailField>
      <DetailField label="Quote amount">{kick.quoteAmount ?? "—"}</DetailField>
      {kick.stepDecayRateBps != null ? <DetailField label="Step decay">{bpsToPercent(kick.stepDecayRateBps)}</DetailField> : null}
      {kick.settleToken ? <DetailField label="Pre-kick settle">{tokenValue(kick.settleToken, kick.settleToken === kick.tokenAddress ? kick.tokenSymbol : null)}</DetailField> : null}
    </DetailGroup> : null}
    {hasDiagnostics ? <DetailGroup title="Diagnostics">
      {kick.stuckAbortReason ? <DetailField label="Reason">{kick.stuckAbortReason}</DetailField> : null}
      {kick.errorMessage ? <DetailField label="Error" error>{kick.errorMessage}</DetailField> : null}
      {summary ? <DetailField label="Quote summary">
        {summary.requested_providers != null ? <div>Providers {summary.successful_providers ?? 0}/{summary.requested_providers}</div> : null}
        {summary.high_amount_out != null ? <div>High {providerAmount(summary.high_amount_out)}</div> : null}
        {summary.low_amount_out != null ? <div>Low {providerAmount(summary.low_amount_out)}</div> : null}
        {summary.median_amount_out != null ? <div>Median {providerAmount(summary.median_amount_out)}</div> : null}
      </DetailField> : null}
      {providers.length ? <DetailField label="Provider responses">
        <details className="provider-details"><summary>{providers.length} providers</summary>
          <dl className="provider-ledger">{providers.map(([name, entry]) => <div key={name}>
            <dt>{name}</dt><dd>{providerAmount(entry?.amount_out, entry?.status)}</dd>
          </div>)}</dl>
        </details>
      </DetailField> : null}
      {quote?.requestUrl ? <DetailField label="Quote request"><a className="kick-external-link" href={quote.requestUrl} target="_blank" rel="noopener noreferrer">View quote via API <OutboundLinkGlyph /></a></DetailField> : null}
    </DetailGroup> : null}
    {(getAuctionScanHref(kick) || kick.auctionAddress) ? <div className="log-detail-links">
      {getAuctionScanHref(kick) ? <AuctionScanTextLink kick={kick} /> : null}
      {kick.auctionAddress ? <a href={`${COW_EXPLORER_URL}${kick.auctionAddress}`} target="_blank" rel="noopener noreferrer" className="kick-external-link">CoW Explorer <OutboundLinkGlyph /></a> : null}
    </div> : null}
  </div>;
}

function DetailPanel({ colSpan, children }) {
  return (
    <tr className="kick-detail">
      <td colSpan={colSpan}>{children}</td>
    </tr>
  );
}

function DetailModal({ onClose, label = "Activity details", children }) {
  const sheetRef = useRef(null);
  const bodyRef = useRef(null);
  const backdropRef = useRef(null);
  const dragRef = useRef({ startY: 0, startTime: 0, dy: 0, dragging: false, dismissed: false });

  useDialogFocus(sheetRef, onClose);

  function onTouchStart(e) {
    const d = dragRef.current;
    d.startY = e.touches[0].clientY;
    d.startTime = Date.now();
    d.dy = 0;
    d.dragging = false;
    d.dismissed = false;
  }

  function onTouchMove(e) {
    const d = dragRef.current;
    if (d.dismissed) return;
    const dy = e.touches[0].clientY - d.startY;
    if ((bodyRef.current.scrollTop <= 0 && dy > 0) || d.dragging) {
      d.dragging = true;
      d.dy = Math.max(0, dy);
      sheetRef.current.style.transition = "none";
      sheetRef.current.style.transform = `translateY(${d.dy}px)`;
      backdropRef.current.style.opacity = Math.max(0, 1 - d.dy / (window.innerHeight * 0.5));
    }
  }

  function onTouchEnd() {
    const d = dragRef.current;
    if (!d.dragging) return;
    const velocity = d.dy / Math.max(1, Date.now() - d.startTime);
    const dismiss = d.dy > 80 || velocity > 0.5;
    sheetRef.current.style.transition = "transform 200ms ease-out";
    backdropRef.current.style.transition = "opacity 200ms ease-out";
    if (dismiss) {
      d.dismissed = true;
      sheetRef.current.style.transform = "translateY(100%)";
      backdropRef.current.style.opacity = "0";
      setTimeout(onClose, 200);
    } else {
      sheetRef.current.style.transform = "translateY(0)";
      backdropRef.current.style.opacity = "1";
    }
    d.dragging = false;
  }

  return createPortal(
    <div ref={backdropRef} className="kick-modal-backdrop" onMouseDown={onClose}>
      <div
        ref={sheetRef}
        className="kick-modal"
        role="dialog"
        aria-modal="true"
        aria-label={label}
        tabIndex={-1}
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        <div className="kick-modal-handle" />
        <button type="button" className="kick-modal-close" onClick={onClose}>Close details</button>
        <div ref={bodyRef} className="kick-modal-body">
          {children}
        </div>
      </div>
    </div>,
    document.body
  );
}

function KickDetailPanel({ kick, onOpenAuctionScan }) {
  return (
    <DetailPanel colSpan={6}>
      <KickDetailContent kick={kick} onOpenAuctionScan={onOpenAuctionScan} />
    </DetailPanel>
  );
}

function KickDetailModal({ kick, onClose, onOpenAuctionScan }) {
  return (
    <DetailModal onClose={onClose}>
      <KickDetailContent kick={kick} onOpenAuctionScan={onOpenAuctionScan} />
    </DetailModal>
  );
}

function KickLogRow({ kick, nowMs, isExpanded, onToggle, rowRef, isMobile }) {
  const operationMeta = getOperationMeta(kick.operationType);
  return <>
    <tr ref={rowRef} data-log-id={kick.id} className={`kick-log-row ${isExpanded ? "is-expanded" : ""}`}>
      <td className="mono muted kick-time-cell" headers="log-time">
        <time dateTime={kick.createdAt} title={formatUtcTimestamp(kick.createdAt)}>{formatRelativeTimestamp(kick.createdAt, nowMs)}</time>
      </td>
      <td className="log-activity-cell" headers="log-activity">
        <button type="button" className="log-open" onClick={onToggle} aria-expanded={isExpanded}
          aria-label={`${isExpanded ? "Hide" : "Show"} details for log ${kick.id}`}>
          <Chevron expanded={isExpanded} /><span>{formatKickPairLabel(kick)}</span>
        </button>
        <div className="log-result"><StatusBadge status={kick.status} /></div>
      </td>
      <td className="log-source-cell" headers="log-source">
        <EntityIdentity primary={kick.sourceName ? formatStrategyDisplayName(kick.sourceName) : "Unknown source"}
          primaryTitle={kick.sourceName} address={kick.sourceAddress} />
      </td>
      <td className="log-auction-cell" headers="log-auction" data-label="Auction">
        {kick.auctionAddress ? <AddressLinkCopy address={kick.auctionAddress}
          copyAriaLabel={`Copy auction address ${checksumAddress(kick.auctionAddress)}`} /> : <span className="muted">—</span>}
      </td>
      <td className="log-transaction-cell" headers="log-transaction" data-label="Transaction">
        <div className="activity-links">{kick.txHash ? <EtherscanTxLink txHash={kick.txHash} /> : <span className="muted">No transaction</span>}<AuctionScanIconLink kick={kick} /></div>
      </td>
      <td className="mono align-right log-usd-cell" headers="log-usd" title={operationMeta.showUsd ? undefined : "Not applicable to this operation"}>
        {operationMeta.showUsd ? (parseBig(kick.usdValue) !== null ? `$${formatBalance(kick.usdValue)}` : "?") : "—"}
      </td>
    </tr>
    {isExpanded && !isMobile ? <KickDetailPanel kick={kick} /> : null}
    {isExpanded && isMobile ? <KickDetailModal kick={kick} onClose={onToggle} /> : null}
  </>;
}

function KickLogSkeletonRows() {
  return Array.from({ length: 10 }, (_, index) => <tr key={index} className="kick-log-skeleton">
    {Array.from({ length: 6 }, (_, column) => <td key={column}><span className="skeleton" /></td>)}
  </tr>);
}

function KickLogPager({
  state,
  nowMs,
  offset,
  pageSize,
  total,
  loading,
  hasMore,
  onPrev,
  onNext,
}) {
  const rangeStart = total === 0 ? 0 : offset + 1;
  const rangeEnd = total === 0 ? 0 : Math.min(offset + pageSize, total);

  return (
    <div className="kick-log-pagination">
      <div className="kick-log-pagination-meta" role="status">
        {total === 0 ? "Showing 0 results" : `Showing ${rangeStart.toLocaleString()}-${rangeEnd.toLocaleString()} of ${total.toLocaleString()}`}
      </div>
      {state ? <RefreshStatus state={state} nowMs={nowMs} /> : null}
      <div className="kick-log-pagination-actions">
        <button type="button" className="kick-log-page-btn" onClick={onPrev} disabled={loading || offset === 0}>
          Newer
        </button>
        <button type="button" className="kick-log-page-btn" onClick={onNext} disabled={loading || !hasMore}>
          Older
        </button>
      </div>
    </div>
  );
}

function RefreshStatus({ state, scannedAt, evaluatedAt, nowMs = Date.now() }) {
  const evaluationStale = evaluatedAt !== undefined && (!Number.isFinite(Date.parse(evaluatedAt)) || Math.abs(nowMs - Date.parse(evaluatedAt)) > 65000);
  const stale = state.error || evaluationStale || (state.updatedAt && nowMs - state.updatedAt > 65000);
  const scanOverdue = scannedAt && nowMs - new Date(scannedAt).getTime() > SCAN_STALE_AFTER_MS;
  return (
    <div className={`refresh-status${stale || scanOverdue ? " is-stale" : ""}`}>
      <span role="status">
        {stale ? "Data may be stale · " : ""}
        {scannedAt !== undefined ? (
          <>
            <time dateTime={scannedAt || undefined} title={formatUtcTimestamp(scannedAt)}>
              {scannedAt ? `${scanOverdue ? "Scan overdue" : "Scanned"} ${formatRelativeTimestamp(scannedAt, nowMs)}` : "No scan available"}
            </time>
            <span aria-hidden="true"> · </span>
          </>
        ) : null}
        <span className="refresh-updated" title={formatUtcTimestamp(evaluatedAt !== undefined ? evaluatedAt : state.updatedAt)}>
          {evaluatedAt !== undefined ? evaluatedAt ? `Evaluated ${formatRelativeTimestamp(evaluatedAt, nowMs)}` : "No evaluation available" : state.updatedAt ? `Updated ${new Date(state.updatedAt).toLocaleTimeString()}` : "Not yet updated"}
        </span>
      </span>
      <button type="button" className="kick-log-page-btn" onClick={state.refresh} disabled={state.refreshing}>
        {state.refreshing ? "Refreshing…" : "Refresh"}
      </button>
    </div>
  );
}

const logRowKey = (kick) => kick.id;

function KickLogPage({ nowMs }) {
  const [initial] = useState(parseLocation);
  const [statusFilter, setStatusFilter] = useState(initial.logsStatus);
  const [searchTerm, setSearchTerm] = useState(initial.logsQuery);
  const [debouncedSearchTerm, setDebouncedSearchTerm] = useState(initial.logsQuery);
  const [offset, setOffset] = useState(initial.logsOffset);
  const [expandedRows, setExpandedRows] = useState(() => new Set());
  const [focusedKickId, setFocusedKickId] = useState(initial.kickId);
  const [focusedRunId, setFocusedRunId] = useState(initial.runId);
  const [tableHovered, setTableHovered] = useState(false);
  const [tableFocused, setTableFocused] = useState(false);
  const highlightedRowRef = useRef(null);
  const openedTargetRef = useRef(null);
  const isMobile = useMediaQuery("(max-width: 960px)");
  const focusedView = Boolean(focusedKickId || focusedRunId);

  useEffect(() => {
    const timerId = window.setTimeout(() => setDebouncedSearchTerm(searchTerm), 250);
    return () => window.clearTimeout(timerId);
  }, [searchTerm]);

  useEffect(() => {
    const restoreLocation = () => {
      const location = parseLocation();
      if (location.page !== "kicks") return;
      setStatusFilter(location.logsStatus);
      setSearchTerm(location.logsQuery);
      setDebouncedSearchTerm(location.logsQuery);
      setOffset(location.logsOffset);
      setFocusedKickId(location.kickId);
      setFocusedRunId(location.runId);
      setExpandedRows(new Set());
      openedTargetRef.current = null;
    };
    window.addEventListener("popstate", restoreLocation);
    return () => window.removeEventListener("popstate", restoreLocation);
  }, []);

  const params = new URLSearchParams({ limit: String(KICK_LOG_PAGE_SIZE), offset: String(offset) });
  if (statusFilter !== "all") params.set("status", statusFilter);
  if (debouncedSearchTerm.trim()) params.set("q", debouncedSearchTerm.trim());
  if (focusedKickId) params.set("kick_id", String(focusedKickId));
  else if (focusedRunId) params.set("run_id", focusedRunId);
  const queryKey = params.toString();
  const logs = useLiveData(apiUrl(`/logs/kicks?${queryKey}`), { errorMessage: "Unable to load logs" });
  const { loading, error } = logs;
  const liveKicks = useMemo(() => Array.isArray(logs.data?.kicks) ? logs.data.kicks.map(normalizeKick) : [], [logs.data]);
  const kicks = useStableRowOrder(liveKicks, queryKey, tableHovered || tableFocused || expandedRows.size > 0, { key: logRowKey, pinPage: true });
  const total = logs.data?.total || 0;
  const hasMore = Boolean(logs.data?.hasMore);

  useEffect(() => {
    const search = new URLSearchParams();
    if (offset) search.set("offset", String(offset));
    if (statusFilter !== "all") search.set("status", statusFilter);
    if (debouncedSearchTerm.trim()) search.set("q", debouncedSearchTerm.trim());
    if (focusedKickId) search.set("kick_id", String(focusedKickId));
    else if (focusedRunId) search.set("run_id", focusedRunId);
    window.history.replaceState(null, "", `/logs${search.size ? `?${search}` : ""}`);
  }, [offset, statusFilter, debouncedSearchTerm, focusedKickId, focusedRunId]);

  useEffect(() => {
    const target = focusedKickId ? `kick:${focusedKickId}` : focusedRunId ? `run:${focusedRunId}` : null;
    if (!target) { openedTargetRef.current = null; return; }
    if (loading || openedTargetRef.current === target) return;
    const match = focusedKickId ? kicks.find(kick => String(kick.id) === String(focusedKickId)) : kicks.find(kick => kick.runId === focusedRunId);
    if (!match) return;
    openedTargetRef.current = target;
    setExpandedRows(new Set([match.id]));
    // Deep links scroll once, never again on a background refresh.
    if (!isMobile) requestAnimationFrame(() => highlightedRowRef.current?.scrollIntoView({ block: "center" }));
  }, [loading, focusedKickId, focusedRunId, kicks, isMobile]);

  function buildNavParams(overrides = {}) {
    return { offset: offset ? String(offset) : null, status: statusFilter !== "all" ? statusFilter : null,
      q: debouncedSearchTerm.trim() || null, run_id: focusedRunId || null, ...overrides };
  }
  function toggleRow(kick) {
    const expanding = !expandedRows.has(kick.id);
    setExpandedRows(previous => {
      const next = isMobile && expanding ? new Set() : new Set(previous);
      if (expanding) next.add(kick.id); else next.delete(kick.id);
      return next;
    });
    if (!expanding && String(focusedKickId) === String(kick.id)) setFocusedKickId(null);
    navigateTo("kicks", buildNavParams({ kick_id: expanding ? String(kick.id) : null }));
  }
  function changePage(next) {
    setExpandedRows(new Set());
    setOffset(next);
  }
  function clearFocusedView() {
    setFocusedKickId(null);
    setFocusedRunId(null);
    setExpandedRows(new Set());
    navigateTo("kicks", buildNavParams({ kick_id: null, run_id: null }));
  }
  const pagerProps = { offset, pageSize: KICK_LOG_PAGE_SIZE, total, loading, hasMore,
    onPrev: () => changePage(Math.max(0, offset - KICK_LOG_PAGE_SIZE)), onNext: () => changePage(offset + KICK_LOG_PAGE_SIZE) };

  return <section className="logs-page">
    <section className="kick-log-controls" aria-label="Filter logs">
      <label className="control control-search">
        <span className="sr-only">Search logs</span>
        <input type="search" value={searchTerm} onChange={event => { setSearchTerm(event.target.value); setOffset(0); setExpandedRows(new Set()); }}
          disabled={focusedView} placeholder="Search tokens, operations, addresses, transactions…" />
      </label>
      <label className="control control-status">
        <span className="sr-only">Result</span>
        <select aria-label="Result" value={statusFilter} onChange={event => { setStatusFilter(event.target.value); setOffset(0); setExpandedRows(new Set()); }} disabled={focusedView}>
          <option value="all">All results</option><option value="confirmed">Confirmed</option><option value="failed">Failed</option>
        </select>
      </label>
    </section>
    {focusedView ? <div className="kick-log-focusbar">
      <span className="toolbar-meta">{focusedKickId ? `Selected log ${focusedKickId}` : `Run ${focusedRunId}`}</span>
      <RefreshStatus state={logs} nowMs={nowMs} />
      <button type="button" className="kick-log-page-btn" onClick={clearFocusedView}>Show all logs</button>
    </div> : <KickLogPager {...pagerProps} state={logs} nowMs={nowMs} />}
    {error ? <p className="error" role="alert">{error}</p> : null}
    <div className="log-table-shell"
      onPointerEnter={event => { if (event.pointerType !== "touch") setTableHovered(true); }}
      onPointerLeave={() => setTableHovered(false)}
      onFocusCapture={() => setTableFocused(true)}
      onBlurCapture={event => { if (!event.currentTarget.contains(event.relatedTarget)) setTableFocused(false); }}>
      <table className="kick-log-table">
        <thead><tr>
          <th id="log-time" scope="col">Time</th><th id="log-activity" scope="col">Activity</th>
          <th id="log-source" scope="col">Source</th><th id="log-auction" scope="col">Auction</th>
          <th id="log-transaction" scope="col">Transaction</th><th id="log-usd" scope="col" className="align-right">USD</th>
        </tr></thead>
        <tbody>
          {loading ? <KickLogSkeletonRows /> : null}
          {!loading && !kicks.length ? <tr><td colSpan={6} className="kick-log-empty">{error ? "Logs unavailable" : "No activity found"}</td></tr> : null}
          {!loading ? kicks.map(kick => <KickLogRow key={kick.id} kick={kick} nowMs={nowMs}
            isExpanded={expandedRows.has(kick.id)} onToggle={() => toggleRow(kick)} isMobile={isMobile}
            rowRef={(focusedKickId != null && String(kick.id) === String(focusedKickId)) || (focusedKickId == null && focusedRunId != null && kick.runId === focusedRunId) ? highlightedRowRef : undefined} />) : null}
        </tbody>
      </table>
    </div>
    {!focusedView && !loading ? <KickLogPager {...pagerProps} /> : null}
  </section>;
}

function TokenLogo({ src, alt }) {
  const [failedSrc, setFailedSrc] = useState(null);

  if (!src || failedSrc === src) {
    return <span className="token-logo-placeholder" aria-hidden="true" />;
  }

  return (
    <img
      src={src}
      alt={alt}
      className="token-logo"
      loading="lazy"
      decoding="async"
      referrerPolicy="no-referrer"
      onError={() => setFailedSrc(src)}
    />
  );
}

function KickPauseIcon({ title }) {
  return (
    <span
      className="kick-pause-icon"
      title={title}
      aria-label={title}
      role="img"
    >
      <svg viewBox="0 0 12 12" aria-hidden="true">
        <rect x="2.5" y="2" width="2.25" height="8" rx="0.75" />
        <rect x="7.25" y="2" width="2.25" height="8" rx="0.75" />
      </svg>
    </span>
  );
}

function TokenBalances({
  balances,
  displayMode,
  onToggleMode,
}) {
  return (
    <div className="token-cell">
      <div className="token-stack">
        {balances.map((balance) => {
          const auctionTooltip = getAuctionSellTokenTooltip(balance);
          const kickPrepareTooltip = getKickPrepareTooltip(balance);
          const balanceTitle = displayMode === "usd"
            ? "Click to show token amounts"
            : "Click to show USD values";
          const title = auctionTooltip ? `${auctionTooltip}\n${balanceTitle}` : balanceTitle;
          const tokenSymbol = balance.tokenSymbol || "UNKNOWN";
          const itemClassNames = ["token-item"];
          if (balance.auctionSellTokenStatus === "disabled") {
            itemClassNames.push("is-auction-disabled");
          }
          const value = displayMode === "usd"
            ? (balance.usdValue == null ? "?" : `$${formatBalance(balance.usdValue)}`)
            : formatBalance(balance.normalizedBalance);
          if (value.length > 11) itemClassNames.push("is-long-balance");
          const itemClassName = itemClassNames.join(" ");

          return (
            <div
              key={`${balance.tokenAddress}-${tokenSymbol}`}
              className={itemClassName}
              title={auctionTooltip || undefined}
            >
              <TokenLogo
                src={balance.tokenLogoUrl}
                alt={`${balance.tokenSymbol} logo`}
              />
              <span className="token-symbol-wrap">
                <span className="mono token-symbol" title={auctionTooltip || undefined}>
                  {tokenSymbol}
                </span>
                <CopyIconButton
                  valueToCopy={checksumAddress(balance.tokenAddress)}
                  title={`Copy token address ${checksumAddress(balance.tokenAddress)}`}
                  ariaLabel={`Copy token address for ${tokenSymbol || "token"}`}
                />
                {kickPrepareTooltip ? <KickPauseIcon title={kickPrepareTooltip} /> : null}
              </span>
              <span className="token-balance-wrap">
                <button
                  type="button"
                  className="mono token-balance token-balance-button"
                  onClick={(event) => {
                    event.stopPropagation();
                    onToggleMode();
                  }}
                  title={title}
                  aria-label={`${tokenSymbol}: ${value} ${displayMode === "usd" ? "USD" : "tokens"}. ${balanceTitle}`}
                >
                  {value}
                </button>
              </span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function RewardSummary({ row, displayMode, onToggleMode, expanded, onToggleExpand }) {
  const detailsId = useId();
  const total = row.totalUsdValue == null ? "?" : `$${formatBalance(row.totalUsdValue)}`;
  const paused = row.balances.filter((balance) => getKickPrepareTooltip(balance));
  const disabled = row.balances.filter((balance) => balance.auctionSellTokenStatus === "disabled");
  const unknown = row.balances.some((balance) => balance.auctionSellTokenStatus === "unknown");
  const symbols = row.balances.map((balance) => balance.tokenSymbol || "UNKNOWN");

  if (!row.balances.length)
    return (
      <div className="reward-empty">
        <span>$0.00</span>
        <span className="row-secondary">No visible rewards</span>
      </div>
    );

  return (
    <div className={`reward-summary${expanded ? " is-expanded" : ""}`}>
      <button
        type="button"
        className={`reward-summary-button${total.length > 11 ? " has-long-total" : ""}`}
        onClick={onToggleExpand}
        aria-expanded={expanded}
        aria-controls={detailsId}
        aria-label={`${expanded ? "Collapse" : "Expand"} rewards for ${formatStrategyDisplayName(
          row.sourceName
        )}`}
      >
        <Chevron expanded={expanded} />
        {!expanded ? (
          <>
            <span className="reward-logos" aria-hidden="true">
              {row.balances.slice(0, 3).map((balance) => (
                <TokenLogo key={balance.tokenAddress} src={balance.tokenLogoUrl} alt="" />
              ))}
              {row.balances.length > 3 ? (
                <span className="reward-more">+{row.balances.length - 3}</span>
              ) : null}
            </span>
            <span
              className="reward-total"
              title={
                row.totalUsdValue == null
                  ? "Total USD unavailable: one or more tokens are unpriced"
                  : "Total reward value in USD"
              }
            >
              {total}
            </span>
          </>
        ) : null}
      </button>
      {!expanded ? (
        <div className="reward-caption">
          <span className="reward-symbols" title={symbols.join(" + ")}>
            {symbols.join(" + ")}
          </span>
          {paused.length ? (
            <span
              className="reward-paused"
              title={paused
                .map((balance) => `${balance.tokenSymbol}: ${getKickPrepareTooltip(balance)}`)
                .join("\n")}
            >
              <KickPauseIcon title="Some rewards are paused; expand for token details" />
              paused
            </span>
          ) : null}
          {disabled.length ? (
            <span
              className="reward-warning"
              title={disabled
                .map((balance) => `${balance.tokenSymbol}: ${getAuctionSellTokenTooltip(balance)}`)
                .join("\n")}
            >
              not enabled
            </span>
          ) : null}
          {unknown ? <span className="reward-warning">status unknown</span> : null}
        </div>
      ) : null}
      <div className="reward-breakdown" id={detailsId} hidden={!expanded}>
        {expanded ? (
          <>
            <TokenBalances balances={row.balances} displayMode={displayMode} onToggleMode={onToggleMode} />
            {row.balances.length > 1 ? (
              <div className={`reward-breakdown-total${total.length > 11 ? " has-long-total" : ""}`}>
                <span className="reward-total-label">{displayMode === "usd" ? "Total" : "Total USD"}</span>
                <span
                  className="reward-total"
                  title={
                    row.totalUsdValue == null
                      ? "Total USD unavailable: one or more tokens are unpriced"
                      : "Total reward value in USD"
                  }
                >
                  {total}
                </span>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </div>
  );
}

function RowScanStatus({ row, latestScanAt }) {
  const rowTime = new Date(row.scannedAt).getTime();
  const latestTime = new Date(latestScanAt).getTime();
  const missing = !row.scannedAt || !Number.isFinite(rowTime);
  // Scanning individual rows takes time; a minute of skew is not a missed scan.
  const behind = Number.isFinite(latestTime) && latestTime - rowTime > 60000;
  if (!missing && !behind && !row.kickGuardDisabled) return null;
  return (
    <div className="row-scan-status">
      {missing || behind ? (
        <span
          title={`Last scan: ${formatUtcTimestamp(row.scannedAt)}. Latest scan: ${formatUtcTimestamp(
            latestScanAt
          )}.`}
        >
          {missing ? "Scan unavailable" : "Older scan"}
        </span>
      ) : null}
      {row.kickGuardDisabled ? (
        <span title={row.kickGuardDetail || "Strategy kick disabled"}>Kicks disabled</span>
      ) : null}
    </div>
  );
}

function StrategyDetailContent({
  row,
  nowMs,
  displayMode,
  onToggleMode,
  deployState,
  onDeploy,
  onCheckDeploy,
  initialHistoryExpanded = false,
}) {
  const [showRelativeTimestamp, setShowRelativeTimestamp] = useState(false);
  const [historyExpanded, setHistoryExpanded] = useState(() => initialHistoryExpanded);

  return (
    <div className="kick-detail-grid strategy-detail-grid">
      <div className="kick-detail-item">
        <div className="kick-detail-label">Last Scan</div>
        <div
          className="kick-detail-value clickable"
          title={showRelativeTimestamp ? formatTimestamp(row.scannedAt) : row.scannedAt}
          onClick={() => setShowRelativeTimestamp((value) => !value)}
          style={{ cursor: "pointer" }}
        >
          {row.scannedAt
            ? showRelativeTimestamp
              ? formatRelativeTimestamp(row.scannedAt, nowMs)
              : formatTimestamp(row.scannedAt)
            : "—"}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Want Token</div>
        <div className="kick-detail-value">
          <WantTokenValue address={row.wantAddress} symbol={row.wantSymbol} />
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Vault</div>
        <div className="kick-detail-value strategy-detail-entity">
          <EntityIdentity
            primary={row.contextSymbol || row.contextName || "Unknown Vault"}
            secondary={row.contextName && row.contextSymbol !== row.contextName ? row.contextName : null}
            address={row.contextAddress}
          />
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Strategy</div>
        <div className="kick-detail-value strategy-detail-entity">
          <EntityIdentity
            primary={formatStrategyDisplayName(row.sourceName)}
            address={row.sourceAddress}
          />
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Auction</div>
        <div className="kick-detail-value">
          <AuctionAddressCell
            address={row.auctionAddress}
            version={row.auctionVersion}
            wantAddress={row.wantAddress}
            wantSymbol={row.wantSymbol}
            emptyContent={
              <MissingAuctionAction
                deployState={deployState}
                onDeploy={onDeploy}
                onCheck={onCheckDeploy}
              />
            }
          />
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">History</div>
        <div className="kick-detail-value">
          <KickHistoryCell
            kicks={row.kicks}
            nowMs={nowMs}
            isExpanded={historyExpanded}
            onToggleExpand={() => setHistoryExpanded((value) => !value)}
            fallbackAuctionAddress={row.auctionAddress}
          />
        </div>
      </div>
      <div className="kick-detail-item strategy-detail-balances">
        <div className="kick-detail-label">Token Balances</div>
        <div className="kick-detail-value">
          {row.balances.length ? (
            <TokenBalances
              balances={row.balances}
              displayMode={displayMode}
              onToggleMode={onToggleMode}
            />
          ) : (
            <div className="row-secondary">No balances above the visibility threshold.</div>
          )}
        </div>
      </div>
    </div>
  );
}

function StrategyDetailPanel({
  row,
  nowMs,
  displayMode,
  onToggleMode,
  deployState,
  onDeploy,
  onCheckDeploy,
  initialHistoryExpanded = false,
}) {
  return (
    <DetailPanel colSpan={4}>
      <StrategyDetailContent
        row={row}
        nowMs={nowMs}
        displayMode={displayMode}
        onToggleMode={onToggleMode}
        deployState={deployState}
        onDeploy={onDeploy}
        onCheckDeploy={onCheckDeploy}
        initialHistoryExpanded={initialHistoryExpanded}
      />
    </DetailPanel>
  );
}

function StrategyDetailModal({
  row,
  nowMs,
  displayMode,
  onToggleMode,
  deployState,
  onDeploy,
  onCheckDeploy,
  initialHistoryExpanded = false,
  onClose,
}) {
  return (
    <DetailModal onClose={onClose} label="Strategy details">
      <StrategyDetailContent
        row={row}
        nowMs={nowMs}
        displayMode={displayMode}
        onToggleMode={onToggleMode}
        deployState={deployState}
        onDeploy={onDeploy}
        onCheckDeploy={onCheckDeploy}
        initialHistoryExpanded={initialHistoryExpanded}
      />
    </DetailModal>
  );
}

function FeeBurnerInventory({ row, latestScanAt, refreshStatus, nowMs, activityExpanded, onToggleActivity }) {
  const activityId = useId();
  const name = row.sourceName || "Unnamed fee burner";
  const total = row.totalUsdValue == null ? "?" : `$${formatBalance(row.totalUsdValue)}`;
  const latestKick = row.kicks[0];

  return (
    <section className="fee-burner-inventory" aria-label={`${name} token inventory`}>
      <div className="fee-burner-context">
        <div className="fee-burner-identity">
          <EntityIdentity primary={name} address={row.sourceAddress} />
          <RowScanStatus row={row} latestScanAt={latestScanAt} />
        </div>
        <div className="fee-burner-auction">
          <span className="fee-context-label">Auction</span>
          <AuctionAddressCell
            address={row.auctionAddress}
            version={row.auctionVersion}
            wantAddress={row.wantAddress}
            wantSymbol={row.wantSymbol}
            emptyContent={<span className="row-secondary">No auction</span>}
          />
        </div>
        {refreshStatus}
      </div>

      <table className="fee-token-table" aria-label={`Token balances for ${name}`}>
        <thead>
          <tr>
            <th scope="col">Token</th>
            <th scope="col" className="fee-amount-heading align-right">
              Amount
            </th>
            <th scope="col" className="align-right">
              USD
            </th>
          </tr>
        </thead>
        <tbody>
          {row.balances.map((balance) => {
            const symbol = balance.tokenSymbol || "UNKNOWN";
            const address = checksumAddress(balance.tokenAddress);
            const auctionWarning = getAuctionSellTokenTooltip(balance);
            const pauseWarning = getKickPrepareTooltip(balance);
            const amount =
              parseBig(balance.normalizedBalance) == null ? "?" : formatBalance(balance.normalizedBalance);
            return (
              <tr
                key={balance.tokenAddress || symbol}
                className="fee-token-row"
                data-token={balance.tokenAddress}
              >
                <td className="fee-token-identity">
                  <div className="fee-token-name">
                    <TokenLogo src={balance.tokenLogoUrl} alt={`${symbol} logo`} />
                    <span className="token-symbol-wrap">
                      <span className="token-symbol" title={address || symbol}>
                        {symbol}
                      </span>
                      <CopyIconButton
                        valueToCopy={address}
                        title={`Copy token address ${address}`}
                        ariaLabel={`Copy token address for ${symbol}`}
                      />
                    </span>
                  </div>
                  {auctionWarning || pauseWarning ? (
                    <div className="fee-token-warnings">
                      {auctionWarning ? (
                        <span title={auctionWarning}>
                          {balance.auctionSellTokenStatus === "disabled"
                            ? "Not enabled in auction"
                            : "Auction status unknown"}
                        </span>
                      ) : null}
                      {pauseWarning ? (
                        <span title={pauseWarning}>
                          <KickPauseIcon title={pauseWarning} /> Paused
                        </span>
                      ) : null}
                    </div>
                  ) : null}
                </td>
                <td
                  className={`fee-token-amount align-right${amount.length > 20 ? " is-long-amount" : ""}`}
                  data-label="Amount"
                  title={`${balance.normalizedBalance ?? "?"} ${symbol}`}
                >
                  {amount}
                </td>
                <td
                  className="fee-token-usd align-right"
                  title={
                    balance.usdValue == null
                      ? "USD value unavailable: token price or amount is unknown"
                      : "Value in USD"
                  }
                >
                  {balance.usdValue == null ? "?" : `$${formatBalance(balance.usdValue)}`}
                </td>
              </tr>
            );
          })}
          {!row.balances.length ? (
            <tr>
              <td colSpan={3} className="empty">
                No balances above the visibility threshold.
              </td>
            </tr>
          ) : null}
        </tbody>
        {row.balances.length > 1 ? (
          <tfoot>
            <tr>
              <th scope="row" colSpan={2}>
                Total
              </th>
              <td
                className="fee-inventory-total"
                title={
                  row.totalUsdValue == null
                    ? "Total USD unavailable: one or more tokens are unpriced"
                    : "Total value in USD"
                }
              >
                {total}
              </td>
            </tr>
          </tfoot>
        ) : null}
      </table>

      <div className="fee-burner-activity">
        {latestKick ? (
          <>
            <div className="fee-activity-heading">
              <button
                type="button"
                className="fee-activity-toggle"
                aria-expanded={activityExpanded}
                aria-controls={activityId}
                onClick={onToggleActivity}
                aria-label={`${activityExpanded ? "Hide" : "Show"} recent activity for ${name}`}
              >
                <Chevron expanded={activityExpanded} /> Recent activity
              </button>
              {!activityExpanded ? (
                <time dateTime={latestKick.createdAt} title={formatUtcTimestamp(latestKick.createdAt)}>
                  {formatRelativeTimestamp(latestKick.createdAt, nowMs)}
                </time>
              ) : null}
            </div>
            <div id={activityId} className="fee-activity-list" hidden={!activityExpanded}>
              {activityExpanded ? (
                <KickHistoryCell
                  kicks={row.kicks}
                  nowMs={nowMs}
                  isExpanded
                  fallbackAuctionAddress={row.auctionAddress}
                />
              ) : null}
            </div>
          </>
        ) : (
          <span className="fee-activity-empty">Recent activity · None recorded</span>
        )}
      </div>
    </section>
  );
}

function FeeBurnerPage({ rows, state, nowMs, expandedKickRows, onToggleExpand }) {
  const latestScanAt = rows.reduce(
    (latest, row) => (row.scannedAt > (latest || "") ? row.scannedAt : latest),
    null
  );
  const refreshStatus = <RefreshStatus state={state} scannedAt={latestScanAt} nowMs={nowMs} />;

  return (
    <section className="inventory-page" aria-label="Fee burner inventory">
      {state.error ? (
        <p className="error" role="alert">
          {state.error}
        </p>
      ) : null}
      {!rows.length ? (
        <>
          {refreshStatus}
          {state.loading ? (
            <div className="fee-inventory-loading" role="status" aria-label="Loading fee burner inventory">
              {Array.from({ length: 4 }, (_, index) => (
                <span key={index} className="skeleton" />
              ))}
            </div>
          ) : (
            <p className="empty">
              {state.error ? "Fee burner inventory unavailable." : "No fee burners are available."}
            </p>
          )}
        </>
      ) : (
        rows.map((row, index) => (
          <FeeBurnerInventory
            key={row.sourceAddress}
            row={row}
            latestScanAt={latestScanAt}
            refreshStatus={index === 0 ? refreshStatus : null}
            nowMs={nowMs}
            activityExpanded={expandedKickRows.has(row.sourceAddress)}
            onToggleActivity={() => onToggleExpand(row.sourceAddress)}
          />
        ))
      )}
    </section>
  );
}

function EvidenceAmount({ value, raw = false }) {
  const amount = parseBig(value);
  if (!amount) return <span className="evidence-amount">?</span>;
  const exact = amount.toFixed();
  // The alerts contract omits token decimals. Never infer them from magnitude.
  const display =
    raw && amount.abs().gte("1000000000") ? amount.toExponential(2) : formatBalance(amount.toString());
  return (
    <span className="evidence-amount" title={`${exact} ${raw ? "raw sell units" : "buy tokens"}`}>
      {display}
    </span>
  );
}

function AlertRound({ round, nowMs }) {
  const [expanded, setExpanded] = useState(false);
  const roundId = useId();
  const hasProviders = Boolean(round.providers?.entries?.length);
  const exactValues = [
    ["Requested · raw sell units", round.requestedAmount],
    ["Placed · raw sell units", round.placedAmount],
    ["Recovered · raw sell units", round.recoveredAmount],
    ["Quote · buy tokens", round.quoteAmount],
    ["Minimum · buy tokens", round.minimumQuote],
  ].filter(([, value]) => value != null);
  return (
    <>
      <tr className="alert-round-row">
        <td>
          <button
            type="button"
            className="log-open"
            onClick={() => setExpanded((value) => !value)}
            aria-expanded={expanded}
            aria-controls={expanded ? roundId : undefined}
            aria-label={`${expanded ? "Hide" : "Show"} round details for log ${round.kickId}`}
          >
            <Chevron expanded={expanded} />
            <span>Log {round.kickId}</span>
          </button>
          <time
            className="round-time"
            dateTime={round.kickAt || undefined}
            title={formatUtcTimestamp(round.kickAt)}
          >
            {formatRelativeTimestamp(round.kickAt, nowMs)}
          </time>
        </td>
        <td className="alert-state-text" data-label="Outcome">
          {String(round.outcome || "UNKNOWN").replaceAll("_", " ")}
        </td>
        <td data-label="Placed · raw sell units">
          <EvidenceAmount value={round.placedAmount} raw />
        </td>
        <td data-label="Recovered · raw sell units">
          <EvidenceAmount value={round.recoveredAmount} raw />
        </td>
        <td data-label="Quote · buy tokens">
          <EvidenceAmount value={round.quoteAmount} />
        </td>
        <td data-label="Minimum · buy tokens">
          <EvidenceAmount value={round.minimumQuote} />
        </td>
      </tr>
      {expanded ? (
        <tr className="alert-round-detail">
          <td colSpan={6}>
            <div id={roundId}>
              <dl className="alert-evidence-list">
                {round.reasonCode ? (
                  <div>
                    <dt>Reason</dt>
                    <dd>{round.reasonCode}</dd>
                  </div>
                ) : null}
                <div>
                  <dt>Kick</dt>
                  <dd>
                    <time dateTime={round.kickAt || undefined}>{formatUtcTimestamp(round.kickAt)}</time>
                    {round.kickTxHash ? <EtherscanTxLink txHash={round.kickTxHash} /> : null}
                  </dd>
                </div>
                {round.closeId != null || round.closeAt || round.closeTxHash ? (
                  <div>
                    <dt>Close{round.closeId != null ? ` · log ${round.closeId}` : ""}</dt>
                    <dd>
                      <time dateTime={round.closeAt || undefined}>{formatUtcTimestamp(round.closeAt)}</time>
                      {round.closeTxHash ? <EtherscanTxLink txHash={round.closeTxHash} /> : null}
                    </dd>
                  </div>
                ) : null}
                {exactValues.map(([label, value]) => (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd className="mono">{parseBig(value)?.toFixed() ?? String(value)}</dd>
                  </div>
                ))}
              </dl>
              {hasProviders ? (
                <details className="provider-details">
                  <summary>Provider diagnostics · {round.providers.entries.length} responses</summary>
                  <dl className="provider-ledger">
                    {round.providers.entries.map((provider) => (
                      <div key={provider.name}>
                        <dt>{provider.name}</dt>
                        <dd>
                          {provider.status || "unknown"}
                          {provider.amountOut != null
                            ? ` · ${
                                parseBig(provider.amountOut)?.toFixed() ?? String(provider.amountOut)
                              } raw buy units`
                            : ""}
                        </dd>
                      </div>
                    ))}
                  </dl>
                  {round.providers.spreadPct != null ? (
                    <p className="provider-note">
                      Provider spread {round.providers.spreadPct}%. Agreement does not identify the cause.
                    </p>
                  ) : null}
                </details>
              ) : null}
            </div>
          </td>
        </tr>
      ) : null}
    </>
  );
}

function AlertRoundTimeline({ rounds = [], nowMs }) {
  if (!rounds.length) return <p className="muted">No round evidence available.</p>;
  return (
    <div className="alert-round-shell">
      <p className="evidence-note">
        Sell amounts are raw units; token decimals are unavailable. Quotes are in buy tokens. Open a log row
        for exact values and diagnostics.
      </p>
      <table className="alert-round-ledger">
        <thead>
          <tr>
            <th scope="col">Round</th>
            <th scope="col">Outcome</th>
            <th scope="col">
              Placed<span>raw sell units</span>
            </th>
            <th scope="col">
              Recovered<span>raw sell units</span>
            </th>
            <th scope="col">
              Quote<span>buy tokens</span>
            </th>
            <th scope="col">
              Minimum<span>buy tokens</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {rounds.map((round) => (
            <AlertRound key={round.kickId} round={round} nowMs={nowMs} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function AlertCard({ item, nowMs, lastObservedAt }) {
  const sourceLabel = item.scope?.sourceType === "fee_burner" ? "Fee burner" : "Strategy";
  const retryLabel = item.retryAt
    ? new Date(item.retryAt).getTime() <= nowMs
      ? "Retry eligible"
      : `Retry ${formatRelativeTimestamp(item.retryAt, nowMs)}`
    : null;
  const otherEvidence = Object.entries(item.evidence || {}).filter(([key]) => key !== "rounds");
  return (
    <article className={`alert-card alert-${item.severity}`} aria-label={item.title}>
      <div className="alert-card-header">
        <div className="alert-title">
          <span className="alert-kicker">{item.severity}</span>
          <h3>{item.title}</h3>
        </div>
        <div className="alert-age">
          <time dateTime={item.openedAt} title={formatUtcTimestamp(item.openedAt)}>
            Opened {formatRelativeTimestamp(item.openedAt, nowMs)}
          </time>
          {lastObservedAt ? (
            <time dateTime={lastObservedAt} title={formatUtcTimestamp(lastObservedAt)}>
              Observed {formatRelativeTimestamp(lastObservedAt, nowMs)}
            </time>
          ) : null}
        </div>
      </div>
      <p className="alert-summary">{item.summary}</p>
      <div className="alert-addresses">
        {item.scope?.sourceAddress ? (
          <div>
            <span>{sourceLabel}</span>
            <AddressLinkCopy address={item.scope.sourceAddress} />
          </div>
        ) : null}
        {item.scope?.auctionAddress ? (
          <div>
            <span>Auction</span>
            <AddressLinkCopy address={item.scope.auctionAddress} />
          </div>
        ) : null}
        {item.scope?.tokenAddress ? (
          <div>
            <span>Token</span>
            <AddressLinkCopy address={item.scope.tokenAddress} />
          </div>
        ) : null}
        {retryLabel ? (
          <time className="alert-retry" dateTime={item.retryAt} title={formatUtcTimestamp(item.retryAt)}>
            {retryLabel}
          </time>
        ) : null}
      </div>
      {item.nextAction?.instruction || item.nextAction?.command ? (
        <div className="alert-next-action">
          {item.nextAction?.instruction ? (
            <p>
              <span className="next-action-label">Next</span>
              {item.nextAction.instruction}
            </p>
          ) : null}
          {item.nextAction?.command ? (
            <span className="alert-command" title={item.nextAction.command}>
              Copy retry command
              <CopyIconButton
                valueToCopy={item.nextAction.command}
                title="Copy retry command"
                ariaLabel="Copy retry command"
              />
            </span>
          ) : null}
        </div>
      ) : null}
      <div className="alert-actions">
        <div className="alert-links">
          {item.links?.logs ? <a href={item.links.logs}>Logs</a> : null}
          {item.links?.etherscan ? (
            <a href={item.links.etherscan} target="_blank" rel="noopener noreferrer">
              Etherscan <OutboundLinkGlyph />
            </a>
          ) : null}
          {item.links?.auctionScan ? (
            <a href={item.links.auctionScan} target="_blank" rel="noopener noreferrer">
              AuctionScan <OutboundLinkGlyph />
            </a>
          ) : null}
        </div>
        <details className="alert-details">
          <summary>
            Evidence{item.evidence?.rounds?.length ? ` · ${item.evidence.rounds.length} rounds` : ""}
          </summary>
          {item.kind === "auction_retry" ? (
            <AlertRoundTimeline rounds={item.evidence?.rounds || []} nowMs={nowMs} />
          ) : null}
          {otherEvidence.length ? (
            <dl className="alert-evidence-list">
              {otherEvidence.map(([key, value]) => (
                <div key={key}>
                  <dt>{key.replaceAll(/([A-Z])/g, " $1")}</dt>
                  <dd className="mono">
                    {typeof value === "object" && value !== null
                      ? JSON.stringify(value, null, 2)
                      : String(value ?? "—")}
                  </dd>
                </div>
              ))}
            </dl>
          ) : null}
        </details>
      </div>
    </article>
  );
}

function AlertsPage({ state, nowMs }) {
  const { data, loading, error } = state;
  const items = Array.isArray(data?.items) ? data.items : [];
  const needsAction = items.filter((item) => item.status === "needs_action");
  const watching = items.filter((item) => item.status === "watching");
  const hasResults =
    Array.isArray(data?.items) &&
    items.length === needsAction.length + watching.length &&
    Number.isInteger(data?.needsActionCount) &&
    data.needsActionCount === needsAction.length;
  const evaluatedTime = Date.parse(data?.evaluatedAt);
  const scannedTime = Date.parse(data?.latestSuccessfulScanAt);
  const evaluationFresh = Number.isFinite(evaluatedTime) && Math.abs(nowMs - evaluatedTime) <= 65000;
  const scanFresh = Number.isFinite(scannedTime) && Math.abs(nowMs - scannedTime) <= SCAN_STALE_AFTER_MS;
  const current =
    hasResults &&
    !error &&
    evaluationFresh &&
    scanFresh &&
    Number.isFinite(state.updatedAt) &&
    nowMs - state.updatedAt <= 65000;
  return (
    <section className="alerts-page">
      <div className="alerts-meta">
        <div className="alert-counts" role="status">
          <span>
            <strong>{loading && !data ? "—" : needsAction.length}</strong> Needs action
          </span>
          <span>
            <strong>{loading && !data ? "—" : watching.length}</strong> Watching
          </span>
        </div>
        <RefreshStatus state={state} evaluatedAt={data?.evaluatedAt || null} nowMs={nowMs} />
      </div>
      {loading && !data ? (
        <p className="muted" role="status">
          Loading alerts…
        </p>
      ) : null}
      {error ? (
        <p className="error" role="alert">
          {error}
        </p>
      ) : null}
      {!loading && !current ? (
        <p className="alert-health-warning" role="status">
          Current health is unverified.{" "}
          {error
            ? "Refresh failed; showing the last available data."
            : !hasResults
            ? "Alert results are incomplete."
            : !evaluationFresh
            ? "Alert evaluation is missing or overdue."
            : !scanFresh
            ? "The latest successful scan is missing or overdue."
            : "Refresh is overdue."}{" "}
          Refresh before acting.
        </p>
      ) : null}
      {!loading && !items.length ? (
        <div className={`alerts-empty${current ? " is-current" : ""}`}>
          <strong>{current ? "No operator action needed" : "No current alert results to confirm"}</strong>
          <span>
            Latest successful scan{" "}
            <time
              dateTime={data?.latestSuccessfulScanAt || undefined}
              title={formatUtcTimestamp(data?.latestSuccessfulScanAt)}
            >
              {formatRelativeTimestamp(data?.latestSuccessfulScanAt, nowMs)}
            </time>
          </span>
        </div>
      ) : null}
      {needsAction.length ? (
        <section className="alert-section" aria-label="Needs action">
          {needsAction.map((item) => (
            <AlertCard key={item.id} item={item} nowMs={nowMs} lastObservedAt={data?.evaluatedAt} />
          ))}
        </section>
      ) : null}
      {watching.length ? (
        <section className="alert-section" aria-label="Watching">
          <h2>Watching</h2>
          {watching.map((item) => (
            <AlertCard key={item.id} item={item} nowMs={nowMs} lastObservedAt={data?.evaluatedAt} />
          ))}
        </section>
      ) : null}
    </section>
  );
}

export default function App() {
  const [initialLocation] = useState(() => parseLocation());
  const [activePage, setActivePage] = useState(() => initialLocation.page);
  const [initialRunId] = useState(() => initialLocation.runId);
  const [initialKickId] = useState(() => initialLocation.kickId);
  const [initialLogsOffset] = useState(() => initialLocation.logsOffset);
  const [initialLogsStatus] = useState(() => initialLocation.logsStatus);
  const [initialLogsQuery] = useState(() => initialLocation.logsQuery);
  const [selectedToken, setSelectedToken] = useState(getTokenFromUrl);
  const [balanceSortDirection, setBalanceSortDirection] = useState("desc");
  const [themePreference, setThemePreference] = useState(getStoredThemePreference);
  const [systemTheme, setSystemTheme] = useState(resolveSystemTheme);
  const [showZeroBalance, setShowZeroBalance] = useState(false);
  const [showClosedVaults, setShowClosedVaults] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const dashboard = useLiveData(apiUrl("/dashboard"), {
    active: activePage === "strategies" || activePage === "fee-burner",
    viewKey: activePage,
    errorMessage: "Unable to load dashboard",
  });
  const alerts = useLiveData(apiUrl("/alerts"), {
    active: activePage === "alerts", loadInitially: true, errorMessage: "Unable to load alerts",
  });
  const rows = useMemo(() => dashboard.data?.rows || [], [dashboard.data]);
  const summary = useMemo(() => ({
    ...dashboard.data?.summary,
    latestScanAt: dashboard.data?.latestScanAt || dashboard.data?.summary?.latestScanAt || null,
  }), [dashboard.data]);
  const { loading: loadingRows, error } = dashboard;
  const { data: alertsData } = alerts;
  const [displayMode, setDisplayMode] = useState("usd");
  const [nowMs, setNowMs] = useState(() => Date.now());
  const isMobile = useMediaQuery("(max-width: 780px)");
  const [expandedStrategyRows, setExpandedStrategyRows] = useState(() => new Set());
  const [expandedKickRows, setExpandedKickRows] = useState(() => new Set());
  const [expandedRewardRows, setExpandedRewardRows] = useState(() => new Set());
  const [deployStates, setDeployStates] = useState({});
  const [deployConfirm, setDeployConfirm] = useState(null);
  const deployChecksRef = useRef(new Set());
  const [tableHovered, setTableHovered] = useState(false);
  const [tableFocused, setTableFocused] = useState(false);

  const handlePageChange = (page) => {
    setActivePage(page);
    navigateTo(page);
  };

  useEffect(() => {
    const onPopState = () => {
      setActivePage(parseLocation().page);
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, []);

  const resolvedTheme = themePreference || systemTheme;
  const headerLogoSrc = resolvedTheme === "dark" ? "/tidal-logo-dark.svg" : "/tidal-logo-light.svg";

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return undefined;
    }

    const mediaQuery = window.matchMedia("(prefers-color-scheme: dark)");
    const onChange = (event) => {
      setSystemTheme(event.matches ? "dark" : "light");
    };
    mediaQuery.addEventListener("change", onChange);

    return () => {
      mediaQuery.removeEventListener("change", onChange);
    };
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }

    if (!themePreference) {
      document.documentElement.removeAttribute("data-theme");
    } else {
      document.documentElement.setAttribute("data-theme", themePreference);
    }
  }, [themePreference]);

  useEffect(() => {
    if (typeof window === "undefined") {
      return;
    }
    if (themePreference) {
      window.localStorage.setItem(THEME_STORAGE_KEY, themePreference);
      window.localStorage.removeItem(LEGACY_THEME_STORAGE_KEY);
    } else {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
      window.localStorage.removeItem(LEGACY_THEME_STORAGE_KEY);
    }
  }, [themePreference]);

  useEffect(() => {
    const timerId = window.setInterval(() => {
      setNowMs(Date.now());
    }, 30000);
    return () => {
      window.clearInterval(timerId);
    };
  }, []);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (selectedToken === ALL_TOKENS) {
      params.delete("token");
    } else {
      params.set("token", selectedToken);
    }

    const nextQuery = params.toString();
    const nextUrl = `${window.location.pathname}${nextQuery ? `?${nextQuery}` : ""}`;
    window.history.replaceState({}, "", nextUrl);
  }, [selectedToken]);

  const decoratedRows = useMemo(() => {
    return rows
      .map((row) => {
        const normalizedRow = normalizeDashboardRow(row);
        const visibleBalances = normalizedRow.balances
          .map((balance) => {
            const normalizedBalance = parseBig(balance.normalizedBalance);
            const tokenPriceUsd = parseBig(balance.tokenPriceUsd);
            const usdValue =
              normalizedBalance && tokenPriceUsd
                ? normalizedBalance.times(tokenPriceUsd)
                : null;

            return {
              ...balance,
              usdValue: usdValue ? usdValue.toString() : null,
            };
          })
          .filter((balance) => {
            const normalizedBalance = parseBig(balance.normalizedBalance);
            if (normalizedBalance && normalizedBalance.eq(0)) {
              return false;
            }
            if (!balance.usdValue) {
              return true;
            }
            const usdValue = parseBig(balance.usdValue);
            if (!usdValue) {
              return true;
            }
            return usdValue.gte(MIN_USD_VISIBLE);
          });

        const missingAnyUsdValue = visibleBalances.some((balance) => !balance.usdValue);
        const totalUsdValue = !missingAnyUsdValue
          ? visibleBalances.reduce((sum, balance) => {
              const usdValue = parseBig(balance.usdValue);
              return usdValue ? sum.plus(usdValue) : sum;
            }, new Big(0)).toString()
          : null;

        return {
          ...normalizedRow,
          balances: visibleBalances,
          totalUsdValue,
        };
      });
  }, [rows]);

  const strategyRows = useMemo(
    () => decoratedRows.filter((row) => row.sourceType === "strategy"),
    [decoratedRows],
  );

  const feeBurnerRows = useMemo(
    () => decoratedRows.filter((row) => row.sourceType === "fee_burner"),
    [decoratedRows],
  );

  const tokenOptions = useMemo(() => {
    const byAddress = new Map();

    for (const row of strategyRows) {
      for (const balance of row.balances) {
        if (!balance.tokenAddress) {
          continue;
        }
        const key = balance.tokenAddress.toLowerCase();
        const existing = byAddress.get(key);
        byAddress.set(key, {
          tokenAddress: balance.tokenAddress,
          tokenSymbol: String(balance.tokenSymbol || "UNKNOWN").trim() || "UNKNOWN",
          strategyCount: existing ? existing.strategyCount + 1 : 1,
        });
      }
    }

    return Array.from(byAddress.values()).sort(
      (a, b) => b.strategyCount - a.strategyCount || a.tokenSymbol.localeCompare(b.tokenSymbol),
    );
  }, [strategyRows]);

  const visibleStrategyRows = useMemo(() => {
    return strategyRows.filter(
      (row) => (showZeroBalance || row.balances.length > 0) && (showClosedVaults || row.depositLimit !== "0"),
    );
  }, [strategyRows, showZeroBalance, showClosedVaults]);

  const filteredStrategyRows = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    const filtered = visibleStrategyRows.filter((row) => {
      const tokenMatch =
        selectedToken === ALL_TOKENS
          ? true
          : row.balances.some(
              (balance) => balance.tokenAddress && balance.tokenAddress.toLowerCase() === selectedToken.toLowerCase(),
            );

      if (!tokenMatch) {
        return false;
      }

      if (!term) {
        return true;
      }

      const searchable = [
        row.sourceName,
        row.sourceAddress,
        row.contextAddress,
        row.contextName,
        row.contextSymbol,
        row.auctionAddress,
        row.wantAddress,
        row.wantSymbol,
        ...row.balances.map((balance) => `${balance.tokenSymbol} ${balance.tokenAddress}`),
      ]
        .filter(Boolean)
        .join(" ")
        .toLowerCase();

      return searchable.includes(term);
    });

    filtered.sort((a, b) => {
      const totalA = parseBig(a.totalUsdValue);
      const totalB = parseBig(b.totalUsdValue);

      if (!totalA && !totalB) {
        return (a.sourceAddress || "").localeCompare(b.sourceAddress || "");
      }
      if (!totalA) {
        return 1;
      }
      if (!totalB) {
        return -1;
      }

      const cmp = totalA.cmp(totalB);
      if (cmp === 0) {
        return (a.sourceAddress || "").localeCompare(b.sourceAddress || "");
      }
      return balanceSortDirection === "desc" ? -cmp : cmp;
    });

    return filtered;
  }, [visibleStrategyRows, searchTerm, selectedToken, balanceSortDirection]);

  const orderedStrategyRows = useStableRowOrder(
    filteredStrategyRows,
    JSON.stringify([searchTerm, selectedToken, showZeroBalance, showClosedVaults, balanceSortDirection]),
    tableHovered || tableFocused || Boolean(deployConfirm) || Object.values(deployStates).some((state) => ["preparing", "wallet", "checking"].includes(state.status)),
  );

  const latestVisibleScan = useMemo(() => {
    if (summary?.latestScanAt) return summary.latestScanAt;
    return strategyRows.reduce((latest, row) => {
      if (!latest) {
        return row.scannedAt;
      }
      return row.scannedAt > latest ? row.scannedAt : latest;
    }, null);
  }, [strategyRows, summary]);

  function toggleDisplayMode() {
    setDisplayMode((prev) => (prev === "token" ? "usd" : "token"));
  }

  function toggleBalanceSortDirection() {
    setBalanceSortDirection((prev) => (prev === "desc" ? "asc" : "desc"));
  }

  function toggleKickExpand(sourceAddress) {
    setExpandedKickRows((prev) => {
      const next = new Set(prev);
      if (next.has(sourceAddress)) {
        next.delete(sourceAddress);
      } else {
        next.add(sourceAddress);
      }
      return next;
    });
  }

  function toggleRewardExpand(sourceAddress) {
    setExpandedRewardRows((previous) => {
      const next = new Set(previous);
      if (next.has(sourceAddress)) next.delete(sourceAddress);
      else next.add(sourceAddress);
      return next;
    });
  }

  function toggleStrategyExpand(sourceAddress) {
    setExpandedStrategyRows((prev) => {
      if (prev.has(sourceAddress)) {
        const next = new Set(prev);
        next.delete(sourceAddress);
        return next;
      }
      if (isMobile) {
        return new Set([sourceAddress]);
      }
      const next = new Set(prev);
      next.add(sourceAddress);
      return next;
    });
  }

  function updateDeployState(sourceAddress, updates) {
    setDeployStates((prev) => ({
      ...prev,
      [sourceAddress]: {
        status: "idle",
        error: "",
        txHash: null,
        ...(prev[sourceAddress] || {}),
        ...updates,
      },
    }));
  }

  async function handleDeployStrategy(row) {
    const sourceAddress = row.sourceAddress;
    if (!sourceAddress) {
      return;
    }

    const provider = await getEthereumProvider();
    if (!provider) {
      updateDeployState(sourceAddress, { status: "idle", error: "No injected wallet found" });
      return;
    }

    updateDeployState(sourceAddress, { status: "preparing", error: "" });

    try {
      const response = await apiFetch(`/strategies/${sourceAddress}/deploy-defaults`);

      let payload = null;
      try {
        payload = await response.json();
      } catch {
        payload = null;
      }

      if (!response.ok) {
        throw new Error(payload?.detail || "Unable to load deploy defaults");
      }

      const deployDefaults = payload?.data;
      if (!deployDefaults?.wantAddress || !deployDefaults?.factoryAddress || !deployDefaults?.startingPrice) {
        throw new Error("Deploy defaults payload is incomplete");
      }

      const accounts = await provider.request({ method: "eth_requestAccounts" });
      const account = Array.isArray(accounts) ? accounts[0] : null;
      if (!account) throw new Error("No wallet account connected");
      const prepareResponse = await apiFetch("/auctions/deploy/browser-prepare", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          want: deployDefaults.wantAddress,
          receiver: deployDefaults.receiverAddress || deployDefaults.strategyAddress || sourceAddress,
          sender: account,
          factory: deployDefaults.factoryAddress,
          governance: deployDefaults.governanceAddress,
          startingPrice: deployDefaults.startingPrice,
          salt: deployDefaults.salt,
        }),
      });
      const preparedPayload = await prepareResponse.json();
      if (!prepareResponse.ok) throw new Error(preparedPayload?.detail || "Unable to prepare deploy transaction");
      const txRequest = preparedPayload?.data?.transactions?.[0];
      if (!txRequest?.to || !txRequest?.data) throw new Error("Deploy transaction payload is incomplete");
      const warnings = [...new Set([
        ...(Array.isArray(payload.warnings) ? payload.warnings : []),
        ...(Array.isArray(deployDefaults.warnings) ? deployDefaults.warnings : []),
        ...(Array.isArray(preparedPayload.warnings) ? preparedPayload.warnings : []),
      ])];
      setDeployConfirm({ sourceAddress, payload: { ...deployDefaults, warnings }, provider, account, txRequest });
    } catch (deployError) {
      updateDeployState(sourceAddress, {
        status: "idle",
        error: formatDeployError(deployError),
      });
    }
  }

  function handleDeployCancel() {
    if (deployConfirm) {
      updateDeployState(deployConfirm.sourceAddress, { status: "idle", error: "" });
    }
    setDeployConfirm(null);
  }

  async function checkDeployment(sourceAddress, provider, txHash, chainId, attempts = 60) {
    if (deployChecksRef.current.has(sourceAddress)) return;
    deployChecksRef.current.add(sourceAddress);
    updateDeployState(sourceAddress, { status: "checking", error: "", txHash, chainId });
    try {
      const receipt = await waitForTransactionReceipt(provider, txHash, chainId, attempts);
      const receiptStatus = normalizeReceiptStatus(receipt?.status);
      if (receiptStatus === 1) {
        updateDeployState(sourceAddress, { status: "confirmed", error: "" });
        dashboard.refresh();
      } else if (receiptStatus === 0) {
        updateDeployState(sourceAddress, { status: "reverted", error: "Deployment transaction reverted" });
      } else {
        updateDeployState(sourceAddress, { status: "pending", error: "Receipt not yet confirmed. Check again shortly." });
      }
    } catch (error) {
      updateDeployState(sourceAddress, { status: "pending", error: formatDeployError(error) });
    } finally {
      deployChecksRef.current.delete(sourceAddress);
    }
  }

  async function handleCheckDeployment(sourceAddress) {
    const state = deployStates[sourceAddress];
    if (!state?.txHash || deployChecksRef.current.has(sourceAddress)) return;
    const provider = await getEthereumProvider();
    if (!provider) {
      updateDeployState(sourceAddress, { error: "Connect your wallet to check this transaction." });
      return;
    }
    await checkDeployment(sourceAddress, provider, state.txHash, state.chainId, 1);
  }

  async function handleDeployConfirm() {
    if (!deployConfirm) return;
    const { sourceAddress, provider, account, txRequest } = deployConfirm;
    setDeployConfirm(null);

    updateDeployState(sourceAddress, { status: "wallet", error: "" });
    let txHash = null;

    try {
      const accounts = await provider.request({ method: "eth_accounts" });
      if (!Array.isArray(accounts) || accounts[0]?.toLowerCase() !== account.toLowerCase()) {
        throw new Error("Wallet account changed; prepare the deployment again.");
      }

      const requiredChainId = normalizeChainIdValue(txRequest.chainId);
      if (requiredChainId != null) {
        const activeChainId = normalizeChainIdValue(await provider.request({ method: "eth_chainId" }));
        if (activeChainId !== requiredChainId) {
          await provider.request({
            method: "wallet_switchEthereumChain",
            params: [{ chainId: toHexChainId(requiredChainId) }],
          });
        }
      }

      txHash = await provider.request({
        method: "eth_sendTransaction",
        params: [
          {
            from: account,
            to: txRequest.to,
            data: txRequest.data,
            value: txRequest.value || "0x0",
          },
        ],
      });

      await checkDeployment(sourceAddress, provider, txHash, requiredChainId);
    } catch (deployError) {
      updateDeployState(sourceAddress, {
        status: txHash ? "pending" : "idle",
        error: formatDeployError(deployError),
        txHash,
      });
    }
  }

  function cycleThemePreference() {
    const currentTheme = themePreference || systemTheme;
    const currentIndex = THEME_SEQUENCE.indexOf(currentTheme);
    const next = THEME_SEQUENCE[(currentIndex + 1) % THEME_SEQUENCE.length];
    setThemePreference(next);
  }

  return (
    <main className="page">
      <header className="header">
        <div className="header-row">
          <h1 className="header-title">
            <img src={headerLogoSrc} alt="" className="brand-logo" aria-hidden="true" />
            <span>Tidal</span>
          </h1>
          <TabBar
            activePage={activePage}
            onChangePage={handlePageChange}
            alertCount={alertsData?.needsActionCount || 0}
          />
          <ThemeSwitch
            themePreference={themePreference}
            resolvedTheme={resolvedTheme}
            onCycle={cycleThemePreference}
          />
        </div>
      </header>

      {activePage === "kicks" ? (
        <KickLogPage
          nowMs={nowMs}
          initialRunId={initialRunId}
          initialKickId={initialKickId}
          initialOffset={initialLogsOffset}
          initialStatus={initialLogsStatus}
          initialSearch={initialLogsQuery}
        />
      ) : null}

      {activePage === "alerts" ? (
        <AlertsPage state={alerts} nowMs={nowMs} />
      ) : null}

      {activePage === "strategies" ? (
      <>
      <section className="toolbar">
        <div className="toolbar-controls">
          <label className="control control-search">
            <input
              type="search"
              aria-label="Search strategies, vaults, tokens, addresses"
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="Search strategies, vaults, tokens, addresses..."
            />
          </label>

          <label className="control control-token">
            <select
              aria-label="Filter by reward token"
              value={selectedToken}
              onChange={(event) => setSelectedToken(event.target.value)}
            >
              <option value={ALL_TOKENS}>All tokens</option>
              {selectedToken !== ALL_TOKENS && !tokenOptions.some((token) => token.tokenAddress.toLowerCase() === selectedToken.toLowerCase()) ? (
                <option value={selectedToken}>Selected token ({shortenAddress(selectedToken)})</option>
              ) : null}
              {tokenOptions.map((token) => (
                <option key={token.tokenAddress} value={token.tokenAddress}>
                  {token.tokenSymbol} ({token.strategyCount})
                </option>
              ))}
            </select>
          </label>

          <label className="toggle-filter">
            <input
              type="checkbox"
              checked={showZeroBalance}
              onChange={(e) => setShowZeroBalance(e.target.checked)}
            />
            <span>Include zero rewards</span>
          </label>

          <label className="toggle-filter">
            <input
              type="checkbox"
              checked={showClosedVaults}
              onChange={(e) => setShowClosedVaults(e.target.checked)}
            />
            <span>Include retired</span>
          </label>
        </div>

      </section>

      <div className="toolbar-meta page-meta">
        <span className="result-count" role="status"><strong>{filteredStrategyRows.length.toLocaleString()}</strong> of {strategyRows.length.toLocaleString()} strategies</span>
        <RefreshStatus state={dashboard} scannedAt={latestVisibleScan} nowMs={nowMs} />
      </div>

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell ledger-table-shell"
        onPointerEnter={(event) => { if (event.pointerType !== "touch") setTableHovered(true); }}
        onPointerLeave={() => setTableHovered(false)}
        onFocusCapture={() => setTableFocused(true)}
        onBlurCapture={(event) => { if (!event.currentTarget.contains(event.relatedTarget)) setTableFocused(false); }}>
        <table className="strategies-table ledger-table">
          <colgroup><col className="strategy-col" /><col className="auction-col" /><col className="history-col" /><col className="token-col" /></colgroup>
          <thead>
            <tr>
              <th id="strategy-heading" scope="col">Strategy</th>
              <th id="auction-heading" scope="col" className="auction-col">Auction</th>
              <th id="activity-heading" scope="col" className="history-col">Last activity</th>
              <th id="rewards-heading" scope="col" className="token-col" aria-sort={balanceSortDirection === "desc" ? "descending" : "ascending"}>
                <div className="rewards-heading"><span>Rewards</span>
                <button
                  type="button"
                  className="th-sort-button"
                  onClick={toggleBalanceSortDirection}
                  aria-label={`Sort rewards ${balanceSortDirection === "desc" ? "ascending" : "descending"}`}
                  title={`Sort by total token USD (${balanceSortDirection === "desc" ? "descending" : "ascending"})`}
                >
                  USD
                  <span className="sort-indicator" aria-hidden="true">
                    {balanceSortDirection === "desc" ? "↓" : "↑"}
                  </span>
                </button>
                </div>
              </th>
            </tr>
          </thead>
          <tbody>
            {loadingRows ? <SkeletonRows /> : null}
            {!loadingRows && !filteredStrategyRows.length ? (
              <tr>
                <td colSpan={4} className="empty">No strategies match the current filters.</td>
              </tr>
            ) : null}
            {!loadingRows
              ? orderedStrategyRows.map((row) => (
                  <Fragment key={row.sourceAddress}>
                    <tr
                      className={`strategy-row ledger-row ${expandedStrategyRows.has(row.sourceAddress) ? "is-expanded" : ""}`}
                      data-strategy={row.sourceAddress}
                    >
                      <td data-label="Strategy" headers="strategy-heading" className="strategy-identity-cell">
                        <EntityIdentity
                          primary={formatStrategyDisplayName(row.sourceName)}
                          address={row.sourceAddress}
                          onOpen={() => toggleStrategyExpand(row.sourceAddress)}
                          expanded={expandedStrategyRows.has(row.sourceAddress)}
                        />
                        <RowScanStatus row={row} latestScanAt={latestVisibleScan} />
                      </td>
                      <td className="auction-cell" data-label="Auction" headers="auction-heading">
                        <AuctionAddressCell
                          address={row.auctionAddress}
                          version={row.auctionVersion}
                          wantAddress={row.wantAddress}
                          wantSymbol={row.wantSymbol}
                          emptyContent={
                            <MissingAuctionAction
                              deployState={deployStates[row.sourceAddress]}
                              onDeploy={() => handleDeployStrategy(row)}
                              onCheck={() => handleCheckDeployment(row.sourceAddress)}
                            />
                          }
                        />
                      </td>
                      <td className="history-cell" data-label="Last activity" headers="activity-heading">
                        <KickHistoryCell
                          kicks={row.kicks}
                          nowMs={nowMs}
                          isExpanded={expandedKickRows.has(row.sourceAddress)}
                          onToggleExpand={() => toggleKickExpand(row.sourceAddress)}
                          fallbackAuctionAddress={row.auctionAddress}
                        />
                      </td>
                      <td className="balances-cell" data-label="Rewards" headers="rewards-heading">
                        <RewardSummary
                          row={row}
                          expanded={expandedRewardRows.has(row.sourceAddress)}
                          onToggleExpand={() => toggleRewardExpand(row.sourceAddress)}
                          displayMode={displayMode}
                          onToggleMode={toggleDisplayMode}
                        />
                      </td>
                    </tr>
                    {expandedStrategyRows.has(row.sourceAddress) && !isMobile ? (
                      <StrategyDetailPanel
                        row={row}
                        nowMs={nowMs}
                        displayMode={displayMode}
                        onToggleMode={toggleDisplayMode}
                        deployState={deployStates[row.sourceAddress]}
                        onDeploy={() => handleDeployStrategy(row)}
                        onCheckDeploy={() => handleCheckDeployment(row.sourceAddress)}
                        initialHistoryExpanded={expandedKickRows.has(row.sourceAddress)}
                      />
                    ) : null}
                    {expandedStrategyRows.has(row.sourceAddress) && isMobile ? (
                      <StrategyDetailModal
                        row={row}
                        nowMs={nowMs}
                        displayMode={displayMode}
                        onToggleMode={toggleDisplayMode}
                        deployState={deployStates[row.sourceAddress]}
                        onDeploy={() => handleDeployStrategy(row)}
                        onCheckDeploy={() => handleCheckDeployment(row.sourceAddress)}
                        initialHistoryExpanded={expandedKickRows.has(row.sourceAddress)}
                        onClose={() => toggleStrategyExpand(row.sourceAddress)}
                      />
                    ) : null}
                  </Fragment>
                ))
              : null}
          </tbody>
        </table>
      </div>
      </>
      ) : null}

      {activePage === "fee-burner" ? (
        <FeeBurnerPage
          rows={feeBurnerRows}
          state={dashboard}
          nowMs={nowMs}
          expandedKickRows={expandedKickRows}
          onToggleExpand={toggleKickExpand}
        />
      ) : null}
      {deployConfirm ? (
        <DeployConfirmModal
          payload={deployConfirm.payload}
          onConfirm={handleDeployConfirm}
          onCancel={handleDeployCancel}
        />
      ) : null}
    </main>
  );
}
