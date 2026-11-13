import { Router, Tabs, staticClasses } from "@decky/ui";
import { definePlugin } from "@decky/api";
import { FC, ReactNode, useEffect, useRef, useState } from "react";
import { FaBolt, FaFan, FaGamepad, FaHome, FaLightbulb } from "react-icons/fa";
import { MdScreenRotation } from "react-icons/md";
import { GiToolbox } from "react-icons/gi";

import { onResume, onSuspend, setRunningApp } from "./backend";
import { flushWrites, load, refreshStatus, useStore } from "./store";
import { ControlsTab } from "./tabs/Controls";
import { FanTab } from "./tabs/Fan";
import { GyroTab } from "./tabs/Gyro";
import { SteamGyroSection } from "./tabs/SteamGyro";
import { HomeTab } from "./tabs/Home";
import { LightsTab } from "./tabs/Lights";
import { PowerTab } from "./tabs/Power";
import { PLUGIN_NAME } from "./ui";

const TAB_STYLES = `
  .lgo-toolbox-tabs > div > div:first-child {
    background: #0d141c;
    box-shadow: none;
    backdrop-filter: none;
  }
  .lgo-toolbox-tabs [role="tabpanel"] {
    padding-left: 8px !important;
    padding-right: 8px !important;
  }
  .lgo-toolbox-tabs .lgo-toolbox-tab-content {
    padding-bottom: 96px;
  }
  .lgo-toolbox-tabs [role="tablist"] {
    display: flex;
    flex-wrap: nowrap;
    justify-content: center;
  }
  .lgo-toolbox-tabs [role="tab"] {
    flex: 0 1 auto;
    min-width: 0;
    box-sizing: border-box;
    padding-left: 5px !important;
    padding-right: 5px !important;
    display: flex !important;
    align-items: center;
    justify-content: center;
  }
  .lgo-toolbox-tabs [role="tab"] svg {
    display: block;
    margin: 0;
  }
`;

const wrap = (content: ReactNode) => <div className="lgo-toolbox-tab-content">{content}</div>;

const Content: FC = () => {
  const { state, error } = useStore();
  const [tab, setTab] = useState("Home");

  // The gyro bars want quick updates; everything else is fine at 1 Hz.
  useEffect(() => {
    void load();
    return () => flushWrites();
  }, []);
  const root = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const id = setInterval(
      () => {
        // The panel stays mounted while the menu is closed: do not poll then.
        const el = root.current;
        if (el && el.getClientRects().length === 0) return;
        void refreshStatus();
      },
      tab === "Gyro" ? 250 : 1000,
    );
    return () => clearInterval(id);
  }, [tab]);

  if (!state) {
    return (
      <div style={{ padding: "16px" }}>
        {error ? `Backend not reachable: ${error}` : "Loading…"}
      </div>
    );
  }

  const tabs = [
    { id: "Home", title: <FaHome size={18} />, content: wrap(<HomeTab go={setTab} />) },
    { id: "Power", title: <FaBolt size={18} />, content: wrap(<PowerTab />) },
    { id: "Fan", title: <FaFan size={18} />, content: wrap(<FanTab />) },
    { id: "Lights", title: <FaLightbulb size={18} />, content: wrap(<LightsTab />) },
    { id: "Controls", title: <FaGamepad size={18} />, content: wrap(<ControlsTab />) },
    { id: "Gyro", title: <MdScreenRotation size={18} />, content: wrap(
        <>
          <SteamGyroSection />
          <GyroTab />
        </>,
      ),
    },
  ];

  return (
    <div
      ref={root}
      className="lgo-toolbox-tabs"
      style={{ height: "95%", width: "300px", position: "fixed", marginTop: "-12px", overflow: "hidden" }}
    >
      <style>{TAB_STYLES}</style>
      <Tabs activeTab={tab} onShowTab={(next: string) => setTab(next)} tabs={tabs} />
    </div>
  );
};

/** Tell the backend which game runs, so per-game power profiles and the
 *  frame-rate measurement follow it even while the menu is closed. */
function watchRunningApp(): () => void {
  let last: string | null = null;
  const check = () => {
    try {
      const app = Router.MainRunningApp;
      const id = app?.appid != null ? Number(app.appid) : null;
      const key = id == null ? "none" : String(id);
      if (key === last) return;
      last = key;
      void setRunningApp(id, app?.display_name ?? "").catch(() => {
        last = null; // try again on the next tick
      });
    } catch {
      /* Steam not ready yet */
    }
  };
  check();
  const timer = setInterval(check, 2000);
  return () => clearInterval(timer);
}

interface Unregisterable {
  unregister: () => void;
}

function watchSleep(): () => void {
  const subs: Unregisterable[] = [];
  try {
    const sys = (window as unknown as { SteamClient?: { System?: Record<string, unknown> } })
      .SteamClient?.System;
    const hook = (name: string, fn: () => void) => {
      const register = sys?.[name] as ((cb: () => void) => Unregisterable) | undefined;
      if (typeof register === "function") subs.push(register.call(sys, fn));
    };
    hook("RegisterForOnSuspendRequest", () => void onSuspend().catch(() => undefined));
    hook("RegisterForOnResumeFromSuspend", () => void onResume().catch(() => undefined));
  } catch {
    /* older Steam client without these hooks */
  }
  return () => subs.forEach((s) => s?.unregister?.());
}

export default definePlugin(() => {
  const stopApp = watchRunningApp();
  const stopSleep = watchSleep();
  return {
    name: PLUGIN_NAME,
    titleView: <div className={staticClasses.Title}>{PLUGIN_NAME}</div>,
    alwaysRender: true,
    content: <Content />,
    icon: <GiToolbox />,
    onDismount() {
      stopApp();
      stopSleep();
      flushWrites();
    },
  };
});
