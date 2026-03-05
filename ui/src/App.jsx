import { useEffect, useMemo, useRef, useState } from "react";
import Big from "big.js";

const ALL_TOKENS = "__all__";
const MIN_USD_VISIBLE = new Big("0.01");
const THEME_SEQUENCE = ["light", "dark"];

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
  const stored = window.localStorage.getItem("tidal_theme_preference");
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

function AuctionAddressCell({ address }) {
  if (!address) {
    return <span className="row-secondary mono">—</span>;
  }
  return <AddressCopy address={address} />;
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

function TokenBalances({
  balances,
  displayMode,
  onToggleMode,
  isExpanded,
  onToggleExpanded,
  totalUsdValue,
}) {
  const summaryLogos = balances.slice(0, 4);
  const hiddenLogoCount = Math.max(0, balances.length - summaryLogos.length);

  return (
    <div className="token-cell">
      <button
        type="button"
        className={`token-summary ${isExpanded ? "is-open" : ""}`}
        onClick={onToggleExpanded}
        title={isExpanded ? "Hide token breakdown" : "Show token breakdown"}
      >
        <span className="token-summary-caret" aria-hidden="true">›</span>
        <span className="token-logo-stack" aria-hidden="true">
          {summaryLogos.map((balance) => (
            <img
              key={`summary-${balance.tokenAddress}`}
              src={`/api/token-logo/${balance.tokenAddress}`}
              alt=""
              className="token-logo token-logo-stack-item"
              loading="lazy"
              decoding="async"
              onError={(event) => {
                event.currentTarget.style.visibility = "hidden";
              }}
            />
          ))}
          {hiddenLogoCount > 0 ? (
            <span className="token-logo-more mono">+{hiddenLogoCount}</span>
          ) : null}
        </span>
        <span className="mono token-summary-total">
          {totalUsdValue ? `$${formatBalance(totalUsdValue)}` : "?"}
        </span>
      </button>

      {isExpanded ? (
        <div className="token-stack">
          {balances.map((balance) => (
            <div key={`${balance.tokenAddress}-${balance.tokenSymbol}`} className="token-item">
              <img
                src={`/api/token-logo/${balance.tokenAddress}`}
                alt={`${balance.tokenSymbol} logo`}
                className="token-logo"
                loading="lazy"
                decoding="async"
                onError={(event) => {
                  event.currentTarget.style.visibility = "hidden";
                }}
              />
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
      ) : null}
    </div>
  );
}

export default function App() {
  const [selectedToken, setSelectedToken] = useState(getTokenFromUrl);
  const [themePreference, setThemePreference] = useState(getStoredThemePreference);
  const [systemTheme, setSystemTheme] = useState(resolveSystemTheme);
  const [searchTerm, setSearchTerm] = useState("");
  const [tokens, setTokens] = useState([]);
  const [rows, setRows] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loadingRows, setLoadingRows] = useState(true);
  const [error, setError] = useState("");
  const [displayMode, setDisplayMode] = useState("usd");
  const [expandedRows, setExpandedRows] = useState({});

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
      window.localStorage.setItem("tidal_theme_preference", themePreference);
    } else {
      window.localStorage.removeItem("tidal_theme_preference");
    }
  }, [themePreference]);

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

    async function loadCatalog() {
      try {
        const [summaryResponse, tokenResponse] = await Promise.all([
          fetch("/api/summary"),
          fetch("/api/tokens"),
        ]);

        if (!summaryResponse.ok || !tokenResponse.ok) {
          throw new Error("Unable to load dashboard metadata");
        }

        const summaryPayload = await summaryResponse.json();
        const tokenPayload = await tokenResponse.json();

        if (!isMounted) {
          return;
        }

        setSummary(summaryPayload);
        setTokens(tokenPayload.tokens || []);
      } catch (loadError) {
        if (isMounted) {
          setError(loadError.message || "Unable to load metadata");
        }
      }
    }

    loadCatalog();

    return () => {
      isMounted = false;
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

  useEffect(() => {
    let isMounted = true;
    const controller = new AbortController();

    async function loadRows() {
      setLoadingRows(true);
      setError("");

      try {
        const response = await fetch("/api/strategy-balances?limit=2500", {
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error("Unable to load strategy balances");
        }

        const payload = await response.json();

        if (!isMounted) {
          return;
        }

        setRows(payload.rows || []);
      } catch (loadError) {
        if (isMounted && loadError.name !== "AbortError") {
          setError(loadError.message || "Unable to load strategy balances");
          setRows([]);
        }
      } finally {
        if (isMounted) {
          setLoadingRows(false);
        }
      }
    }

    loadRows();

    return () => {
      isMounted = false;
      controller.abort();
    };
  }, []);

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
      .filter((row) => row.balances.length > 0);
  }, [rows]);

  const filteredRows = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();

    return normalizedRows.filter((row) => {
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
  }, [normalizedRows, searchTerm, selectedToken]);

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

  function toggleExpandedRow(strategyAddress) {
    setExpandedRows((prev) => ({
      ...prev,
      [strategyAddress]: !prev[strategyAddress],
    }));
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
            <span>Tidal Scan Dashboard</span>
          </h1>
          <ThemeSwitch
            themePreference={themePreference}
            resolvedTheme={resolvedTheme}
            onCycle={cycleThemePreference}
          />
        </div>
      </header>

      <section className="meta">
        <div>Strategies: <strong>{(summary?.strategyCount || 0).toLocaleString()}</strong></div>
        <div>Tokens: <strong>{tokenOptions.length.toLocaleString()}</strong></div>
        <div>Latest scan: <strong>{formatTimestamp(latestVisibleScan)}</strong></div>
      </section>

      <section className="controls">
        <label>
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

        <label>
          <span>Search</span>
          <input
            value={searchTerm}
            onChange={(event) => setSearchTerm(event.target.value)}
            placeholder="strategy, vault, auction, token symbol, address"
          />
        </label>
      </section>

      {error ? <p className="error">{error}</p> : null}

      <div className="table-shell">
        <table>
          <thead>
            <tr>
              <th>Strategy</th>
              <th>Vault</th>
              <th>Auction</th>
              <th>Token Balances</th>
              <th>Scanned</th>
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
                    <td>
                      <EntityIdentity
                        primary={row.strategyName || "Unnamed Strategy"}
                        address={row.strategyAddress}
                      />
                    </td>
                    <td>
                      <EntityIdentity
                        primary={row.vaultSymbol || row.vaultName || "Unknown Vault"}
                        address={row.vaultAddress}
                      />
                    </td>
                    <td className="auction-cell">
                      <AuctionAddressCell address={row.auctionAddress} />
                    </td>
                    <td>
                      <TokenBalances
                        balances={row.balances}
                        displayMode={displayMode}
                        onToggleMode={toggleDisplayMode}
                        isExpanded={Boolean(expandedRows[row.strategyAddress])}
                        onToggleExpanded={() => toggleExpandedRow(row.strategyAddress)}
                        totalUsdValue={row.totalUsdValue}
                      />
                    </td>
                    <td className="mono muted">{formatTimestamp(row.scannedAt)}</td>
                  </tr>
                ))
              : null}
          </tbody>
        </table>
      </div>
    </main>
  );
}
