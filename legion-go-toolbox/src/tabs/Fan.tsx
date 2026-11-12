import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { FC } from "react";

import { deleteFanProfile, FanProfile } from "../backend";
import { replaceSettings, update, useStore } from "../store";
import { ACCENT, ErrorRow, Info, Unavailable } from "../ui";

const TEMPS = [10, 20, 30, 40, 50, 60, 70, 80, 90, 100];
// Lenovo's minimum curve for the Legion Go: nothing below it is accepted.
const FLOOR = [44, 48, 55, 60, 71, 79, 87, 87, 100, 100];
const MAX_LEVEL = 115;
const PRESETS: Record<string, number[]> = {
  silent: FLOOR.slice(),
  balanced: [44, 50, 58, 66, 78, 88, 96, 100, 105, 110],
  performance: [50, 56, 64, 74, 86, 96, 105, 110, 115, 115],
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

function agoText(seconds: number): string {
  if (seconds < 60) return "just now";
  const min = Math.round(seconds / 60);
  return min < 60 ? `${min} min ago` : `${Math.round(min / 60)} h ago`;
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

/** Readable name of a fan profile key. */
function keyLabel(key: string, profiles: Record<string, FanProfile>, perPower: boolean): string {
  const [base, suffix] = key.split(":");
  const who = base === "default" ? "All games" : profiles[key]?.name || profiles[base]?.name || `App ${base}`;
  if (suffix === "ac") return `${who} · on charger`;
  return perPower ? `${who} · on battery` : who;
}

function profileSummary(p: FanProfile): string {
  if (p.full_speed) return "Full speed";
  const preset = presetOf(p.curve);
  return preset === "custom" ? "Custom curve" : (PRESET_OPTIONS.find((o) => o.data === preset)?.label ?? "");
}

export const FanTab: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const f = settings.fan;
  const sensors = status.fan;
  const reading = [
    sensors.temp != null ? `${sensors.temp} °C` : null,
    sensors.rpm != null ? `${sensors.rpm} RPM` : null,
    f.per_power ? (sensors.on_ac ? "on charger" : "on battery") : null,
  ]
    .filter(Boolean)
    .join(" · ");

  // Which profile the controls below edit: the running game and the power
  // source, as far as the two switches separate them.
  const app = status.app;
  const gamePart = f.per_game && app.id != null ? String(app.id) : "default";
  const editKey = gamePart + (f.per_power && sensors.on_ac ? ":ac" : "");
  const inUse = f.profiles[sensors.profile_key] ?? f.profiles.default;
  const profile: FanProfile = f.profiles[editKey] ?? inUse;
  const editingOwn = editKey in f.profiles;
  const setProfile = (patch: Partial<FanProfile>) =>
    update({
      fan: {
        profiles: {
          [editKey]: {
            ...profile,
            ...patch,
            name: gamePart === "default" ? "" : app.name || profile.name || "",
          },
        },
      },
    });
  const removeProfile = async (key: string) => replaceSettings(await deleteFanProfile(key));

  const powerMode = settings.power.enabled
    ? (settings.power.profiles[status.power.profile_key] ?? settings.power.profiles.default).mode
    : null;
  const waitingForCustom =
    f.enabled && !inUse.full_speed && powerMode != null && powerMode !== "custom";
  const saved = Object.keys(f.profiles)
    .filter((k) => k !== "default")
    .sort((a, b) =>
      keyLabel(a, f.profiles, f.per_power).localeCompare(keyLabel(b, f.profiles, f.per_power)),
    );

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
    <>
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
        {f.enabled && sensors.restores > 0 && (
          <Info label="Kept on">
            {`The firmware dropped the fan setting ${sensors.restores === 1 ? "once" : `${sensors.restores} times`}${
              sensors.restored_ago != null ? ` (last ${agoText(sensors.restored_ago)})` : ""
            }; the plugin put it back. This happens when the power mode changes.`}
          </Info>
        )}
        {waitingForCustom && (
          <Info label="Not in use right now" warn>
            The firmware only follows a custom curve in the Custom power mode.
          </Info>
        )}
        {f.enabled && (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Separate settings per game"
                description="A game can have its own fan setting; others use the general one."
                checked={f.per_game}
                onChange={(v) => update({ fan: { per_game: v } })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Separate settings on charger"
                description="One setting on battery and another while plugged in. Switches by itself when you plug in or unplug."
                checked={f.per_power}
                onChange={(v) => update({ fan: { per_power: v } })}
              />
            </PanelSectionRow>
          </>
        )}
      </PanelSection>

      {f.enabled && (
        <PanelSection title={keyLabel(editKey, { ...f.profiles, [editKey]: { ...profile, name: app.name || profile.name } }, f.per_power)}>
          {editKey !== "default" && (
            <Info label={editingOwn ? "Its own setting" : "Using the general setting"}>
              {editingOwn
                ? "Changes below apply only here."
                : `Now uses: ${keyLabel(sensors.profile_key, f.profiles, f.per_power)}. Change anything below to give it its own setting.`}
            </Info>
          )}
          <PanelSectionRow>
            <ToggleField
              label="Full speed"
              description="Runs the fan flat out, ignoring the curve. The plugin checks every few seconds and turns it back on if the firmware switches it off."
              checked={profile.full_speed}
              onChange={(v) => setProfile({ full_speed: v })}
            />
          </PanelSectionRow>
          {!profile.full_speed && (
            <>
              <PanelSectionRow>
                <DropdownItem
                  label="Preset"
                  rgOptions={PRESET_OPTIONS}
                  selectedOption={presetOf(profile.curve)}
                  onChange={(o) => {
                    const preset = PRESETS[o.data as string];
                    if (preset) setProfile({ curve: preset.slice() });
                  }}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <Field childrenLayout="below" bottomSeparator="none" padding="compact">
                  <CurvePreview curve={profile.curve} temp={sensors.temp} />
                </Field>
              </PanelSectionRow>
              {TEMPS.map((t, i) => (
                <PanelSectionRow key={t}>
                  <SliderField
                    label={`At ${t} °C`}
                    value={profile.curve[i]}
                    min={FLOOR[i]}
                    max={MAX_LEVEL}
                    step={1}
                    showValue
                    valueSuffix="%"
                    onChange={(v) => setProfile({ curve: withPoint(profile.curve, i, v) })}
                  />
                </PanelSectionRow>
              ))}
              <Info label="Safety">
                The curve always rises with temperature and never goes below Lenovo's own minimum
                (44 % at 10 °C up to 100 % at 90 °C). Silent is that minimum.
              </Info>
            </>
          )}
          {editingOwn && editKey !== "default" && (
            <PanelSectionRow>
              <ButtonItem layout="below" onClick={() => void removeProfile(editKey)}>
                Use the general setting here
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      {f.enabled && saved.length > 0 && (
        <PanelSection title="Saved fan settings">
          <PanelSectionRow>
            <Field
              description="Select one to remove it; that case then uses the general setting again."
              padding="compact"
              bottomSeparator="none"
            />
          </PanelSectionRow>
          {saved.map((key) => (
            <PanelSectionRow key={key}>
              <ButtonItem
                layout="below"
                description={profileSummary(f.profiles[key])}
                onClick={() => void removeProfile(key)}
              >
                {keyLabel(key, f.profiles, f.per_power)}
              </ButtonItem>
            </PanelSectionRow>
          ))}
        </PanelSection>
      )}
    </>
  );
};
