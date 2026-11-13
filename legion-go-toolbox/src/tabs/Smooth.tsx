import { ButtonItem, DropdownItem, Field, PanelSection, PanelSectionRow } from "@decky/ui";
import { toaster } from "@decky/api";
import { FC, useState } from "react";

import { setSmoothGame, smoothPlan } from "../backend";
import { addPrefix, readLaunchOptions, removePrefix, writeLaunchOptions } from "../launchOptions";
import { replaceSettings, useStore } from "../store";
import { ErrorRow, Info, PLUGIN_NAME, Unavailable } from "../ui";

const OFF = 0;

function refreshFor(fps: number, rates: number[]): number | null {
  const sorted = [...rates].sort((a, b) => b - a);
  return sorted.find((r) => r % fps === 0) ?? null;
}

/** Smooth frame rate per game: an even frame cap plus the matching refresh rate. */
export const SmoothSection: FC = () => {
  const { state } = useStore();
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  if (!state) return null;
  const { caps, settings, status } = state;
  const st = status.smooth;
  const games = settings.smooth?.games ?? {};
  const app = status.app;
  const appKey = app.id != null ? String(app.id) : null;
  const current = appKey ? games[appKey] : undefined;

  /** Change one game's cap: launch options first, then the plugin's record. */
  const apply = async (appId: number, name: string, fps: number) => {
    setBusy(true);
    setProblem(null);
    try {
      const entry = games[String(appId)];
      let options = await readLaunchOptions(appId);
      if (entry) options = removePrefix(options, entry.prefix, entry.added_command);
      if (fps === OFF) {
        await writeLaunchOptions(appId, options);
        replaceSettings(await setSmoothGame(appId, null, name, "", false));
      } else {
        const plan = await smoothPlan(fps);
        const next = addPrefix(options, plan.prefix);
        await writeLaunchOptions(appId, next.value);
        replaceSettings(await setSmoothGame(appId, fps, name, plan.prefix, next.addedCommand));
      }
      toaster.toast({
        title: PLUGIN_NAME,
        body:
          fps === OFF
            ? `Smooth mode off for ${name}. Restart the game to remove the cap.`
            : `${name}: ${fps} fps. Restart the game for the cap to apply.`,
      });
    } catch (e) {
      setProblem(`${e instanceof Error ? e.message : e}`);
    } finally {
      setBusy(false);
    }
  };

  if (!caps.smooth?.available && Object.keys(games).length === 0) {
    return (
      <PanelSection title="Smooth frame rate">
        <Unavailable cap={caps.smooth} what="Smooth mode" />
      </PanelSection>
    );
  }

  const rates = st?.rates ?? [];
  const choices = st?.choices?.length ? st.choices : [30, 60];
  const label = (f: number) => {
    const r = refreshFor(f, rates);
    return r ? `${f} fps · screen ${r} Hz` : `${f} fps`;
  };
  const measured = st?.measured;
  const saved = Object.entries(games).sort((a, b) => a[1].name.localeCompare(b[1].name));

  return (
    <PanelSection title="Smooth frame rate">
      {app.id != null ? (
        <PanelSectionRow>
          <DropdownItem
            label={app.name || "This game"}
            description="Even frame pacing: an early frame limiter plus a screen refresh rate the cap divides evenly. Applies from the game's next start."
            disabled={busy}
            rgOptions={[
              { data: OFF, label: "Off" },
              ...choices.map((f) => ({ data: f, label: label(f) })),
            ]}
            selectedOption={current?.fps ?? OFF}
            onChange={(o) => void apply(app.id as number, app.name || "This game", o.data as number)}
          />
        </PanelSectionRow>
      ) : (
        <Info label="Start a game">Smooth mode is set per game while it runs.</Info>
      )}

      {current && (
        <>
          <Info label="Now">
            {measured?.fps != null
              ? `${measured.fps.toFixed(1)} fps · slowest frame ${measured.low != null ? measured.low.toFixed(0) : "–"} fps${
                  st?.refresh ? ` · screen ${st.refresh} Hz` : ""
                }`
              : "Waiting for the game to draw frames"}
          </Info>
          <Info label="For the smoothest result">
            Set Steam's Framerate limit (Performance menu) to off for this game, and turn off the
            game's own frame limiter and V-Sync. Two limiters at once cause uneven frames.
          </Info>
        </>
      )}
      <ErrorRow message={problem ?? status.errors.smooth} />

      {saved.length > 0 && (
        <>
          <PanelSectionRow>
            <Field
              label="Games with Smooth mode"
              description="Select one to turn it off for that game."
              padding="compact"
              bottomSeparator="none"
            />
          </PanelSectionRow>
          {saved.map(([key, g]) => (
            <PanelSectionRow key={key}>
              <ButtonItem
                layout="below"
                disabled={busy}
                description={label(g.fps)}
                onClick={() => void apply(Number(key), g.name || `App ${key}`, OFF)}
              >
                {g.name || `App ${key}`}
              </ButtonItem>
            </PanelSectionRow>
          ))}
        </>
      )}
    </PanelSection>
  );
};
