// One shared copy of the plugin state. Tabs read it through useStore(), so
// switching tabs or reopening the menu never starts from an empty screen.
import { useEffect, useState } from "react";

import {
  getState,
  getStatus,
  gyroSetSettings,
  updateSettings,
  Settings,
  SettingsPatch,
  State,
} from "./backend";
import type { GyroPatch, GyroSettings } from "./gyro_types";

const SAVE_DEBOUNCE_MS = 250;

export interface Store {
  state: State | null;
  error: string | null;
}

let store: Store = { state: null, error: null };
const listeners = new Set<() => void>();

function publish(next: Partial<Store>) {
  store = { ...store, ...next };
  listeners.forEach((fn) => fn());
}

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

export function deepMerge<T>(base: T, patch: unknown): T {
  if (!isObject(base) || !isObject(patch)) return (patch === undefined ? base : patch) as T;
  const out: Record<string, unknown> = { ...base };
  for (const key of Object.keys(patch)) {
    out[key] = isObject(out[key]) && isObject(patch[key])
      ? deepMerge(out[key], patch[key])
      : patch[key];
  }
  return out as T;
}

export async function load(): Promise<void> {
  try {
    publish({ state: await getState(), error: null });
  } catch (e) {
    publish({ error: `${e}` });
  }
}

let polling = false;
export async function refreshStatus(): Promise<void> {
  if (polling || !store.state) return;
  polling = true;
  try {
    const st = await getStatus();
    if (store.state) {
      publish({
        state: { ...store.state, status: st.hub, gyro: { ...store.state.gyro, status: st.gyro } },
        error: null,
      });
    }
  } catch (e) {
    publish({ error: `${e}` });
  } finally {
    polling = false;
  }
}

// ---- debounced writers -----------------------------------------------------

function debounced<P>(send: (patch: P) => Promise<void>) {
  let pending: P | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const flush = async () => {
    timer = null;
    const patch = pending;
    pending = null;
    if (patch === null) return;
    try {
      await send(patch);
    } catch (e) {
      publish({ error: `${e}` });
    }
  };
  return {
    push(patch: P) {
      pending = pending === null ? patch : deepMerge(pending, patch);
      if (timer) clearTimeout(timer);
      timer = setTimeout(flush, SAVE_DEBOUNCE_MS);
    },
    flush() {
      if (timer) {
        clearTimeout(timer);
        void flush();
      }
    },
  };
}

let hubEdits = 0;
const hubWriter = debounced<SettingsPatch>(async (patch) => {
  const seen = hubEdits;
  const saved = await updateSettings(patch);
  // Take the backend's (validated) copy unless the user kept editing meanwhile.
  if (store.state && seen === hubEdits) publish({ state: { ...store.state, settings: saved } });
});

const gyroWriter = debounced<GyroPatch>(async (patch) => {
  await gyroSetSettings(patch);
});

/** Change hardware settings: shown at once, sent to the backend shortly after. */
export function update(patch: SettingsPatch): void {
  if (!store.state) return;
  hubEdits += 1;
  publish({
    state: { ...store.state, settings: deepMerge<Settings>(store.state.settings, patch) },
  });
  hubWriter.push(patch);
}

export function updateGyro(patch: GyroPatch): void {
  if (!store.state) return;
  const gyro = store.state.gyro;
  publish({
    state: {
      ...store.state,
      gyro: { ...gyro, settings: deepMerge<GyroSettings>(gyro.settings, patch) },
    },
  });
  gyroWriter.push(patch);
}

export function replaceSettings(settings: Settings): void {
  if (store.state) publish({ state: { ...store.state, settings } });
}

export function replaceGyroSettings(settings: GyroSettings): void {
  if (store.state) {
    publish({ state: { ...store.state, gyro: { ...store.state.gyro, settings } } });
  }
}

export function flushWrites(): void {
  hubWriter.flush();
  gyroWriter.flush();
}

export function useStore(): Store {
  const [, setTick] = useState(0);
  useEffect(() => {
    const fn = () => setTick((n) => n + 1);
    listeners.add(fn);
    return () => {
      listeners.delete(fn);
    };
  }, []);
  return store;
}
