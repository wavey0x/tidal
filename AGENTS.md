# AGENTS

## UI Compliance Guardrails

These rules apply to `/Users/wavey/yearn/tidal/ui`.

1. Theme behavior:
- Theme toggle supports user-selected `light` and `dark`.
- Before first user selection, follow system theme (`prefers-color-scheme`).
- Persist only explicit user selections in `localStorage` key: `tidal_theme_preference`.
- Apply explicit overrides using `document.documentElement[data-theme]`.

2. Contrast requirements:
- Body/small interactive text target contrast >= 4.5:1.
- Tiny UI labels (table headers/metadata) target >= 4.5:1 where feasible; never below 3:1.
- Avoid low-contrast gray text tokens that drift below these thresholds.
- Keep light-mode success/check green at or darker than `#217F46` for accessibility on white backgrounds.

3. Component consistency:
- Address and token copy affordances must use the same animated copy icon treatment.
- Strategy and vault identity cells must use the shared entity component pattern.
- Token balance summary row uses: caret, token logos, total USD.

4. Token visuals:
- Token icon sizes are standardized with CSS variables:
  - `--token-icon-size`
  - `--token-summary-icon-size`
- Keep icon size updates centralized in CSS vars.

5. Data display:
- Balances render with 2 decimals.
- USD mode is default.
- Unknown USD values display `?`.
- Token entries below `$0.01` (when priced) are hidden.

6. Verify before shipping:
- Run `npm run build` in `/Users/wavey/yearn/tidal/ui`.
- Check light and dark themes visually.
- Confirm copy icon animation/contrast in both themes.
- Re-check palette contrast when token colors change.
