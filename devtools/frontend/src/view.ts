import { useState } from "react";

export function useView(name: string) {
  const [view, setView] = useState<"structured" | "dictionary">(() => {
    try {
      return localStorage.getItem("kivyfn.view." + name) === "dictionary"
        ? "dictionary"
        : "structured";
    } catch {
      return "structured";
    }
  });
  const change = (value: "structured" | "dictionary") => {
    setView(value);
    try {
      localStorage.setItem("kivyfn.view." + name, value);
    } catch {
      /* Preferences are optional. */
    }
  };
  return [view, change] as const;
}
