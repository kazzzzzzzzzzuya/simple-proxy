import { ButtonItem, PanelSection, PanelSectionRow, ToggleField } from "@decky/ui";
import { toaster } from "@decky/api";
import { FC } from "react";

import { resetSettings } from "../backend";
import { replaceSettings, update, useStore } from "../store";
import { ErrorRow, Info, modeLabel, PLUGIN_NAME, Unavailable } from "../ui";

const MODELS: Record<string, string> = {
  "83E1": "Legion Go",
  "83N0": "Legion Go 2",
  "83N1": "Legion Go 2",
  "83L3": "Legion Go S",
  "83N6": "Legion Go S",
};

export const HomeTab: FC<{ go: (tab: string) => void }> = ({ go }) => {
  const { state, error } = useStore();
  if (!state) return null;
  const { caps, settings, status, gyro } = state;
  const errors = status.errors;
  const batt = status.battery;

  const powerLine = () => {
    if (!caps.power.available) return "Not available on this system";
    if (!settings.power.enabled) return "Not managed";
    const st = status.power;
    const parts = [st.mode ? modeLabel(st.mode).replace(" (set the watts)", "") : "?"];
    if (st.mode === "custom" && st.tdp != null) parts.push(`${st.tdp} W`);
    if (st.auto.active) {
      parts.push(st.auto.fps != null ? `auto · ${Math.round(st.auto.fps)} fps` : "auto");
    }
    return parts.join(" · ");
  };

  const fanLine = () => {
    const temp = status.fan.temp != null ? `${status.fan.temp} °C` : null;
    const mode = !caps.fan.available
      ? "Firmware curve"
      : !settings.fan.enabled
        ? "Firmware curve"
        : settings.fan.full_speed
          ? "Full speed"
          : "Custom curve";
    return [mode, temp].filter(Boolean).join(" · ");
  };

  const lightsLine = () => {
    const s = settings.lighting.sticks;
    const sticks = !caps.sticks.available
      ? null
      : !s.managed
        ? "sticks unchanged"
        : s.on
          ? `sticks ${s.mode}`
          : "sticks off";
    const led =
      status.power_led == null ? null : status.power_led ? "power light on" : "power light off";
    return [sticks, led].filter(Boolean).join(" · ") || "Not available";
  };

  const controlsLine = () => {
    const c = settings.controls;
    const parts = [];
    if (c.swap_menu) parts.push(status.swap_menu.active ? "menu buttons swapped" : "swap pending");
    if (c.remap_managed) parts.push("back buttons remapped");
    if (c.touchpad === false) parts.push("touchpad off");
    return parts.join(" · ") || "Default";
  };

  const gyroLine = () => {
    const g = gyro.status;
    if (!gyro.settings.enabled) return "Off";
    if (!g.device.found) return g.device.error ?? "Looking for the controllers…";
    const live = (["left", "right"] as const).filter((n) => g.slots[n].live).length;
    const who =
      g.server.clients === 1 ? "1 emulator connected" : `${g.server.clients} emulators connected`;
    const steam = settings.controls.steam_controller !== "default" ? " · Steam gyro on" : "";
    return `${live} of 2 controllers sending motion · ${who}${steam}`;
  };

  const onReset = async () => {
    try {
      replaceSettings(await resetSettings());
      toaster.toast({ title: PLUGIN_NAME, body: "Power, fan, light and button settings were reset." });
    } catch (e) {
      toaster.toast({ title: PLUGIN_NAME, body: `Reset failed: ${e}` });
    }
  };

  const row = (tab: string, label: string, text: string, problem?: string) => (
    <PanelSectionRow>
      <ButtonItem
        layout="below"
        description={
          problem ? <span style={{ color: "#ffb84d" }}>{problem}</span> : text
        }
        onClick={() => go(tab)}
      >
        {label}
      </ButtonItem>
    </PanelSectionRow>
  );

  return (
    <>
      <PanelSection title="Overview">
        {error && (
          <Info label="Backend not reachable" warn>
            {error}
          </Info>
        )}
        {row("Power", "Power", powerLine(), errors.power)}
        {row("Fan", "Fan", fanLine(), errors.fan)}
        {row("Lights", "Lights", lightsLine(), errors.sticks ?? errors.power_led)}
        {row("Controls", "Buttons", controlsLine(), errors.swap_menu ?? errors.remap)}
        {row("Gyro", "Gyro", gyroLine())}
      </PanelSection>

      <PanelSection title="Battery">
        <Info label="Battery">
          {batt.info ? `${batt.info.percent}% · ${batt.info.status}` : "No reading"}
        </Info>
        {caps.charge_limit.available ? (
          <>
            <PanelSectionRow>
              <ToggleField
                label="Limit charge to 80%"
                description="Keeps the battery healthier when you mostly play plugged in."
                checked={settings.battery.charge_limit ?? batt.charge_limit ?? false}
                onChange={(v) => update({ battery: { charge_limit: v } })}
              />
            </PanelSectionRow>
            <ErrorRow message={errors.charge_limit} />
          </>
        ) : (
          <Unavailable cap={caps.charge_limit} what="The charge limit" />
        )}
      </PanelSection>

      <PanelSection title="About">
        <Info label="Device">
          {MODELS[status.model] ? `${MODELS[status.model]} (${status.model})` : status.model || "Unknown"}
        </Info>
        <Info label="Version">{state.version}</Info>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onReset}>
            Reset hardware settings
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>
    </>
  );
};
