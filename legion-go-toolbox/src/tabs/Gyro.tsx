import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
} from "@decky/ui";
import { toaster } from "@decky/api";
import { FC, useState } from "react";

import { gyroCalibrate as calibrate, gyroCalibrateAll as calibrateAll, gyroResetSettings } from "../backend";
import type {
  GyroPatch as SettingsPatch,
  GyroSettings as Settings,
  GyroStatus as Status,
  Rotation,
  SlotName,
  SlotSettings,
  SlotStatus,
  SourceName,
} from "../gyro_types";
import { replaceGyroSettings, updateGyro, useStore } from "../store";

const PLUGIN_NAME = "Legion Go Toolbox";
/** Rotation below this (deg/s) is treated as "at rest" by the bars. */
const REST_NOISE_DPS = 3;

const SLOT_TITLES: Record<SlotName, string> = {
  right: "Right controller · slot 2",
  left: "Left controller · slot 1",
  combined: "Handheld · slot 0",
};

const SENSOR_LABELS: Record<string, string> = {
  left: "left controller",
  right: "right controller",
  tablet: "tablet sensor",
};

const ROTATION_OPTIONS: { data: Rotation; label: string }[] = [
  { data: 0, label: "Upright (as attached)" },
  { data: 90, label: "Turned 90° clockwise" },
  { data: 180, label: "Upside down (180°)" },
  { data: 270, label: "Turned 90° counter-clockwise" },
];

const SOURCE_OPTIONS: { data: SourceName; label: string }[] = [
  { data: "auto", label: "Automatic" },
  { data: "right", label: "Right controller" },
  { data: "left", label: "Left controller" },
  { data: "tablet", label: "Tablet sensor" },
];

const PORT_OPTIONS = [26760, 26761, 26762, 26763].map((p) => ({
  data: p,
  label: p === 26760 ? `${p} (default)` : `${p}`,
}));

function applyPatch(s: Settings, patch: SettingsPatch): Settings {
  const next: Settings = { ...s, ...patch, slots: { ...s.slots } };
  for (const key of Object.keys(patch.slots ?? {}) as SlotName[]) {
    next.slots[key] = { ...s.slots[key], ...(patch.slots![key] ?? {}) };
  }
  return next;
}

/** A centred bar: grows right for positive, left for negative values. */
const Meter: FC<{ label: string; value: number; active: boolean }> = ({
  label,
  value,
  active,
}) => {
  const full = 250; // deg/s at which the bar is full
  // Sensor noise of a resting controller should not make the bar twitch
  const shown = Math.abs(value) < REST_NOISE_DPS ? 0 : value;
  const frac = Math.max(-1, Math.min(1, shown / full));
  const width = Math.pow(Math.abs(frac), 0.7) * 50;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: "8px", fontSize: "12px" }}>
      <div style={{ width: "34px", opacity: 0.7 }}>{label}</div>
      <div
        style={{
          position: "relative",
          flex: 1,
          height: "6px",
          borderRadius: "3px",
          background: "rgba(255,255,255,0.12)",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: frac >= 0 ? "50%" : `${50 - width}%`,
            width: `${width}%`,
            background: active ? "#1a9fff" : "rgba(255,255,255,0.3)",
          }}
        />
        <div
          style={{
            position: "absolute",
            top: 0,
            bottom: 0,
            left: "50%",
            width: "1px",
            background: "rgba(255,255,255,0.5)",
          }}
        />
      </div>
      <div style={{ width: "44px", textAlign: "right", fontVariantNumeric: "tabular-nums" }}>
        {active ? Math.round(value) : "–"}
      </div>
    </div>
  );
};

function slotSummary(name: SlotName, st: SlotStatus, serviceOn: boolean): string {
  if (!serviceOn) return "Gyro service is off";
  if (name !== "combined") {
    if (!st.controller_connected) return "Controller not connected";
    const where = st.attached ? "Attached" : "Detached";
    const batt = st.battery != null ? ` · ${st.battery}%` : "";
    if (!st.connected) return `${where}${batt} · output disabled`;
    const motion = st.live ? `${Math.round(st.rate)} Hz` : "waiting for sensor…";
    return `${where}${batt} · ${motion}`;
  }
  if (!st.connected) return "No motion source available";
  const src = st.source ? SENSOR_LABELS[st.source] : "none";
  const motion = st.live ? `${Math.round(st.rate)} Hz` : "waiting for sensor…";
  return `Using ${src} · ${motion}`;
}

interface SlotSectionProps {
  name: SlotName;
  settings: SlotSettings;
  status: SlotStatus;
  serviceOn: boolean;
  tabletAvailable: boolean;
  update: (patch: SettingsPatch) => void;
}

const SlotSection: FC<SlotSectionProps> = ({
  name,
  settings,
  status,
  serviceOn,
  tabletAvailable,
  update,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [busy, setBusy] = useState(false);
  const set = (patch: Partial<SlotSettings>) => update({ slots: { [name]: patch } });
  const showMotion = serviceOn && status.connected;

  const onCalibrate = async () => {
    setBusy(true);
    try {
      const result = await calibrate(name);
      const body =
        result === "ok"
          ? "Calibration saved."
          : result === "moved"
            ? "It was being moved the whole time. Put it down and try again."
            : "The motion sensor is not sending data yet.";
      toaster.toast({ title: SLOT_TITLES[name].split(" · ")[0], body });
    } catch (e) {
      toaster.toast({ title: PLUGIN_NAME, body: `Calibration failed: ${e}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <PanelSection title={SLOT_TITLES[name]}>
      <PanelSectionRow>
        <ToggleField
          label="Enabled"
          description={slotSummary(name, status, serviceOn)}
          checked={settings.enabled}
          onChange={(v) => set({ enabled: v })}
        />
      </PanelSectionRow>
      {showMotion && (
        <PanelSectionRow>
          <Field childrenLayout="below" bottomSeparator="none" padding="compact">
            <div style={{ display: "flex", flexDirection: "column", gap: "4px", width: "100%" }}>
              <Meter label="Pitch" value={status.gyro[0]} active={status.live} />
              <Meter label="Yaw" value={status.gyro[1]} active={status.live} />
              <Meter label="Roll" value={status.gyro[2]} active={status.live} />
            </div>
          </Field>
        </PanelSectionRow>
      )}
      {name === "combined" && settings.enabled && (
        <PanelSectionRow>
          <DropdownItem
            label="Motion source"
            description={
              settings.source === "tablet" && !tabletAvailable
                ? "The tablet sensor was not found on this system."
                : "Automatic uses the right controller, then the left one, then the tablet."
            }
            rgOptions={SOURCE_OPTIONS}
            selectedOption={settings.source ?? "auto"}
            onChange={(o) => set({ source: o.data as SourceName })}
          />
        </PanelSectionRow>
      )}
      {settings.enabled && (
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || !showMotion || !status.live}
            description={
              !status.calibrated
                ? "Not calibrated yet – lay it down and calibrate once."
                : status.rest_noise != null
                  ? `Last calibration: sensor noise ${status.rest_noise.toFixed(1)} °/s`
                  : undefined
            }
            onClick={onCalibrate}
          >
            {busy ? "Hold it still…" : "Calibrate (keep still)"}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {settings.enabled && (
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={() => setExpanded(!expanded)}>
            {expanded ? "Hide tuning" : "Tuning…"}
          </ButtonItem>
        </PanelSectionRow>
      )}
      {settings.enabled && expanded && (
        <>
          <PanelSectionRow>
            <SliderField
              label="Sensitivity"
              value={settings.sensitivity}
              min={0.2}
              max={3}
              step={0.1}
              showValue
              valueSuffix="×"
              resetValue={1}
              onChange={(v) => set({ sensitivity: Math.round(v * 10) / 10 })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <SliderField
              label="Deadzone"
              description="Ignores tiny movements. Keep at 0 for pointer and sports games."
              value={settings.deadzone}
              min={0}
              max={5}
              step={0.25}
              showValue
              valueSuffix=" °/s"
              resetValue={0}
              onChange={(v) => set({ deadzone: Math.round(v * 100) / 100 })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <DropdownItem
              label="How it is held"
              description="Looking down onto the buttons, compared to how it sits on the tablet."
              rgOptions={ROTATION_OPTIONS}
              selectedOption={settings.rotation}
              onChange={(o) => set({ rotation: o.data as Rotation })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Invert pitch"
              checked={settings.invert_pitch}
              onChange={(v) => set({ invert_pitch: v })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Invert yaw"
              checked={settings.invert_yaw}
              onChange={(v) => set({ invert_yaw: v })}
            />
          </PanelSectionRow>
          <PanelSectionRow>
            <ToggleField
              label="Invert roll"
              checked={settings.invert_roll}
              onChange={(v) => set({ invert_roll: v })}
            />
          </PanelSectionRow>
        </>
      )}
    </PanelSection>
  );
};

/** One bar showing how much a device is being moved (simple view). */
const ActivityBar: FC<{ gyro: [number, number, number]; active: boolean }> = ({ gyro, active }) => {
  const speed = Math.sqrt(gyro[0] * gyro[0] + gyro[1] * gyro[1] + gyro[2] * gyro[2]);
  const moving = Math.max(0, speed - REST_NOISE_DPS);
  const width = active ? Math.pow(Math.min(1, moving / 300), 0.7) * 100 : 0;
  return (
    <div
      style={{
        height: "6px",
        width: "100%",
        borderRadius: "3px",
        background: "rgba(255,255,255,0.12)",
        overflow: "hidden",
      }}
    >
      <div style={{ height: "100%", width: `${width}%`, background: "#1a9fff" }} />
    </div>
  );
};

function simpleControllerLine(st: SlotStatus, serviceOn: boolean): string {
  if (!serviceOn) return "Gyro is off";
  if (!st.controller_connected) return "Not connected – press a button on it to wake it";
  const where = st.attached ? "Attached" : "Detached";
  const batt = st.battery != null ? ` · ${st.battery}%` : "";
  if (!st.connected) return `${where}${batt} · turned off in Advanced options`;
  return `${where}${batt} · ${st.live ? "motion working" : "starting sensor…"}`;
}

function overallLine(st: Status): string {
  if (!st.enabled) return "Off – controller motion sensors are powered down.";
  if (!st.device.found) return st.device.error ?? "Looking for the controllers…";
  if (!st.server.listening) return st.server.error ?? "Starting…";
  if (st.server.clients > 0) {
    return st.server.clients === 1
      ? "Working – 1 emulator is using the motion data."
      : `Working – ${st.server.clients} emulators are using the motion data.`;
  }
  return "Ready – waiting for an emulator to connect.";
}

/** The settings a user can change in the advanced view, as a patch. */
function tuningOf(s: Settings): SettingsPatch {
  return {
    port: s.port,
    lan: s.lan,
    auto_calibrate: s.auto_calibrate,
    slots: { combined: s.slots.combined, left: s.slots.left, right: s.slots.right },
  };
}

function isCustomised(s: Settings, defaults: Settings): boolean {
  return JSON.stringify(tuningOf(applyPatch(defaults, tuningOf(s)))) !== JSON.stringify(tuningOf(defaults));
}

function controllerLine(st: Status): string {
  if (!st.enabled) return "Off";
  if (!st.device.found) return st.device.error ?? "Searching…";
  if (!st.device.receiving) return `${st.device.mode ?? "Found"} · no data yet`;
  return `${st.device.mode ?? "Found"} mode · receiving`;
}

function serverLine(st: Status): string {
  if (!st.enabled) return "Off";
  if (!st.server.listening) return st.server.error ?? "Starting…";
  const who = st.server.clients === 1 ? "1 emulator" : `${st.server.clients} emulators`;
  return `${st.server.host}:${st.server.port} · ${who} connected`;
}

export const GyroTab: FC = () => {
  const { state } = useStore();
  const [calibrating, setCalibrating] = useState(false);
  if (!state) return null;
  const settings = state.gyro.settings;
  const defaults: Settings | null = state.gyro.defaults;
  const status = state.gyro.status;
  const version = "";
  const error: string | null = null;
  const update = updateGyro;

  const onReset = async () => {
    try {
      replaceGyroSettings(await gyroResetSettings());
      toaster.toast({ title: PLUGIN_NAME, body: "Gyro settings and calibration were reset." });
    } catch (e) {
      toaster.toast({ title: PLUGIN_NAME, body: `Reset failed: ${e}` });
    }
  };

  const onCalibrateAll = async () => {
    setCalibrating(true);
    try {
      const results = Object.values(await calibrateAll());
      const body =
        results.length === 0
          ? "No controller is sending motion yet."
          : results.every((r) => r === "ok")
            ? "Calibration saved."
            : results.some((r) => r === "moved")
              ? "A controller was being moved the whole time. Put them down and try again."
              : "A motion sensor stopped sending data. Try again.";
      toaster.toast({ title: PLUGIN_NAME, body });
    } catch (e) {
      toaster.toast({ title: PLUGIN_NAME, body: `Calibration failed: ${e}` });
    } finally {
      setCalibrating(false);
    }
  };

  const customised = defaults != null && isCustomised(settings, defaults);
  const anyLive = settings.enabled && (status.slots.left.live || status.slots.right.live);

  const modeToggle = (
    <PanelSectionRow>
      <ToggleField
        label="Advanced options"
        description={
          settings.advanced
            ? "Showing every option. Turn off for the simple view."
            : "Show per-controller tuning, motion source and server options."
        }
        checked={settings.advanced}
        onChange={(v) => update({ advanced: v })}
      />
    </PanelSectionRow>
  );

  if (!settings.advanced) {
    return (
      <>
        <PanelSection title="Gyro for emulators">
          <PanelSectionRow>
            <ToggleField
              label="Controller gyro"
              description={overallLine(status)}
              checked={settings.enabled}
              onChange={(v) => update({ enabled: v })}
            />
          </PanelSectionRow>
          {error && (
            <PanelSectionRow>
              <Field label="Error" description={error} padding="compact" />
            </PanelSectionRow>
          )}
          {(["right", "left"] as const).map((name) => (
            <PanelSectionRow key={name}>
              <Field
                label={name === "right" ? "Right controller" : "Left controller"}
                description={simpleControllerLine(status.slots[name], settings.enabled)}
                childrenLayout="below"
                padding="compact"
              >
                <ActivityBar
                  gyro={status.slots[name].gyro}
                  active={settings.enabled && status.slots[name].connected && status.slots[name].live}
                />
              </Field>
            </PanelSectionRow>
          ))}
          <PanelSectionRow>
            <ButtonItem
              layout="below"
              disabled={calibrating || !anyLive}
              description="Fixes drift. Press, then leave the controllers alone for a few seconds."
              onClick={onCalibrateAll}
            >
              {calibrating ? "Hold them still…" : "Calibrate"}
            </ButtonItem>
          </PanelSectionRow>
          {customised && (
            <PanelSectionRow>
              <ButtonItem
                layout="below"
                description="Some advanced options were changed and are still in effect."
                onClick={() => defaults && update(tuningOf(defaults))}
              >
                Use default settings
              </ButtonItem>
            </PanelSectionRow>
          )}
        </PanelSection>

        <PanelSection title="In your emulator">
          <PanelSectionRow>
            <Field
              childrenLayout="below"
              bottomSeparator="none"
              description={
                <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                  <div>
                    Add a DSU / CemuHook motion server:{" "}
                    <b>
                      127.0.0.1 port {status.server.port}
                    </b>
                  </div>
                  <div>Slot 1 = left controller</div>
                  <div>Slot 2 = right controller</div>
                  <div>Slot 0 = whole handheld</div>
                </div>
              }
            />
          </PanelSectionRow>
        </PanelSection>

        <PanelSection>{modeToggle}</PanelSection>
      </>
    );
  }

  const slotProps = (name: SlotName) => ({
    name,
    settings: settings.slots[name],
    status: status.slots[name],
    serviceOn: settings.enabled,
    tabletAvailable: status.tablet.available,
    update,
  });

  return (
    <>
      <PanelSection title="Status">
        <PanelSectionRow>
          <ToggleField
            label="Gyro service"
            description="Reads the controller motion sensors and serves them to emulators."
            checked={settings.enabled}
            onChange={(v) => update({ enabled: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Controllers" description={controllerLine(status)} padding="compact" />
        </PanelSectionRow>
        <PanelSectionRow>
          <Field label="Motion server (DSU)" description={serverLine(status)} padding="compact" />
        </PanelSectionRow>
        {modeToggle}
        {error && (
          <PanelSectionRow>
            <Field label="Error" description={error} padding="compact" />
          </PanelSectionRow>
        )}
      </PanelSection>

      <SlotSection {...slotProps("right")} />
      <SlotSection {...slotProps("left")} />
      <SlotSection {...slotProps("combined")} />

      <PanelSection title="Server">
        <PanelSectionRow>
          <ToggleField
            label="Auto-calibrate when resting"
            description="Removes gyro drift whenever a controller lies perfectly still."
            checked={settings.auto_calibrate}
            onChange={(v) => update({ auto_calibrate: v })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <DropdownItem
            label="Port"
            description="Emulators expect 26760. Change it only if another motion server uses that port."
            rgOptions={
              PORT_OPTIONS.some((o) => o.data === settings.port)
                ? PORT_OPTIONS
                : [...PORT_OPTIONS, { data: settings.port, label: `${settings.port}` }]
            }
            selectedOption={settings.port}
            onChange={(o) => update({ port: o.data as number })}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ToggleField
            label="Allow other devices"
            description="Lets emulators on other machines in your network use this motion data."
            checked={settings.lan}
            onChange={(v) => update({ lan: v })}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Emulator setup">
        <PanelSectionRow>
          <Field
            childrenLayout="below"
            bottomSeparator="none"
            description={
              <div style={{ display: "flex", flexDirection: "column", gap: "6px" }}>
                <div>
                  Add a DSU / CemuHook motion server in the emulator:{" "}
                  <b>
                    127.0.0.1 port {status.server.port}
                  </b>
                  .
                </div>
                <div>Slot 0 = whole handheld · Slot 1 = left controller · Slot 2 = right controller.</div>
                <div>
                  Dolphin: Controllers → Alternate Input Sources → DSU Client. Switch emulators: enable motion,
                  choose CemuHook and set the slot per Joy-Con.
                </div>
              </div>
            }
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={onReset}>
            Reset to defaults
          </ButtonItem>
        </PanelSectionRow>
        {version && (
          <PanelSectionRow>
            <Field
              label="Version"
              description={`${version}${status.model ? ` · device ${status.model}` : ""}`}
              padding="compact"
              bottomSeparator="none"
            />
          </PanelSectionRow>
        )}
      </PanelSection>
    </>
  );
};
