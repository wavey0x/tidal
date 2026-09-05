import { useLayoutEffect } from "react";

// Fixed dialogs must fit the visible screen, not the larger layout viewport
// retained by mobile browsers during pinch zoom or keyboard/chrome changes.
export function useVisibleViewport(overlayRef) {
  useLayoutEffect(() => {
    const overlay = overlayRef.current;
    const viewport = window.visualViewport;
    let frame = null;
    const update = () => {
      frame = null;
      const values = {
        left: viewport?.offsetLeft ?? 0,
        top: viewport?.offsetTop ?? 0,
        width: viewport?.width ?? document.documentElement.clientWidth,
        height: viewport?.height ?? window.innerHeight,
      };
      for (const [name, value] of Object.entries(values)) {
        overlay.style.setProperty(`--visible-${name}`, `${value}px`);
      }
    };
    const schedule = () => { if (frame === null) frame = requestAnimationFrame(update); };
    update();
    viewport?.addEventListener("resize", schedule);
    viewport?.addEventListener("scroll", schedule);
    window.addEventListener("resize", schedule);
    return () => {
      if (frame !== null) cancelAnimationFrame(frame);
      viewport?.removeEventListener("resize", schedule);
      viewport?.removeEventListener("scroll", schedule);
      window.removeEventListener("resize", schedule);
    };
  }, [overlayRef]);
}
