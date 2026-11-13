import { ButtonItem, DropdownItem, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { FC } from "react";

import { deletePowerProfile, PowerProfile } from "../backend";
import { replaceSettings, update, useStore } from "../store";
import { ErrorRow, Info, modeLabel, Unavailable } from "../ui";
import { SmoothSection } from "./Smooth";

const TARGETS = [30, 40, 45, 50, 60, 72, 90, 120, 144];

const AUTO_STATES: Record<string, string> = {
  starting: "starting",
  waiting: "waiting for the game to draw frames",
  settling: "checking the new setting",
  raising: "below target – adding power",
  lowering: "target held – trying less power",
  holding: "holding the target",
  optimal: "at the lowest power that holds the target",
  at_max: "at your maximum – the target needs more power than allowed",
  at_min: "at your minimum",
  no_game: "no game running",
};

export const PowerTab: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const p = settings.power;
  const st = status.power;

  if (!caps.power.available) {
    return (
      <PanelSection title="Power">
        <Unavailable cap={caps.power} what="TDP control">
          <div>Steam's own TDP slider in the Performance menu may still work.</div>
        </Unavailable>
      </PanelSection>
    );
  }

  const appKey = status.app.id != null ? String(status.app.id) : null;
  const gameName = status.app.name || "this game";
  const hasGameProfile = appKey != null && appKey in p.profiles;
  // What the sliders below edit: the running game's own profile when there
  // is one (or per-game is on and a game runs), else the default.
  const editKey = p.per_game && appKey != null ? appKey : "default";
  const profile: PowerProfile = p.profiles[editKey] ?? p.profiles.default;
  const setProfile = (patch: Partial<PowerProfile>) =>
    update({ power: { profiles: { [editKey]: patch } } });

  const range = st.range ?? [5, 30];
  const modes = ["custom", ...st.modes.filter((m) => m !== "custom")];
  const modeOptions = modes.map((m) => ({ data: m, label: modeLabel(m) }));
  const custom = profile.mode === "custom";
  const tdp = Math.max(range[0], Math.min(range[1], profile.tdp));
  const minTdp = Math.max(range[0], Math.min(tdp, profile.min_tdp));
  const boost = p.cpu_boost ?? st.cpu_boost;

  const nowLine = () => {
    if (!p.enabled) return "Not managed by this plugin";
    const parts = [st.mode ? modeLabel(st.mode).replace(" (set the watts)", "") : "?"];
    if (st.mode === "custom" && st.tdp != null) parts.push(`${st.tdp} W`);
    if (st.auto.active && st.auto.fps != null) {
      parts.push(`${Math.round(st.auto.fps)} fps`);
      if (st.auto.low != null) parts.push(`slowest ${Math.round(st.auto.low)}`);
    }
    return parts.join(" · ");
  };

  return (
    <>
      <PanelSection title="Power">
        <PanelSectionRow>
          <ToggleField
            label="Manage power"
            description="Sets the power limit (TDP) for you. Turn off Steam's own TDP limit in the Performance menu so the two do not fight."
            checked={p.enabled}
            onChange={(v) => update({ power: { enabled: v } })}
          />
        </PanelSectionRow>
        <Info label="Now">{nowLine()}</Info>
        <ErrorRow message={status.errors.power} />
      </PanelSection>

      {p.enabled && (
        <PanelSection title={editKey === "default" ? "Default profile" : gameName}>
          <PanelSectionRow>
            <ToggleField
              label="Separate settings per game"
              description={
                !p.per_game
                  ? "One profile for everything."
                  : appKey == null
                    ? "Start a game to give it its own profile. Editing the default now."
                    : hasGameProfile
                      ? `Editing the profile of ${gameName}.`
                      : `Change anything below to create a profile for ${gameName}.`
              }
              checked={p.per_game}
              onChange={(v) => update({ power: { per_game: v } })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <DropdownItem
              label="Power mode"
              rgOptions={modeOptions}
              selectedOption={profile.mode}
              onChange={(o) => setProfile({ mode: o.data as string })}
            />
          </PanelSectionRow>
          {custom && (
            <>
              <PanelSectionRow>
                <SliderField
                  label={profile.auto ? "Maximum TDP" : "TDP"}
                  value={tdp}
                  min={range[0]}
                  max={range[1]}
                  step={1}
                  showValue
                  valueSuffix=" W"
                  onChange={(v) => setProfile({ tdp: v })}
                />
              </PanelSectionRow>
              <PanelSectionRow>
                <ToggleField
                  label="Auto TDP for a frame rate"
                  description={
                    caps.fps.available
                      ? "Uses the lowest power that still holds your target frame rate, and adds power again the moment it drops."
                      : (caps.fps.reason ?? "Frame rates cannot be measured right now.")
                  }
                  checked={profile.auto}
                  onChange={(v) => setProfile({ auto: v })}
                />
              </PanelSectionRow>
              {profile.auto && (
                <>
                  <PanelSectionRow>
                    <DropdownItem
                      label="Target frame rate"
                      description={
                        status.smooth?.fps
                          ? `Ignored while this game has Smooth mode: Auto TDP aims at its ${status.smooth.fps} fps cap.`
                          : "Best with a frame limit at the same number (Smooth mode below sets one per game)."
                      }
                      rgOptions={(TARGETS.includes(profile.target_fps)
                        ? TARGETS
                        : [...TARGETS, profile.target_fps].sort((a, b) => a - b)
                      ).map((f) => ({ data: f, label: `${f} fps` }))}
                      selectedOption={profile.target_fps}
                      onChange={(o) => setProfile({ target_fps: o.data as number })}
                    />
                  </PanelSectionRow>
                  <PanelSectionRow>
                    <SliderField
                      label="Minimum TDP"
                      description="Auto TDP never goes below this."
                      value={minTdp}
                      min={range[0]}
                      max={tdp}
                      step={1}
                      showValue
                      valueSuffix=" W"
                      onChange={(v) => setProfile({ min_tdp: v })}
                    />
                  </PanelSectionRow>
                  <Info label="Auto TDP" warn={st.auto.state === "at_max" || !!st.auto.probe_error}>
                    {st.auto.probe_error ??
                      (st.auto.state ? (AUTO_STATES[st.auto.state] ?? st.auto.state) : "starting")}
                  </Info>
                </>
              )}
            </>
          )}
          {p.per_game && hasGameProfile && appKey != null && (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                onClick={async () => replaceSettings(await deletePowerProfile(appKey))}
              >
                Use the default for this game
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>
      )}

      <SmoothSection />

      {p.enabled && boost != null && (
        <PanelSection title="Processor">
          <PanelSectionRow>
            <ToggleField
              label="CPU boost"
              description="Off runs cooler and quieter and often costs few frames."
              checked={boost}
              onChange={(v) => update({ power: { cpu_boost: v } })}
            />
          </PanelSectionRow>
        </PanelSection>
      )}
    </>
  );
};
