import {
  ButtonItem,
  DropdownItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import { definePlugin, toaster } from "@decky/api";
import { FC, useCallback, useEffect, useRef, useState } from "react";
import { MdScreenRotation } from "react-icons/md";

import {
  calibrate,
  calibrateAll,
  getState,
  getStatus,
  resetSettings,
  setSettings,
  Rotation,
  Settings,
  SettingsPatch,
  SlotName,
  SlotSettings,
  SlotStatus,
  SourceName,
  Status,
} from "./backend";

const PLUGIN_NAME = "Legion Go Gyro";
const STATUS_INTERVAL_MS = 250;
const SAVE_DEBOUNCE_MS = 250;

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

function mergePatch(a: SettingsPatch, b: SettingsPatch): SettingsPatch {
  const out: SettingsPatch = { ...a, ...b };
  if (a.slots || b.slots) {
    const slots: NonNullable<SettingsPatch["slots"]> = { ...(a.slots ?? {}) };
    for (const key of Object.keys(b.slots ?? {}) as SlotName[]) {
      slots[key] = { ...(slots[key] ?? {}), ...(b.slots![key] ?? {}) };
    }
    out.slots = slots;
  }
  return out;
}

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
  const frac = Math.max(-1, Math.min(1, value / full));
  const width = Math.sqrt(Math.abs(frac)) * 50;
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
            ? "It moved during the measurement. Lay it on a table and try again."
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
              status.calibrated ? undefined : "Not calibrated yet – lay it down and calibrate once."
            }
            onClick={onCalibrate}
          >
            {busy ? "Calibrating – keep it still…" : "Calibrate (keep still)"}
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
  const width = active ? Math.sqrt(Math.min(1, speed / 300)) * 100 : 0;
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

const Content: FC = () => {
  const [settings, setLocalSettings] = useState<Settings | null>(null);
  const [defaults, setDefaults] = useState<Settings | null>(null);
  const [calibrating, setCalibrating] = useState(false);
  const [status, setStatus] = useState<Status | null>(null);
  const [version, setVersion] = useState("");
  const [error, setError] = useState<string | null>(null);
  const pending = useRef<SettingsPatch | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const flush = useCallback(async () => {
    timer.current = null;
    const patch = pending.current;
    pending.current = null;
    if (!patch) return;
    try {
      await setSettings(patch);
    } catch (e) {
      setError(`${e}`);
    }
  }, []);

  const update = useCallback(
    (patch: SettingsPatch) => {
      setLocalSettings((s) => (s ? applyPatch(s, patch) : s));
      pending.current = pending.current ? mergePatch(pending.current, patch) : patch;
      if (timer.current) clearTimeout(timer.current);
      timer.current = setTimeout(flush, SAVE_DEBOUNCE_MS);
    },
    [flush],
  );

  useEffect(() => {
    let alive = true;
    let polling = false;

    getState()
      .then((state) => {
        if (!alive) return;
        setLocalSettings(state.settings);
        setDefaults(state.defaults);
        setStatus(state.status);
        setVersion(state.version);
      })
      .catch((e) => alive && setError(`${e}`));

    const id = setInterval(async () => {
      if (polling) return;
      polling = true;
      try {
        const st = await getStatus();
        if (alive) {
          setStatus(st);
          setError(null);
        }
      } catch (e) {
        if (alive) setError(`${e}`);
      } finally {
        polling = false;
      }
    }, STATUS_INTERVAL_MS);

    return () => {
      alive = false;
      clearInterval(id);
      // Do not lose a change made right before the menu closed
      if (timer.current) {
        clearTimeout(timer.current);
        void flush();
      }
    };
  }, [flush]);

  if (!settings || !status) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label={error ? "Backend not reachable" : "Loading…"} description={error ?? undefined} />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const onReset = async () => {
    if (timer.current) clearTimeout(timer.current);
    pending.current = null;
    try {
      setLocalSettings(await resetSettings());
      toaster.toast({ title: PLUGIN_NAME, body: "Settings and calibration were reset." });
    } catch (e) {
      setError(`${e}`);
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
              ? "Something moved. Lay the controllers on a table and try again."
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
        <PanelSection title="Gyro">
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
              description="Fixes drift. Lay the controllers on a table, then press."
              onClick={onCalibrateAll}
            >
              {calibrating ? "Calibrating – keep them still…" : "Calibrate"}
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

export default definePlugin(() => {
  return {
    name: PLUGIN_NAME,
    titleView: <div className={staticClasses.Title}>{PLUGIN_NAME}</div>,
    content: <Content />,
    icon: <MdScreenRotation />,
    onDismount() {},
  };
});
