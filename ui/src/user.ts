import { createContext, useContext, useState } from "react";

const KEY = "hecklr.user";

interface UserState {
  user: string;
  setUser: (name: string) => void;
}

/** Who labels and reviews: the server's default (`hecklr ui --user`) until the reader changes it. */
export const UserContext = createContext<UserState>({ user: "", setUser: () => undefined });

export function useUser(): UserState {
  return useContext(UserContext);
}

function readStored(): string | null {
  try {
    return localStorage.getItem(KEY);
  } catch {
    return null; // Storage blocked (private mode): the name lasts for this page only.
  }
}

/** The reader's name, remembered in this browser; `fallback` until they type one. */
export function useStoredUser(fallback: string | undefined): [string, (name: string) => void] {
  const [stored, setStored] = useState<string | null>(readStored);
  const setUser = (name: string) => {
    setStored(name);
    try {
      localStorage.setItem(KEY, name);
    } catch {
      // Blocked storage only means the name is not remembered next time.
    }
  };
  return [stored ?? fallback ?? "", setUser];
}
