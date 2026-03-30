import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import Big from "big.js";

const ALL_TOKENS = "__all__";
const MIN_USD_VISIBLE = new Big("0.01");
const THEME_SEQUENCE = ["light", "dark"];
const THEME_STORAGE_KEY = "tidal_theme_preference";
const LEGACY_THEME_STORAGE_KEY = "factory_dashboard_theme_preference";
const API_TOKEN = import.meta.env.VITE_TIDAL_API_KEY || import.meta.env.VITE_TIDAL_API_TOKEN || "";
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
const FAILED_STATUSES = new Set(["REVERTED", "ERROR", "ESTIMATE_FAILED"]);
const FAINT_STATUSES = new Set(["DRY_RUN", "SUBMITTED", "USER_SKIPPED", "SKIP"]);
const KICK_LOG_PAGE_SIZE = 25;

function apiUrl(path) {
  return `${API_BASE_URL}${path}`;
}

async function apiFetch(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (API_TOKEN) {
    headers.set("Authorization", `Bearer ${API_TOKEN}`);
  }
  return fetch(apiUrl(path), {
    ...options,
    headers,
  });
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
  const slug = page === "kicks" ? "logs" : page === "fee-burner" ? "fee-burner" : "strategies";
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

function shortenAddress(address) {
  if (!address || address.length < 13) {
    return address || "—";
  }
  return `${address.slice(0, 6)}...${address.slice(-4)}`;
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
    `Factory: ${shortenAddress(spec.factoryAddress)}`,
    `Receiver: ${shortenAddress(spec.receiverAddress || spec.strategyAddress)}`,
    `Want: ${spec.wantSymbol || shortenAddress(spec.wantAddress)}`,
  ];

  if (spec.inference?.sellTokenAddress) {
    lines.push(
      `Inference token: ${spec.inference.sellTokenSymbol || shortenAddress(spec.inference.sellTokenAddress)}`,
    );
  }
  if (spec.startingPrice) {
    lines.push(`Starting price: ${spec.startingPrice}`);
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

async function waitForTransactionReceipt(provider, txHash, attempts = 60, delayMs = 2000) {
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const receipt = await provider.request({
      method: "eth_getTransactionReceipt",
      params: [txHash],
    });
    if (receipt) {
      return receipt;
    }
    await new Promise((resolve) => {
      window.setTimeout(resolve, delayMs);
    });
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
  const chainId = normalizeChainIdValue(kick.chainId);
  const auctionAddress = kick.auctionAddress || null;
  const auctionScanRoundId = kick.auctionScanRoundId ?? null;
  const auctionScanLinkable = isAuctionScanLinkableKick(kick, chainId);
  return {
    ...kick,
    chainId,
    operationType: kick.operationType || "kick",
    sourceType: kick.sourceType || (kick.strategyAddress ? "strategy" : null),
    sourceAddress: kick.sourceAddress || kick.strategyAddress || null,
    sourceName: kick.sourceName || kick.strategyName || null,
    auctionScanRoundId,
    auctionScanMatchedAt: kick.auctionScanMatchedAt || null,
    auctionScanLastCheckedAt: kick.auctionScanLastCheckedAt || null,
    auctionScanResolved: Boolean(kick.auctionScanResolved ?? (auctionScanRoundId != null)),
    auctionScanEligible: kick.auctionScanEligible ?? undefined,
    auctionScanAuctionUrl:
      auctionScanLinkable ? (kick.auctionScanAuctionUrl || buildAuctionScanAuctionUrl(chainId, auctionAddress)) : null,
    auctionScanRoundUrl:
      auctionScanLinkable ? (kick.auctionScanRoundUrl || buildAuctionScanRoundUrl(chainId, auctionAddress, auctionScanRoundId)) : null,
    auctionScanResolving: Boolean(kick.auctionScanResolving),
    auctionScanResolveError: kick.auctionScanResolveError || "",
  };
}

function isAuctionScanLinkableKick(kick, chainId = normalizeChainIdValue(kick.chainId)) {
  return Boolean(
    chainId
    && kick.auctionAddress
    && kick.txHash
    && (kick.operationType || "kick") === "kick"
    && kick.status === "CONFIRMED"
  );
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

function formatKickPairLabel(kick) {
  if (kick.operationType === "settle") {
    return `SETTLE ${kick.tokenSymbol || "?"}`;
  }
  if (kick.operationType === "sweep_and_settle") {
    return `ABORT ${kick.tokenSymbol || "?"}`;
  }
  return `${kick.tokenSymbol || "?"} → ${kick.wantSymbol || "?"}`;
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
    return diffSeconds >= 0 ? "just now" : "in a moment";
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
    <tr key={`skeleton-${index}`} className="strategy-skeleton">
      <td><span className="skeleton" /></td>
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
  if (!address) {
    return <span className="row-secondary mono">—</span>;
  }

  return (
    <span className="address-copy" title={address}>
      <span className="mono address-value">{shortenAddress(address)}</span>
      <CopyIconButton
        valueToCopy={address}
        title={`Copy address ${address}`}
        ariaLabel={`Copy address ${address}`}
      />
    </span>
  );
}

function EntityIdentity({ primary, secondary, address }) {
  return (
    <div className="entity-cell">
      <div className="row-primary">{primary || "—"}</div>
      {secondary ? <div className="entity-secondary mono">{secondary}</div> : null}
      <AddressCopy address={address} />
    </div>
  );
}

function EtherscanTxLink({ txHash }) {
  const normalized = txHash.startsWith("0x") ? txHash : `0x${txHash}`;
  return (
    <a
      className="etherscan-link mono"
      href={`${ETHERSCAN_TX_URL}${normalized}`}
      title={normalized}
      target="_blank"
      rel="noopener noreferrer"
    >
      {`${normalized.slice(0, 6)}...${normalized.slice(-4)}`}
    </a>
  );
}

function getAuctionScanHref(kick) {
  if (!isAuctionScanLinkableKick(kick)) {
    return null;
  }
  return kick.auctionScanRoundUrl || kick.auctionScanAuctionUrl || null;
}

function getAuctionScanTargetLabel(kick) {
  return kick.auctionScanRoundUrl ? "round" : "auction";
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

function AuctionScanTextLink({ kick, onOpen }) {
  const href = getAuctionScanHref(kick);
  if (!href) {
    return null;
  }

  const target = getAuctionScanTargetLabel(kick);
  const handleClick = (event) => {
    event.stopPropagation();
    if (!kick.auctionScanRoundUrl && onOpen) {
      event.preventDefault();
      onOpen(kick);
    }
  };

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="kick-external-link"
      onClick={handleClick}
    >
      <span>{`view ${target} on`}</span>
      <AuctionScanFavicon />
      <span>auctionscan.info</span>
    </a>
  );
}

function AuctionScanIconLink({ kick, onOpen }) {
  const href = getAuctionScanHref(kick);
  if (!href) {
    return null;
  }

  const target = getAuctionScanTargetLabel(kick);
  const handleClick = (event) => {
    event.stopPropagation();
    if (!kick.auctionScanRoundUrl && onOpen) {
      event.preventDefault();
      onOpen(kick);
    }
  };

  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      className="kick-auctionscan-link"
      title={`View ${target} on AuctionScan`}
      aria-label={`View ${target} on AuctionScan`}
      onClick={handleClick}
    >
      <AuctionScanFavicon className="kick-auctionscan-link-icon" />
      <OutboundLinkGlyph className="kick-auctionscan-link-glyph" />
    </a>
  );
}

function MissingAuctionAction({ deployState, onDeploy }) {
  const status = deployState?.status || "idle";
  const txHash = deployState?.txHash || null;
  const error = deployState?.error || "";
  const isBusy = status === "preparing" || status === "wallet";

  return (
    <div className="auction-missing-state">
      {txHash ? (
        <div className="auction-action-status">
          <span className="row-secondary mono">submitted</span>
          <span className="kick-separator mono">·</span>
          <EtherscanTxLink txHash={txHash} />
        </div>
      ) : (
        <button type="button" className="auction-action-link" onClick={onDeploy} disabled={isBusy}>
          {status === "wallet" ? (
            <span className="mono">confirm in wallet…</span>
          ) : status === "preparing" ? (
            <span className="mono">preparing…</span>
          ) : (
            <>
              <span className="deploy-cta">N/A</span>
              <br />
              <span className="deploy-cta">click to deploy now 🚀</span>
            </>
          )}
        </button>
      )}
      {error ? <div className="auction-action-error">{error}</div> : null}
    </div>
  );
}

function DeployConfirmModal({ payload, onConfirm, onCancel }) {
  useEffect(() => {
    const onKeyDown = (e) => { if (e.key === "Escape") onCancel(); };
    window.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = "";
    };
  }, [onCancel]);

  const spec = payload || {};
  const warnings = Array.isArray(spec.warnings) ? spec.warnings.filter(Boolean) : [];
  const rows = [
    ["Factory", shortenAddress(spec.factoryAddress)],
    ["Receiver", shortenAddress(spec.receiverAddress)],
    ["Want", spec.wantSymbol || shortenAddress(spec.wantAddress)],
  ];
  if (spec.inference?.sellTokenAddress) {
    rows.push(["Inference token", spec.inference.sellTokenSymbol || shortenAddress(spec.inference.sellTokenAddress)]);
  }
  if (spec.startingPrice) {
    rows.push(["Starting price", spec.startingPrice]);
  }
  if (spec.startPriceBufferBps != null) {
    rows.push(["Start-price buffer", `+${(Number(spec.startPriceBufferBps) / 100).toFixed(1)}%`]);
  }
  if (spec.predictedAuctionAddress) {
    rows.push(["Predicted auction", shortenAddress(spec.predictedAuctionAddress)]);
  }

  return createPortal(
    <div className="deploy-modal-backdrop" onMouseDown={onCancel}>
      <div className="deploy-modal" onMouseDown={(e) => e.stopPropagation()}>
        <div className="deploy-modal-title">
          Deploy auction for {spec.strategyName || shortenAddress(spec.strategyAddress)}?
        </div>
        <dl className="deploy-modal-details">
          {rows.map(([label, value]) => (
            <div key={label} className="deploy-modal-row">
              <dt>{label}</dt>
              <dd className="mono">{value}</dd>
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
          <button type="button" className="deploy-modal-btn deploy-modal-btn-cancel" onClick={onCancel}>
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

function AuctionAddressCell({ address, version, kicks, nowMs, isExpanded, onToggleExpand, emptyContent = null }) {
  const hasKicks = kicks && kicks.length > 0;
  const hasChevron = kicks && kicks.length > 1;

  if (!address) {
    return (
      <span className="auction-value-slot">
        {emptyContent || <span className="row-secondary mono">—</span>}
      </span>
    );
  }

  return (
    <div className="auction-value-slot">
      <span className="auction-address-row">
        <AddressCopy address={address} />
        {version ? <span className="auction-version-badge mono">{version}</span> : null}
      </span>
      {hasKicks ? (
        <div className="kick-history">
          <div className="kick-summary">
            {hasChevron ? (
              <button
                type="button"
                className={`chevron-toggle ${isExpanded ? "is-expanded" : ""}`}
                onClick={onToggleExpand}
                aria-label={isExpanded ? "Collapse kick history" : "Expand kick history"}
              >
                ▶
              </button>
            ) : null}
            <KickRow kick={kicks[0]} nowMs={nowMs} />
          </div>
          {isExpanded && kicks.length > 1 ? (
            <div className="kick-expanded">
              {kicks.slice(1, 5).map((kick, i) => (
                <div key={kick.txHash || i} className="kick-row">
                  <KickRow kick={kick} nowMs={nowMs} />
                </div>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

function KickRow({ kick, nowMs }) {
  return (
    <span className="kick-row-inner">
      <span className="kick-time mono">{formatRelativeTimestamp(kick.createdAt, nowMs)}</span>
      <span className="kick-separator mono">·</span>
      <EtherscanTxLink txHash={kick.txHash} />
    </span>
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

function TabBar({ activePage, onChangePage }) {
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
    </nav>
  );
}

function formatKickStatusLabel(status, operationType) {
  if (status !== "CONFIRMED") {
    return status;
  }

  if (operationType === "settle") {
    return "SETTLED";
  }
  if (operationType === "sweep_and_settle") {
    return "ABORTED";
  }

  return "KICKED";
}

function StatusBadge({ status, operationType }) {
  let className = "status-badge";
  if (status === "CONFIRMED") {
    className += " status-confirmed";
  } else if (FAILED_STATUSES.has(status)) {
    className += " status-error";
  } else if (FAINT_STATUSES.has(status)) {
    className += " status-faint";
  }

  return <span className={className}>{formatKickStatusLabel(status, operationType)}</span>;
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

function KickDetailContent({ kick, onOpenAuctionScan }) {
  const [showRelativeTimestamp, setShowRelativeTimestamp] = useState(false);
  let quoteProviders = null;
  let quoteSummary = null;
  let tokenOutDecimals = null;
  let quoteRequestUrl = null;
  let identifierLabel = null;
  let identifierValue = null;

  if (kick.quoteResponseJson) {
    try {
      const parsed = JSON.parse(kick.quoteResponseJson);
      if (parsed.providers && typeof parsed.providers === "object") {
        tokenOutDecimals = parsed.tokenOutDecimals ?? parsed.token_out?.decimals ?? null;
        quoteProviders = Object.entries(parsed.providers).map(([name, entry]) => ({
          name,
          status: entry?.status ?? null,
          amountOut: entry?.amount_out ?? null,
        }));
      }
      if (parsed.summary && typeof parsed.summary === "object") {
        quoteSummary = parsed.summary;
      }
      if (parsed.requestUrl) {
        quoteRequestUrl = parsed.requestUrl;
      }
    } catch {
      // ignore parse errors
    }
  }

  if (kick.runId && kick.runId.startsWith("api-action:")) {
    identifierLabel = "Action ID";
    identifierValue = kick.runId.slice("api-action:".length) || kick.runId;
  } else if (kick.runId && kick.runId !== "api-prepare") {
    identifierLabel = "Run ID";
    identifierValue = kick.runId;
  }

  const bpsToPercent = (bps) => {
    if (bps == null) return null;
    return `${(Number(bps) / 100).toFixed(1)}%`;
  };

  return (
    <div className="kick-detail-grid">
      <div className="kick-detail-item">
        <div className="kick-detail-label">Action</div>
        <div className="kick-detail-value">
          {kick.operationType === "settle"
            ? "Settle"
            : kick.operationType === "sweep_and_settle"
              ? "Sweep + Settle"
              : "Kick"}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Timestamp</div>
        <div
          className="kick-detail-value clickable"
          title={showRelativeTimestamp ? formatTimestamp(kick.createdAt) : kick.createdAt}
          onClick={() => setShowRelativeTimestamp(v => !v)}
          style={{ cursor: "pointer" }}
        >
          {kick.createdAt
            ? showRelativeTimestamp
              ? formatRelativeTimestamp(kick.createdAt, Date.now())
              : formatTimestamp(kick.createdAt)
            : "—"}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Source</div>
        <div className="kick-detail-value">
          {kick.sourceName ? <div className="row-primary">{kick.sourceName}</div> : null}
          <AddressCopy address={kick.sourceAddress} />
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Tokens</div>
        <div className="kick-detail-value kick-detail-tokens">
          <span>
            <span className="kick-detail-token-direction">Sell</span>
            <span className="address-copy" title={kick.tokenAddress}>
              <span className="mono address-value">{kick.tokenSymbol || shortenAddress(kick.tokenAddress)}</span>
              <CopyIconButton
                valueToCopy={kick.tokenAddress}
                title={`Copy address ${kick.tokenAddress}`}
                ariaLabel={`Copy address ${kick.tokenAddress}`}
              />
            </span>
          </span>
          <span>
            <span className="kick-detail-token-direction">
              {kick.operationType === "kick" ? "Buy" : "Auction want"}
            </span>
            {kick.wantAddress ? (
              <span className="address-copy" title={kick.wantAddress}>
                <span className="mono address-value">{kick.wantSymbol || shortenAddress(kick.wantAddress)}</span>
                <CopyIconButton
                  valueToCopy={kick.wantAddress}
                  title={`Copy address ${kick.wantAddress}`}
                  ariaLabel={`Copy address ${kick.wantAddress}`}
                />
              </span>
            ) : "—"}
          </span>
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Normalized Balance</div>
        <div className="kick-detail-value">
          {kick.normalizedBalance ? `${formatBalance(kick.normalizedBalance)} ${kick.tokenSymbol || ""}` : "—"}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Start Quote</div>
        <div className="kick-detail-value">
          {kick.startingPrice || "—"}
          {kick.startPriceBufferBps != null ? ` (+${bpsToPercent(kick.startPriceBufferBps)} buffer)` : ""}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Min Quote</div>
        <div className="kick-detail-value">
          {kick.minimumQuote || "—"}
          {kick.minPriceBufferBps != null ? ` (-${bpsToPercent(kick.minPriceBufferBps)} buffer)` : ""}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Min Price (scaled)</div>
        <div className="kick-detail-value">{kick.minimumPrice || "—"}</div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Quote Amount</div>
        <div className="kick-detail-value">{kick.quoteAmount || "—"}</div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Step Decay</div>
        <div className="kick-detail-value">
          {kick.stepDecayRateBps != null ? `${(Number(kick.stepDecayRateBps) / 100).toFixed(2)}%` : "—"}
        </div>
      </div>
      <div className="kick-detail-item">
        <div className="kick-detail-label">Pre-Kick Settle</div>
        <div className="kick-detail-value">
          {kick.settleToken
            ? (kick.settleToken === kick.tokenAddress
              ? (kick.tokenSymbol || shortenAddress(kick.settleToken))
              : shortenAddress(kick.settleToken))
            : "—"}
        </div>
      </div>
      {kick.stuckAbortReason ? (
        <div className="kick-detail-item">
          <div className="kick-detail-label">Abort Reason</div>
          <div className="kick-detail-value">{kick.stuckAbortReason}</div>
        </div>
      ) : null}
      {quoteProviders ? (
        <div className="kick-detail-item">
          <div className="kick-detail-label">Quote Providers</div>
          <div className="kick-detail-value">
            {quoteProviders.map((p) => (
              <div key={p.name}>
                {p.name}: {formatProviderAmount(p.amountOut, tokenOutDecimals, p.status)}
              </div>
            ))}
          </div>
        </div>
      ) : null}
      {quoteSummary ? (
        <div className="kick-detail-item">
          <div className="kick-detail-label">Quote Summary</div>
          <div className="kick-detail-value">
            {quoteSummary.requested_providers != null ? (
              <div>Providers: {quoteSummary.successful_providers ?? 0}/{quoteSummary.requested_providers}</div>
            ) : null}
            {quoteSummary.high_amount_out != null ? (
              <div>High: {formatProviderAmount(quoteSummary.high_amount_out, tokenOutDecimals)}</div>
            ) : null}
            {quoteSummary.low_amount_out != null ? (
              <div>Low: {formatProviderAmount(quoteSummary.low_amount_out, tokenOutDecimals)}</div>
            ) : null}
            {quoteSummary.median_amount_out != null ? (
              <div>Median: {formatProviderAmount(quoteSummary.median_amount_out, tokenOutDecimals)}</div>
            ) : null}
          </div>
        </div>
      ) : null}
      {kick.errorMessage ? (
        <div className="kick-detail-item">
          <div className="kick-detail-label">Error</div>
          <div className="kick-detail-value error-text">{kick.errorMessage}</div>
        </div>
      ) : null}
      {identifierLabel ? (
        <div className="kick-detail-item">
          <div className="kick-detail-label">{identifierLabel}</div>
          <div className="kick-detail-value">{identifierValue}</div>
        </div>
      ) : null}
      {(kick.auctionAddress || quoteRequestUrl) ? (
        <div className="kick-detail-item" style={{ gridColumn: "1 / -1" }}>
          <div className="kick-detail-value">
            {kick.auctionScanRoundUrl ? (
              <div>
                <AuctionScanTextLink kick={kick} onOpen={onOpenAuctionScan} />
                {kick.auctionScanResolving ? (
                  <span className="kick-link-status"> checking for round…</span>
                ) : null}
              </div>
            ) : kick.auctionScanAuctionUrl ? (
              <div>
                <AuctionScanTextLink kick={kick} onOpen={onOpenAuctionScan} />
                {kick.auctionScanResolving ? (
                  <span className="kick-link-status"> checking for round…</span>
                ) : null}
              </div>
            ) : null}
            {kick.auctionAddress ? (
              <div>
                <a
                  href={`${COW_EXPLORER_URL}${kick.auctionAddress}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="kick-external-link"
                >
                  view on 🐮 explorer
                </a>
              </div>
            ) : null}
            {quoteRequestUrl ? (
              <div>
                <a
                  href={quoteRequestUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="kick-external-link"
                >
                  view new quote via 🌊 api
                </a>
              </div>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}

function KickDetailPanel({ kick, onOpenAuctionScan }) {
  return (
    <tr className="kick-detail">
      <td colSpan={8}>
        <KickDetailContent kick={kick} onOpenAuctionScan={onOpenAuctionScan} />
      </td>
    </tr>
  );
}

function KickDetailModal({ kick, onClose, onOpenAuctionScan }) {
  const sheetRef = useRef(null);
  const bodyRef = useRef(null);
  const backdropRef = useRef(null);
  const dragRef = useRef({ startY: 0, startTime: 0, dy: 0, dragging: false, dismissed: false });

  useEffect(() => {
    const onKeyDown = (e) => { if (e.key === "Escape") onClose(); };
    window.addEventListener("keydown", onKeyDown);
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = "";
    };
  }, [onClose]);

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
        onMouseDown={(e) => e.stopPropagation()}
        onTouchStart={onTouchStart}
        onTouchMove={onTouchMove}
        onTouchEnd={onTouchEnd}
      >
        <div className="kick-modal-handle" />
        <div ref={bodyRef} className="kick-modal-body">
          <KickDetailContent kick={kick} onOpenAuctionScan={onOpenAuctionScan} />
        </div>
      </div>
    </div>,
    document.body
  );
}

function KickLogRow({ kick, nowMs, isExpanded, onToggle, rowRef, isMobile, onOpenAuctionScan }) {
  const sourceLabel = truncateMiddle(kick.sourceName || kick.sourceAddress, 18);

  return (
    <>
      <tr ref={rowRef} className={`kick-log-row ${isExpanded ? "is-expanded" : ""}`} onClick={onToggle}>
        <td className="mono muted kick-time-cell" title={kick.createdAt} data-label="Time">
          {formatRelativeTimestamp(kick.createdAt, nowMs)}
        </td>
        <td data-label="Status">
          <StatusBadge status={kick.status} operationType={kick.operationType} />
        </td>
        <td className="mono" data-label="Pair">
          {formatKickPairLabel(kick)}
        </td>
        <td className="mono align-right" data-label="USD Value">
          {kick.operationType === "settle" ? "N/A" : kick.usdValue ? `$${formatBalance(kick.usdValue)}` : "—"}
        </td>
        <td data-label="Auction">
          {kick.auctionAddress ? (
            <span className="address-copy" title={kick.auctionAddress}>
              <a
                className="mono address-value"
                href={`${ETHERSCAN_ADDRESS_URL}${kick.auctionAddress}`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
              >
                {shortenAddress(kick.auctionAddress)}
              </a>
              <CopyIconButton
                valueToCopy={kick.auctionAddress}
                title={`Copy ${kick.auctionAddress}`}
                ariaLabel={`Copy auction address ${kick.auctionAddress}`}
              />
            </span>
          ) : "—"}
        </td>
        <td data-label="Source">
          {kick.sourceAddress ? (
            <span className="address-copy" title={kick.sourceName || kick.sourceAddress}>
              <a
                className="mono address-value"
                href={`${ETHERSCAN_ADDRESS_URL}${kick.sourceAddress}`}
                target="_blank"
                rel="noopener noreferrer"
                onClick={(e) => e.stopPropagation()}
              >
                {sourceLabel}
              </a>
              <CopyIconButton
                valueToCopy={kick.sourceAddress}
                title={`Copy ${kick.sourceAddress}`}
                ariaLabel={`Copy source address ${kick.sourceAddress}`}
              />
            </span>
          ) : "—"}
        </td>
        <td className="kick-auctionscan-cell" data-label="AuctionScan">
          <AuctionScanIconLink kick={kick} onOpen={onOpenAuctionScan} />
        </td>
        <td data-label="Tx">
          {kick.txHash ? (
            <span onClick={(e) => e.stopPropagation()}>
              <EtherscanTxLink txHash={kick.txHash} />
            </span>
          ) : "—"}
        </td>
      </tr>
      {isExpanded && !isMobile ? <KickDetailPanel kick={kick} onOpenAuctionScan={onOpenAuctionScan} /> : null}
      {isExpanded && isMobile ? (
        <KickDetailModal kick={kick} onClose={onToggle} onOpenAuctionScan={onOpenAuctionScan} />
      ) : null}
    </>
  );
}

function KickLogSkeletonRows() {
  return [...Array(10)].map((_, index) => (
    <tr key={`kick-skeleton-${index}`} className="kick-log-skeleton">
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
      <td><span className="skeleton" /></td>
    </tr>
  ));
}

function KickLogPager({
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
    <div className="kick-log-pagination" aria-live="polite">
      <div className="kick-log-pagination-meta">
        {total === 0 ? "Showing 0 results" : `Showing ${rangeStart.toLocaleString()}-${rangeEnd.toLocaleString()} of ${total.toLocaleString()}`}
      </div>
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

function KickLogPage({
  nowMs,
  initialRunId,
  initialKickId,
  initialOffset = 0,
  initialStatus = "all",
  initialSearch = "",
}) {
  const [kicks, setKicks] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState(initialStatus);
  const [searchTerm, setSearchTerm] = useState(initialSearch);
  const [debouncedSearchTerm, setDebouncedSearchTerm] = useState(initialSearch);
  const [offset, setOffset] = useState(initialOffset);
  const [hasMore, setHasMore] = useState(false);
  const [expandedRows, setExpandedRows] = useState(() => new Set());
  const [focusedKickId, setFocusedKickId] = useState(initialKickId);
  const [focusedRunId, setFocusedRunId] = useState(initialRunId);
  const highlightedRowRef = useRef(null);
  const kicksRef = useRef([]);
  const auctionScanRequestsRef = useRef(new Map());
  const pageCacheRef = useRef(new Map());
  const filterResetRef = useRef(true);
  const isMobile = useMediaQuery("(max-width: 600px)");
  const focusedView = Boolean(focusedKickId || focusedRunId);

  useEffect(() => {
    kicksRef.current = kicks;
  }, [kicks]);

  useEffect(() => {
    const timerId = window.setTimeout(() => {
      setDebouncedSearchTerm(searchTerm);
    }, 250);
    return () => window.clearTimeout(timerId);
  }, [searchTerm]);

  useEffect(() => {
    if (filterResetRef.current) {
      filterResetRef.current = false;
      return;
    }
    setOffset(0);
    setExpandedRows(new Set());
  }, [statusFilter, debouncedSearchTerm]);

  useEffect(() => {
    let isMounted = true;
    const controller = new AbortController();

    const normalizedQuery = debouncedSearchTerm.trim();
    const requestKey = JSON.stringify({
      kickId: focusedKickId || null,
      runId: focusedRunId || null,
      offset,
      status: statusFilter,
      q: normalizedQuery,
    });

    function applyPayload(data) {
      setKicks(Array.isArray(data.kicks) ? data.kicks.map(normalizeKick) : []);
      setTotal(data.total || 0);
      setHasMore(Boolean(data.hasMore));
    }

    async function prefetchNextPage(data) {
      if (focusedView || !data?.hasMore) {
        return;
      }
      const nextOffset = offset + KICK_LOG_PAGE_SIZE;
      const nextKey = JSON.stringify({
        kickId: null,
        runId: null,
        offset: nextOffset,
        status: statusFilter,
        q: normalizedQuery,
      });
      if (pageCacheRef.current.has(nextKey)) {
        return;
      }
      const params = new URLSearchParams({
        limit: String(KICK_LOG_PAGE_SIZE),
        offset: String(nextOffset),
      });
      if (statusFilter !== "all") {
        params.set("status", statusFilter);
      }
      if (normalizedQuery) {
        params.set("q", normalizedQuery);
      }
      try {
        const response = await apiFetch(`/logs/kicks?${params.toString()}`);
        if (!response.ok) {
          return;
        }
        const payload = await response.json();
        pageCacheRef.current.set(nextKey, payload?.data || {});
      } catch {
        // Ignore background prefetch failures.
      }
    }

    async function loadKicks() {
      const cached = pageCacheRef.current.get(requestKey);
      if (cached) {
        setError("");
        applyPayload(cached);
        setLoading(false);
        prefetchNextPage(cached);
        return;
      }

      setLoading(true);
      setError("");
      try {
        const params = new URLSearchParams({
          limit: String(KICK_LOG_PAGE_SIZE),
          offset: String(offset),
        });
        if (statusFilter !== "all") {
          params.set("status", statusFilter);
        }
        if (normalizedQuery) {
          params.set("q", normalizedQuery);
        }
        if (focusedKickId) {
          params.set("kick_id", String(focusedKickId));
        } else if (focusedRunId) {
          params.set("run_id", focusedRunId);
        }

        const response = await apiFetch(`/logs/kicks?${params.toString()}`, {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Unable to load kicks");
        const payload = await response.json();
        const data = payload?.data || {};
        if (!isMounted) return;
        pageCacheRef.current.set(requestKey, data);
        applyPayload(data);
        prefetchNextPage(data);
      } catch (err) {
        if (isMounted && err.name !== "AbortError") {
          setError(err.message || "Unable to load kicks");
        }
      } finally {
        if (isMounted) setLoading(false);
      }
    }

    loadKicks();
    return () => {
      isMounted = false;
      controller.abort();
    };
  }, [debouncedSearchTerm, focusedKickId, focusedRunId, offset, statusFilter, focusedView]);

  useEffect(() => {
    if (loading || (!focusedKickId && !focusedRunId) || !kicks.length) return;
    const match = focusedKickId
      ? kicks.find((k) => String(k.id) === String(focusedKickId))
      : kicks.find((k) => k.runId === focusedRunId);
    if (match) {
      setExpandedRows(new Set([match.id]));
      if (!isMobile) {
        requestAnimationFrame(() => {
          highlightedRowRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
        });
      }
    }
  }, [loading, focusedKickId, focusedRunId, kicks, isMobile]);

  function getAuctionScanUrls(kick) {
    if (!kick) {
      return { roundUrl: null, auctionUrl: null };
    }
    return {
      roundUrl: kick.auctionScanRoundUrl || null,
      auctionUrl: kick.auctionScanAuctionUrl || null,
    };
  }

  async function ensureAuctionScanLink(kickId) {
    const current = kicksRef.current.find((kick) => kick.id === kickId);
    if (!current) {
      return null;
    }
    if (
      current.auctionScanRoundId != null
      || !current.auctionAddress
      || current.operationType !== "kick"
      || current.status !== "CONFIRMED"
      || !current.txHash
    ) {
      return getAuctionScanUrls(current);
    }

    const inFlight = auctionScanRequestsRef.current.get(kickId);
    if (inFlight) {
      return inFlight;
    }

    setKicks((prev) => prev.map((kick) => (
      kick.id === kickId
        ? { ...kick, auctionScanResolving: true, auctionScanResolveError: "" }
        : kick
    )));

    const request = (async () => {
      try {
        const response = await apiFetch(`/kicks/${kickId}/auctionscan`);
        if (!response.ok) {
          throw new Error("Unable to resolve AuctionScan link");
        }
        const payload = await response.json();
        const data = payload?.data || {};
        const nextKick = normalizeKick({
          ...current,
          chainId: data.chainId ?? current.chainId,
          auctionScanEligible: data.eligible,
          auctionScanResolved: data.resolved,
          auctionScanRoundId: data.roundId,
          auctionScanAuctionUrl: data.auctionUrl,
          auctionScanRoundUrl: data.roundUrl,
          auctionScanLastCheckedAt: data.lastCheckedAt,
          auctionScanMatchedAt: data.matchedAt,
          auctionScanResolving: false,
          auctionScanResolveError: "",
        });
        setKicks((prev) => prev.map((kick) => (kick.id === kickId ? nextKick : kick)));
        return getAuctionScanUrls(nextKick);
      } catch (error) {
        setKicks((prev) => prev.map((kick) => (
          kick.id === kickId
            ? {
                ...kick,
                auctionScanResolving: false,
                auctionScanResolveError: error?.message || "Unable to resolve AuctionScan link",
              }
            : kick
        )));
        return getAuctionScanUrls(current);
      } finally {
        auctionScanRequestsRef.current.delete(kickId);
      }
    })();

    auctionScanRequestsRef.current.set(kickId, request);
    return request;
  }

  async function openAuctionScanLink(kick) {
    const current = kicksRef.current.find((item) => item.id === kick.id) || kick;
    if (current.auctionScanRoundUrl) {
      window.open(current.auctionScanRoundUrl, "_blank", "noopener,noreferrer");
      return;
    }

    const popup = window.open("about:blank", "_blank");
    if (popup) {
      try {
        popup.opener = null;
      } catch (error) {
        // Ignore browser-specific opener restrictions.
      }
    }

    const resolvedUrls = await ensureAuctionScanLink(current.id);
    const href = resolvedUrls?.roundUrl || resolvedUrls?.auctionUrl || current.auctionScanAuctionUrl || null;

    if (!href) {
      popup?.close();
      return;
    }

    if (popup) {
      popup.location.replace(href);
      return;
    }

    window.open(href, "_blank", "noopener,noreferrer");
  }

  function buildNavParams(overrides = {}) {
    const params = {
      offset: offset > 0 && !focusedView ? String(offset) : null,
      status: statusFilter !== "all" ? statusFilter : null,
      q: debouncedSearchTerm.trim() || null,
      run_id: focusedRunId || null,
      ...overrides,
    };
    return params;
  }

  function toggleRow(kick) {
    const expanding = !expandedRows.has(kick.id);
    setExpandedRows((prev) => {
      if (prev.has(kick.id)) {
        const next = new Set(prev);
        next.delete(kick.id);
        return next;
      }
      if (isMobile) {
        return new Set([kick.id]);
      }
      const next = new Set(prev);
      next.add(kick.id);
      return next;
    });
    if (expanding) {
      navigateTo("kicks", buildNavParams({ kick_id: String(kick.id) }));
    } else {
      if (focusedKickId && String(focusedKickId) === String(kick.id)) {
        setFocusedKickId(null);
      }
      navigateTo("kicks", buildNavParams({ kick_id: null }));
    }
    if (expanding) {
      ensureAuctionScanLink(kick.id);
    }
  }

  function clearFocusedView() {
    setFocusedKickId(null);
    setFocusedRunId(null);
    setExpandedRows(new Set());
    navigateTo("kicks", buildNavParams({ kick_id: null, run_id: null }));
  }

  return (
    <>
      <section className="kick-log-controls">
        <label className="control control-search">
          <span>Search</span>
          <input
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            disabled={focusedView}
            placeholder="token symbol, auction address, tx hash"
          />
        </label>
        <label className="control control-status">
          <span>Status</span>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)} disabled={focusedView}>
            <option value="all">All</option>
            <option value="confirmed">Confirmed</option>
            <option value="failed">Failed</option>
          </select>
        </label>
      </section>

      {focusedView ? (
        <div className="kick-log-focusbar">
          <div className="toolbar-meta">
            {focusedKickId ? `Showing selected kick ${focusedKickId}` : `Showing run ${focusedRunId}`}
          </div>
          <button type="button" className="kick-log-page-btn" onClick={clearFocusedView}>
            Show all logs
          </button>
        </div>
      ) : (
        <KickLogPager
          offset={offset}
          pageSize={KICK_LOG_PAGE_SIZE}
          total={total}
          loading={loading}
          hasMore={hasMore}
          onPrev={() => setOffset((current) => Math.max(0, current - KICK_LOG_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + KICK_LOG_PAGE_SIZE)}
        />
      )}

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell">
        <table className="kick-log-table">
          <thead>
            <tr>
              <th className="kick-time-col">Time</th>
              <th>Status</th>
              <th>Pair</th>
              <th className="align-right">USD Value</th>
              <th>Auction</th>
              <th>Source</th>
              <th className="kick-auctionscan-col" title="AuctionScan" aria-label="AuctionScan" />
              <th>Tx</th>
            </tr>
          </thead>
          <tbody>
            {loading ? <KickLogSkeletonRows /> : null}
            {!loading && !kicks.length ? (
              <tr>
                <td colSpan={8} className="kick-log-empty">No activity found</td>
              </tr>
            ) : null}
            {!loading
              ? kicks.map((kick) => (
                  <KickLogRow
                    key={kick.id}
                    kick={kick}
                    nowMs={nowMs}
                    isExpanded={expandedRows.has(kick.id)}
                    onToggle={() => toggleRow(kick)}
                    onOpenAuctionScan={openAuctionScanLink}
                    rowRef={
                      (focusedKickId != null && String(kick.id) === String(focusedKickId))
                      || (focusedKickId == null && focusedRunId != null && kick.runId === focusedRunId)
                        ? highlightedRowRef
                        : undefined
                    }
                    isMobile={isMobile}
                  />
                ))
              : null}
          </tbody>
        </table>
      </div>

      {!focusedView && !loading ? (
        <KickLogPager
          offset={offset}
          pageSize={KICK_LOG_PAGE_SIZE}
          total={total}
          loading={loading}
          hasMore={hasMore}
          onPrev={() => setOffset((current) => Math.max(0, current - KICK_LOG_PAGE_SIZE))}
          onNext={() => setOffset((current) => current + KICK_LOG_PAGE_SIZE)}
        />
      ) : null}
    </>
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
          const balanceTitle = displayMode === "usd"
            ? "Click to show token amounts"
            : "Click to show USD values";
          const title = auctionTooltip ? `${auctionTooltip}\n${balanceTitle}` : balanceTitle;
          const itemClassName = balance.auctionSellTokenStatus === "disabled"
            ? "token-item is-auction-disabled"
            : "token-item";

          return (
            <div
              key={`${balance.tokenAddress}-${balance.tokenSymbol}`}
              className={itemClassName}
              title={auctionTooltip || undefined}
            >
              {balance.tokenLogoUrl ? (
                <img
                  src={balance.tokenLogoUrl}
                  alt={`${balance.tokenSymbol} logo`}
                  className="token-logo"
                  loading="lazy"
                  decoding="async"
                  referrerPolicy="no-referrer"
                  onError={(event) => {
                    event.currentTarget.style.visibility = "hidden";
                  }}
                />
              ) : <span className="token-logo-placeholder" />}
              <span className="token-symbol-wrap">
                <span className="mono token-symbol" title={auctionTooltip || undefined}>
                  {balance.tokenSymbol || "UNKNOWN"}
                </span>
                <CopyIconButton
                  valueToCopy={balance.tokenAddress}
                  title={`Copy token address ${balance.tokenAddress}`}
                  ariaLabel={`Copy token address for ${balance.tokenSymbol || "token"}`}
                />
              </span>
              <button
                type="button"
                className="mono token-balance token-balance-button"
                onClick={onToggleMode}
                title={title}
              >
                {displayMode === "usd"
                  ? (balance.usdValue ? `$${formatBalance(balance.usdValue)}` : "?")
                  : formatBalance(balance.normalizedBalance)}
              </button>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function FeeBurnerPage({
  rows,
  loading,
  error,
  nowMs,
  displayMode,
  onToggleMode,
  expandedKickRows,
  onToggleExpand,
}) {
  const latestScanAt = rows.reduce((latest, row) => {
    if (!row.scannedAt) {
      return latest;
    }
    if (!latest || row.scannedAt > latest) {
      return row.scannedAt;
    }
    return latest;
  }, null);

  return (
    <>
      <div className="toolbar-meta">
        <span>Showing {rows.length.toLocaleString()} fee burner{rows.length === 1 ? "" : "s"}</span>
        <span className="meta-sep" aria-hidden="true">&middot;</span>
        <span>Scanned {formatTimestamp(latestScanAt)}</span>
      </div>

      {error ? <p className="error">{error}</p> : null}

      {loading ? (
        <div className="fee-burner-empty">Loading fee burner rows...</div>
      ) : !rows.length ? (
        <div className="fee-burner-empty">No fee burner rows are available.</div>
      ) : (
        <section className="fee-burner-grid">
          {rows.map((row) => (
            <article key={row.sourceAddress} className="fee-burner-card">
              <div className="fee-burner-meta-row">
                <div className="fee-burner-top-item">
                  <div className="fee-burner-label">Last Scan</div>
                  <div className="fee-burner-value mono">
                    {row.scannedAt ? formatRelativeTimestamp(row.scannedAt, nowMs) : "—"}
                  </div>
                </div>
                <div className="fee-burner-top-item fee-burner-source">
                  <div className="fee-burner-label">Fee Burner</div>
                  <EntityIdentity
                    primary={row.sourceName || "Unnamed Fee Burner"}
                    address={row.sourceAddress}
                  />
                </div>
                <div className="fee-burner-top-item">
                  <div className="fee-burner-label">Want</div>
                  <div className="fee-burner-value">
                    {row.wantAddress ? (
                      <span className="address-copy" title={row.wantAddress}>
                        <span className="mono address-value">
                          {row.wantSymbol || shortenAddress(row.wantAddress)}
                        </span>
                        <CopyIconButton
                          valueToCopy={row.wantAddress}
                          title={`Copy address ${row.wantAddress}`}
                          ariaLabel={`Copy address ${row.wantAddress}`}
                        />
                      </span>
                    ) : "—"}
                  </div>
                </div>
                <div className="fee-burner-top-item fee-burner-auction">
                  <div className="fee-burner-label">Auction</div>
                  <AuctionAddressCell
                    address={row.auctionAddress}
                    version={row.auctionVersion}
                    kicks={row.kicks}
                    nowMs={nowMs}
                    isExpanded={expandedKickRows.has(row.sourceAddress)}
                    onToggleExpand={() => onToggleExpand(row.sourceAddress)}
                  />
                </div>
              </div>
              <div className="fee-burner-balance-panel">
                <div className="fee-burner-balance-header">
                  <div className="fee-burner-label">Token Balances</div>
                  {row.balances.length ? (
                    <div className="fee-burner-balance-total">
                      <span className="fee-burner-balance-total-label">Total</span>
                      <span className="mono fee-burner-balance-total-value">
                        {row.totalUsdValue ? `$${formatBalance(row.totalUsdValue)}` : "?"}
                      </span>
                    </div>
                  ) : null}
                </div>
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
            </article>
          ))}
        </section>
      )}
    </>
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
  const [auctionFilter, setAuctionFilter] = useState("all");
  const [isAuctionFilterMenuOpen, setIsAuctionFilterMenuOpen] = useState(false);
  const [balanceSortDirection, setBalanceSortDirection] = useState("desc");
  const [themePreference, setThemePreference] = useState(getStoredThemePreference);
  const [systemTheme, setSystemTheme] = useState(resolveSystemTheme);
  const [showZeroBalance, setShowZeroBalance] = useState(false);
  const [showClosedVaults, setShowClosedVaults] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loadingRows, setLoadingRows] = useState(true);
  const [error, setError] = useState("");
  const [displayMode, setDisplayMode] = useState("usd");
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [expandedKickRows, setExpandedKickRows] = useState(() => new Set());
  const [deployStates, setDeployStates] = useState({});
  const [deployConfirm, setDeployConfirm] = useState(null);
  const auctionFilterMenuRef = useRef(null);

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
    if (!isAuctionFilterMenuOpen) {
      return undefined;
    }

    const onMouseDown = (event) => {
      if (auctionFilterMenuRef.current && !auctionFilterMenuRef.current.contains(event.target)) {
        setIsAuctionFilterMenuOpen(false);
      }
    };
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        setIsAuctionFilterMenuOpen(false);
      }
    };

    window.addEventListener("mousedown", onMouseDown);
    window.addEventListener("keydown", onKeyDown);

    return () => {
      window.removeEventListener("mousedown", onMouseDown);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [isAuctionFilterMenuOpen]);

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

  useEffect(() => {
    let isMounted = true;
    const controller = new AbortController();

    async function loadDashboard() {
      setLoadingRows(true);
      setError("");

      try {
        const response = await apiFetch("/dashboard", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error("Unable to load dashboard");
        }

        const payload = await response.json();
        const data = payload?.data || {};

        if (!isMounted) {
          return;
        }

        const summaryPayload = data.summary
          ? {
              ...data.summary,
              latestScanAt: data.latestScanAt || data.summary.latestScanAt || null,
            }
          : {
              strategyCount: Array.isArray(data.rows) ? data.rows.length : 0,
              tokenCount: Array.isArray(data.tokens) ? data.tokens.length : 0,
              latestScanAt: data.latestScanAt || null,
            };

        setSummary(summaryPayload);
        setRows(data.rows || []);
      } catch (loadError) {
        if (isMounted && loadError.name !== "AbortError") {
          setError(loadError.message || "Unable to load dashboard");
          setSummary(null);
          setRows([]);
        }
      } finally {
        if (isMounted) {
          setLoadingRows(false);
        }
      }
    }

    loadDashboard();

    return () => {
      isMounted = false;
      controller.abort();
    };
  }, []);

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

  useEffect(() => {
    if (selectedToken === ALL_TOKENS || !tokenOptions.length) {
      return;
    }

    const exists = tokenOptions.some(
      (option) => option.tokenAddress.toLowerCase() === selectedToken.toLowerCase(),
    );
    if (!exists) {
      setSelectedToken(ALL_TOKENS);
    }
  }, [selectedToken, tokenOptions]);

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

      const auctionMatch =
        auctionFilter === "all"
          ? true
          : auctionFilter === "null"
            ? !row.auctionAddress
            : Boolean(row.auctionAddress);
      if (!auctionMatch) {
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
  }, [visibleStrategyRows, searchTerm, selectedToken, auctionFilter, balanceSortDirection]);


  const latestVisibleScan = useMemo(() => {
    if (!filteredStrategyRows.length) {
      return summary?.latestScanAt || null;
    }

    return filteredStrategyRows.reduce((latest, row) => {
      if (!latest) {
        return row.scannedAt;
      }
      return row.scannedAt > latest ? row.scannedAt : latest;
    }, null);
  }, [filteredStrategyRows, summary]);

  function toggleDisplayMode() {
    setDisplayMode((prev) => (prev === "token" ? "usd" : "token"));
  }

  function toggleBalanceSortDirection() {
    setBalanceSortDirection((prev) => (prev === "desc" ? "asc" : "desc"));
  }

  function toggleAuctionFilterMenu() {
    setIsAuctionFilterMenuOpen((prev) => !prev);
  }

  function selectAuctionFilter(next) {
    setAuctionFilter(next);
    setIsAuctionFilterMenuOpen(false);
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

      setDeployConfirm({ sourceAddress, payload: deployDefaults, provider });
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

  async function handleDeployConfirm() {
    if (!deployConfirm) return;
    const { sourceAddress, payload: deployDefaults, provider } = deployConfirm;
    setDeployConfirm(null);

    updateDeployState(sourceAddress, { status: "wallet", error: "" });

    try {
      const accounts = await provider.request({ method: "eth_requestAccounts" });
      const account = Array.isArray(accounts) ? accounts[0] : null;
      if (!account) {
        throw new Error("No wallet account connected");
      }

      const prepareResponse = await apiFetch("/auctions/deploy/prepare", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
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

      let preparedPayload = null;
      try {
        preparedPayload = await prepareResponse.json();
      } catch {
        preparedPayload = null;
      }

      if (!prepareResponse.ok) {
        throw new Error(preparedPayload?.detail || "Unable to prepare deploy transaction");
      }

      const preparedAction = preparedPayload?.data;
      const txRequest = preparedAction?.transactions?.[0];
      const actionId = preparedAction?.actionId;
      if (!txRequest?.to || !txRequest?.data || !actionId) {
        throw new Error("Deploy transaction payload is incomplete");
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

      const txHash = await provider.request({
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

      await apiFetch(`/actions/${actionId}/broadcast`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          txIndex: 0,
          sender: account,
          txHash,
          broadcastAt: new Date().toISOString(),
        }),
      });

      const receipt = await waitForTransactionReceipt(provider, txHash);
      if (receipt) {
        await apiFetch(`/actions/${actionId}/receipt`, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
          },
          body: JSON.stringify({
            txIndex: 0,
            receiptStatus: receipt.status === "0x1" ? "CONFIRMED" : "REVERTED",
            blockNumber: hexToNumber(receipt.blockNumber),
            gasUsed: hexToNumber(receipt.gasUsed),
            gasPriceGwei: null,
            observedAt: new Date().toISOString(),
          }),
        });
      }

      updateDeployState(sourceAddress, { status: "submitted", error: "", txHash });
    } catch (deployError) {
      updateDeployState(sourceAddress, {
        status: "idle",
        error: formatDeployError(deployError),
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
          <TabBar activePage={activePage} onChangePage={handlePageChange} />
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

      {activePage === "strategies" ? (
      <>
      <section className="toolbar">
        <div className="toolbar-controls">
          <label className="control control-search">
            <input
              value={searchTerm}
              onChange={(event) => setSearchTerm(event.target.value)}
              placeholder="Search strategies, vaults, tokens, addresses..."
            />
          </label>

          <label className="control control-token">
            <select
              value={selectedToken}
              onChange={(event) => setSelectedToken(event.target.value)}
            >
              <option value={ALL_TOKENS}>All tokens</option>
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
            <span>Show strats with no rewards</span>
          </label>

          <label className="toggle-filter">
            <input
              type="checkbox"
              checked={showClosedVaults}
              onChange={(e) => setShowClosedVaults(e.target.checked)}
            />
            <span>Show retired</span>
          </label>
        </div>

      </section>

      <div className="toolbar-meta">
        <span>Showing {filteredStrategyRows.length.toLocaleString()} results</span>
        <span className="meta-sep" aria-hidden="true">&middot;</span>
        <span>Scanned {formatTimestamp(latestVisibleScan)}</span>
      </div>

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell">
        <table className="strategies-table">
          <thead>
            <tr>
              <th className="last-scan-col">Last Scan</th>
              <th>Vault</th>
              <th>Strategy</th>
              <th className="auction-col">Auction</th>
              <th className="token-col">
                <button
                  type="button"
                  className="th-sort-button"
                  onClick={toggleBalanceSortDirection}
                  title={`Sort by total token USD (${balanceSortDirection === "desc" ? "descending" : "ascending"})`}
                >
                  Token Balances
                  <span className="sort-indicator" aria-hidden="true">
                    {balanceSortDirection === "desc" ? "↓" : "↑"}
                  </span>
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {loadingRows ? <SkeletonRows /> : null}
            {!loadingRows && !filteredStrategyRows.length ? (
              <tr>
                <td colSpan={5} className="empty">No strategies match the current filters.</td>
              </tr>
            ) : null}
            {!loadingRows
              ? filteredStrategyRows.map((row) => (
                  <tr key={row.sourceAddress}>
                    <td className="mono muted last-scan-cell" title={formatTimestamp(row.scannedAt)} data-label="Last Scan">
                      {formatRelativeTimestamp(row.scannedAt, nowMs)}
                    </td>
                    <td data-label="Vault">
                      <EntityIdentity
                        primary={row.contextSymbol || row.contextName || "Unknown Vault"}
                        secondary={row.contextName && row.contextSymbol !== row.contextName ? row.contextName : null}
                        address={row.contextAddress}
                      />
                    </td>
                    <td data-label="Strategy">
                      <EntityIdentity
                        primary={formatStrategyDisplayName(row.sourceName)}
                        address={row.sourceAddress}
                      />
                    </td>
                    <td className={`auction-cell${row.auctionAddress ? "" : " auction-cell-empty"}`} data-label="Auction">
                      <AuctionAddressCell
                        address={row.auctionAddress}
                        version={row.auctionVersion}
                        kicks={row.kicks}
                        nowMs={nowMs}
                        isExpanded={expandedKickRows.has(row.sourceAddress)}
                        onToggleExpand={() => toggleKickExpand(row.sourceAddress)}
                        emptyContent={
                          <MissingAuctionAction
                            deployState={deployStates[row.sourceAddress]}
                            onDeploy={() => handleDeployStrategy(row)}
                          />
                        }
                      />
                    </td>
                    <td data-label="Balances">
                      <TokenBalances
                        balances={row.balances}
                        displayMode={displayMode}
                        onToggleMode={toggleDisplayMode}
                      />
                    </td>
                  </tr>
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
          loading={loadingRows}
          error={error}
          nowMs={nowMs}
          displayMode={displayMode}
          onToggleMode={toggleDisplayMode}
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
