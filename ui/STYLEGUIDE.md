# Tidal Dashboard Style Guide

This style guide defines the visual and accessibility baseline for the `ui` dashboard.
The system is monochrome, compact, and data-first.

## 1. Core Direction

- Quiet, document-like, technical UI
- Divider-led hierarchy (not card-heavy)
- Dense rows with controlled spacing
- Minimal visual noise, strong data legibility

## 2. Theme System

Theme behavior:
- Toggle supports explicit user states: `light` and `dark`
- Default behavior (no saved choice): follow system theme (`prefers-color-scheme`)

Implementation requirements:
- Persist only explicit user choice in `localStorage` (`tidal_theme_preference`)
- Apply explicit overrides with `html[data-theme="light|dark"]`
- With no saved choice, do not force `data-theme`; use OS preference

Theme control:
- Minimal icon switch using sun/moon glyphs
- Keep switch small and unobtrusive

## 3. Color Tokens (Current)

### Light

- `--bg`: `#FFFFFF`
- `--text-primary`: `#111111`
- `--text-secondary`: `#5F5F5F`
- `--text-faint`: `#767676`
- `--divider-strong`: `#CCCCCC`
- `--divider-subtle`: `#E6E7EB`
- `--hover`: `rgba(0,0,0,0.04)`
- `--success`: `#217F46`

### Dark

- `--bg`: `#1A1A1A`
- `--text-primary`: `#FAFAFA`
- `--text-secondary`: `#D0D0D0`
- `--text-faint`: `#B1B1B1`
- `--divider-strong`: `#404040`
- `--divider-subtle`: `#2A2A2A`
- `--hover`: `rgba(255,255,255,0.04)`
- `--success`: `#5BC47D`

## 4. Contrast Requirements

- Standard text and interactive text: target >= 4.5:1
- Tiny labels and table headers: keep >= 4.5:1 when possible, never below 3:1
- Do not introduce lighter grays that reduce legibility
- Verify copy icon and success/check states in both themes

Current token contrast against background:
- Light `--text-primary` / `--bg`: `18.88:1`
- Light `--text-secondary` / `--bg`: `6.39:1`
- Light `--text-faint` / `--bg`: `4.54:1`
- Light `--success` / `--bg`: `5.01:1`
- Dark `--text-primary` / `--bg`: `16.67:1`
- Dark `--text-secondary` / `--bg`: `11.28:1`
- Dark `--text-faint` / `--bg`: `8.12:1`

## 5. Typography

- UI text: system sans-serif
- Numeric/address text: monospace
- Page title: `18px`
- Body: `12px`
- Labels: `11px`
- Table headers: `10px`, uppercase, spaced

## 6. Layout and Spacing

- Max content width: `980px`
- 4px spacing grid (`4, 8, 12, 16, 24, 32`)
- Tight spacing in rows, larger spacing between sections
- Keep `html { overflow-y: scroll; }` for layout stability

## 7. Component Standards

### Entity Cells
- Strategy and Vault use shared rendering structure
- Primary line + copied address line
- Copy icon style must be shared globally

### Copy Icon
- Minimal, monochrome, inline
- On click: animate to green checkmark for 1.5s, then reset

### Token Balances Cell
- Default collapsed: `caret | side-by-side token icons | total USD`
- Expand via caret to show line-by-line token rows
- Line item values remain right-aligned in a fixed value column
- Clicking any value toggles USD/token mode globally

### Token Icons
- Use CSS vars for sizing:
  - `--token-icon-size`
  - `--token-summary-icon-size`
- Current sizing is +10% over previous baseline

## 8. Data Display Rules

- Numeric balances: 2 decimals
- USD is default mode
- Missing USD values: display `?`
- Tokens with priced USD value `< $0.01`: hide from display
- Addresses shown short, copied as full checksummed values

## 9. Mobile

- Collapse controls to one column
- Preserve readable numeric alignment
- Maintain tap-friendly controls and icon targets

## 10. QA Checklist

- [ ] Theme switch toggles `light <-> dark`
- [ ] First load follows system theme before user selection
- [ ] Contrast remains acceptable in both themes
- [ ] Copy icon animation works on all copy targets
- [ ] Token USD/value alignment is stable when expanded
- [ ] `npm run build` passes
