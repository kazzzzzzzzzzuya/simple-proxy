import { callable } from "@decky/api";

import type {
  CalibrationResult,
  GyroPatch,
  GyroSettings,
  GyroStatus,
  SlotName,
} from "./gyro_types";

export type StickMode = "solid" | "pulse" | "rainbow" | "spiral";
export type RemapButton = "Y1" | "Y2" | "Y3" | "M2" | "M3";

export interface PowerProfile {
  /** "custom" (TDP slider) or one of the device's own power modes. */
  mode: string;
  tdp: number;
  auto: boolean;
  target_fps: number;
  min_tdp: number;
}

export interface Settings {
  lighting: {
    sticks: {
      managed: boolean;
      on: boolean;
      mode: StickMode;
      color: [number, number, number];
      brightness: number;
      speed: number;
    };
    power_led: { managed: boolean; on: boolean; off_in_sleep: boolean };
  };
  battery: { charge_limit: boolean | null };
  fan: { enabled: boolean; full_speed: boolean; curve: number[] };
  power: {
    enabled: boolean;
    per_game: boolean;
    cpu_boost: boolean | null;
    profiles: Record<string, PowerProfile>;
  };
  controls: {
    swap_menu: boolean;
    swap_layout: "steam_menu" | "steam_view";
    /** "deck-uhid" (default), "xbox" (no gyro in Steam), "ds5-edge" or "ds5". */
    steam_controller: string;
    /** Games with their own controller type, by app id. */
    steam_controller_games: Record<string, { type: string; name: string }>;
    remap_managed: boolean;
    remap: Record<RemapButton, string>;
    touchpad: boolean | null;
  };
}

type DeepPartial<T> = T extends (infer U)[]
  ? U[]
  : T extends object
    ? { [K in keyof T]?: DeepPartial<T[K]> }
    : T;
export type SettingsPatch = DeepPartial<Settings>;

export interface Capability {
  available: boolean;
  reason: string | null;
}
export type CapName =
  | "power"
  | "fps"
  | "fan"
  | "charge_limit"
  | "power_led"
  | "sticks"
  | "remap"
  | "swap_menu"
  | "steam_gyro";
export type Caps = Record<CapName, Capability> & {
  steam_gyro: Capability & { types: string[] };
};

export interface HubStatus {
  model: string;
  errors: Record<string, string>;
  app: { id: number | null; name: string };
  steam_controller: { type: string; source: "game" | "default" };
  power: {
    profile_key: string;
    mode: string | null;
    modes: string[];
    tdp: number | null;
    range: [number, number] | null;
    cpu_boost: boolean | null;
    auto: {
      active: boolean;
      state: string | null;
      fps: number | null;
      probe_error: string | null;
    };
  };
  fan: {
    temp: number | null;
    rpm: number | null;
    /** How often the plugin had to put the fan setting back. */
    restores: number;
    restored_ago: number | null;
  };
  battery: {
    info: { percent: number; status: string } | null;
    charge_limit: boolean | null;
    backend: string | null;
  };
  power_led: boolean | null;
  swap_menu: { active: boolean };
}

export interface State {
  version: string;
  caps: Caps;
  settings: Settings;
  status: HubStatus;
  gyro: { settings: GyroSettings; defaults: GyroSettings; status: GyroStatus };
}

export const getState = callable<[], State>("get_state");
export const getStatus = callable<[], { hub: HubStatus; gyro: GyroStatus }>("get_status");
export const updateSettings = callable<[patch: SettingsPatch], Settings>("update_settings");
export const resetSettings = callable<[], Settings>("reset_settings");
export const deletePowerProfile = callable<[key: string], Settings>("delete_power_profile");
export const setRunningApp = callable<[appId: number | null, name: string], void>(
  "set_running_app",
);
export const setGameController = callable<
  [appId: number, kind: string | null, name: string],
  Settings
>("set_game_controller");
export const onSuspend = callable<[], void>("on_suspend");
export const onResume = callable<[], void>("on_resume");

export const gyroSetSettings = callable<[patch: GyroPatch], GyroSettings>("gyro_set_settings");
export const gyroResetSettings = callable<[], GyroSettings>("gyro_reset_settings");
export const gyroCalibrate = callable<[target: SlotName], CalibrationResult>("gyro_calibrate");
export const gyroCalibrateAll = callable<[], Record<string, CalibrationResult>>(
  "gyro_calibrate_all",
);
