import { useState, useRef, useMemo, useCallback, useEffect } from 'react';

// src/useHoverPin.ts
function useHoverPin({ excludeRefs = [], excludeSelectors = [] } = {}) {
  const [hovered, setHovered] = useState(null);
  const [pinned, setPinned] = useState(null);
  const suppressed = useRef(null);
  const touch = useMemo(
    () => typeof window !== "undefined" && window.matchMedia?.("(hover: none)").matches,
    []
  );
  const hover = useCallback(
    (k) => {
      if (touch) return;
      if (k === null || !Object.is(k, suppressed.current)) suppressed.current = null;
      setHovered(Object.is(k, suppressed.current) ? null : k);
    },
    [touch]
  );
  const togglePin = useCallback((k) => {
    setPinned((prev) => {
      if (Object.is(prev, k)) {
        suppressed.current = k;
        setHovered((h) => Object.is(h, k) ? null : h);
        return null;
      }
      return k;
    });
  }, []);
  const clearPin = useCallback(() => setPinned(null), []);
  useEffect(() => {
    if (pinned === null) return;
    const onDown = (e) => {
      const t = e.target;
      if (!t) return;
      if (excludeRefs.some((r) => r.current?.contains(t))) return;
      if (excludeSelectors.some((sel) => t.closest(sel))) return;
      setPinned(null);
    };
    const onKey = (e) => {
      if (e.key === "Escape") setPinned(null);
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [pinned]);
  return { hovered, pinned, active: pinned ?? hovered, hover, togglePin, clearPin };
}

export { useHoverPin };
//# sourceMappingURL=chunk-HMG5G7YN.js.map
//# sourceMappingURL=chunk-HMG5G7YN.js.map