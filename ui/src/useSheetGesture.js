import { useCallback, useLayoutEffect, useRef } from "react";

const INTERACTIVE = 'a, button, input, select, textarea, summary, [role="button"], [contenteditable]:not([contenteditable="false"])';
const EASING = "cubic-bezier(0.22, 1, 0.36, 1)";

// One gesture owner for every detail sheet. Native touch listeners let the body
// keep browser scrolling unless a downward pull starts at its scroll boundary.
export function useSheetGesture(sheetRef, headerRef, bodyRef, backdropRef, onClose) {
  const closeRef = useRef(onClose);
  closeRef.current = onClose;
  const dismissRef = useRef(() => {});
  const dismiss = useCallback(() => dismissRef.current(), []);

  useLayoutEffect(() => {
    const sheet = sheetRef.current;
    const header = headerRef.current;
    const body = bodyRef.current;
    const backdrop = backdropRef.current;
    const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let gesture = null;
    let closing = false;
    let animations = [];
    let suppressClick = false;

    const offset = () => new DOMMatrixReadOnly(getComputedStyle(sheet).transform).m42;
    const opacity = () => {
      const channels = getComputedStyle(backdrop).backgroundColor.match(/[\d.]+/g).map(Number);
      return (channels[3] ?? 1) / 0.45;
    };
    const paint = (y, opacity = 1 - Math.min(1, y / sheet.offsetHeight)) => {
      sheet.style.transform = y ? `translateY(${y}px)` : "none";
      backdrop.style.setProperty("--sheet-dim", opacity);
    };
    const stopAnimation = () => {
      const y = offset();
      const dim = opacity();
      animations.forEach(animation => animation.cancel());
      animations = [];
      paint(y, dim);
      return y;
    };
    const animateTo = (y, dim, done) => {
      const from = offset();
      const fromDim = opacity();
      stopAnimation();
      paint(y, dim);
      if (reducedMotion.matches) {
        done?.();
        return;
      }
      const options = { duration: 240, easing: EASING };
      // Animate a separate backdrop surface: fading the overlay itself also
      // fades the sheet's text and made dismissal look like a flash.
      const motion = sheet.animate([{ transform: `translateY(${from}px)` }, { transform: y ? `translateY(${y}px)` : "none" }], options);
      const shade = backdrop.animate([{ backgroundColor: `rgba(0, 0, 0, ${fromDim * 0.45})` },
        { backgroundColor: `rgba(0, 0, 0, ${dim * 0.45})` }], options);
      animations = [motion, shade];
      motion.onfinish = () => { animations = []; done?.(); };
    };
    const clearGesture = () => {
      const current = gesture;
      gesture = null;
      delete sheet.dataset.dragging;
      if (current?.pointerId != null && header.hasPointerCapture(current.pointerId)) header.releasePointerCapture(current.pointerId);
      return current;
    };
    dismissRef.current = () => {
      if (closing) return;
      closing = true;
      clearGesture();
      sheet.dataset.closing = "true";
      animateTo(sheet.offsetHeight, 0, () => closeRef.current());
    };
    const cancel = () => {
      const current = clearGesture();
      if (current?.dragging && !closing) animateTo(0, 1);
    };
    const begin = (point, target, pointerId) => {
      if (closing || gesture || target.closest(INTERACTIVE) || sheet.closest("[inert]")) return;
      const inHeader = header.contains(target);
      if (!inHeader && (pointerId != null || !body.contains(target))) return;
      // Do not steal scrolling from the body or a nested scroll container.
      for (let node = target; !inHeader && node && node !== sheet; node = node.parentElement) {
        if (node.scrollTop > 0) return;
      }
      suppressClick = false;
      gesture = { x: point.clientX, y: point.clientY, pointerId, dragging: false, samples: [] };
    };
    const move = (point, event) => {
      const current = gesture;
      if (!current) return;
      const dx = point.clientX - current.x;
      const dy = point.clientY - current.y;
      if (!current.dragging) {
        if (Math.max(Math.abs(dx), Math.abs(dy)) < 6) return;
        // Once a gesture becomes a scroll, horizontal swipe or pinch, it stays
        // with the browser until release, even if it later reaches scrollTop=0.
        if (dy <= 0 || Math.abs(dx) >= dy || !event.cancelable) { clearGesture(); return; }
        current.base = stopAnimation();
        current.dragging = true;
        sheet.dataset.dragging = "true";
        if (current.pointerId != null) header.setPointerCapture(current.pointerId);
      }
      if (!event.cancelable) { cancel(); return; }
      event.preventDefault();
      suppressClick = true;
      const distance = Math.max(0, dy);
      if (Math.abs(distance - (current.distance || 0)) > 2) current.reversing = distance < current.distance;
      current.distance = distance;
      const now = performance.now();
      current.samples.push({ y: current.distance, time: now });
      current.samples = current.samples.filter(sample => now - sample.time <= 100);
      paint(current.base + current.distance);
    };
    const end = () => {
      const current = clearGesture();
      if (!current?.dragging) return;
      const first = current.samples[0];
      const last = current.samples.at(-1);
      const velocity = (last.y - first.y) / Math.max(1, performance.now() - first.time);
      const threshold = Math.min(120, sheet.offsetHeight * 0.22);
      if (!current.reversing && (current.distance > threshold || (current.distance > 24 && velocity > 0.6))) dismissRef.current();
      else animateTo(0, 1);
    };
    const touchStart = event => {
      suppressClick = false;
      if (event.touches.length !== 1) { cancel(); return; }
      begin(event.touches[0], event.target);
    };
    const touchMove = event => {
      if (event.touches.length !== 1) { cancel(); return; }
      move(event.touches[0], event);
    };
    const pointerDown = event => {
      if (event.pointerType === "touch" || !event.isPrimary || event.button !== 0) return;
      suppressClick = false;
      begin(event, event.target, event.pointerId);
    };
    const pointerMove = event => { if (gesture?.pointerId === event.pointerId) move(event, event); };
    const pointerUp = event => { if (gesture?.pointerId === event.pointerId) end(); };
    const pointerCancel = event => { if (gesture?.pointerId === event.pointerId) cancel(); };
    const click = event => {
      if (closing || (suppressClick && event.detail > 0)) { event.preventDefault(); event.stopPropagation(); }
      suppressClick = false;
    };
    paint(sheet.offsetHeight, 0);
    animateTo(0, 1);
    const listeners = [
      [sheet, "touchstart", touchStart, { passive: true }],
      [sheet, "touchmove", touchMove, { passive: false }],
      [sheet, "touchend", end], [sheet, "touchcancel", cancel],
      [sheet, "pointerdown", pointerDown],
      [window, "pointermove", pointerMove], [window, "pointerup", pointerUp],
      [header, "lostpointercapture", pointerCancel], [window, "pointercancel", pointerCancel],
      [sheet, "click", click, true], [window, "blur", cancel], [window, "resize", cancel],
      [window.visualViewport, "resize", cancel],
    ];
    listeners.forEach(([node, type, handler, options]) => node?.addEventListener(type, handler, options));
    return () => {
      listeners.forEach(([node, type, handler, options]) => node?.removeEventListener(type, handler, options));
      clearGesture();
      animations.forEach(animation => animation.cancel());
      dismissRef.current = () => {};
    };
  }, [sheetRef, headerRef, bodyRef, backdropRef]);

  return dismiss;
}
