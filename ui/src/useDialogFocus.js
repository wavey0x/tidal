import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

const dialogs = [];
let restorePage;
const inertElements = new Map();

function isolateTopDialog() {
  const top = dialogs.at(-1);
  for (const node of document.body.children) {
    if (!(node instanceof HTMLElement)) continue;
    if (!inertElements.has(node)) inertElements.set(node, node.inert);
    node.inert = top ? !node.contains(top.dialog) : inertElements.get(node);
  }
  if (!top) {
    for (const [node, inert] of inertElements) node.inert = inert;
    inertElements.clear();
  }
}

function lockPage() {
  const body = document.body;
  const root = document.documentElement;
  const x = window.scrollX;
  const y = window.scrollY;
  const keys = ["position", "top", "left", "width", "overflow", "paddingRight"];
  const styles = Object.fromEntries(keys.map(key => [key, body.style[key]]));
  const overflow = root.style.overflow;
  const gap = window.innerWidth - root.clientWidth;
  if (gap) body.style.paddingRight = `${parseFloat(getComputedStyle(body).paddingRight) + gap}px`;
  Object.assign(body.style, { position: "fixed", top: `${-y}px`, left: `${-x}px`, width: "100%", overflow: "hidden" });
  root.style.overflow = "hidden";
  return () => {
    Object.assign(body.style, styles);
    root.style.overflow = overflow;
    window.scrollTo({ left: x, top: y, behavior: "instant" });
  };
}

// Keep portaled dialogs usable with a keyboard, including nested confirmation dialogs.
export function useDialogFocus(dialogRef, onClose, initialFocusRef) {
  const closeRef = useRef(onClose);
  const backdropPressRef = useRef(false);
  closeRef.current = onClose;

  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    const entry = { dialog, previousFocus };
    if (!dialogs.length) restorePage = lockPage();
    dialogs.push(entry);
    isolateTopDialog();
    const focusable = () => [...dialog.querySelectorAll(FOCUSABLE)].filter((node) => {
      if (!node.getClientRects().length || getComputedStyle(node).visibility !== "visible") return false;
      // Closed details can retain layout rectangles after being opened once.
      // Only their summary belongs in the keyboard loop, not hidden controls.
      for (let parent = node.parentElement; parent && parent !== dialog; parent = parent.parentElement) {
        if (parent.tagName === "DETAILS" && !parent.open && !parent.querySelector(":scope > summary")?.contains(node)) return false;
      }
      return true;
    });
    (initialFocusRef?.current || focusable()[0] || dialog).focus({ preventScroll: true });

    const onKeyDown = (event) => {
      // A confirmation dialog may open above a mobile detail sheet.
      if (event.defaultPrevented || dialogs.at(-1) !== entry) return;
      if (event.key === "Escape") {
        event.preventDefault();
        closeRef.current();
      } else if (event.key === "Tab") {
        const items = focusable();
        const first = items[0] || dialog;
        const last = items[items.length - 1] || dialog;
        if (
          !dialog.contains(document.activeElement) ||
          document.activeElement === dialog ||
          (event.shiftKey ? document.activeElement === first : document.activeElement === last)
        ) {
          event.preventDefault();
          (event.shiftKey ? last : first).focus();
        }
      }
    };
    const onFocusIn = (event) => {
      if (dialogs.at(-1) === entry && !dialog.contains(event.target)) {
        (initialFocusRef?.current || focusable()[0] || dialog).focus({ preventScroll: true });
      }
    };
    document.addEventListener("keydown", onKeyDown);
    document.addEventListener("focusin", onFocusIn);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.removeEventListener("focusin", onFocusIn);
      const wasTop = dialogs.at(-1) === entry;
      dialogs.splice(dialogs.indexOf(entry), 1);
      // A parent may unmount before its confirmation; retain its return target.
      for (const remaining of dialogs) {
        if (dialog.contains(remaining.previousFocus)) remaining.previousFocus = entry.previousFocus;
      }
      isolateTopDialog();
      if (!dialogs.length) { restorePage?.(); restorePage = null; }
      if (wasTop && entry.previousFocus?.isConnected && !entry.previousFocus.closest("[inert]")) entry.previousFocus.focus({ preventScroll: true });
    };
  }, [dialogRef, initialFocusRef]);

  // Dismiss only a completed backdrop click, never a drag that crosses the
  // dialog edge or the initial press before the user releases their pointer.
  return {
    onPointerDown(event) { backdropPressRef.current = event.target === event.currentTarget && event.button === 0; },
    onPointerUp(event) { if (event.target !== event.currentTarget) backdropPressRef.current = false; },
    onPointerCancel() { backdropPressRef.current = false; },
    onClick(event) {
      const dismiss = backdropPressRef.current && event.target === event.currentTarget;
      backdropPressRef.current = false;
      if (dismiss) closeRef.current();
    },
  };
}
