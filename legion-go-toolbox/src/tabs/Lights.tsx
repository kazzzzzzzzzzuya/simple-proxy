import { DropdownItem, Field, PanelSection, PanelSectionRow, SliderField, ToggleField } from "@decky/ui";
import { FC } from "react";

import type { StickMode } from "../backend";
import { update, useStore } from "../store";
import { ErrorRow, hueSatToRgb, rgbToHueSat, Unavailable } from "../ui";

const MODE_OPTIONS: { data: StickMode; label: string }[] = [
  { data: "solid", label: "Solid colour" },
  { data: "pulse", label: "Pulse" },
  { data: "rainbow", label: "Rainbow" },
  { data: "spiral", label: "Spiral" },
];

export const LightsTab: FC = () => {
  const { state } = useStore();
  if (!state) return null;
  const { caps, settings, status } = state;
  const sticks = settings.lighting.sticks;
  const led = settings.lighting.power_led;
  const { hue, sat } = rgbToHueSat(sticks.color);
  const setSticks = (patch: Partial<typeof sticks>) =>
    update({ lighting: { sticks: { managed: true, ...patch } } });
  const usesColor = sticks.mode === "solid" || sticks.mode === "pulse";
  const usesSpeed = sticks.mode !== "solid";
  const ledOn = led.managed ? led.on : (status.power_led ?? true);

  return (
    <>
      <PanelSection title="Stick lights">
        {!caps.sticks.available ? (
          <Unavailable cap={caps.sticks} what="Stick lighting" />
        ) : (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Stick lights"
                description={
                  sticks.managed
                    ? "The lights around both sticks."
                    : "Not changed by this plugin yet. Switch or adjust to take control."
                }
                checked={sticks.managed ? sticks.on : true}
                onChange={(v) => setSticks({ on: v })}
              />
            </PanelSectionRow>
            <ErrorRow message={status.errors.sticks} />
            {sticks.on && (
              <>
                <PanelSectionRow>
                  <DropdownItem
                    label="Effect"
                    rgOptions={MODE_OPTIONS}
                    selectedOption={sticks.mode}
                    onChange={(o) => setSticks({ mode: o.data as StickMode })}
                  />
                </PanelSectionRow>
                {usesColor && (
                  <>
                    <PanelSectionRow>
                      <Field label="Colour" padding="compact" bottomSeparator="none">
                        <div
                          style={{
                            width: "56px",
                            height: "18px",
                            borderRadius: "4px",
                            border: "1px solid rgba(255,255,255,0.4)",
                            background: `rgb(${sticks.color[0]},${sticks.color[1]},${sticks.color[2]})`,
                          }}
                        />
                      </Field>
                    </PanelSectionRow>
                    <PanelSectionRow>
                      <SliderField
                        label="Hue"
                        value={hue}
                        min={0}
                        max={359}
                        step={1}
                        onChange={(v) => setSticks({ color: hueSatToRgb(v, sat) })}
                      />
                    </PanelSectionRow>
                    <PanelSectionRow>
                      <SliderField
                        label="Saturation"
                        description="All the way down is white."
                        value={Math.round(sat * 100)}
                        min={0}
                        max={100}
                        step={1}
                        onChange={(v) => setSticks({ color: hueSatToRgb(hue, v / 100) })}
                      />
                    </PanelSectionRow>
                  </>
                )}
                <PanelSectionRow>
                  <SliderField
                    label="Brightness"
                    value={Math.round(sticks.brightness * 100)}
                    min={5}
                    max={100}
                    step={5}
                    showValue
                    valueSuffix="%"
                    onChange={(v) => setSticks({ brightness: v / 100 })}
                  />
                </PanelSectionRow>
                {usesSpeed && (
                  <PanelSectionRow>
                    <SliderField
                      label="Speed"
                      value={Math.round(sticks.speed * 100)}
                      min={0}
                      max={100}
                      step={5}
                      showValue
                      valueSuffix="%"
                      onChange={(v) => setSticks({ speed: v / 100 })}
                    />
                  </PanelSectionRow>
                )}
              </>
            )}
          </>
        )}
      </PanelSection>

      <PanelSection title="Power button light">
        {!caps.power_led.available ? (
          <Unavailable cap={caps.power_led} what="The power light" />
        ) : (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Power light"
                checked={ledOn}
                onChange={(v) => update({ lighting: { power_led: { managed: true, on: v } } })}
              />
            </PanelSectionRow>
            <PanelSectionRow>
              <ToggleField
                label="Off while sleeping"
                description="Turns the light off when the device goes to sleep and back on when it wakes."
                checked={led.off_in_sleep}
                onChange={(v) => update({ lighting: { power_led: { off_in_sleep: v } } })}
              />
            </PanelSectionRow>
            <ErrorRow message={status.errors.power_led} />
          </>
        )}
      </PanelSection>
    </>
  );
};
