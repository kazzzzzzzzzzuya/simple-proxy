import { DropdownItem, Field, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { FC } from "react";

import { update, useStore } from "../store";
import { ACCENT, ErrorRow, Info, Unavailable } from "../ui";

const TEMPS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
const FLOOR = [0, 0, 0, 0, 0, 0, 30, 50, 70, 90];
const MAX_LEVEL = 115;
const PRESETS: Record<string, number[]> = {
  silent: [0, 0, 0, 0, 25, 35, 45, 60, 80, 100],
  balanced: [0, 0, 0, 25, 35, 45, 60, 75, 90, 100],
  performance: [0, 0, 30, 40, 50, 65, 80, 95, 105, 115],
};
const PRESET_OPTIONS = [
  { data: "silent", label: "Silent" },
  { data: "balanced", label: "Balanced" },
  { data: "performance", label: "Cool" },
  { data: "custom", label: "Custom" },
];

function presetOf(curve: number[]): string {
  for (const key of Object.keys(PRESETS)) {
    if (PRESETS[key].every((v, i) => v === curve[i])) return key;
  }
  return "custom";
}

/** Moving one point keeps the curve rising and above the safety floor. */
function withPoint(curve: number[], index: number, value: number): number[] {
  const next = curve.slice();
  next[index] = Math.max(FLOOR[index], Math.min(MAX_LEVEL, Math.round(value)));
  for (let i = index + 1; i < next.length; i++) next[i] = Math.max(next[i], next[i - 1]);
  for (let i = index - 1; i >= 0; i--) next[i] = Math.min(next[i], next[i + 1]);
  return next.map((v, i) => Math.max(v, FLOOR[i]));
}

const CurvePreview: FC<{ curve: number[]; temp: number | null }> = ({ curve, temp }) => {
  const w = 260;
  const h = 80;
  const x = (t: number) => ((t - 10) / 90) * w;
  const y = (v: number) => h - (v / MAX_LEVEL) * h;
  const points = curve.map((v, i) => `${x(TEMPS[i]).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`-2 -2 ${w + 4} ${h + 4}`} width="100%" height="84">
      <rect x={0} y={0} width={w} height={h} fill="rgba(255,255,255,0.06)" rx={3} />
      <polyline points={`0,${h} ${points} ${w},${h}`} fill="rgba(26,159,255,0.18)" stroke="none" />
      <polyline points={points} fill="none" stroke={ACCENT} strokeWidth={2} />
      {temp != null && temp >= 10 && temp <= 100 && (
        <line x1={x(temp)} x2={x(temp)} y1={0} y2={h} stroke="#fff" strokeOpacity={0.6} strokeDasharray="3 3" />
      )}
    </svg>
  );
};

export const FanTab: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const f = settings.fan;
  const sensors = status.fan;
  const reading = [
    sensors.temp != null ? `${sensors.temp} °C` : null,
    sensors.rpm != null ? `${sensors.rpm} RPM` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  const powerMode = settings.power.enabled
    ? (settings.power.profiles[status.power.profile_key] ?? settings.power.profiles.default).mode
    : null;
  const waitingForCustom = f.enabled && !f.full_speed && powerMode != null && powerMode !== "custom";

  if (!caps.fan.available) {
    return (
      <PanelSection title="Fan">
        {reading && <Info label="Now">{reading}</Info>}
        <Unavailable cap={caps.fan} what="A custom fan curve">
          <div>
            The Legion Go only accepts a fan curve through a Lenovo firmware call that needs the
            acpi_call kernel module. This plugin does not install kernel modules. If you install it
            yourself (for example with Legion Center's "Install Fan Support"), the curve editor
            appears here.
          </div>
          <div>Until then the fan follows the firmware's own curve for the chosen power mode.</div>
        </Unavailable>
      </PanelSection>
    );
  }

  return (
    <PanelSection title="Fan">
      <PanelSectionRow>
        <ToggleField
          label="Custom fan curve"
          description="The firmware still controls the fan and its overheating protection; this only changes the curve it follows."
          checked={f.enabled}
          onChange={(v) => update({ fan: { enabled: v } })}
        />
      </PanelSectionRow>
      {reading && <Info label="Now">{reading}</Info>}
      <ErrorRow message={status.errors.fan} />
      {waitingForCustom && (
        <Info label="Not in use right now" warn>
          The firmware only follows a custom curve in the Custom power mode.
        </Info>
      )}
      {f.enabled && (
        <>
          <PanelSectionRow>
            <ToggleField
              label="Full speed"
              description="Runs the fan flat out, ignoring the curve."
              checked={f.full_speed}
              onChange={(v) => update({ fan: { full_speed: v } })}
            />
          </PanelSectionRow>
          {!f.full_speed && (
            <>
              <PanelSectionRow>
                <DropdownItem
                  label="Preset"
                  rgOptions={PRESET_OPTIONS}
                  selectedOption={presetOf(f.curve)}
                  onChange={(o) => {
                    const preset = PRESETS[o.data as string];
                    if (preset) update({ fan: { curve: preset.slice() } });
                  }}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <Field childrenLayout="below" bottomSeparator="none" padding="compact">
                  <CurvePreview curve={f.curve} temp={sensors.temp} />
                </Field>
              </PanelSectionRow>
              {TEMPS.map((t, i) => (
                <PanelSectionRow key={t}>
                  <SliderField
                    label={`At ${t} °C`}
                    value={f.curve[i]}
                    min={0}
                    max={MAX_LEVEL}
                    step={5}
                    showValue
                    valueSuffix="%"
                    onChange={(v) => update({ fan: { curve: withPoint(f.curve, i, v) } })}
                  />
                </PanelSectionRow>
              ))}
              <Info label="Safety">
                The curve always rises with temperature and cannot go below 30 / 50 / 70 / 90 % at
                70 / 80 / 90 / 100 °C.
              </Info>
            </>
          )}
        </>
      )}
    </PanelSection>
  );
};
