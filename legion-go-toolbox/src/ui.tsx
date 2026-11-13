import { Field, PanelSectionRow } from "@decky/ui";
import { FC, ReactNode } from "react";

import type { Capability } from "./backend";

export const PLUGIN_NAME = "Legion Go Toolbox";
export const ACCENT = "#1a9fff";
export const WARN = "#ffb84d";

/** A plain information row. */
export const Info: FC<{ label: ReactNode; children?: ReactNode; warn?: boolean }> = ({
  label,
  children,
  warn,
}) => (
  <PanelSectionRow>
    <Field
      label={label}
      description={
        children ? <span style={warn ? { color: WARN } : undefined}>{children}</span> : undefined
      }
      padding="compact"
    />
  </PanelSectionRow>
);

/** Shown instead of a module's controls when the device cannot do it. */
export const Unavailable: FC<{ cap: Capability; what: string; children?: ReactNode }> = ({
  cap,
  what,
  children,
}) => (
  <PanelSectionRow>
    <Field
      label={`${what} is not available`}
      childrenLayout="below"
      description={
        <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
          <div>{cap.reason ?? "Not supported on this device."}</div>
          {children}
        </div>
      }
    />
  </PanelSectionRow>
);

export const ErrorRow: FC<{ message?: string }> = ({ message }) =>
  message ? (
    <Info label="Problem" warn>
      {message}
    </Info>
  ) : null;

/** r, g, b (0-255) <-> hue (0-360) and saturation (0-1) at full value. */
export function rgbToHueSat(rgb: [number, number, number]): { hue: number; sat: number } {
  const r = rgb[0] / 255;
  const g = rgb[1] / 255;
  const b = rgb[2] / 255;
  const max = Math.max(r, g, b);
  const min = Math.min(r, g, b);
  const d = max - min;
  let hue = 0;
  if (d > 0) {
    if (max === r) hue = ((g - b) / d) % 6;
    else if (max === g) hue = (b - r) / d + 2;
    else hue = (r - g) / d + 4;
    hue *= 60;
    if (hue < 0) hue += 360;
  }
  return { hue: Math.round(hue), sat: max === 0 ? 0 : d / max };
}

export function hueSatToRgb(hue: number, sat: number): [number, number, number] {
  const h = (((hue % 360) + 360) % 360) / 60;
  const c = sat;
  const x = c * (1 - Math.abs((h % 2) - 1));
  let rgb: [number, number, number];
  if (h < 1) rgb = [c, x, 0];
  else if (h < 2) rgb = [x, c, 0];
  else if (h < 3) rgb = [0, c, x];
  else if (h < 4) rgb = [0, x, c];
  else if (h < 5) rgb = [x, 0, c];
  else rgb = [c, 0, x];
  const m = 1 - c;
  return [
    Math.round((rgb[0] + m) * 255),
    Math.round((rgb[1] + m) * 255),
    Math.round((rgb[2] + m) * 255),
  ];
}

export function modeLabel(mode: string): string {
  switch (mode) {
    case "custom":
      return "Custom (set the watts)";
    case "low-power":
    case "quiet":
      return "Quiet";
    case "balanced":
      return "Balanced";
    case "balanced-performance":
      return "Balanced performance";
    case "performance":
      return "Performance";
    default:
      return mode.charAt(0).toUpperCase() + mode.slice(1).replace(/[-_]/g, " ");
  }
}
