import { useEffect, useMemo, useRef, useState } from "react";
import Big from "big.js";

const ALL_TOKENS = "__all__";
const MIN_USD_VISIBLE = new Big("0.01");
const THEME_SEQUENCE = ["light", "dark"];
const API_BASE_URL = (import.meta.env.VITE_FACTORY_DASHBOARD_API_BASE_URL || "/api").replace(/\/$/, "");
const ETHERSCAN_TX_URL = "https://etherscan.io/tx/";
const ETHERSCAN_ADDRESS_URL = "https://etherscan.io/address/";
const COW_EXPLORER_URL = "https://explorer.cow.fi/address/";
const FAILED_STATUSES = new Set(["REVERTED", "ERROR", "ESTIMATE_FAILED"]);
const FAINT_STATUSES = new Set(["DRY_RUN", "SUBMITTED", "USER_SKIPPED", "SKIP"]);

function apiUrl(path) {
  return `${API_BASE_URL}${path}`;
}

function getTokenFromUrl() {
  const params = new URLSearchParams(window.location.search);
  return params.get("token") || ALL_TOKENS;
}

function shortenAddress(address) {
  if (!address || address.length < 14) {
    return address || "—";
  }
  return `${address.slice(0, 8)}...${address.slice(-6)}`;
}

function formatStrategyDisplayName(name) {
  if (!name) {
    return "Unnamed Strategy";
  }

  let output = name;
  if (output.startsWith("Strategy")) {
    output = output.slice("Strategy".length);
  }
  output = output.replaceAll("Boosted", "");
  output = output.replaceAll("Factory", "");
  output = output.replace(/-{2,}/g, "-").trim();
  output = output.replace(/^-+/, "").replace(/-+$/, "");
  return output || name;
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
  const stored = window.localStorage.getItem("factory_dashboard_theme_preference");
  if (stored === "light" || stored === "dark") {
    return stored;
  }
  return null;
}

function SkeletonRows() {
  return [...Array(10)].map((_, index) => (
    <tr key={`skeleton-${index}`}>
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

function AuctionAddressCell({ address, version, kicks, nowMs, isExpanded, onToggleExpand }) {
  const hasKicks = kicks && kicks.length > 0;
  const hasChevron = kicks && kicks.length > 1;

  if (!address) {
    return (
      <span className="auction-value-slot">
        <span className="row-secondary mono">—</span>
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
    <nav className="tab-bar">
      <button
        type="button"
        className={`tab-item ${activePage === "strategies" ? "is-active" : ""}`}
        onClick={() => onChangePage("strategies")}
      >
        Strategies
      </button>
      <button
        type="button"
        className={`tab-item ${activePage === "kicks" ? "is-active" : ""}`}
        onClick={() => onChangePage("kicks")}
      >
        Kick Log
      </button>
    </nav>
  );
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

  return <span className={className}>{status}</span>;
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

function KickDetailPanel({ kick }) {
  let quoteProviders = null;
  let quoteSummary = null;
  let tokenOutDecimals = null;

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
    } catch {
      // ignore parse errors
    }
  }

  const bpsToPercent = (bps) => {
    if (bps == null) return null;
    return `${bps} bps / ${(Number(bps) / 100).toFixed(1)}%`;
  };

  return (
    <tr className="kick-detail">
      <td colSpan={7}>
        <div className="kick-detail-grid">
          <div className="kick-detail-item">
            <div className="kick-detail-label">Strategy</div>
            <div className="kick-detail-value"><AddressCopy address={kick.strategyAddress} /></div>
          </div>
          <div className="kick-detail-item">
            <div className="kick-detail-label">Token Address</div>
            <div className="kick-detail-value"><AddressCopy address={kick.tokenAddress} /></div>
          </div>
          <div className="kick-detail-item">
            <div className="kick-detail-label">Normalized Balance</div>
            <div className="kick-detail-value">
              {kick.normalizedBalance ? `${formatBalance(kick.normalizedBalance)} ${kick.tokenSymbol || ""}` : "—"}
            </div>
          </div>
          <div className="kick-detail-item">
            <div className="kick-detail-label">Start Price</div>
            <div className="kick-detail-value">
              {kick.startingPrice || "—"}
              {kick.startPriceBufferBps != null ? ` (${bpsToPercent(kick.startPriceBufferBps)})` : ""}
            </div>
          </div>
          <div className="kick-detail-item">
            <div className="kick-detail-label">Min Price</div>
            <div className="kick-detail-value">
              {kick.minimumPrice || "—"}
              {kick.minPriceBufferBps != null ? ` (${bpsToPercent(kick.minPriceBufferBps)})` : ""}
            </div>
          </div>
          <div className="kick-detail-item">
            <div className="kick-detail-label">Quote Amount</div>
            <div className="kick-detail-value">{kick.quoteAmount || "—"}</div>
          </div>
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
                {quoteSummary.high_amount_out != null && tokenOutDecimals != null ? (
                  <div>Best: {formatProviderAmount(quoteSummary.high_amount_out, tokenOutDecimals)}</div>
                ) : quoteSummary.high_amount_out != null ? (
                  <div>Best: {String(quoteSummary.high_amount_out)}</div>
                ) : null}
                {quoteSummary.successful_providers != null ? (
                  <div>Successful providers: {quoteSummary.successful_providers}</div>
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
          <div className="kick-detail-item">
            <div className="kick-detail-label">Run ID</div>
            <div className="kick-detail-value">{kick.runId || "—"}</div>
          </div>
          {kick.auctionAddress ? (
            <div className="kick-detail-item">
              <div className="kick-detail-label">CoW Protocol</div>
              <div className="kick-detail-value">
                <a
                  href={`${COW_EXPLORER_URL}${kick.auctionAddress}`}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="cow-explorer-link"
                >
                  view on 🐮 explorer
                </a>
              </div>
            </div>
          ) : null}
        </div>
      </td>
    </tr>
  );
}

function KickLogRow({ kick, nowMs, isExpanded, onToggle }) {
  return (
    <>
      <tr className={`kick-log-row ${isExpanded ? "is-expanded" : ""}`} onClick={onToggle}>
        <td className="mono muted" title={kick.createdAt}>
          {formatRelativeTimestamp(kick.createdAt, nowMs)}
        </td>
        <td className="mono">
          {kick.tokenSymbol || "?"} → {kick.wantSymbol || "?"}
        </td>
        <td className="mono align-right">
          {kick.usdValue ? `$${formatBalance(kick.usdValue)}` : "—"}
        </td>
        <td>
          <StatusBadge status={kick.status} />
        </td>
        <td>
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
        <td>
          {kick.txHash ? (
            <span onClick={(e) => e.stopPropagation()}>
              <EtherscanTxLink txHash={kick.txHash} />
            </span>
          ) : "—"}
        </td>
        <td className="mono align-right">
          {kick.gasUsed ? `${withGrouping(String(kick.gasUsed))} @ ${kick.gasPriceGwei || "?"} gwei` : "—"}
        </td>
      </tr>
      {isExpanded ? <KickDetailPanel kick={kick} /> : null}
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
    </tr>
  ));
}

function KickLogPage({ nowMs }) {
  const [kicks, setKicks] = useState([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [statusFilter, setStatusFilter] = useState("all");
  const [searchTerm, setSearchTerm] = useState("");
  const [expandedRows, setExpandedRows] = useState(() => new Set());
  const hasFetchedRef = useRef(false);

  useEffect(() => {
    if (hasFetchedRef.current) return;
    hasFetchedRef.current = true;

    let isMounted = true;
    const controller = new AbortController();

    async function loadKicks() {
      setLoading(true);
      setError("");
      try {
        const response = await fetch(apiUrl("/kicks?limit=500"), {
          signal: controller.signal,
        });
        if (!response.ok) throw new Error("Unable to load kicks");
        const payload = await response.json();
        if (!isMounted) return;
        setKicks(payload.kicks || []);
        setTotal(payload.total || 0);
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
  }, []);

  const filteredKicks = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    return kicks.filter((kick) => {
      if (statusFilter === "confirmed" && kick.status !== "CONFIRMED") return false;
      if (statusFilter === "failed" && !FAILED_STATUSES.has(kick.status)) return false;

      if (term) {
        const searchable = [
          kick.tokenSymbol,
          kick.wantSymbol,
          kick.auctionAddress,
          kick.txHash,
          kick.strategyAddress,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!searchable.includes(term)) return false;
      }

      return true;
    });
  }, [kicks, statusFilter, searchTerm]);

  function toggleRow(id) {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  return (
    <>
      <section className="kick-log-controls">
        <label className="control control-search">
          <span>Search</span>
          <input
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            placeholder="token symbol, auction address, tx hash"
          />
        </label>
        <label className="control control-status">
          <span>Status</span>
          <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
            <option value="all">All ({total})</option>
            <option value="confirmed">Confirmed</option>
            <option value="failed">Failed</option>
          </select>
        </label>
      </section>

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell">
        <table className="kick-log-table">
          <thead>
            <tr>
              <th>Time</th>
              <th>Pair</th>
              <th className="align-right">USD Value</th>
              <th>Status</th>
              <th>Auction</th>
              <th>Tx</th>
              <th className="align-right">Gas</th>
            </tr>
          </thead>
          <tbody>
            {loading ? <KickLogSkeletonRows /> : null}
            {!loading && !filteredKicks.length ? (
              <tr>
                <td colSpan={7} className="kick-log-empty">No kicks found</td>
              </tr>
            ) : null}
            {!loading
              ? filteredKicks.map((kick) => (
                  <KickLogRow
                    key={kick.id}
                    kick={kick}
                    nowMs={nowMs}
                    isExpanded={expandedRows.has(kick.id)}
                    onToggle={() => toggleRow(kick.id)}
                  />
                ))
              : null}
          </tbody>
        </table>
      </div>
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
        {balances.map((balance) => (
          <div key={`${balance.tokenAddress}-${balance.tokenSymbol}`} className="token-item">
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
              <span className="mono token-symbol">{balance.tokenSymbol || "UNKNOWN"}</span>
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
              title={displayMode === "usd" ? "Click to show token amounts" : "Click to show USD values"}
            >
              {displayMode === "usd"
                ? (balance.usdValue ? `$${formatBalance(balance.usdValue)}` : "?")
                : formatBalance(balance.normalizedBalance)}
            </button>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function App() {
  const [activePage, setActivePage] = useState("strategies");
  const [selectedToken, setSelectedToken] = useState(getTokenFromUrl);
  const [auctionFilter, setAuctionFilter] = useState("all");
  const [isAuctionFilterMenuOpen, setIsAuctionFilterMenuOpen] = useState(false);
  const [balanceSortDirection, setBalanceSortDirection] = useState("desc");
  const [themePreference, setThemePreference] = useState(getStoredThemePreference);
  const [systemTheme, setSystemTheme] = useState(resolveSystemTheme);
  const [showZeroBalance, setShowZeroBalance] = useState(false);
  const [showClosedVaults, setShowClosedVaults] = useState(false);
  const [searchTerm, setSearchTerm] = useState("");
  const [tokens, setTokens] = useState([]);
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loadingRows, setLoadingRows] = useState(true);
  const [error, setError] = useState("");
  const [displayMode, setDisplayMode] = useState("usd");
  const [nowMs, setNowMs] = useState(() => Date.now());
  const [expandedKickRows, setExpandedKickRows] = useState(() => new Set());
  const auctionFilterMenuRef = useRef(null);

  const resolvedTheme = themePreference || systemTheme;
  const headerLogoSrc = resolvedTheme === "dark" ? "/factory-dashboard-logo-dark.svg" : "/factory-dashboard-logo-light.svg";

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
      window.localStorage.setItem("factory_dashboard_theme_preference", themePreference);
    } else {
      window.localStorage.removeItem("factory_dashboard_theme_preference");
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
        const response = await fetch(apiUrl(""), {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error("Unable to load dashboard");
        }

        const payload = await response.json();

        if (!isMounted) {
          return;
        }

        const summaryPayload = payload.summary
          ? {
              ...payload.summary,
              latestScanAt: payload.latestScanAt || payload.summary.latestScanAt || null,
            }
          : {
              strategyCount: Array.isArray(payload.rows) ? payload.rows.length : 0,
              tokenCount: Array.isArray(payload.tokens) ? payload.tokens.length : 0,
              latestScanAt: payload.latestScanAt || null,
            };

        setSummary(summaryPayload);
        setTokens(payload.tokens || []);
        setRows(payload.rows || []);
      } catch (loadError) {
        if (isMounted && loadError.name !== "AbortError") {
          setError(loadError.message || "Unable to load dashboard");
          setSummary(null);
          setTokens([]);
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

  const tokenOptions = useMemo(() => {
    const bySymbol = new Map();

    for (const token of tokens) {
      const symbol = String(token.tokenSymbol || "UNKNOWN").trim() || "UNKNOWN";
      const key = symbol.toUpperCase();
      const existing = bySymbol.get(key);

      if (!existing || Number(token.strategyCount || 0) > Number(existing.strategyCount || 0)) {
        bySymbol.set(key, {
          tokenAddress: token.tokenAddress,
          tokenSymbol: symbol,
          strategyCount: Number(token.strategyCount || 0),
        });
      }
    }

    return Array.from(bySymbol.values()).sort(
      (a, b) => b.strategyCount - a.strategyCount || a.tokenSymbol.localeCompare(b.tokenSymbol),
    );
  }, [tokens]);

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

  const normalizedRows = useMemo(() => {
    return rows
      .map((row) => {
        const visibleBalances = row.balances
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
          ...row,
          balances: visibleBalances,
          totalUsdValue,
        };
      })
      .filter((row) => (showZeroBalance || row.balances.length > 0) && (showClosedVaults || row.depositLimit !== "0"));
  }, [rows, showZeroBalance, showClosedVaults]);

  const filteredRows = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    const filtered = normalizedRows.filter((row) => {
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
        row.strategyName,
        row.strategyAddress,
        row.vaultAddress,
        row.vaultName,
        row.vaultSymbol,
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
        return a.strategyAddress.localeCompare(b.strategyAddress);
      }
      if (!totalA) {
        return 1;
      }
      if (!totalB) {
        return -1;
      }

      const cmp = totalA.cmp(totalB);
      if (cmp === 0) {
        return a.strategyAddress.localeCompare(b.strategyAddress);
      }
      return balanceSortDirection === "desc" ? -cmp : cmp;
    });

    return filtered;
  }, [normalizedRows, searchTerm, selectedToken, auctionFilter, balanceSortDirection]);


  const latestVisibleScan = useMemo(() => {
    if (!filteredRows.length) {
      return summary?.latestScanAt || null;
    }

    return filteredRows.reduce((latest, row) => {
      if (!latest) {
        return row.scannedAt;
      }
      return row.scannedAt > latest ? row.scannedAt : latest;
    }, null);
  }, [filteredRows, summary]);

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

  function toggleKickExpand(strategyAddress) {
    setExpandedKickRows((prev) => {
      const next = new Set(prev);
      if (next.has(strategyAddress)) {
        next.delete(strategyAddress);
      } else {
        next.add(strategyAddress);
      }
      return next;
    });
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
            <span>Factory Dashboard</span>
          </h1>
          <ThemeSwitch
            themePreference={themePreference}
            resolvedTheme={resolvedTheme}
            onCycle={cycleThemePreference}
          />
        </div>
      </header>

      <TabBar activePage={activePage} onChangePage={setActivePage} />

      {activePage === "kicks" ? <KickLogPage nowMs={nowMs} /> : null}

      {activePage === "strategies" ? (
      <>
      <section className="meta">
        <div>Strategies: <strong>{(summary?.strategyCount || 0).toLocaleString()}</strong></div>
        <div>Tokens: <strong>{tokenOptions.length.toLocaleString()}</strong></div>
        <div>Latest scan: <strong>{formatTimestamp(latestVisibleScan)}</strong></div>
      </section>

      <section className="controls">
        <label className="control control-search">
          <span>Search</span>
          <input
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            placeholder="strategy, vault, auction, token symbol, address"
          />
        </label>

        <label className="control control-token">
          <span>Token</span>
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

        <label className="zero-balance-toggle">
          <input
            type="checkbox"
            checked={showZeroBalance}
            onChange={(e) => setShowZeroBalance(e.target.checked)}
          />
          <span>Show strats with 0 rewards</span>
        </label>

        <label className="zero-balance-toggle">
          <input
            type="checkbox"
            checked={showClosedVaults}
            onChange={(e) => setShowClosedVaults(e.target.checked)}
          />
          <span>Show retired vaults</span>
        </label>
      </section>

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th className="last-scan-col">Last Scan</th>
              <th>Vault</th>
              <th>Strategy</th>
              <th className="auction-col">
                <span className="th-header-inline">
                  <span>Auction</span>
                  <span className="th-filter-wrap" ref={auctionFilterMenuRef}>
                    <button
                      type="button"
                      className={`th-filter-icon ${auctionFilter !== "all" ? "is-active" : ""}`}
                      title={`Auction filter: ${auctionFilter}`}
                      aria-label={`Auction filter: ${auctionFilter}`}
                      aria-haspopup="menu"
                      aria-expanded={isAuctionFilterMenuOpen}
                      onClick={toggleAuctionFilterMenu}
                    >
                      <svg viewBox="0 0 16 16" aria-hidden="true">
                        <path d="M2.5 3.5h11l-4.5 5v3.5l-2 1v-4.5z" />
                      </svg>
                    </button>
                    {isAuctionFilterMenuOpen ? (
                      <div className="th-filter-popover" role="menu">
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={auctionFilter === "all"}
                          className={`th-filter-option ${auctionFilter === "all" ? "is-active" : ""}`}
                          onClick={() => selectAuctionFilter("all")}
                        >
                          all
                        </button>
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={auctionFilter === "null"}
                          className={`th-filter-option ${auctionFilter === "null" ? "is-active" : ""}`}
                          onClick={() => selectAuctionFilter("null")}
                        >
                          null
                        </button>
                        <button
                          type="button"
                          role="menuitemradio"
                          aria-checked={auctionFilter === "not_null"}
                          className={`th-filter-option ${auctionFilter === "not_null" ? "is-active" : ""}`}
                          onClick={() => selectAuctionFilter("not_null")}
                        >
                          not null
                        </button>
                      </div>
                    ) : null}
                  </span>
                </span>
              </th>
              <th>
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
            {!loadingRows && !filteredRows.length ? (
              <tr>
                <td colSpan={5} className="empty">No strategies match the current filters.</td>
              </tr>
            ) : null}
            {!loadingRows
              ? filteredRows.map((row) => (
                  <tr key={row.strategyAddress}>
                    <td className="mono muted last-scan-cell" title={formatTimestamp(row.scannedAt)}>
                      {formatRelativeTimestamp(row.scannedAt, nowMs)}
                    </td>
                    <td>
                      <EntityIdentity
                        primary={row.vaultSymbol || row.vaultName || "Unknown Vault"}
                        address={row.vaultAddress}
                      />
                    </td>
                    <td>
                      <EntityIdentity
                        primary={formatStrategyDisplayName(row.strategyName)}
                        address={row.strategyAddress}
                      />
                    </td>
                    <td className="auction-cell">
                      <AuctionAddressCell
                        address={row.auctionAddress}
                        version={row.auctionVersion}
                        kicks={row.kicks}
                        nowMs={nowMs}
                        isExpanded={expandedKickRows.has(row.strategyAddress)}
                        onToggleExpand={() => toggleKickExpand(row.strategyAddress)}
                      />
                    </td>
                    <td>
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
    </main>
  );
}
