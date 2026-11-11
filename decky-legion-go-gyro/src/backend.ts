import { callable } from "@decky/api";

export type SlotName = "combined" | "left" | "right";
export type SourceName = "auto" | "right" | "left" | "tablet";
export type Rotation = 0 | 90 | 180 | 270;

export interface SlotSettings {
  enabled: boolean;
  sensitivity: number;
  deadzone: number;
  rotation: Rotation;
  invert_pitch: boolean;
  invert_yaw: boolean;
  invert_roll: boolean;
  /** Only present on the combined slot. */
  source?: SourceName;
}

export interface Settings {
  enabled: boolean;
  port: number;
  lan: boolean;
  auto_calibrate: boolean;
  slots: Record<SlotName, SlotSettings>;
}

export interface SlotStatus {
  slot: number;
  connected: boolean;
  source: "left" | "right" | "tablet" | null;
  live: boolean;
  rate: number;
  accel: [number, number, number];
  gyro: [number, number, number];
  calibrated: boolean;
  calibrating: boolean;
  controller_connected?: boolean;
  attached?: boolean;
  battery?: number | null;
}

export interface Status {
  enabled: boolean;
  running: boolean;
  model: string;
  device: {
    found: boolean;
    receiving: boolean;
    mode: string | null;
    error: string | null;
  };
  server: {
    listening: boolean;
    host: string | null;
    port: number;
    clients: number;
    error: string | null;
  };
  tablet: { available: boolean; active: boolean; error: string | null };
  slots: Record<SlotName, SlotStatus>;
}

export interface State {
  version: string;
  settings: Settings;
  status: Status;
}

export type CalibrationResult = "ok" | "moved" | "nodata";

/** Partial settings update; nested objects are merged by the backend. */
export type SettingsPatch = Partial<Omit<Settings, "slots">> & {
  slots?: Partial<Record<SlotName, Partial<SlotSettings>>>;
};

export const getState = callable<[], State>("get_state");
export const getStatus = callable<[], Status>("get_status");
export const setSettings = callable<[patch: SettingsPatch], Settings>("set_settings");
export const resetSettings = callable<[], Settings>("reset_settings");
export const calibrate = callable<[target: SlotName], CalibrationResult>("calibrate");
