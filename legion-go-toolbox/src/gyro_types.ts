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

export interface GyroSettings {
  enabled: boolean;
  /** Panel layout only: false = simple view, true = all options. */
  advanced: boolean;
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
  /** Gyro noise (deg/s) measured by the last manual calibration. */
  rest_noise: number | null;
  controller_connected?: boolean;
  attached?: boolean;
  battery?: number | null;
}

export interface GyroStatus {
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

export type CalibrationResult = "ok" | "moved" | "nodata";

/** Partial settings update; nested objects are merged by the backend. */
export type GyroPatch = Partial<Omit<GyroSettings, "slots">> & {
  slots?: Partial<Record<SlotName, Partial<SlotSettings>>>;
};
