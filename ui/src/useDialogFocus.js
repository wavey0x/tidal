import { useEffect, useRef } from "react";

const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]:not([tabindex="-1"])';

// Keep portaled dialogs usable with a keyboard, including nested confirmation dialogs.
export function useDialogFocus(dialogRef, onClose, initialFocusRef) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;

  useEffect(() => {
    const dialog = dialogRef.current;
    const previousFocus = document.activeElement;
    const previousOverflow = document.body.style.overflow;
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
    document.body.style.overflow = "hidden";

    const onKeyDown = (event) => {
      // A confirmation dialog may open above a mobile detail sheet.
      const dialogs = document.querySelectorAll('[role="dialog"]');
      if (dialogs[dialogs.length - 1] !== dialog) return;
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
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (previousFocus?.isConnected) previousFocus.focus({ preventScroll: true });
    };
  }, [dialogRef, initialFocusRef]);
}
