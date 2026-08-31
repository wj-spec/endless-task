import { useEffect, useState } from "react";

/**
 * Reactively tracks a media query against the current viewport.
 *
 * The client is a Vite SPA, so `window` is available at mount time; the
 * initial state still guards against environments where `matchMedia` is
 * missing (e.g. jsdom) so tests and SSR-like renders do not throw.
 */
export function useMediaQuery(query: string): boolean {
  const getMatch = () =>
    typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(query).matches
      : false;

  const [matches, setMatches] = useState(getMatch);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const queryList = window.matchMedia(query);
    const handleChange = (event: MediaQueryListEvent) => setMatches(event.matches);
    setMatches(queryList.matches);
    queryList.addEventListener("change", handleChange);
    return () => queryList.removeEventListener("change", handleChange);
  }, [query]);

  return matches;
}